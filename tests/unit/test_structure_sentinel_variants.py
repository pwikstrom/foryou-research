"""Variant-keyed sentinel baselines for client-reviewed (pruned) donations.

A browser-reviewed TikTok upload is missing whole sections by design (DMs,
login history, settings are stripped before upload), so against the verbatim
export baseline it would quarantine as ``missing_core_paths``. Reviewed files
must instead learn and evaluate against their own ``__reviewed`` baseline —
these tests pin the whole variant path: key derivation, check_raw, commit,
and admin approval.
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
    "Direct Messages.Chat History.chat history with *[].Content|str",
    "Profile.Profile Information.userName|str",
]
PRUNED_PATHS = [
    "Activity.Video Browsing History.VideoList[].Date|str",
    "Activity.Video Browsing History.VideoList[].Link|str",
]


@pytest.fixture
def stores(monkeypatch):
    """In-memory baseline/verdict stores — nothing touches data_io."""
    baselines = {"schema_version": 1, "baselines": {}}
    verdicts = {"schema_version": 1, "files": {}}
    monkeypatch.setattr(ss, "load_baselines", lambda: baselines)
    monkeypatch.setattr(ss, "save_baselines", lambda b: None)
    monkeypatch.setattr(ss, "load_verdicts", lambda: verdicts)
    monkeypatch.setattr(ss, "save_verdicts", lambda v: None)
    monkeypatch.setattr(ss.data_io, "getsize", lambda **kw: 1024 * 1024)
    return baselines, verdicts


def _mature_legacy_baseline(baselines):
    """A legacy baseline past the learn-only threshold, with FULL_PATHS core."""
    baseline = ss._empty_baseline()
    for i in range(ss.MIN_ACCEPTED_FOR_STRUCTURE_CHECKS):
        ss.learn_file(baseline, _fp(FULL_PATHS), {"raw_rows": 100}, None, f"legacy-{i}.json")
    baselines["baselines"][ss.baseline_key("tiktok", "ddp")] = baseline
    return baseline


def test_baseline_key_variant():
    assert ss.baseline_key("tiktok", "ddp") == "tiktok_ddp"
    assert ss.baseline_key("tiktok", "ddp", None) == "tiktok_ddp"
    assert ss.baseline_key("tiktok", "ddp", "reviewed") == "tiktok_ddp__reviewed"


def test_pruned_file_quarantines_on_legacy_baseline(stores):
    # Sanity: without the variant, a pruned file IS missing-core-path drift.
    baselines, _ = stores
    _mature_legacy_baseline(baselines)
    sentinel = ss.StructureSentinel()
    verdict = sentinel.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "pruned.json",
                                 pd.DataFrame({"a": range(20)}))
    assert verdict["status"] == "quarantined"
    assert any(f["code"] == "missing_core_paths" for f in verdict["findings"])


def test_reviewed_variant_uses_own_baseline(stores):
    baselines, _ = stores
    _mature_legacy_baseline(baselines)
    sentinel = ss.StructureSentinel()
    verdict = sentinel.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "pruned.json",
                                 pd.DataFrame({"a": range(20)}), variant="reviewed")
    # Fresh __reviewed baseline: learn-only, never quarantined by the legacy shape.
    assert verdict["status"] == "learning"
    assert verdict["variant"] == "reviewed"

    sentinel.commit({"pruned.json"})
    reviewed = baselines["baselines"][ss.baseline_key("tiktok", "ddp", "reviewed")]
    legacy = baselines["baselines"][ss.baseline_key("tiktok", "ddp")]
    assert "pruned.json" in reviewed["learned_files"]
    assert "pruned.json" not in legacy["learned_files"]


def test_mature_reviewed_baseline_accepts_pruned_shape(stores):
    baselines, _ = stores
    _mature_legacy_baseline(baselines)
    reviewed = ss._empty_baseline()
    for i in range(ss.MIN_ACCEPTED_FOR_STRUCTURE_CHECKS):
        ss.learn_file(reviewed, _fp(PRUNED_PATHS), {"raw_rows": 100}, None, f"rev-{i}.json")
    baselines["baselines"][ss.baseline_key("tiktok", "ddp", "reviewed")] = reviewed

    sentinel = ss.StructureSentinel()
    verdict = sentinel.check_raw(FakeCollection(_fp(PRUNED_PATHS)), "another.json",
                                 pd.DataFrame({"a": range(20)}), variant="reviewed")
    assert verdict["status"] == "ok"


def test_check_processed_reuses_observed_variant(stores):
    baselines, _ = stores
    sentinel = ss.StructureSentinel()
    col = FakeCollection(_fp(PRUNED_PATHS))
    df = pd.DataFrame({"a": range(20)})
    sentinel.check_raw(col, "pruned.json", df, variant="reviewed")
    verdict = sentinel.check_processed(col, "pruned.json", df)
    assert verdict["variant"] == "reviewed"


def test_approve_learns_into_variant_baseline(stores):
    baselines, verdicts = stores
    verdicts["files"]["pruned.json"] = {
        "status": "quarantined",
        "platform": "tiktok",
        "source": "ddp",
        "variant": "reviewed",
        "findings": [],
        "raw_stats": {"raw_rows": 100},
        "processed_stats": None,
        "fingerprint": _fp(PRUNED_PATHS),
    }
    entry = ss.approve_file("pruned.json", reviewed_by="admin")
    assert entry["status"] == "approved"
    reviewed_key = ss.baseline_key("tiktok", "ddp", "reviewed")
    assert "pruned.json" in baselines["baselines"][reviewed_key]["learned_files"]
    assert ss.baseline_key("tiktok", "ddp") not in baselines["baselines"]
