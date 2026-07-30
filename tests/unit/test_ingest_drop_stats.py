"""Per-file intake drop stats + ledger extension (S3 item 1, backend).

Uses a throwaway concrete collection subclass with synthetic frames — no data
files are read or written. The subclass is removed from the auto-registry on
teardown so ``get_main_collection()`` in later tests is unaffected.
"""

import pandas as pd
import pytest

from fyp.ingest.base import ForYouBaseCollection, ForYouCollection


@pytest.fixture
def dummy_collection():
    class _DropStatsProbeCollection(ForYouBaseCollection):
        def load_single_raw(self, filename: str) -> pd.DataFrame:
            raise NotImplementedError

        def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
            # Platform-typical behaviour: rows without a parseable timestamp
            # are silently dropped inside process_single.
            return df[df["utc_timestamp"].notna()].copy()

    try:
        col = _DropStatsProbeCollection(verbose=False)
        col.source_platform = "tiktok"
        col.data_source = "probe"
        yield col
    finally:
        ForYouBaseCollection._registry.remove(_DropStatsProbeCollection)






def _raw_frame() -> pd.DataFrame:
    ts = pd.to_datetime([
        "2024-01-01 10:00", None, "2024-01-01 11:00",   # file a: 1 unparseable
        "2024-01-02 10:00", "2024-01-02 11:00",          # file b: all fine
    ])
    return pd.DataFrame({
        "raw_file": pd.array(["a.zip", "a.zip", "a.zip", "b.zip", "b.zip"], dtype="string[pyarrow]"),
        "item_id": pd.array(["1", "2", "3", "4", "5"], dtype="string[pyarrow]"),
        "activity_type": pd.array(["play"] * 5, dtype="string[pyarrow]"),
        "utc_timestamp": ts,
        "collection_id": pd.array(["c1"] * 3 + ["c2"] * 2, dtype="string[pyarrow]"),
        "tz_offset": pd.array([0] * 5, dtype="int64[pyarrow]"),
        "ts_added_to_dataset": pd.to_datetime(["2026-01-01"] * 5),
    })






def test_record_file_drops_accumulates(dummy_collection):
    col = dummy_collection
    col._record_file_drops({"a.zip": 3}, "not_parseable")
    col._record_file_drops(pd.Series({"a.zip": 2, "b.zip": 1}), "missing_required")
    col._record_file_drops({"a.zip": 0, "b.zip": -1}, "noise")  # ignored

    assert col.file_stats_this_run["a.zip"]["dropped"] == {
        "not_parseable": 3, "missing_required": 2}
    assert col.file_stats_this_run["b.zip"]["dropped"] == {"missing_required": 1}






def test_process_counts_per_file_parse_drops(dummy_collection):
    col = dummy_collection
    col.data = _raw_frame()
    col.state = "raw"

    col.process()

    assert col.state == "processed"
    # File a lost its NaT-timestamp row inside process_single; file b lost none.
    assert col.file_stats_this_run["a.zip"]["dropped"]["not_parseable"] == 1
    assert "not_parseable" not in col.file_stats_this_run.get("b.zip", {}).get("dropped", {})
    # The kept rows really are 2 + 2
    assert len(col.data) == 4






def test_standardize_counts_required_core_drops(dummy_collection):
    col = dummy_collection
    df = _raw_frame().dropna(subset=["utc_timestamp"]).copy()
    # Null a required-core field (activity_type) on one of file b's rows.
    df.loc[df["item_id"] == "4", "activity_type"] = pd.NA
    col.data = df
    col.state = "raw"

    col.process()

    assert col.file_stats_this_run["b.zip"]["dropped"]["missing_required"] == 1
    assert len(col.data) == 3






def test_update_ledger_persists_new_fields():
    col = ForYouCollection.__new__(ForYouCollection)
    col.discarded_raw_files = []
    col.ledger = {
        "schema_version": 1,
        "files": {
            "old.zip": {  # pre-extension entry: must survive untouched semantics
                "outcome": "fully_deduped",
                "raw_rows": 50,
                "kept_rows": 0,
                "ts_first_seen": "2026-01-01T00:00:00+00:00",
            },
        },
    }

    col.update_ledger([
        {
            "filename": "new.zip",
            "outcome": "added_as_new",
            "raw_rows": 100,
            "processed_rows": 90,
            "final_rows": 85,
            "deduped_rows": 5,
            "dropped": {"not_parseable": 8, "missing_required": 2},
            "canonical_collection_id": "c9",
            "merged_with_siblings": [],
            "platform": "tiktok",
            "source": "ddp",
        },
        {
            "filename": "tiny.zip",
            "outcome": "discarded_at_load",
            "raw_rows": 4,  # the true count — no longer clobbered to 0
            "processed_rows": 0,
            "final_rows": 0,
            "deduped_rows": 0,
            "dropped": {},
        },
    ])

    entry = col.ledger["files"]["new.zip"]
    assert entry["processed_rows"] == 90
    assert entry["deduped_rows"] == 5
    assert entry["dropped"] == {"not_parseable": 8, "missing_required": 2}
    assert entry["kept_rows"] == 85
    assert entry["ts_first_seen"]  # stamped

    assert col.ledger["files"]["tiny.zip"]["raw_rows"] == 4
    # Untouched legacy entry keeps its shape
    assert col.ledger["files"]["old.zip"]["raw_rows"] == 50
    assert "dropped" not in col.ledger["files"]["old.zip"]

    # LEDGER_SKIP_OUTCOMES behaviour: the discarded file is skip-listed
    assert "tiny.zip" in col.discarded_raw_files
    assert "new.zip" not in col.discarded_raw_files
