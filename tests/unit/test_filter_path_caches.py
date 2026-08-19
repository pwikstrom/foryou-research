"""The /api/explore/filter hot path: combined masks + per-request caches.

2026-08-19: a single checkbox tick on a 2.4M-row study took 5.2s (7.7s with
two filters) because the endpoint re-did per-request work that never changes
between requests: filter_dataframe materialised every projected column once
PER criterion, the explorer metadata JSON was exists()+downloaded from GCS
every request, and enrich_with_user_tags rebuilt three O(rows) columns per
keystroke. These tests pin the fixes:

- filter_dataframe AND-combines vectorized criteria into ONE row selection
  and must select exactly the rows the old sequential narrowing selected.
- enrich_with_user_tags serves its computed columns from the mtime-keyed
  caches when ``study`` is passed, and recomputes when the user's JSON blob
  is refetched.
- get_explorer_metadata_cached parses the JSON once per (study, mtime).
"""

import numpy as np
import pandas as pd
import pytest

from web_interface import explorer_backend
from web_interface.services import study_data


@pytest.fixture()
def frame():
    df = pd.DataFrame(
        {
            "item_id": pd.array(["a", "b", "c", "d", "e"], dtype="string[pyarrow]"),
            "score": pd.array([1.0, 2.0, 3.0, None, 5.0], dtype="float64[pyarrow]"),
            "niche": pd.array(
                ["cats", "dogs", "cats", "dogs", None], dtype="string[pyarrow]"
            ),
            "tags": pd.Series([["x"], ["y"], ["x", "y"], [], None], dtype=object),
        }
    )
    col_types = {"item_id": "identifier", "score": "number",
                 "niche": "category", "tags": "list"}
    return df, col_types


def test_combined_filters_match_sequential_semantics(frame):
    """Numeric range + category + list criteria together must keep exactly
    the rows that pass every criterion — NA never matches."""
    df, col_types = frame
    filters = {
        "score": {"value": {"min": 1.5, "max": 4.0}},   # b, c (NaN row d out)
        "niche": {"value": ["cats"]},                    # a, c
        "tags": {"value": ["y"]},                        # b, c (None row out)
    }
    out = explorer_backend.filter_dataframe(df, col_types, filters)
    assert list(out["item_id"]) == ["c"]


def test_single_category_filter_keeps_row_order(frame):
    df, col_types = frame
    out = explorer_backend.filter_dataframe(
        df, col_types, {"niche": {"value": ["cats", "dogs"]}})
    assert list(out["item_id"]) == ["a", "b", "c", "d"]


def test_no_filters_returns_all_rows_unmutated(frame):
    df, col_types = frame
    out = explorer_backend.filter_dataframe(df, col_types, {})
    assert len(out) == len(df)
    assert out is not df  # caller's frame must never be handed back writable


def test_numeric_range_with_na_rows(frame):
    """An NA in the filtered column drops the row rather than raising —
    Arrow comparisons yield NA, which must be treated as no-match."""
    df, col_types = frame
    out = explorer_backend.filter_dataframe(
        df, col_types, {"score": {"value": {"min": 0.0}}})
    assert list(out["item_id"]) == ["a", "b", "c", "e"]


@pytest.fixture()
def _clear_annot_caches():
    with study_data._annot_cols_lock:
        study_data._user_annot_cols_cache.clear()
        study_data._machine_annot_cols_cache.clear()
    yield
    with study_data._annot_cols_lock:
        study_data._user_annot_cols_cache.clear()
        study_data._machine_annot_cols_cache.clear()


def _enrich_frame():
    return pd.DataFrame(
        {
            "item_id": pd.array(["1", "2", "3"], dtype="string[pyarrow]"),
            "annotated_ok": pd.array([True, False, None], dtype="bool[pyarrow]"),
            "annotation_version": pd.array(["v1", None, None], dtype="string[pyarrow]"),
        }
    )


def test_enrichment_columns_are_cached_per_study_mtime(monkeypatch, _clear_annot_caches):
    df = _enrich_frame()
    blob = {"annotations": {"1": {"tags": ["keep"]}}}
    monkeypatch.setattr(study_data, "get_user_json_cached", lambda u: blob)
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 111.0)

    calls = {"user": 0, "machine": 0}
    real_user = study_data._compute_user_annotation_columns
    real_machine = study_data._compute_machine_annotation_columns

    def counting_user(*a, **k):
        calls["user"] += 1
        return real_user(*a, **k)

    def counting_machine(*a, **k):
        calls["machine"] += 1
        return real_machine(*a, **k)

    monkeypatch.setattr(study_data, "_compute_user_annotation_columns", counting_user)
    monkeypatch.setattr(study_data, "_compute_machine_annotation_columns", counting_machine)

    out1, ct1 = study_data.enrich_with_user_tags(df, {}, "user1", study="s")
    out2, ct2 = study_data.enrich_with_user_tags(df, {}, "user1", study="s")

    assert calls == {"user": 1, "machine": 1}
    assert list(out2["Has Annotation"]) == [True, False, False]
    assert list(out2["Machine Annotations"]) == [
        "Machine Annotated", "Cannot Machine Annotate", "Not Attempted"]
    assert ct2["Has Annotation"] == "category"
    # A different user computes their own columns but reuses the machine ones.
    study_data.enrich_with_user_tags(df, {}, "user2", study="s")
    assert calls == {"user": 2, "machine": 1}


def test_user_tags_column_is_arrow_backed(monkeypatch, _clear_annot_caches):
    """The 'User Tags' column must be an Arrow list column, not object dtype:
    get_current_stats' object-list path is a per-row python loop that
    measured ~0.8s over 2.4M rows per stats pass (2026-08-19)."""
    df = _enrich_frame()
    blob = {"annotations": {"2": {"tags": ["keep", "also"]}}}
    monkeypatch.setattr(study_data, "get_user_json_cached", lambda u: blob)
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 1.0)

    out, ct = study_data.enrich_with_user_tags(df, {}, "user1", study="s")
    assert isinstance(out["User Tags"].dtype, pd.ArrowDtype)
    assert "list" in str(out["User Tags"].dtype)
    assert sorted(out["User Tags"].iloc[1]) == ["also", "keep"]
    assert list(out["User Tags"].iloc[0]) == []
    assert ct["User Tags"] == "list"


def test_enrichment_cache_invalidates_on_new_user_blob(monkeypatch, _clear_annot_caches):
    """A refetched user JSON (new object identity) must force a recompute —
    that is how tag saves and the 60s TTL propagate into the columns."""
    df = _enrich_frame()
    blobs = [{"annotations": {}}, {"annotations": {"2": {"tags": ["new"]}}}]
    monkeypatch.setattr(study_data, "get_user_json_cached", lambda u: blobs.pop(0))
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 111.0)

    out1, _ = study_data.enrich_with_user_tags(df, {}, "user1", study="s")
    out2, _ = study_data.enrich_with_user_tags(df, {}, "user1", study="s")
    assert list(out1["Has Annotation"]) == [False, False, False]
    assert list(out2["Has Annotation"]) == [False, True, False]


def test_enrichment_never_mutates_the_cached_metadata_types(monkeypatch, _clear_annot_caches):
    """col_types handed in must not gain keys in place (shared dict safety)."""
    df = _enrich_frame()
    monkeypatch.setattr(study_data, "get_user_json_cached", lambda u: None)
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 1.0)
    col_types = {"item_id": "identifier"}
    _, out_types = study_data.enrich_with_user_tags(df, col_types, "u", study="s")
    assert "Has Annotation" not in col_types
    assert out_types["Has Annotation"] == "category"


def test_explorer_metadata_parsed_once_per_mtime(monkeypatch):
    with study_data._explorer_meta_lock:
        study_data._explorer_meta_cache.clear()
    loads = {"n": 0}

    def fake_load_json(storage_location, filename):
        loads["n"] += 1
        return {"total_stats": {"x": 1}}

    monkeypatch.setattr(study_data, "_ttl_mtime", lambda f: 42.0)
    monkeypatch.setattr(study_data.data_io, "load_json", fake_load_json)

    a = study_data.get_explorer_metadata_cached("mystudy")
    b = study_data.get_explorer_metadata_cached("mystudy")
    assert loads["n"] == 1
    assert a is b and a["total_stats"] == {"x": 1}

    # A new mtime invalidates.
    monkeypatch.setattr(study_data, "_ttl_mtime", lambda f: 43.0)
    study_data.get_explorer_metadata_cached("mystudy")
    assert loads["n"] == 2
    with study_data._explorer_meta_lock:
        study_data._explorer_meta_cache.clear()
