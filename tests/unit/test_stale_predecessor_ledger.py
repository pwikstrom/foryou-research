"""SIGKILL dead-letter gap: a stale 'running' corpse is ledgered on restart."""

from datetime import UTC, datetime, timedelta

import pytest

from web_interface.routes import process_routes






@pytest.fixture
def recorded(monkeypatch):
    calls: list[dict] = []

    def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(process_routes.task_failures, "record_failure", fake_record)
    return calls






def _status(state: str, age_seconds: float | None):
    st = {"state": state, "message": "Processing biggest yet (6/7)"}
    if age_seconds is not None:
        st["updated_at"] = (datetime.now(UTC)
                            - timedelta(seconds=age_seconds)).isoformat()
    return st






def test_stale_running_corpse_is_ledgered(monkeypatch, recorded):
    monkeypatch.setattr(process_routes, "read_task_status",
                        lambda key: _status("running", 3600))
    process_routes._ledger_stale_predecessor("pca_refresh", "pca_refresh")
    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["task"] == "pca_refresh"
    assert entry["phase"] == "presumed_oom"
    assert entry["disposition"] == process_routes.task_failures.DISPOSITION_DEAD
    assert "SIGKILL" in entry["error"] and "biggest yet" in entry["error"]






@pytest.mark.parametrize("state,age", [
    ("running", 30),        # fresh heartbeat: genuinely running
    ("completed", 3600),    # finished cleanly
    ("failed", 3600),       # already recorded by the failure wrapper
])
def test_non_corpse_states_not_ledgered(monkeypatch, recorded, state, age):
    monkeypatch.setattr(process_routes, "read_task_status",
                        lambda key: _status(state, age))
    process_routes._ledger_stale_predecessor("pca_refresh", "pca_refresh")
    assert recorded == []






def test_missing_or_malformed_status_is_safe(monkeypatch, recorded):
    monkeypatch.setattr(process_routes, "read_task_status", lambda key: None)
    process_routes._ledger_stale_predecessor("x", "x")

    monkeypatch.setattr(process_routes, "read_task_status",
                        lambda key: {"state": "running", "updated_at": "garbage"})
    process_routes._ledger_stale_predecessor("x", "x")

    def boom(key):
        raise OSError("gcs down")

    monkeypatch.setattr(process_routes, "read_task_status", boom)
    process_routes._ledger_stale_predecessor("x", "x")  # must not raise
    assert recorded == []
