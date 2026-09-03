"""The refresh-run step view carries wall-clock bounds for the timeline chart.

The Dataset Assembly page draws the last (or running) refresh run as a timeline:
one bar per step, placed by when it started and sized by how long it ran —
time, not progress. That needs, per step, ``started_at``/``ended_at`` for a
finished step, ``started_at`` for a running one and ``queued_at`` for a leaf
stamped queued at the fork. A finished step's start is its recorded end minus
its recorded duration; for a self-chaining step (timelines, sessions) that
duration spans the whole chain, so the bar does too.

Usage:
    python -m pytest tests/unit/test_pipeline_step_view_timing.py
"""

from datetime import datetime, timedelta

import pytest

from web_interface.services import refresh_pipeline as rp
from web_interface.services import worker_status as ws

T0 = "2026-09-03T00:23:20+00:00"          # run seeded / consolidation started
STEPS = ["recode_refresh_studies", "meta_refresh_groups", "pca_refresh",
         "timelines_refresh", "sessions_refresh"]


def _iso(base: str, plus_s: float) -> str:
    return (datetime.fromisoformat(base) + timedelta(seconds=plus_s)).isoformat()


def _run(dispatched=STEPS):
    """A consolidate-origin run whose named steps were dispatched."""
    record = rp.plan_run("consolidate_enrichment", kind="consolidate")
    record["started_ts"] = T0
    for step in dispatched:
        record["steps"][step] = {"state": "dispatched"}
    return record


@pytest.fixture
def stores(monkeypatch):
    state = {"process_stats": {}, "processes": {}, "task_status": {}}
    monkeypatch.setattr(ws, "process_stats", state["process_stats"])
    monkeypatch.setattr(ws, "processes", state["processes"])
    monkeypatch.setattr(ws, "read_task_status", lambda step: state["task_status"].get(step))
    monkeypatch.setattr(ws, "is_cloud_run", lambda: True)
    monkeypatch.setattr(rp, "load_run",
                        lambda reload=True: state["process_stats"].get(rp.RUN_KEY))
    state["process_stats"][rp.RUN_KEY] = _run()
    return state


def _by_step(view):
    return {row["step"]: row for row in view}


def test_finished_steps_get_start_end_and_duration(stores):
    stores["process_stats"]["consolidate_enrichment"] = {
        "last_run_end_time": _iso(T0, 42), "last_run_duration": 42.0,
        "last_run_outcome": "Success",
    }
    stores["process_stats"]["recode_refresh_studies"] = {
        "last_run_end_time": _iso(T0, 42 + 145), "last_run_duration": 145.0,
        "last_run_outcome": "Success",
    }
    # A self-chaining leaf: the recorded duration spans its whole chain.
    stores["process_stats"]["timelines_refresh"] = {
        "last_run_end_time": _iso(T0, 42 + 145 + 120), "last_run_duration": 120.0,
        "last_run_outcome": "Failed",
    }

    rows = _by_step(ws._build_pipeline_step_view(pipeline_active=False))

    c = rows["consolidate_enrichment"]
    assert c["state"] == "success"
    assert c["started_at"] == T0 and c["ended_at"] == _iso(T0, 42) and c["duration_s"] == 42.0
    r = rows["recode_refresh_studies"]
    assert r["started_at"] == _iso(T0, 42) and r["ended_at"] == _iso(T0, 187)
    t = rows["timelines_refresh"]
    assert t["state"] == "failed"
    assert t["started_at"] == _iso(T0, 187) and t["duration_s"] == 120.0
    # Dispatched but never reported: no bounds at all.
    for step in ("meta_refresh_groups", "pca_refresh", "sessions_refresh"):
        row = rows[step]
        assert row["state"] == "skipped"
        assert row["started_at"] is None and row["ended_at"] is None and row["queued_at"] is None


def test_running_step_carries_its_live_start_and_queued_leaf_its_stamp(stores):
    stores["process_stats"]["consolidate_enrichment"] = {
        "last_run_end_time": _iso(T0, 42), "last_run_duration": 42.0,
        "last_run_outcome": "Success",
    }
    stores["task_status"]["recode_refresh_studies"] = {
        "state": "running", "start_time": _iso(T0, 43), "updated_at": _iso(T0, 90),
        "progress": {"percent": 40, "message": "Study 2/5"},
    }
    stores["task_status"]["timelines_refresh"] = {
        "state": "queued", "start_time": None, "updated_at": _iso(T0, 88),
        "progress": {"percent": 0, "message": "Queued — waiting for a worker…"},
    }

    rows = _by_step(ws._build_pipeline_step_view(pipeline_active=True))

    r = rows["recode_refresh_studies"]
    assert r["state"] == "running" and r["started_at"] == _iso(T0, 43)
    assert r["ended_at"] is None and r["duration_s"] is None
    assert r["percent"] == 40 and r["message"] == "Study 2/5"
    q = rows["timelines_refresh"]
    assert q["state"] == "queued" and q["queued_at"] == _iso(T0, 88) and q["started_at"] is None
    assert rows["pca_refresh"]["state"] == "pending"


def test_stale_status_from_an_earlier_run_is_not_used_for_timing(stores):
    """A running status older than the run belongs to a previous one."""
    stores["process_stats"]["consolidate_enrichment"] = {}
    stores["task_status"]["pca_refresh"] = {
        "state": "running", "start_time": _iso(T0, -900), "updated_at": _iso(T0, -600),
        "progress": {},
    }
    rows = _by_step(ws._build_pipeline_step_view(pipeline_active=True))
    assert rows["pca_refresh"]["state"] == "pending"
    assert rows["pca_refresh"]["started_at"] is None


def test_a_bad_duration_does_not_break_the_view(stores):
    stores["process_stats"]["consolidate_enrichment"] = {
        "last_run_end_time": _iso(T0, 42), "last_run_duration": "not-a-number",
        "last_run_outcome": "Success",
    }
    c = _by_step(ws._build_pipeline_step_view(pipeline_active=False))["consolidate_enrichment"]
    assert c["state"] == "success" and c["ended_at"] == _iso(T0, 42)
    assert c["started_at"] is None and c["duration_s"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
