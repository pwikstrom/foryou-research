"""The parse-rate floor: a file whose parser kept almost nothing quarantines
without any learned baseline.

Pinned by the 2026-09-07 file whose 85,933 raw rows became 900: the drift
layer needed five accepted files' statistics and had fewer, so nothing made
the operator look before approving. The floor needs no history. It is
computed on the rows the parser should have read (raw rows minus the
sections it excludes by design), so a file that is mostly Off TikTok
Activity does not trip it once those rows are counted as such.
"""

import pandas as pd
import pytest

import fyp.core.structure_sentinel as ss


class FakeCollection:
    source_platform = "tiktok"
    data_source = "ddp"
    raw_path = "ddp_raw"

    def __init__(self, fingerprint, file_stats=None):
        self._fingerprint = fingerprint
        self.file_stats_this_run = file_stats or {}

    def fingerprint_raw(self, filename):
        return self._fingerprint


def _fp():
    return {"kind": "json", "member_paths": [],
            "key_paths": ["Activity.Video Browsing History.VideoList[].Date|str"], "stats": {}}


def _rows(n: int) -> pd.DataFrame:
    return pd.DataFrame({"item_id": [f"v{i}" for i in range(n)], "activity_type": "play"})


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


def test_parse_rate_uses_the_ingestible_denominator():
    stats = ss.compute_processed_stats(85_933, _rows(900), outside_whitelist=85_000)
    assert stats["kept_ratio"] == round(900 / 85_933, 4)
    assert stats["ingestible_rows"] == 933
    assert stats["parse_rate"] == round(900 / 933, 4)
    assert ss.evaluate_parse_floor(stats) == []


def test_floor_finding_when_the_parser_kept_almost_nothing():
    stats = ss.compute_processed_stats(85_933, _rows(900))
    findings = ss.evaluate_parse_floor(stats)
    assert [f["code"] for f in findings] == ["parse_rate_floor"]
    assert findings[0]["severity"] == "quarantine"
    assert "900 of 85,933" in findings[0]["detail"]


def test_floor_quarantines_even_while_the_baseline_is_learning(stores):
    """No accepted files at all: structure checks are learn-only, the floor is not."""
    sentinel = ss.StructureSentinel()
    coll = FakeCollection(_fp())
    raw = pd.DataFrame({"x": range(1000)})
    verdict = sentinel.check_raw(coll, "loss.json", raw)
    assert verdict["status"] == "learning"

    verdict = sentinel.check_processed(coll, "loss.json", _rows(50))

    assert verdict["status"] == "quarantined"
    assert [f["code"] for f in verdict["findings"] if f["layer"] == "stats"] == ["parse_rate_floor"]


def test_by_design_exclusions_do_not_trip_the_floor(stores):
    sentinel = ss.StructureSentinel()
    coll = FakeCollection(_fp(), {"loss.json": {"raw_rows": 1000, "dropped": {"outside_whitelist": 940}}})
    raw = pd.DataFrame({"x": range(1000)})
    sentinel.check_raw(coll, "loss.json", raw)

    verdict = sentinel.check_processed(coll, "loss.json", _rows(50))

    assert verdict["status"] == "learning"
    assert verdict["processed_stats"]["parse_rate"] == round(50 / 60, 4)
    assert not [f for f in verdict["findings"] if f["code"] == "parse_rate_floor"]


def test_an_approval_of_the_floor_finding_sticks(stores):
    """An operator who has seen the counts can still approve the file past the floor."""
    sentinel = ss.StructureSentinel()
    coll = FakeCollection(_fp())
    raw = pd.DataFrame({"x": range(1000)})
    sentinel.check_raw(coll, "loss.json", raw)
    verdict = sentinel.check_processed(coll, "loss.json", _rows(50))
    assert verdict["status"] == "quarantined"
    prior = dict(verdict, status="approved", review_action="approve",
                 reviewed_at="2026-09-18T00:00:00+00:00")

    again = ss.StructureSentinel()
    again.prior_verdicts = {"loss.json": prior}
    again.check_raw(coll, "loss.json", raw)
    verdict = again.check_processed(coll, "loss.json", _rows(50))

    assert verdict["status"] == "approved"
