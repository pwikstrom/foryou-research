"""
Task status reporting for background processes.

Provides two reporter implementations:
- LocalStatusReporter: prints ::PROGRESS:: / ::DATA:: to stdout (subprocess mode)
- GCSStatusReporter: writes status JSON to GCS (Cloud Tasks mode)

Both share the same interface so worker functions are execution-mode agnostic.
"""

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

import fyp.data_io as data_io

STATUS_PREFIX = "task_status"
CANCEL_SUFFIX = "_cancel.json"
THROTTLE_INTERVAL = 5  # seconds between GCS writes




class TaskStatusReporter(ABC):
    """Base interface for task status reporting."""

    # Default stage info injected into every update_progress call when the
    # caller doesn't pass stage_* explicitly. Set by the task runner so
    # pipeline-aware display survives even when worker code was written
    # without pipeline awareness.
    _default_stage_index: int | None = None
    _default_stage_total: int | None = None
    _default_stage_name: str | None = None

    def set_stage(
        self,
        stage_index: int | None,
        stage_total: int | None,
        stage_name: str | None,
    ) -> None:
        """Set pipeline stage defaults applied to subsequent update_progress calls."""
        self._default_stage_index = stage_index
        self._default_stage_total = stage_total
        self._default_stage_name = stage_name

    def _resolve_stage(
        self,
        stage_index: int | None,
        stage_total: int | None,
        stage_name: str | None,
    ) -> tuple[int | None, int | None, str | None]:
        """Merge explicit stage args with reporter defaults."""
        return (
            stage_index if stage_index is not None else self._default_stage_index,
            stage_total if stage_total is not None else self._default_stage_total,
            stage_name if stage_name is not None else self._default_stage_name,
        )

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def update_progress(
        self,
        percent: int,
        message: str,
        stage_index: int | None = None,
        stage_total: int | None = None,
        stage_name: str | None = None,
    ) -> None:
        ...

    @abstractmethod
    def emit_data(self, payload: dict) -> None:
        ...

    @abstractmethod
    def complete(self, data: dict | None = None) -> None:
        ...

    @abstractmethod
    def fail(self, error: str) -> None:
        ...

    @abstractmethod
    def log(self, message: str) -> None:
        ...

    @abstractmethod
    def check_cancelled(self) -> bool:
        ...




class LocalStatusReporter(TaskStatusReporter):
    """Prints progress/data directives to stdout for subprocess-mode parsing."""

    def __init__(self, name: str) -> None:
        self.name = name

    def start(self) -> None:
        print(f"[{self.name}] Starting...")

    def update_progress(
        self,
        percent: int,
        message: str,
        stage_index: int | None = None,
        stage_total: int | None = None,
        stage_name: str | None = None,
    ) -> None:
        stage_index, stage_total, stage_name = self._resolve_stage(
            stage_index, stage_total, stage_name
        )
        payload_dict = {"percent": percent, "message": message}
        if stage_index is not None:
            payload_dict["stage_index"] = stage_index
        if stage_total is not None:
            payload_dict["stage_total"] = stage_total
        if stage_name is not None:
            payload_dict["stage_name"] = stage_name
        print(f"::PROGRESS::{json.dumps(payload_dict)}")

    def emit_data(self, payload: dict) -> None:
        print(f"::DATA::{json.dumps(payload)}")

    def complete(self, data: dict | None = None) -> None:
        if data:
            self.emit_data(data)
        self.update_progress(100, "Completed")

    def fail(self, error: str) -> None:
        print(f"Process failed: {error}")

    def log(self, message: str) -> None:
        print(message)

    def check_cancelled(self) -> bool:
        from web_interface.process_manager import check_graceful_stop
        return check_graceful_stop(self.name)




# In-memory store for locally-run study_refresh tasks (one thread per study key).
# Mirrors the structure of GCSStatusReporter's payload so the same client-side
# poller (`/api/status/study_refresh/<name>`) can consume either source.
_local_thread_status: dict[str, dict] = {}
_local_thread_status_lock = threading.Lock()




class LocalThreadStatusReporter(TaskStatusReporter):
    """Writes status into an in-process dict so the HTTP poller can read it.

    Used when save_study runs `run_study_refresh` on a background thread in
    local dev, emulating the Cloud Tasks dispatch-and-poll flow.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        with _local_thread_status_lock:
            _local_thread_status[key] = {
                "state": "running",
                "start_time": datetime.now(UTC).isoformat(),
                "progress": {"percent": 0, "message": "Starting..."},
                "data": {},
                "error": None,
                "updated_at": datetime.now(UTC).isoformat(),
            }

    def _update(self, patch: dict) -> None:
        with _local_thread_status_lock:
            current = _local_thread_status.setdefault(self.key, {})
            current.update(patch)
            current["updated_at"] = datetime.now(UTC).isoformat()

    def start(self) -> None:
        self._update({"state": "running"})
        print(f"[{self.key}] Starting...")

    def update_progress(
        self,
        percent: int,
        message: str,
        stage_index: int | None = None,
        stage_total: int | None = None,
        stage_name: str | None = None,
    ) -> None:
        stage_index, stage_total, stage_name = self._resolve_stage(
            stage_index, stage_total, stage_name
        )
        payload_dict = {"percent": percent, "message": message}
        if stage_index is not None:
            payload_dict["stage_index"] = stage_index
        if stage_total is not None:
            payload_dict["stage_total"] = stage_total
        if stage_name is not None:
            payload_dict["stage_name"] = stage_name
        self._update({"progress": payload_dict})
        print(f"::PROGRESS::{json.dumps(payload_dict)}")

    def emit_data(self, payload: dict) -> None:
        with _local_thread_status_lock:
            current = _local_thread_status.setdefault(self.key, {})
            current_data = current.get("data", {}) or {}
            current_data.update(payload)
            current["data"] = current_data
            current["updated_at"] = datetime.now(UTC).isoformat()
        print(f"::DATA::{json.dumps(payload)}")

    def complete(self, data: dict | None = None) -> None:
        if data:
            self.emit_data(data)
        self._update({
            "state": "succeeded",
            "progress": {"percent": 100, "message": "Completed"},
        })

    def fail(self, error: str) -> None:
        self._update({"state": "failed", "error": error})
        print(f"Process failed: {error}")

    def log(self, message: str) -> None:
        print(message)

    def check_cancelled(self) -> bool:
        with _local_thread_status_lock:
            return bool(_local_thread_status.get(self.key, {}).get("cancelled"))




def read_local_thread_status(key: str) -> dict | None:
    """Return a shallow copy of the in-process status for `key`, or None."""
    with _local_thread_status_lock:
        status = _local_thread_status.get(key)
        return dict(status) if status else None




class GCSStatusReporter(TaskStatusReporter):
    """Writes status JSON to GCS for Cloud Tasks mode."""

    HEARTBEAT_INTERVAL = 30  # seconds between background heartbeat writes

    def __init__(self, name: str) -> None:
        self.name = name
        self._status: dict = {
            "state": "pending",
            "start_time": None,
            "progress": {},
            "data": {},
            "error": None,
            "logs": [],
            "updated_at": None,
        }
        self._lock = threading.Lock()
        self._last_write: float = 0.0
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def _filename(self) -> str:
        return f"{STATUS_PREFIX}/{self.name}.json"

    def _cancel_filename(self) -> str:
        return f"{STATUS_PREFIX}/{self.name}{CANCEL_SUFFIX}"

    def _write_status(self, force: bool = False) -> None:
        """Write status to GCS, throttled unless force=True."""
        now = time.monotonic()
        if not force and (now - self._last_write) < THROTTLE_INTERVAL:
            return
        with self._lock:
            self._status["updated_at"] = datetime.now(UTC).isoformat()
            try:
                data_io.save_json(
                    data=self._status,
                    storage_location="cache",
                    filename=self._filename(),
                    verbose=False,
                )
                self._last_write = time.monotonic()
            except Exception as e:
                print(f"[GCSStatusReporter] Failed to write status: {e}")

    def _heartbeat_loop(self) -> None:
        """Background thread that periodically writes status to GCS."""
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
            self._write_status(force=True)

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

    def start(self) -> None:
        self._status["state"] = "running"
        self._status["start_time"] = datetime.now(UTC).isoformat()
        self._status["progress"] = {}
        self._status["data"] = {}
        self._status["error"] = None
        self._status["logs"] = []
        self._write_status(force=True)
        self._start_heartbeat()

    def resume(self) -> None:
        """Resume an existing task (chain continuation).

        Loads the current GCS status so progress and data from the previous
        chain link are preserved, then starts the heartbeat.
        """
        existing = read_task_status(self.name)
        if existing:
            self._status = existing
        self._status["state"] = "running"
        self._status["error"] = None
        self._write_status(force=True)
        self._start_heartbeat()

    def update_progress(
        self,
        percent: int,
        message: str,
        stage_index: int | None = None,
        stage_total: int | None = None,
        stage_name: str | None = None,
    ) -> None:
        stage_index, stage_total, stage_name = self._resolve_stage(
            stage_index, stage_total, stage_name
        )
        payload_dict = {"percent": percent, "message": message}
        if stage_index is not None:
            payload_dict["stage_index"] = stage_index
        if stage_total is not None:
            payload_dict["stage_total"] = stage_total
        if stage_name is not None:
            payload_dict["stage_name"] = stage_name
        self._status["progress"] = payload_dict
        stage_prefix = ""
        if stage_index is not None and stage_total is not None:
            stage_prefix = f"[Stage {stage_index}/{stage_total}] "
        print(f"[{self.name}] {stage_prefix}{percent}% - {message}")
        self._write_status()

    def emit_data(self, payload: dict) -> None:
        self._status["data"].update(payload)
        self._write_status()

    def complete(self, data: dict | None = None) -> None:
        self._stop_heartbeat()
        if data:
            self._status["data"].update(data)
        self._status["state"] = "completed"
        self._status["progress"] = {"percent": 100, "message": "Completed"}
        self._write_status(force=True)
        self._clear_cancel()

    def fail(self, error: str) -> None:
        self._stop_heartbeat()
        self._status["state"] = "failed"
        self._status["error"] = error
        print(f"[{self.name}] FAILED: {error}")
        self._write_status(force=True)
        self._clear_cancel()

    def log(self, message: str) -> None:
        print(f"[{self.name}] {message}")
        self._status["logs"].append(message)
        # Trim to last 200 log lines
        if len(self._status["logs"]) > 200:
            self._status["logs"] = self._status["logs"][-200:]
        # Heartbeat: write to GCS periodically so stale detection works
        self._write_status()

    def check_cancelled(self) -> bool:
        try:
            return data_io.exists(
                storage_location="cache", filename=self._cancel_filename()
            )
        except Exception:
            return False

    def _clear_cancel(self) -> None:
        try:
            if data_io.exists(storage_location="cache", filename=self._cancel_filename()):
                data_io.remove(storage_location="cache", filename=self._cancel_filename())
        except Exception:
            pass




def read_task_status(name: str) -> dict | None:
    """Read a task's GCS status file. Returns None if not found."""
    filename = f"{STATUS_PREFIX}/{name}.json"
    try:
        if data_io.exists(storage_location="cache", filename=filename):
            return data_io.load_json(storage_location="cache", filename=filename)
    except Exception as e:
        print(f"[task_status] Failed to read status for {name}: {e}")
    return None




def write_cancel_request(name: str) -> None:
    """Write a cancellation sentinel to GCS."""
    filename = f"{STATUS_PREFIX}/{name}{CANCEL_SUFFIX}"
    data_io.save_json(
        data={"requested_at": datetime.now(UTC).isoformat()},
        storage_location="cache",
        filename=filename,
        verbose=False,
    )




def force_clear_status(name: str, reason: str = "cancelled") -> None:
    """Overwrite a task's GCS status file with a terminal state.

    Used when stop_process is called against a status record whose heartbeat
    has clearly stopped — the task-runner pod is gone but the file still says
    "running". Writing a terminal state lets the next start_process succeed
    immediately instead of waiting for the 10-minute staleness window.
    """
    existing = read_task_status(name) or {}
    existing["state"] = reason
    existing["updated_at"] = datetime.now(UTC).isoformat()
    if not existing.get("error"):
        existing["error"] = "Cleared by stop_process (status was stuck)."
    try:
        data_io.save_json(
            data=existing,
            storage_location="cache",
            filename=f"{STATUS_PREFIX}/{name}.json",
            verbose=False,
        )
    except Exception as e:
        print(f"[task_status] Failed to force-clear status for {name}: {e}")




def stamp_task_status(
    name: str,
    state: str,
    message: str = "",
    error: str | None = None,
    stage: dict | None = None,
) -> None:
    """Overwrite a task's GCS status file with a given state and message.

    Used by the consolidate fan-out to give each forked leaf a definitive
    per-run status: ``"queued"`` the instant it is dispatched (so the card shows
    "Queued — waiting for a worker" instead of a stale status from a previous
    run), and ``"failed"`` if the leaf could not be initiated — e.g. a Cloud Run
    429 dropped the task with no retry. Without this, a dropped leaf's card would
    keep showing its previous-run status and look like it was still waiting to
    begin.

    Args:
        name: Task name (status key).
        state: New state, e.g. ``"queued"`` or ``"failed"``.
        message: Short progress message shown on the card.
        error: Optional error detail (shown on failure).
        stage: Optional stage framing keys to merge into ``progress``.
    """
    status = {
        "state": state,
        "start_time": None,
        "progress": {"percent": 0, "message": message},
        "data": {},
        "error": error,
        "logs": [],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if stage:
        status["progress"].update(stage)
    try:
        data_io.save_json(
            data=status,
            storage_location="cache",
            filename=f"{STATUS_PREFIX}/{name}.json",
            verbose=False,
        )
    except Exception as e:
        print(f"[task_status] Failed to stamp status for {name} ({state}): {e}")




def is_cloud_run() -> bool:
    """Check if running on Cloud Run."""
    return bool(os.environ.get("K_SERVICE"))




def get_reporter(name: str) -> TaskStatusReporter:
    """Return the appropriate reporter for the current execution environment."""
    if is_cloud_run():
        return GCSStatusReporter(name)
    return LocalStatusReporter(name)
