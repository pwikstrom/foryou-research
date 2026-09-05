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






def test_run_log_is_opened_before_the_task_is_created(monkeypatch):
    """A hot task runner picks a task up within ~200 ms of its creation. When
    the dispatcher opened the run log AFTER creating the task, the worker's
    attach_run found nothing to adopt and opened a duplicate record under the
    same id, which the dispatcher's record then marked "interrupted" — every
    third worker run showed twice on 2026-09-05. Open first; abort the record
    if the dispatch then fails."""
    import web_interface.task_status as ts

    order: list[tuple] = []
    monkeypatch.setattr(process_manager, "is_cloud_run", lambda: True)
    monkeypatch.setattr(process_manager, "read_task_status", lambda key: None)
    monkeypatch.setattr(process_manager.run_logs, "new_run_id", lambda: "runid")
    monkeypatch.setattr(process_manager.run_logs, "open_run",
                        lambda *a, **k: order.append(("open", k.get("run_id"))) or "runid")
    monkeypatch.setattr(process_manager.run_logs, "abort_run",
                        lambda key, reason: order.append(("abort", reason)))
    monkeypatch.setattr(process_manager, "_journal_worker_started", lambda *a, **k: None)
    monkeypatch.setattr(ts.GCSStatusReporter, "_write_status", lambda self, force=False: None)

    def dispatch_ok(name, task_args, **kw):
        order.append(("dispatch", task_args.get("log_run_id")))
        return True, "Task dispatched"

    monkeypatch.setattr(process_manager, "_dispatch_cloud_task", dispatch_ok)
    ok, _ = process_manager.start_process("queue_annotator_batch", None, task_args={})
    assert ok and order == [("open", "runid"), ("dispatch", "runid")]

    order.clear()

    def dispatch_fails(name, task_args, **kw):
        order.append(("dispatch", task_args.get("log_run_id")))
        return False, "queue refused"

    monkeypatch.setattr(process_manager, "_dispatch_cloud_task", dispatch_fails)
    ok, _ = process_manager.start_process("queue_annotator_batch", None, task_args={})
    assert not ok
    assert order == [("open", "runid"), ("dispatch", "runid"), ("abort", "queue refused")]


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
                                  "video_map_refresh", "meta_refresh_groups",
                                  "consolidate_enrichment"])
def test_known_long_runners_exceed_the_600s_default(dispatched, name):
    process_manager.start_process(name, None, task_args={})
    assert dispatched and dispatched[0]["deadline"] is not None
    assert dispatched[0]["deadline"] > 600, (
        f"{name} would take Cloud Tasks' 600s default and loop")




def test_every_refresh_step_has_an_explicit_deadline():
    """No pipeline step may fall through to Cloud Tasks' 600s default.

    A hand-maintained list of "known long runners" is exactly how two steps
    were missed: video_map_refresh and meta_refresh_groups were the only
    members of the refresh graph without an entry, and nobody noticed until a
    map task was dispatched at 04:32 and delivered at 04:55 (2026-09-04). The
    graph is the source of truth, so assert against the graph — a step added
    to the registry now fails here until it declares a deadline.
    """
    from web_interface.services.refresh_pipeline import STEP_ORDER

    missing = [n for n in STEP_ORDER
               if process_manager.dispatch_deadline_for(n, {}) is None]
    assert not missing, (
        f"these refresh steps would take the 600s default: {missing}")


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




# Cloud Tasks' own limit for HTTP targets. Not ours to raise.
_CLOUD_TASKS_MAX = 1800


def _representative_args(name: str) -> list[dict]:
    """Task args that exercise every branch of dispatch_deadline_for."""
    if name == "queue_annotator":
        return [{}, {"batch_size": 50}, {"batch_size": 1000}, {"batch_size": 5000}]
    return [{}]


def test_no_deadline_exceeds_the_cloud_tasks_maximum():
    """A deadline over 30 min is not 'generous', it is a 400 at task creation.

    2026-09-03 prod: consolidate_enrichment was given 3600 s. Cloud Tasks
    rejected every dispatch — the armed post-scrape trigger and the admin's
    Consolidate button alike — with "Task.dispatchDeadline must be between
    [15s, 30m]", so no consolidation could start until a redeploy. The
    queue_annotator >1000-item branch had carried the same 3600 s since it was
    written and could never have dispatched either.
    """
    too_long = []
    for name in sorted(process_manager.CLOUD_TASK_ELIGIBLE):
        for args in _representative_args(name):
            got = process_manager.dispatch_deadline_for(name, args)
            if got is not None and got > _CLOUD_TASKS_MAX:
                too_long.append(f"{name} {args}: {got}s")
    assert not too_long, (
        "Cloud Tasks would reject these dispatches outright:\n  "
        + "\n  ".join(too_long))
    assert process_manager.CLOUD_TASKS_MAX_DISPATCH_DEADLINE == _CLOUD_TASKS_MAX


def test_dispatch_site_clamps_an_overlong_deadline(monkeypatch):
    """The last line of defence: an illegal value is clamped, never sent."""
    captured: dict = {}

    class _Task:
        """Stands in for tasks_v2.Task; the real duration_pb2 sets the deadline."""
        http_request = None
        dispatch_deadline = None
        schedule_time = None

    class _Client:
        def queue_path(self, *a):
            return "q"
        def create_task(self, request=None, **kw):
            captured["task"] = request["task"] if request else kw["task"]
            class _Resp:
                name = "projects/x/tasks/t"
            return _Resp()

    import sys
    import types

    # Stub only the Cloud Tasks client; google.protobuf is already imported by
    # other deps, so the genuine Duration is what the task ends up holding.
    tasks_v2 = types.SimpleNamespace(
        CloudTasksClient=lambda: _Client(),
        Task=lambda **kw: _Task(),
        HttpRequest=lambda **kw: None,
        HttpMethod=types.SimpleNamespace(POST=1),
        OidcToken=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.tasks_v2", tasks_v2)
    monkeypatch.setattr(process_manager, "is_cloud_run", lambda: True)
    for env in ("GCP_PROJECT_ID", "CLOUD_TASKS_LOCATION", "CLOUD_TASKS_QUEUE",
                "K_SERVICE", "CLOUD_RUN_SERVICE_URL"):
        monkeypatch.setenv(env, "x")

    ok, _msg = process_manager._dispatch_cloud_task(
        "consolidate_enrichment", {}, dispatch_deadline_seconds=3600)
    assert ok, _msg
    sent = captured["task"].dispatch_deadline
    assert sent is not None and sent.seconds == _CLOUD_TASKS_MAX, (
        f"3600s reached Cloud Tasks as {getattr(sent, 'seconds', None)}; "
        f"must clamp to {_CLOUD_TASKS_MAX}")




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
