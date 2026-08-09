"""SIGKILL dead-letter gap: a stale 'running' corpse is ledgered on restart.

An OOM SIGKILL bypasses the failure wrapper entirely — no traceback, no
task_failures entry, and the status file stays at state='running' with a
frozen heartbeat. pca_refresh died this way three times (2026-08-08/09)
without leaving a single ledger record.

The check MUST live on the dispatch side (process_manager.start_process),
because start_process writes a fresh 'running' placeholder immediately after
dispatching the Cloud Task — so by the time the task runner boots, the corpse
has already been overwritten and is unobservable from there. The
process_routes-side check is a secondary net for paths that write no
placeholder (pipeline forks, direct Cloud Task dispatch).
"""

from datetime import UTC, datetime, timedelta

import pytest

from web_interface import process_manager
from web_interface.routes import process_routes






@pytest.fixture
def recorded(monkeypatch):
    calls: list[dict] = []

    def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(process_routes.task_failures, "record_failure", fake_record)
    monkeypatch.setattr(process_manager.task_failures, "record_failure", fake_record)
    return calls






def _status(state: str, age_seconds: float | None, message="Processing biggest yet (7/8)..."):
    st = {"state": state, "progress": {"message": message}}
    if age_seconds is not None:
        st["updated_at"] = (datetime.now(UTC)
                            - timedelta(seconds=age_seconds)).isoformat()
    return st






# --------------------------------------------------------------------------
# The dispatch side — this is the path a UI "Start" click actually takes.
# --------------------------------------------------------------------------


@pytest.fixture
def cloud_dispatch(monkeypatch):
    """Force start_process down the Cloud Tasks branch with a stubbed dispatch."""
    dispatched: list[tuple] = []
    monkeypatch.setattr(process_manager, "is_cloud_run", lambda: True)
    monkeypatch.setattr(process_manager, "_dispatch_cloud_task",
                        lambda *a, **k: dispatched.append((a, k)) or (True, "Task dispatched"))
    monkeypatch.setattr(process_manager.run_logs, "open_run", lambda *a, **k: None)
    monkeypatch.setattr(process_manager.run_logs, "new_run_id", lambda: "runid")
    monkeypatch.setattr(process_manager, "write_task_status", lambda *a, **k: None,
                        raising=False)
    return dispatched






def test_stale_corpse_is_ledgered_when_starting_a_new_run(monkeypatch, recorded,
                                                          cloud_dispatch):
    """The regression: a UI Start over a dead run must dead-letter the corpse."""
    monkeypatch.setattr(process_manager, "read_task_status",
                        lambda key: _status("running", 3600))
    ok, _msg = process_manager.start_process("pca_refresh", None, task_args={})

    assert ok is True  # the stale run must not block the new one
    assert len(recorded) == 1, "stale corpse was not dead-lettered on dispatch"
    entry = recorded[0]
    assert entry["task"] == "pca_refresh"
    assert entry["phase"] == "presumed_oom"
    assert entry["disposition"] == process_manager.task_failures.DISPOSITION_DEAD
    assert "SIGKILL" in entry["error"] and "biggest yet" in entry["error"]






def test_fresh_run_blocks_and_is_not_ledgered(monkeypatch, recorded, cloud_dispatch):
    monkeypatch.setattr(process_manager, "read_task_status",
                        lambda key: _status("running", 30))
    ok, msg = process_manager.start_process("pca_refresh", None, task_args={})
    assert ok is False and "already running" in msg
    assert recorded == []






@pytest.mark.parametrize("state", ["completed", "failed"])
def test_terminal_states_are_not_ledgered(monkeypatch, recorded, cloud_dispatch, state):
    monkeypatch.setattr(process_manager, "read_task_status",
                        lambda key: _status(state, 3600))
    process_manager.start_process("pca_refresh", None, task_args={})
    assert recorded == []






def test_ledger_failure_never_blocks_the_new_run(monkeypatch, cloud_dispatch):
    monkeypatch.setattr(process_manager, "read_task_status",
                        lambda key: _status("running", 3600))

    def boom(**kwargs):
        raise OSError("gcs down")

    monkeypatch.setattr(process_manager.task_failures, "record_failure", boom)
    ok, _ = process_manager.start_process("pca_refresh", None, task_args={})
    assert ok is True






# --------------------------------------------------------------------------
# The task-runner side — secondary net for placeholder-less dispatch paths.
# --------------------------------------------------------------------------


def test_runner_side_ledgers_a_stale_corpse(monkeypatch, recorded):
    monkeypatch.setattr(process_routes, "read_task_status",
                        lambda key: _status("running", 3600))
    process_routes._ledger_stale_predecessor("pca_refresh", "pca_refresh")
    assert len(recorded) == 1
    assert recorded[0]["phase"] == "presumed_oom"






@pytest.mark.parametrize("state,age", [
    ("running", 30),        # fresh heartbeat: genuinely running
    ("completed", 3600),    # finished cleanly
    ("failed", 3600),       # already recorded by the failure wrapper
])
def test_runner_side_ignores_non_corpses(monkeypatch, recorded, state, age):
    monkeypatch.setattr(process_routes, "read_task_status",
                        lambda key: _status(state, age))
    process_routes._ledger_stale_predecessor("pca_refresh", "pca_refresh")
    assert recorded == []






def test_runner_side_is_safe_on_missing_or_malformed_status(monkeypatch, recorded):
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
