"""Initial Cloud Task dispatch deadlines for long-running workers.

Cloud Tasks' default dispatch deadline is 600s: a handler that runs longer
never gets to respond, so the task is re-dispatched from scratch up to
max-attempts and the run "starts over and over" even though each attempt
would eventually succeed.

A self-chaining worker's own ``_DISPATCH_DEADLINE`` governs only the links it
dispatches *itself* — the INITIAL dispatch comes from
``process_manager.start_process``. sessions_refresh shipped with the worker
constant but no start_process entry and looped on its first prod run
(2026-08-09); timelines_refresh and embeddings_refresh had the same latent
gap. This test pins the two sides together.
"""

import re
from pathlib import Path

import pytest

from web_interface import process_manager

WORKER_DIR = Path(process_manager.__file__).parent
_DEADLINE_RE = re.compile(r"^_DISPATCH_DEADLINE\s*=\s*(\d+)", re.MULTILINE)






def _workers_declaring_a_deadline() -> dict[str, int]:
    """Map process name -> the _DISPATCH_DEADLINE its worker module declares."""
    found: dict[str, int] = {}
    for path in sorted(WORKER_DIR.glob("run_*.py")):
        match = _DEADLINE_RE.search(path.read_text())
        if match:
            found[path.stem[len("run_"):]] = int(match.group(1))
    return found






@pytest.fixture
def dispatched(monkeypatch):
    """Capture the deadline start_process passes to _dispatch_cloud_task."""
    calls: list[dict] = []

    def fake_dispatch(name, task_args, dispatch_deadline_seconds=None, **kw):
        calls.append({"name": name, "deadline": dispatch_deadline_seconds})
        return True, "Task dispatched"

    monkeypatch.setattr(process_manager, "is_cloud_run", lambda: True)
    monkeypatch.setattr(process_manager, "_dispatch_cloud_task", fake_dispatch)
    monkeypatch.setattr(process_manager, "read_task_status", lambda key: None)
    monkeypatch.setattr(process_manager.run_logs, "open_run", lambda *a, **k: None)
    monkeypatch.setattr(process_manager.run_logs, "new_run_id", lambda: "runid")
    return calls






def test_every_worker_deadline_is_honoured_on_initial_dispatch(dispatched):
    """A worker that declares _DISPATCH_DEADLINE must get >= it when started."""
    declared = _workers_declaring_a_deadline()
    assert declared, "no workers declare _DISPATCH_DEADLINE — regex broken?"

    missing = []
    for name, want in declared.items():
        # queue_scraper is a template; the real names are per-platform.
        probe = "queue_scraper_tiktok" if name == "queue_scraper" else name
        if probe not in process_manager.CLOUD_TASK_ELIGIBLE:
            continue
        dispatched.clear()
        process_manager.start_process(probe, None, task_args={})
        assert dispatched, f"{probe}: start_process did not dispatch"
        got = dispatched[0]["deadline"]
        if got is None or got < want:
            missing.append(f"{probe}: worker declares {want}s, "
                           f"start_process passes {got}")
    assert not missing, (
        "these workers would be re-dispatched from scratch every 600s:\n  "
        + "\n  ".join(missing))






@pytest.mark.parametrize("name", ["sessions_refresh", "timelines_refresh",
                                  "embeddings_refresh", "pca_refresh",
                                  "recode_refresh_studies"])
def test_known_long_runners_exceed_the_600s_default(dispatched, name):
    process_manager.start_process(name, None, task_args={})
    assert dispatched and dispatched[0]["deadline"] is not None
    assert dispatched[0]["deadline"] > 600, (
        f"{name} would take Cloud Tasks' 600s default and loop")
