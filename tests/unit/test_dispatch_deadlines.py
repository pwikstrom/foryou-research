"""Initial Cloud Task dispatch deadlines for long-running workers.

Cloud Tasks' default dispatch deadline is 600s: a handler that runs longer
never gets to respond, so the task is re-dispatched from scratch up to
max-attempts and the run "starts over and over" even though each attempt
would eventually succeed.

A self-chaining worker's own ``_DISPATCH_DEADLINE`` governs only the links it
dispatches *itself* — the INITIAL dispatch comes from whoever launched it.
sessions_refresh shipped with the worker constant but no start_process entry and
looped on its first prod run (2026-08-09); timelines_refresh and
embeddings_refresh had the same latent gap. This test pins the two sides
together.

``start_process`` is not the only launcher. The consolidate pipeline dispatches
its steps directly — spine advance, recode's fan-out to the leaves, and
"Refresh All Affected" — and every one of those omitted the deadline, so a
worker that got 1800s from its card's Refresh button got 600s from the pipeline.
2026-08-15 prod: timelines_refresh link 0 ran 622s inside the pipeline, Cloud
Tasks re-dispatched it up to max-attempts while the original kept going, and
four concurrent chains wrote one status file (the progress bar jumped
23/94 -> 54/94 -> 90/94). The deadline now comes from one table,
``process_manager.dispatch_deadline_for``, and the tests below cover every
dispatch site rather than just start_process.
"""

import ast
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
                                  "recode_refresh_studies",
                                  "consolidate_enrichment"])
def test_known_long_runners_exceed_the_600s_default(dispatched, name):
    process_manager.start_process(name, None, task_args={})
    assert dispatched and dispatched[0]["deadline"] is not None
    assert dispatched[0]["deadline"] > 600, (
        f"{name} would take Cloud Tasks' 600s default and loop")




def test_consolidate_enrichment_covers_the_shadow_verification():
    """The shadow check is the longest consolidate mode and sets the deadline.

    2026-09-02 prod: the weekly verification ran 772-816 s and every attempt
    answered 200 after Cloud Tasks had already re-delivered at 600 s — five
    attempts, ~66 minutes of an 8-vCPU runner, for one check. A force rebuild
    over the whole corpus sits in the same range.
    """
    got = process_manager.dispatch_deadline_for("consolidate_enrichment", {})
    assert got is not None and got >= 1800, (
        f"consolidate_enrichment gets {got}s; a shadow verification or force "
        "rebuild would be re-dispatched mid-run")




def test_worker_declared_deadlines_are_in_the_shared_table():
    """dispatch_deadline_for is the single source of truth for every launcher."""
    missing = []
    for name, want in _workers_declaring_a_deadline().items():
        probe = "queue_scraper_tiktok" if name == "queue_scraper" else name
        if probe not in process_manager.CLOUD_TASK_ELIGIBLE:
            continue
        got = process_manager.dispatch_deadline_for(probe, {})
        if got is None or got < want:
            missing.append(f"{probe}: worker declares {want}s, table gives {got}")
    assert not missing, "\n  ".join(["deadline table is out of sync:"] + missing)






def _dispatch_call_sites() -> list[str]:
    """Every _dispatch_cloud_task(...) call that omits dispatch_deadline_seconds.

    A missing deadline silently means Cloud Tasks' 600s default, which for a
    long worker means the queue re-dispatches it mid-run — and for a
    self-chaining one, a second concurrent chain.
    """
    root = Path(process_manager.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname != "_dispatch_cloud_task":
                continue
            if not any(kw.arg == "dispatch_deadline_seconds" for kw in node.keywords):
                offenders.append(
                    f"{path.relative_to(root.parent)}:{node.lineno}")
    return offenders






def test_every_dispatch_site_sets_a_deadline_explicitly():
    offenders = _dispatch_call_sites()
    assert not offenders, (
        "these _dispatch_cloud_task calls fall back to Cloud Tasks' 600s "
        "default; pass dispatch_deadline_seconds=dispatch_deadline_for(name, args):\n  "
        + "\n  ".join(offenders))






@pytest.mark.parametrize("name", ["sessions_refresh", "timelines_refresh",
                                  "embeddings_refresh", "pca_refresh",
                                  "recode_refresh_studies"])
def test_pipeline_dispatch_matches_start_process(dispatched, name):
    """A pipeline-launched step gets the same deadline as a button-launched one."""
    process_manager.start_process(name, None, task_args={})
    from_button = dispatched[0]["deadline"]
    from_pipeline = process_manager.dispatch_deadline_for(name, {})
    assert from_pipeline == from_button, (
        f"{name}: card Refresh gives {from_button}s but the pipeline gives "
        f"{from_pipeline}s — the pipeline copy would loop")
