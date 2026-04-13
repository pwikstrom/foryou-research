# -*- coding: utf-8 -*-
"""
Task status reporting for background processes.

Provides two reporter implementations:
- LocalStatusReporter: prints ::PROGRESS:: / ::DATA:: to stdout (subprocess mode)
- GCSStatusReporter: writes status JSON to GCS (Cloud Tasks mode)

Both share the same interface so worker functions are execution-mode agnostic.
"""

import json
import os
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import fyp.data_io as data_io


STATUS_PREFIX = "task_status"
CANCEL_SUFFIX = "_cancel.json"
THROTTLE_INTERVAL = 5  # seconds between GCS writes




class TaskStatusReporter(ABC):
    """Base interface for task status reporting."""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def update_progress(self, percent: int, message: str) -> None:
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

    def update_progress(self, percent: int, message: str) -> None:
        payload = json.dumps({"percent": percent, "message": message})
        print(f"::PROGRESS::{payload}")

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
            self._status["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        self._status["start_time"] = datetime.now(timezone.utc).isoformat()
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

    def update_progress(self, percent: int, message: str) -> None:
        self._status["progress"] = {"percent": percent, "message": message}
        print(f"[{self.name}] {percent}% - {message}")
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
        data={"requested_at": datetime.now(timezone.utc).isoformat()},
        storage_location="cache",
        filename=filename,
        verbose=False,
    )




def is_cloud_run() -> bool:
    """Check if running on Cloud Run."""
    return bool(os.environ.get("K_SERVICE"))




def get_reporter(name: str) -> TaskStatusReporter:
    """Return the appropriate reporter for the current execution environment."""
    if is_cloud_run():
        return GCSStatusReporter(name)
    return LocalStatusReporter(name)
