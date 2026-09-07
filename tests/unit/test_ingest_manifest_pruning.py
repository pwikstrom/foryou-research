"""The ingester never drops a pending upload silently.

2026-09-06: a participant's user_data_tiktok_2.json matched an old test
collection's raw file, so load_raw skipped it and the manifest cleanup pruned
its entry as "processed". Now: a skip-set hit on a manifest entry is a
reported block (the file is never opened, the entry stays), manifests are
pruned only for files THIS run consumed, and the ledger carries the upload's
provenance.
"""

import json

import pandas as pd
import pytest

from fyp.ingest.base import BLOCKED_OUTCOME, ForYouBaseCollection, ForYouCollection

RAW = "probe_raw"




@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A concrete sub-collection over a local raw dir + a local recoded dir.
    load_single_raw raises, so any test that survives a load proves the file
    was never opened."""
    from fyp.fyp_config import fyp_cf

    raw_dir = tmp_path / RAW
    raw_dir.mkdir()
    recoded = tmp_path / "recoded"
    recoded.mkdir()
    monkeypatch.setitem(fyp_cf["paths"], RAW, str(raw_dir))
    monkeypatch.setitem(fyp_cf["paths"], "recoded", str(recoded))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)

    class _PruneProbeCollection(ForYouBaseCollection):
        raw_path = RAW

        def load_single_raw(self, filename: str) -> pd.DataFrame:
            raise AssertionError(f"{filename} must not be opened")

        def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
            return df

    try:
        col = _PruneProbeCollection(verbose=False)
        col.source_platform = "tiktok"
        col.data_source = "probe"
        yield col, raw_dir
    finally:
        ForYouBaseCollection._registry.remove(_PruneProbeCollection)




def _write_manifest(raw_dir, entries):
    (raw_dir / "ingestion_manifest.json").write_text(json.dumps(entries))




def _read_manifest(raw_dir):
    return json.loads((raw_dir / "ingestion_manifest.json").read_text())




def test_pending_entry_in_skip_set_is_blocked_not_opened(probe):
    col, raw_dir = probe
    (raw_dir / "x.json").write_text("{}")
    _write_manifest(raw_dir, {"x.json": {"collection_id": "c", "original_filename": "user_data_tiktok_2.json"}})

    col.load_raw(skip_these_raw_files=["x.json"])          # load_single_raw would raise

    assert col.blocked_this_run == {"x.json": "its name is already a raw file in the dataset"}
    assert col.manifest_this_run["x.json"]["original_filename"] == "user_data_tiktok_2.json"
    assert _read_manifest(raw_dir) == {"x.json": {"collection_id": "c", "original_filename": "user_data_tiktok_2.json"}}




def test_discard_list_hit_is_reported_with_its_own_reason(probe):
    col, raw_dir = probe
    (raw_dir / "y.json").write_text("{}")
    _write_manifest(raw_dir, {"y.json": {"collection_id": "c"}})
    col.discarded_raw_files.append("y.json")
    col.load_raw(skip_these_raw_files=[])
    assert col.blocked_this_run == {"y.json": "its name is in the discard list"}




def test_prune_keeps_blocked_and_unconsumed_drops_consumed_and_missing(probe):
    col, raw_dir = probe
    for fn in ("consumed.json", "blocked.json", "quarantined.json", "unreadable.json", "untouched.json"):
        (raw_dir / fn).write_text("{}")
    _write_manifest(raw_dir, {fn: {"collection_id": fn[:-5]} for fn in (
        "consumed.json", "blocked.json", "quarantined.json", "unreadable.json",
        "untouched.json", "gone.json")})
    col.file_stats_this_run = {"consumed.json": {}, "quarantined.json": {}, "unreadable.json": {}}
    col.quarantined_this_run = {"quarantined.json": {"status": "quarantined"}}
    col.load_failed_this_run = {"unreadable.json": "boom"}
    col.blocked_this_run = {"blocked.json": "reason"}

    main = ForYouCollection(verbose=False)
    main.collections = [col]
    main.prune_manifests()

    assert set(_read_manifest(raw_dir)) == {"blocked.json", "quarantined.json",
                                             "unreadable.json", "untouched.json"}




def test_ledger_skips_blocked_and_copies_provenance(probe):
    col, _raw_dir = probe
    col.manifest_this_run = {
        "a.json": {"collection_id": "cid_a", "original_filename": "user_data_tiktok.json",
                   "uploaded_by": "p@example.org", "user_id": "p@example.org",
                   "tz": "Australia/Melbourne", "client_reviewed": True,
                   "uploaded_at": "2026-09-06T11:29:18+00:00", "display_collection_id": "user_data_tiktok"},
    }
    main = ForYouCollection(verbose=False)
    main.collections = [col]
    main.ledger = {"schema_version": 1, "files": {
        "b.json": {"outcome": "added_as_new", "collection_id": "Test", "raw_rows": 3930}}}
    main.update_ledger([
        {"filename": "a.json", "outcome": "added_as_new", "raw_rows": 10, "final_rows": 10,
         "canonical_collection_id": "cid_a", "platform": "tiktok", "source": "probe"},
        {"filename": "b.json", "outcome": BLOCKED_OUTCOME, "notes": "its name is already a raw file"},
    ])
    files = main.ledger["files"]
    assert files["a.json"]["original_filename"] == "user_data_tiktok.json"
    assert files["a.json"]["uploaded_by"] == "p@example.org"
    assert files["a.json"]["tz"] == "Australia/Melbourne"
    assert files["a.json"]["client_reviewed"] is True
    assert files["a.json"]["display_collection_id"] == "user_data_tiktok"
    # The blocked entry never touches the older file's record.
    assert files["b.json"] == {"outcome": "added_as_new", "collection_id": "Test", "raw_rows": 3930}




def test_per_file_summary_reports_blocked_uploads():
    from web_interface.run_ingest_refresh import _build_per_file_summary

    class _Main:
        data = pd.DataFrame()

    summary = _build_per_file_summary(
        _Main(), raw_counts={}, processed_counts={}, discarded_at_load=set(),
        existing_raw_files=set(),
        blocked={"x.json": {"reason": "its name is already a raw file in the dataset",
                            "platform": "tiktok", "source": "ddp", "collection_id": "cid"}})
    assert len(summary) == 1
    row = summary[0]
    assert row["filename"] == "x.json"
    assert row["outcome"] == BLOCKED_OUTCOME == "blocked_name_collision"
    assert row["notes"] == "its name is already a raw file in the dataset"
    assert row["canonical_collection_id"] == "cid"
    assert row["platform"] == "tiktok" and row["source"] == "ddp"
