"""An admin's approval of a quarantined file survives the next ingest run.

2026-09-07: a browser-pruned TikTok export was quarantined (80 core paths
absent from the verbatim-export baseline), approved three times, and
re-quarantined by the very same finding on every following run — one
learned file cannot move a 20-file baseline's core-path support below the
threshold. Meanwhile an approval that landed while a run was in flight was
overwritten by that run's verdict commit. These tests pin both fixes.
"""

import pandas as pd
import pytest

import fyp.core.structure_sentinel as ss


class FakeCollection:
    source_platform = "tiktok"
    data_source = "ddp"
    raw_path = "ddp_raw"

    def __init__(self, fingerprint):
        self._fingerprint = fingerprint

    def fingerprint_raw(self, filename):
        return self._fingerprint


def _fp(paths):
    return {"kind": "json", "member_paths": [], "key_paths": sorted(paths), "stats": {}}


FULL_PATHS = [
    "Activity.Video Browsing History.VideoList[].Date|str",
    "Activity.Video Browsing History.VideoList[].Link|str",
    "Profile.Profile Information.userName|str",
    "Profile.Settings.App Language|str",
]
PRUNED_PATHS = FULL_PATHS[:2]


@pytest.fixture
def stores(monkeypatch):
    baselines = {"schema_version": 1, "baselines": {}}
    verdicts = {"schema_version": 1, "files": {}}
    monkeypatch.setattr(ss, "load_baselines", lambda: baselines)
    monkeypatch.setattr(ss, "save_baselines", lambda b: None)
    monkeypatch.setattr(ss, "load_verdicts", lambda: verdicts)
    monkeypatch.setattr(ss, "save_verdicts", lambda v: None)
    monkeypatch.setattr(ss.data_io, "getsize", lambda **kw: 1024 * 1024)
    return baselines, verdicts


def _mature_baseline(baselines, n=20):
    """A baseline so mature that learning one pruned file changes nothing."""
    baseline = ss._empty_baseline()
    for i in range(n):
        ss.learn_file(baseline, _fp(FULL_PATHS), {"raw_rows": 100}, None, f"legacy-{i}.json")
    baselines["baselines"][ss.baseline_key("tiktok", "ddp")] = baseline
    return baseline


def _quarantine_then_approve(stores, filename="pruned.json"):
    baselines, verdicts = stores
    _mature_baseline(baselines)
    first = ss.StructureSentinel()
    verdict = first.check_raw(FakeCollection(_fp(PRUNED_PATHS)), filename, pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "quarantined"
    first.commit(set())
    assert verdicts["files"][filename]["status"] == "quarantined"
    ss.approve_file(filename, reviewed_by="admin")
    assert verdicts["files"][filename]["status"] == "approved"
    return baselines, verdicts


def test_approved_file_is_not_requarantined_by_the_same_finding(stores):
    _quarantine_then_approve(stores)
    # The baseline is still overwhelmingly FULL_PATHS: the pruned shape is
    # missing-core-path drift again by the numbers ...
    second = ss.StructureSentinel()
    verdict = second.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "pruned.json", pd.DataFrame({"a": range(20)}))
    # ... but the approval stands.
    assert verdict["status"] == "approved"
    assert verdict["review_action"] == "approve"
    assert verdict["reviewed_by"] == "admin"
    assert any(f["code"] == "missing_core_paths" for f in verdict["findings"]), "findings stay on record"


def test_approved_file_survives_phase_b_and_is_learned_on_commit(stores):
    baselines, verdicts = _quarantine_then_approve(stores)
    second = ss.StructureSentinel()
    col = FakeCollection(_fp(PRUNED_PATHS))
    df = pd.DataFrame({"a": range(20), "activity_type": ["play"] * 20, "item_id": range(20)})
    second.check_raw(col, "pruned.json", df)
    verdict = second.check_processed(col, "pruned.json", df)
    assert verdict["status"] == "approved"
    second.commit({"pruned.json"})
    assert verdicts["files"]["pruned.json"]["status"] == "approved"
    assert "pruned.json" in baselines["baselines"][ss.baseline_key("tiktok", "ddp")]["learned_files"]


def test_new_kind_of_quarantine_finding_still_quarantines(stores):
    _quarantine_then_approve(stores)
    second = ss.StructureSentinel()
    # Same file name, but now a known path comes back with a different type:
    # a finding the reviewer never saw.
    retyped = [*PRUNED_PATHS[:1], "Activity.Video Browsing History.VideoList[].Link|int"]
    verdict = second.check_raw(FakeCollection(_fp(retyped)), "pruned.json", pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "quarantined"
    assert any(f["code"] == "type_changed" for f in verdict["findings"])


def test_apply_review_ignores_rejections_and_unreviewed(stores):
    verdict = {"status": "quarantined", "findings": [
        {"layer": "structure", "severity": "quarantine", "code": "missing_core_paths"}]}
    assert ss.apply_review(dict(verdict), None)["status"] == "quarantined"
    rejected = {"review_action": "reject", "findings": verdict["findings"]}
    assert ss.apply_review(dict(verdict), rejected)["status"] == "quarantined"


def test_commit_keeps_a_review_recorded_after_this_runs_evaluation(stores):
    baselines, verdicts = stores
    _mature_baseline(baselines)
    run = ss.StructureSentinel()
    verdict = run.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "pruned.json", pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "quarantined"
    # The admin approves while the run is still going (their reviewed_at is
    # later than the run's ts_evaluated).
    verdicts["files"]["pruned.json"] = {
        **{k: v for k, v in verdict.items() if k != "fingerprint"},
        "status": "approved", "review_action": "approve", "reviewed_by": "admin",
        "reviewed_at": "9999-01-01T00:00:00+00:00",
    }
    run.commit(set())
    assert verdicts["files"]["pruned.json"]["status"] == "approved"


def test_commit_overwrites_a_review_older_than_this_runs_evaluation(stores):
    baselines, verdicts = stores
    _mature_baseline(baselines)
    verdicts["files"]["pruned.json"] = {
        "status": "approved", "review_action": "approve", "reviewed_by": "admin",
        "reviewed_at": "2000-01-01T00:00:00+00:00", "findings": [
            {"layer": "structure", "severity": "quarantine", "code": "missing_member"}],
    }
    run = ss.StructureSentinel()
    # The old approval covered a different finding, so this run quarantines
    # and its verdict (newer than the review) is what gets stored.
    verdict = run.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "pruned.json", pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "quarantined"
    run.commit(set())
    assert verdicts["files"]["pruned.json"]["status"] == "quarantined"


def test_review_is_newer():
    assert ss.review_is_newer({"review_action": "approve", "reviewed_at": "2026-09-07T07:28:40+00:00"},
                              "2026-09-07T07:27:53+00:00")
    assert not ss.review_is_newer({"review_action": "approve", "reviewed_at": "2026-09-07T07:28:40+00:00"},
                                  "2026-09-07T07:41:11+00:00")
    assert not ss.review_is_newer({"review_action": None, "reviewed_at": None}, "2026-09-07T07:27:53+00:00")
    assert not ss.review_is_newer(None, "2026-09-07T07:27:53+00:00")
