"""Per-request copies of the study frame.

``get_explorer_data`` caches one raw frame per study and hands every caller its
own copy of the filtered rows. On 2026-08-03 that cost took the prod web service
down: the ``all_collections`` frame is 7.25 GB resident, three separate full
copies were made per request (the context filter, the user-tag enrichment and
the filter step), and four concurrent requests fire whenever a study is
selected — 16,734 MiB against a 16 GiB limit.

These tests pin the three fixes: the column projection, and the two copies that
are now shallow because nothing mutates the caller's frame through them.
"""

import pandas as pd

from web_interface import explorer_backend as explorer
from web_interface.services import study_data


_N_ROWS = 40





def _raw_frame():
    """A study frame shaped like a recoded parquet: mask columns, an id, and
    several payload columns standing in for the ~100 real ones."""
    return pd.DataFrame({
        "item_id": [f"v{i:04d}" for i in range(_N_ROWS)],
        "activity_type": ["play" if i % 4 else "fave" for i in range(_N_ROWS)],
        "annotated_ok": [i % 3 != 0 for i in range(_N_ROWS)],
        "scraped_ok": [True] * _N_ROWS,
        "annotation_version": [f"av_{i % 2}" for i in range(_N_ROWS)],
        "desc": [f"caption {i}" for i in range(_N_ROWS)],
        "play_duration": [float(i) for i in range(_N_ROWS)],
        "niche_name": ["Cat Mischief" if i % 2 else "Guitar Covers"
                       for i in range(_N_ROWS)],
    })





def _col_types():
    return {
        "item_id": "identifier",
        "activity_type": "category",
        "annotated_ok": "category",
        "scraped_ok": "category",
        "annotation_version": "category",
        "desc": "long_text",
        "play_duration": "number",
        "niche_name": "category",
    }





def _seed_cache(monkeypatch, study="proj_study"):
    """Seed the RAM cache the way _cached_study_frame would, so no parquet is
    touched. The cache holds the context-filtered frame, so filter here too."""
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 1.0)
    filtered, status = study_data._apply_context_filter(_raw_frame())
    study_data.study_cache.put(study, {
        "df": filtered,
        "col_types": _col_types(),
        "status": status,
        "mtime": 1.0,
    })
    return study, filtered





def test_projection_returns_only_requested_and_mask_columns(monkeypatch):
    """A projected call materialises the named columns plus the ones the
    context filter itself reads — nothing else."""
    study, _ = _seed_cache(monkeypatch)

    df, col_types = study_data.get_explorer_data(
        study, context="explorer", columns=("item_id", "annotation_version"),
    )

    assert set(df.columns) == {
        "item_id", "annotation_version",
        "annotated_ok", "scraped_ok", "activity_type",
    }
    # col_types must be narrowed with it: get_current_stats iterates col_types
    # and indexes the frame with each key.
    assert set(col_types) == set(df.columns)
    assert "desc" not in df.columns and "desc" not in col_types





def test_projection_selects_the_same_rows_as_the_full_frame(monkeypatch):
    """Projection must change the width of the result, never its rows."""
    study, _ = _seed_cache(monkeypatch)

    full, _ = study_data.get_explorer_data(study, context="explorer")
    projected, _ = study_data.get_explorer_data(
        study, context="explorer", columns=("item_id",),
    )

    assert list(projected.index) == list(full.index)
    assert projected["item_id"].tolist() == full["item_id"].tolist()





def test_projection_ignores_unknown_column_names(monkeypatch):
    """Dynamic columns (User Tags, Has Annotation) are added after this call,
    so naming a column the raw frame lacks must not raise."""
    study, _ = _seed_cache(monkeypatch)

    df, _ = study_data.get_explorer_data(
        study, context="explorer", columns=("item_id", "User Tags"),
    )

    assert "User Tags" not in df.columns
    assert "item_id" in df.columns





def test_unprojected_call_still_returns_every_column(monkeypatch):
    """The default is unchanged — /api/explore/filter needs the full width to
    compute stats for every variable."""
    study, cached = _seed_cache(monkeypatch)

    df, col_types = study_data.get_explorer_data(study, context="explorer")

    assert set(df.columns) == set(cached.columns)
    assert set(col_types) == set(_col_types())





def test_enrich_with_user_tags_does_not_mutate_the_callers_frame():
    """The copy is shallow now, so this asserts the caller's frame keeps its
    original columns and values."""
    df = _raw_frame()
    before_columns = list(df.columns)
    before_versions = df["annotation_version"].tolist()

    enriched, col_types = study_data.enrich_with_user_tags(
        df, _col_types(), "nobody@example.com",
    )

    assert list(df.columns) == before_columns
    assert df["annotation_version"].tolist() == before_versions
    # The enrichment itself still happened on the returned frame.
    assert "Has Annotation" in enriched.columns
    assert "Machine Annotations" in enriched.columns
    assert col_types["Has Annotation"] == "category"





def test_enrich_with_user_tags_writes_machine_annotations_correctly():
    """The .loc write lands on the enriched frame, with the values the
    annotated_ok flags imply."""
    df = _raw_frame()

    enriched, _ = study_data.enrich_with_user_tags(
        df, _col_types(), "nobody@example.com",
    )

    machine = enriched["Machine Annotations"]
    assert set(machine[enriched["annotated_ok"]]) <= {"Machine Annotated"} | {
        v for v in machine if v.startswith("av_")
    }
    assert set(machine[~enriched["annotated_ok"]]) == {"Cannot Machine Annotate"}





def test_filter_dataframe_does_not_mutate_the_callers_frame():
    """filter_dataframe narrows by rebinding to mask selections; the shallow
    copy must leave the input untouched."""
    df = _raw_frame()
    before_len = len(df)
    before_values = df["niche_name"].tolist()

    out = explorer.filter_dataframe(
        df, _col_types(), {"niche_name": {"value": ["Cat Mischief"]}}, None,
    )

    assert len(df) == before_len
    assert df["niche_name"].tolist() == before_values
    assert len(out) < before_len
    assert set(out["niche_name"]) == {"Cat Mischief"}





def test_returned_frame_is_independent_of_the_cached_frame(monkeypatch):
    """The context filter no longer copies on top of its mask selection, so
    pin that the cached frame still cannot be reached through the result."""
    study, cached = _seed_cache(monkeypatch)
    before_columns = list(cached.columns)
    before_values = cached["niche_name"].tolist()
    before_len = len(cached)

    df, col_types = study_data.get_explorer_data(study, context="explorer")
    enriched, _ = study_data.enrich_with_user_tags(
        df, col_types, "nobody@example.com",
    )
    filtered = explorer.filter_dataframe(
        enriched, col_types, {"niche_name": {"value": ["Cat Mischief"]}}, None,
    )

    # Nothing downstream may add columns to, or alter values in, the cached
    # frame — which callers now hold a live view of, not a copy.
    assert list(cached.columns) == before_columns
    assert cached["niche_name"].tolist() == before_values
    assert len(cached) == before_len
    assert len(filtered) < before_len





def test_get_explorer_data_returns_a_view_not_a_row_copy(monkeypatch):
    """The cache holds the context-filtered frame, so a request is a column
    view of it — same rows, shared column data."""
    study, cached = _seed_cache(monkeypatch)

    df, _ = study_data.get_explorer_data(study, context="explorer")

    assert list(df.index) == list(cached.index)
    assert df is not cached          # distinct object, so attrs/drops are safe
    assert df["item_id"].tolist() == cached["item_id"].tolist()




def test_context_filter_keeps_only_enriched_play_rows():
    """scraped_ok gates the rows (require_annotated_items is false in config),
    together with the play/observe activity filter and a present item_id."""
    filtered, status = study_data._apply_context_filter(_raw_frame())

    assert set(filtered["activity_type"]) <= {"play", "observe"}
    assert filtered["item_id"].notna().all()
    assert len(filtered) < _N_ROWS
    assert status["ok"] is True




def test_get_explorer_rows_finds_an_item_by_id(monkeypatch):
    """The detail-panel path returns the matching rows without going through
    the full frame."""
    study, cached = _seed_cache(monkeypatch)
    wanted = cached["item_id"].iloc[3]

    rows, col_types = study_data.get_explorer_rows(study, item_id=wanted)

    assert len(rows) == 1
    assert rows["item_id"].iloc[0] == wanted
    # Detail panel needs every column, unlike the projected list endpoints.
    assert set(col_types) == set(_col_types())




def test_get_explorer_rows_prefers_the_row_index(monkeypatch):
    """row_index disambiguates duplicate item_ids, so it wins when valid."""
    study, cached = _seed_cache(monkeypatch)
    idx = cached.index[2]

    rows, _ = study_data.get_explorer_rows(
        study, item_id="does-not-matter", row_index=idx,
    )

    assert list(rows.index) == [idx]




def test_get_explorer_rows_falls_back_when_the_index_is_stale(monkeypatch):
    """A row_index from a stale client chunk must not 404 an item that is
    still present — fall back to matching on item_id."""
    study, cached = _seed_cache(monkeypatch)
    wanted = cached["item_id"].iloc[0]

    rows, _ = study_data.get_explorer_rows(
        study, item_id=wanted, row_index=10**9,
    )

    assert len(rows) == 1
    assert rows["item_id"].iloc[0] == wanted




def test_get_explorer_rows_returns_empty_for_a_filtered_out_item(monkeypatch):
    """An item the context filter excludes is reported as absent, not raised."""
    study, _ = _seed_cache(monkeypatch)
    # index 0 is activity_type "fave", which the context filter drops.
    excluded = _raw_frame()["item_id"].iloc[0]

    rows, _ = study_data.get_explorer_rows(study, item_id=excluded)

    assert rows.empty




def test_get_explorer_rows_does_not_mutate_the_cached_frame(monkeypatch):
    """The returned rows are copied, so the detail panel cannot write back."""
    study, cached = _seed_cache(monkeypatch)
    before = cached["niche_name"].tolist()

    rows, _ = study_data.get_explorer_rows(study, item_id=cached["item_id"].iloc[1])
    rows["niche_name"] = "MUTATED"

    assert cached["niche_name"].tolist() == before
