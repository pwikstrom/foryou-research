"""Registration invariants for the sessions_refresh background worker."""


def test_sessions_refresh_registered_everywhere():
    from web_interface import process_manager
    from web_interface.routes import process_routes

    assert "sessions_refresh" in process_manager.CLOUD_TASK_ELIGIBLE
    assert "sessions_refresh" in process_manager.processes
    # Pure recomputation with fixed output filenames — queue retries are safe.
    assert "sessions_refresh" in process_routes.QUEUE_RETRY_SAFE

    process_routes._ensure_task_functions_loaded()
    assert "sessions_refresh" in process_routes.TASK_FUNCTIONS






def test_sessions_refresh_script_constant():
    from fyp.fyp_config import SESSIONS_REFRESH_SCRIPT

    assert SESSIONS_REFRESH_SCRIPT.name == "run_sessions_refresh.py"
    assert SESSIONS_REFRESH_SCRIPT.exists()
