"""save_ledger merges this process's edits into storage instead of overwriting.

2026-09-07: the hub's Approve button removed two ``quarantined_structure``
ledger entries while an ingest run was holding the ledger in memory; the run
finished a few seconds later and wrote its stale copy back, putting both
entries — and both files' invisible limbo — straight back.
"""

import json
from types import SimpleNamespace

import pytest

from fyp.ingest.base import ForYouCollection

LEDGER = "ingestion_ledger.json"


@pytest.fixture
def recoded(tmp_path, monkeypatch):
    from fyp.fyp_config import fyp_cf

    recoded_dir = tmp_path / "recoded"
    recoded_dir.mkdir()
    monkeypatch.setitem(fyp_cf["paths"], "recoded", str(recoded_dir))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    return recoded_dir


def _write(recoded_dir, files):
    (recoded_dir / LEDGER).write_text(json.dumps({"schema_version": 1, "files": files}))


def _read(recoded_dir):
    return json.loads((recoded_dir / LEDGER).read_text())["files"]


def _entry(outcome, **extra):
    return {"outcome": outcome, "raw_rows": 1, "kept_rows": 1, "collection_id": "c",
            "merged_with_siblings": [], "platform": "tiktok", "source": "ddp",
            "ts_first_seen": "t0", "ts_last_seen": "t0", "notes": None, **extra}


def test_hub_removal_during_a_run_is_not_undone_by_the_runs_save(recoded):
    _write(recoded, {"held.json": _entry("quarantined_structure"), "old.json": _entry("added_as_new")})
    run = ForYouCollection(verbose=False)                 # loads: held + old
    run.ledger["files"]["new.json"] = _entry("added_as_new")

    hub = ForYouCollection(verbose=False)                 # the Approve click
    assert hub.remove_from_ledger("held.json")
    hub.save_ledger()
    assert set(_read(recoded)) == {"old.json"}

    run.save_ledger()                                     # the run finishes
    assert set(_read(recoded)) == {"old.json", "new.json"}


def test_run_entries_land_and_untouched_entries_keep_storages_version(recoded):
    _write(recoded, {"a.json": _entry("added_as_new"), "b.json": _entry("added_as_new")})
    run = ForYouCollection(verbose=False)
    run.ledger["files"]["a.json"] = _entry("added_as_new", raw_rows=99)   # changed here

    other = ForYouCollection(verbose=False)
    other.set_ledger_outcome("b.json", "manually_excluded", note="withdrawn")   # changed elsewhere
    other.save_ledger()

    run.save_ledger()
    stored = _read(recoded)
    assert stored["a.json"]["raw_rows"] == 99
    assert stored["b.json"]["outcome"] == "manually_excluded"
    assert run.ledger["files"]["b.json"]["outcome"] == "manually_excluded", "memory follows storage"


def test_removal_here_removes_in_storage(recoded):
    _write(recoded, {"gone.json": _entry("quarantined_structure"), "kept.json": _entry("added_as_new")})
    col = ForYouCollection(verbose=False)
    assert col.remove_from_ledger("gone.json")
    col.save_ledger()
    assert set(_read(recoded)) == {"kept.json"}


def test_quarantine_reviewed_after_evaluation_is_not_written(recoded, monkeypatch):
    """The run quarantines a NEW file; the admin approves it before the run
    saves. The approval's ledger removal found nothing to remove (the entry
    did not exist yet), so the run itself must hold the entry back."""
    from fyp.core import structure_sentinel as ss

    _write(recoded, {})
    run = ForYouCollection(verbose=False)

    sub = SimpleNamespace(quarantined_this_run={
        "fresh.json": {"status": "quarantined", "ts_evaluated": "2026-09-07T07:27:53+00:00"}})
    run.collections = [sub]
    run.ledger["files"]["fresh.json"] = _entry("quarantined_structure")
    run.ledger["files"]["other.json"] = _entry("quarantined_structure")   # not reviewed

    monkeypatch.setattr(ss, "load_verdicts", lambda: {"files": {
        "fresh.json": {"review_action": "approve", "reviewed_at": "2026-09-07T07:28:40+00:00"},
        "other.json": {"review_action": None, "reviewed_at": None},
    }})
    run.save_ledger()
    stored = _read(recoded)
    assert "fresh.json" not in stored
    assert stored["other.json"]["outcome"] == "quarantined_structure"


def test_save_without_a_stored_ledger_writes_memory(recoded):
    col = ForYouCollection(verbose=False)          # nothing in storage yet
    col.ledger["files"]["x.json"] = _entry("added_as_new")
    col.save_ledger()
    assert set(_read(recoded)) == {"x.json"}
