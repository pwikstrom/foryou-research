"""Registration invariants for the sessions_refresh background worker."""


def test_sessions_refresh_registered_everywhere():
    from web_interface.tasks import process_manager, runtime

    assert "sessions_refresh" in process_manager.CLOUD_TASK_ELIGIBLE
    assert "sessions_refresh" in process_manager.processes
    # Pure recomputation with fixed output filenames — queue retries are safe.
    assert "sessions_refresh" in runtime.QUEUE_RETRY_SAFE

    runtime.ensure_task_functions_loaded()
    assert "sessions_refresh" in runtime.TASK_FUNCTIONS


def test_sessions_refresh_script_constant():
    from web_interface.tasks.worker_registry import worker_script

    assert worker_script("sessions_refresh").name == "run_sessions_refresh.py"
    assert worker_script("sessions_refresh").exists()
