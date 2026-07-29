"""Tuple-column repair after a parquet round-trip that drops pandas metadata.

Regression guard for the intermittent timelines-refresh failure (prod,
2026-04-21): ``_repair_stringified_multiindex`` built ``pd.Index(...)`` from a
list of tuples, which pandas 2.2.x promotes to a MultiIndex only sometimes.
When it landed flat, ``df.loc[row, ('personas', 'active_days')]`` read the
tuple as a list-of-labels and raised — from byte-identical stored data.
"""

import pandas as pd

from fyp.core.data_io import _repair_stringified_multiindex


def test_all_tuple_columns_become_multiindex():
    df = pd.DataFrame(
        [[3, "x"]],
        columns=["('personas', 'active_days')", "('other', 'accepted')"],
        index=["c1"],
    )

    out = _repair_stringified_multiindex(df)

    assert isinstance(out.columns, pd.MultiIndex)
    # The access pattern that used to raise when the Index landed flat
    assert out.loc["c1", ("personas", "active_days")] == 3
    assert out.loc["c1", ("other", "accepted")] == "x"






def test_mixed_columns_stay_flat_and_scalars_accessible():
    """A scalar column alongside tuples must remain directly accessible."""
    df = pd.DataFrame(
        [["c1", 3]],
        columns=["collection_id", "('personas', 'active_days')"],
    )

    out = _repair_stringified_multiindex(df)

    assert not isinstance(out.columns, pd.MultiIndex)
    assert out["collection_id"].tolist() == ["c1"]
    assert out[("personas", "active_days")].tolist() == [3]






def test_no_tuple_columns_is_a_noop():
    df = pd.DataFrame({"collection_id": ["c1"], "active_days": [3]})
    out = _repair_stringified_multiindex(df)
    assert out is df
    assert list(out.columns) == ["collection_id", "active_days"]






def test_unparseable_parenthesised_string_is_kept():
    """A column that merely looks tuple-ish must not be mangled."""
    df = pd.DataFrame({"(not a tuple": [1], "('a', 'b')": [2]})
    out = _repair_stringified_multiindex(df)
    assert "(not a tuple" in out.columns
    assert ("a", "b") in out.columns






def test_active_days_lookup_works_on_both_column_shapes():
    """The timelines-dropdown lookup must survive flat AND MultiIndex columns.

    Selecting a scalar name and a tuple name in one ``df[[...]]`` raises on a
    MultiIndex, which silently emptied the active-days map; the route reads the
    two columns separately instead.
    """
    def _build_map(filtered, target_id_col, active_days_col):
        ad_ids = filtered[target_id_col]
        if isinstance(ad_ids, pd.DataFrame):
            ad_ids = ad_ids.iloc[:, 0]
        ad_vals = filtered[active_days_col]
        if isinstance(ad_vals, pd.DataFrame):
            ad_vals = ad_vals.iloc[:, 0]
        out = {}
        for cid_raw, val in zip(ad_ids, ad_vals):
            if pd.isna(cid_raw):
                continue
            out[str(cid_raw)] = None if pd.isna(val) else int(val)
        return out

    flat = pd.DataFrame({"collection_id": ["c1", "c2"],
                         ("personas", "active_days"): [3, 9]})
    assert _build_map(flat, "collection_id", ("personas", "active_days")) == {"c1": 3, "c2": 9}

    multi = pd.DataFrame(
        [[3], [9]],
        columns=pd.MultiIndex.from_tuples([("personas", "active_days")]),
        index=pd.Index(["c1", "c2"], name="collection_id"),
    ).reset_index()
    assert _build_map(multi, "collection_id", ("personas", "active_days")) == {"c1": 3, "c2": 9}






def test_selective_load_sets_index_before_repair(monkeypatch, tmp_path):
    """load_parquet_selective indexes first, so the repair sees all tuples.

    This is the real prod shape: a collections-metadata frame whose columns are
    stringified tuples plus the scalar 'collection_id' used as the index.
    """
    import pyarrow.parquet as pq

    import fyp.core.data_io as data_io

    source = pd.DataFrame({
        "collection_id": ["c1", "c2"],
        "('personas', 'active_days')": [3, 9],
        "('other', 'accepted')": [True, False],
    })
    path = tmp_path / "collections_metadata.parquet"
    source.to_parquet(path)

    # Mirror the metadata-stripping read the real function performs.
    monkeypatch.setattr(data_io, "_resolve_read_path",
                        lambda *a, **kw: (str(path), "local", None), raising=False)

    tbl = pq.read_table(str(path))
    meta = tbl.schema.metadata or {}
    tbl = tbl.replace_schema_metadata(
        {k: v for k, v in meta.items() if k != b"pandas"} or None)
    df = tbl.to_pandas(types_mapper=pd.ArrowDtype)

    df = df.set_index("collection_id")
    df = data_io._repair_stringified_multiindex(df)

    assert isinstance(df.columns, pd.MultiIndex)
    # The exact lookup run_timelines_refresh performs
    assert df.loc["c1", ("personas", "active_days")] == 3
