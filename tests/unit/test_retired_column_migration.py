#!/usr/bin/env python3
"""Tests for the retired platform-column → generic base-field migration.

Covers the consolidation-time coalesce (``fyp.scrape._coalesce_retired_columns``)
and the presentation-store surface-flag migration
(``fyp.var_presentation._migrate_retired_names`` / ``seed_from_var_schema_frame``).

Usage:
    python tests/unit/test_retired_column_migration.py
    pytest tests/unit/test_retired_column_migration.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fyp import scrape_contract as sc
from fyp import var_presentation as vp
from fyp.scrape import _coalesce_retired_columns




def test_coalesce_single_platform_frame():
    """A legacy TikTok frame's stats_*/author_uniqueId land in the generic columns."""
    df = pd.DataFrame({
        "item_id": pd.Series(["1", "2"], dtype="string[pyarrow]"),
        "stats_diggCount": pd.Series([50, -1], dtype="int64[pyarrow]"),
        "stats_commentCount": pd.Series([10, 3], dtype="int64[pyarrow]"),
        "author_uniqueId": pd.Series(["bob", "eve"], dtype="string[pyarrow]"),
    })
    out = _coalesce_retired_columns(df)

    for retired in ("stats_diggCount", "stats_commentCount", "author_uniqueId"):
        assert retired not in out.columns, retired
    assert int(out["fave_count"].iloc[0]) == 50
    # -1 missing-count sentinels are kept verbatim.
    assert int(out["fave_count"].iloc[1]) == -1
    assert int(out["comment_count"].iloc[1]) == 3
    assert out["author_handle"].iloc[1] == "eve"
    assert str(out["fave_count"].dtype) == "int64[pyarrow]"
    # str(StringDtype("pyarrow")) is plain "string"; the storage shows in repr.
    assert out["author_handle"].dtype == pd.StringDtype("pyarrow")
    print("PASS: single-platform coalesce")




def test_coalesce_mixed_frame_combines_without_duplicates():
    """Several retired sources sharing one target coalesce row-wise, no duplicate labels."""
    df = pd.DataFrame({
        "item_id": pd.Series(["t1", "i1"], dtype="string[pyarrow]"),
        "stats_diggCount": pd.Series([50, None], dtype="int64[pyarrow]"),
        "ig_like_count": pd.Series([None, 70], dtype="int64[pyarrow]"),
        "author_uniqueId": pd.Series(["bob", None], dtype="string[pyarrow]"),
        "ig_author_handle": pd.Series([None, "alice"], dtype="string[pyarrow]"),
    })
    out = _coalesce_retired_columns(df)

    assert not out.columns.duplicated().any()
    assert int(out["fave_count"].iloc[0]) == 50
    assert int(out["fave_count"].iloc[1]) == 70
    assert out["author_handle"].tolist() == ["bob", "alice"]
    print("PASS: mixed-frame coalesce")




def test_coalesce_keeps_existing_generic_values():
    """An already-populated generic column wins over a retired source."""
    df = pd.DataFrame({
        "item_id": pd.Series(["1"], dtype="string[pyarrow]"),
        "fave_count": pd.Series([99], dtype="int64[pyarrow]"),
        "stats_diggCount": pd.Series([50], dtype="int64[pyarrow]"),
    })
    out = _coalesce_retired_columns(df)
    assert int(out["fave_count"].iloc[0]) == 99
    assert "stats_diggCount" not in out.columns
    print("PASS: existing generic value wins")




def test_coalesce_noop_on_canonical_frame():
    """A frame with no retired columns passes through unchanged."""
    df = pd.DataFrame({
        "item_id": pd.Series(["1"], dtype="string[pyarrow]"),
        "fave_count": pd.Series([5], dtype="int64[pyarrow]"),
    })
    out = _coalesce_retired_columns(df)
    assert list(out.columns) == ["item_id", "fave_count"]
    print("PASS: canonical frame no-op")




class _StubDataIO:
    """Captures save_json calls; never touches storage."""

    def __init__(self):
        self.saved = None

    def save_json(self, data=None, storage_location=None, filename=None):
        self.saved = data




def test_presentation_migration_unions_and_persists(monkeypatch=None):
    """Retired names map to successors per surface; the migrated payload persists once."""
    stub = _StubDataIO()
    original = vp._data_io
    vp._data_io = lambda: stub
    try:
        payload = {
            "version": 1,
            "surfaces": {
                "filter": ["author_uniqueId", "play_count"],
                "timeline": [],
                "viz": [],
                "display": ["stats_diggCount", "stats_commentCount", "author_uniqueId",
                            "stats_shareCount", "stats_collectCount", "play_count"],
            },
        }
        out = vp._migrate_retired_names(payload)
        assert out["surfaces"]["filter"] == ["author_handle", "play_count"]
        assert out["surfaces"]["display"] == sorted(
            ["fave_count", "comment_count", "share_count", "save_count", "author_handle", "play_count"]
        )
        assert out["updated_by"] == "retired-column-migration"
        assert stub.saved is not None, "migrated payload must persist"

        # Idempotent: a second pass changes nothing and does not re-save.
        stub.saved = None
        again = vp._migrate_retired_names(out)
        assert again["surfaces"] == out["surfaces"]
        assert stub.saved is None, "no-op pass must not re-save"
    finally:
        vp._data_io = original
    print("PASS: presentation migration")




def test_seed_from_csv_maps_retired_names():
    """Seeding from the legacy CSV prios cannot reintroduce retired names."""
    vs = pd.DataFrame({
        "variable_name": ["stats_diggCount", "author_uniqueId", "play_count"],
        "web_filter_prio": [None, 1, 1],
        "web_timeline_prio": [None, None, None],
        "web_viz_prio": [None, None, None],
        "web_display_prio": [1, 1, 1],
    })
    payload = vp.seed_from_var_schema_frame(vs)
    assert payload["surfaces"]["filter"] == ["author_handle", "play_count"]
    assert payload["surfaces"]["display"] == ["author_handle", "fave_count", "play_count"]
    print("PASS: CSV seed maps retired names")




def test_retirement_map_matches_contract():
    """Every retirement target is a generic base field of the current contract."""
    contract = sc.load_contract()
    base = set(sc.base_field_names(contract))
    assert set(sc.RETIRED_TO_GENERIC.values()) <= base
    field_names = {f["name"] for f in contract.get("fields", [])}
    assert not set(sc.RETIRED_TO_GENERIC) & field_names
    print("PASS: retirement map consistent with contract")




if __name__ == "__main__":
    test_coalesce_single_platform_frame()
    test_coalesce_mixed_frame_combines_without_duplicates()
    test_coalesce_keeps_existing_generic_values()
    test_coalesce_noop_on_canonical_frame()
    test_presentation_migration_unions_and_persists()
    test_seed_from_csv_maps_retired_names()
    test_retirement_map_matches_contract()
    print("All retired-column migration tests passed.")
