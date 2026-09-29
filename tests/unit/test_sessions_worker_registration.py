"""Registration invariants for the sessions_refresh background worker."""

import importlib.util


def test_sessions_refresh_registered_everywhere():
    from web_interface.tasks import process_manager, runtime

    assert "sessions_refresh" in process_manager.CLOUD_TASK_ELIGIBLE
    assert "sessions_refresh" in process_manager.processes
    # Pure recomputation with fixed output filenames — queue retries are safe.
    assert "sessions_refresh" in runtime.QUEUE_RETRY_SAFE

    runtime.ensure_task_functions_loaded()
    assert "sessions_refresh" in runtime.TASK_FUNCTIONS


def test_sessions_refresh_script_constant():
    from web_interface.tasks.worker_registry import worker_module

    assert worker_module("sessions_refresh") == "web_interface.workers.run_sessions_refresh"
    assert importlib.util.find_spec(worker_module("sessions_refresh"))
