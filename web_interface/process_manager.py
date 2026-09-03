import json
import os
import subprocess
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import fyp
import fyp.data_io as data_io
from fyp.fyp_config import PROJECT_ROOT, PYTHON_EXEC, active_config_path
from web_interface import run_logs, task_failures
from web_interface.task_status import (
    force_clear_status,
    is_cloud_run,
    read_task_status,
    write_cancel_request,
)


# A status record whose `updated_at` is older than this is treated as stuck
# in stop_process — the heartbeat (30 s interval) is clearly not running, so
# we overwrite the file with a terminal state instead of just dropping a
# cancel sentinel that no worker will ever consume.
STUCK_STATUS_THRESHOLD_S = 90

GRACEFUL_STOP_DIR = PROJECT_ROOT / "tmp" / "graceful_stop"


def worker_env() -> dict[str, str]:
    """Build the environment for a spawned ``run_*.py`` worker subprocess.

    A worker inherits none of this process's resolved identity by default: it
    rediscovers its own project root by walking up from the working directory
    for ``__proj__.py``, and its ``import fyp`` is served by whichever finder
    answers first — under an editable venv install that is the checkout pip was
    pointed at, not necessarily the one this server runs from. Either route can
    land the child on a different ``config.toml`` (and its gitignored
    ``config.local.toml`` overlay), which means a different data store. On
    2026-08-28 that is exactly what happened: workers spawned during a local
    end-to-end test read and pruned the production scrape queue while the
    server itself was on the local store.

    Two pins remove both degrees of freedom. ``FYP_CONFIG_PATH`` names the
    config file this process actually loaded, which both root-discovery paths
    honour ahead of the directory walk. Putting the project root on
    ``PYTHONPATH`` puts ``fyp`` and ``web_interface`` on ``sys.path`` before
    the interpreter reaches site-packages, so the child imports the tree its
    worker script came from. Both are no-ops when parent and child already
    agree, which is every deployed configuration.

    Returns:
        A copy of the current environment with the worker marker and the
        config/import pins applied.
    """
    env = os.environ.copy()
    env["WEB_INTERFACE"] = "true"
    env["FYP_CONFIG_PATH"] = active_config_path()

    project_root = Path(PROJECT_ROOT).resolve()
    roots = [str(project_root)]
    # A reuse install (FYP_CONFIG_PATH pointing outside a checkout) can leave
    # the project root without a `fyp` package; fall back to wherever this
    # process imported one from, but only when that is a checkout of its own —
    # a site-packages directory must never be prepended ahead of the stdlib.
    fyp_root = Path(fyp.__file__).resolve().parent.parent
    if fyp_root != project_root and (fyp_root / "__proj__.py").exists():
        roots.append(str(fyp_root))

    inherited = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        roots + [p for p in inherited if p not in roots])
    return env


def scrape_platforms() -> list[str]:
    """Platforms registered in the scrape contract (each gets its own worker)."""
    import fyp.scrape_queues as scrape_queues
    return scrape_queues.registered_platforms()


# One scraper process per platform (queue_scraper_<platform>), each draining
# its own to_scrape_<platform>.json queue as its own Cloud Task chain.
SCRAPER_PROCESS_NAMES = [f"queue_scraper_{p}" for p in scrape_platforms()]


# Processes eligible for Cloud Tasks dispatch.
CLOUD_TASK_ELIGIBLE = {
    "consolidate_enrichment",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "study_refresh",
    "queue_annotator",
    "queue_annotator_batch",
    "timelines_refresh",
    "ingest_refresh",
    "aio_fetch",
    "collection_metadata_refresh",
    "collection_delete",
    "benchmark_parquet_read",
    "sequence_refresh",
    "sessions_refresh",
    "embeddings_refresh",
    "video_map_refresh",
    "retokenise_hashtags",
    "ab_eval",
    "ops_report",
    "enrichment_supervisor",
}
CLOUD_TASK_ELIGIBLE |= set(SCRAPER_PROCESS_NAMES)


# Corpus-scale sweeps that run well past Cloud Tasks' 600s default dispatch
# deadline. pca_refresh regenerates every study's recoded frame + runs the
# group-stats sweep (~26 min at 12 studies); recode_refresh_studies is ~7 min.
# sessions_refresh / timelines_refresh / embeddings_refresh are self-chaining:
# their own _DISPATCH_DEADLINE governs only the links they dispatch themselves.
# 1800s is the Cloud Tasks MAXIMUM for HTTP targets, and a batch link can exceed
# even that (44 min observed 2026-08-12), so sessions_refresh's initial link is
# setup-only and its links claim their successor via CAS before chaining — see
# run_sessions_refresh._claim_chain_dispatch.
# Keep in sync with the workers that define _DISPATCH_DEADLINE;
# tests/unit/test_dispatch_deadlines.py pins that.
# Cloud Tasks rejects any HTTP-target dispatchDeadline outside [15s, 30m] with
# a 400 at task-creation time — the task is never queued at all. 2026-09-03
# prod: consolidate_enrichment was given 3600s and every dispatch (the armed
# post-scrape trigger AND the admin's Consolidate button) failed with
# "Task.dispatchDeadline must be between [15s, 30m]" until redeployed.
# _dispatch_cloud_task clamps to this as a last line of defence; the table
# below must never need it.
CLOUD_TASKS_MAX_DISPATCH_DEADLINE = 1800

_LONG_RUNNING_DEADLINES = {
    "pca_refresh": 1800,
    "recode_refresh_studies": 1800,
    "sessions_refresh": 1800,
    "timelines_refresh": 1800,
    "embeddings_refresh": 1800,
    "queue_annotator_batch": 1800,
    # A consolidation is normally ~2 min, but two of its modes are not: a
    # force rebuild over the whole corpus, and the weekly shadow verification
    # (which rebuilds scrapes AND annotations, then signature-compares three
    # artifacts, ~13 min). 2026-09-02 prod: the shadow check ran 772-816s five
    # times, each attempt answering 200 after Cloud Tasks had already given up
    # at 600s and re-delivered — 66 minutes of an 8-vCPU runner for one check.
    "consolidate_enrichment": CLOUD_TASKS_MAX_DISPATCH_DEADLINE,
}


def dispatch_deadline_for(name: str, task_args: dict | None = None) -> int | None:
    """Cloud Tasks dispatch deadline for a task, or None for the 600s default.

    THE single source of truth, because a deadline is a property of the worker,
    not of who launched it. Cloud Tasks' default is 600s: a handler that runs
    longer never gets to respond, so the queue re-dispatches it from scratch up
    to max-attempts while the original attempt keeps running — the run "starts
    over and over", and for a self-chaining worker each doomed attempt spawns
    its own chain. 2026-08-04 pca_refresh looped this way; 2026-08-16 the same
    trap hit timelines_refresh from the *pipeline* side, where every dispatch
    site (spine advance, fork leaves, refresh-downstream) omitted the deadline
    that ``start_process`` was careful to pass — four concurrent chains writing
    one status file, which is what made the progress bar jump backwards.

    Args:
        name: Process name (per-platform scrapers included).
        task_args: The task's arguments; only the annotator reads them (its
            deadline scales with batch size).

    Returns:
        Deadline in seconds, or None to accept the Cloud Tasks default.
    """
    if name == "queue_annotator":
        # Used to scale to 3600 above 1000 items, which Cloud Tasks rejects
        # outright (see CLOUD_TASKS_MAX_DISPATCH_DEADLINE) — the big-batch
        # path could never have dispatched. The ceiling is the deadline.
        return CLOUD_TASKS_MAX_DISPATCH_DEADLINE
    if name.startswith("queue_scraper_"):
        return 1800
    return _LONG_RUNNING_DEADLINES.get(name)



# --- Global State ---
# Store process handles and logs
processes = {
    **{
        name: {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
        for name in SCRAPER_PROCESS_NAMES
    },
    "queue_annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "queue_annotator_batch": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "meta_refresh_groups": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "timelines_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "recode_refresh_studies": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "pca_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "consolidate_enrichment": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "study_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "ingest_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "aio_fetch": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "collection_metadata_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "collection_delete": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "sequence_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "sessions_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "embeddings_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "video_map_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "retokenise_hashtags": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "ab_eval": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "ops_report": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "enrichment_supervisor": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
}

process_stats = {}

# Deep snapshot of process_stats as last loaded/saved. save_process_stats
# diffs the live dict against it to find the keys THIS process changed, and
# merges only those onto the fresh file contents — so two services writing
# different entries concurrently can no longer clobber each other.
_process_stats_snapshot = {}


def _snapshot_process_stats() -> None:
    """Refresh the change-detection snapshot (JSON round-trip deep copy)."""
    _process_stats_snapshot.clear()
    _process_stats_snapshot.update(json.loads(json.dumps(process_stats)))



def load_process_stats():
    global process_stats
    try:
        if data_io.exists(storage_location="cache", filename="process_stats.json"):
            loaded = data_io.load_json(storage_location="cache", filename="process_stats.json")
            process_stats.clear()
            process_stats.update(loaded)
        else:
            process_stats.clear()
    except Exception as e:
        print(f"Failed to load process stats: {e}")
        process_stats.clear()
    _snapshot_process_stats()




def save_process_stats():
    """Persist process_stats without clobbering concurrent writers.

    Runs as an atomic read-modify-write (``data_io.update_json``): only the
    top-level keys that changed since the last load/save are applied onto the
    freshly-read file contents, so an entry written meanwhile by the other
    service (web ↔ task-runner, or a second server instance) survives. The
    in-memory dict is then resynced to the merged authoritative contents.
    """
    try:
        def _merge(fresh):
            fresh = fresh if isinstance(fresh, dict) else {}
            for key, value in process_stats.items():
                if key not in _process_stats_snapshot or _process_stats_snapshot[key] != value:
                    fresh[key] = value
            for key in _process_stats_snapshot:
                if key not in process_stats:
                    fresh.pop(key, None)
            return fresh

        updated = data_io.update_json(
            storage_location="cache",
            filename="process_stats.json",
            mutate=_merge,
            default={},
        )
        if updated is not None:
            process_stats.clear()
            process_stats.update(updated)
        _snapshot_process_stats()
    except Exception as e:
        print(f"Failed to save process stats: {e}")




def enqueue_output(out, queue, process_state, name=None):
    """Drain a worker subprocess's stdout into the UI log and progress state.

    This is the subprocess mode's single log-buffer entry point, and therefore
    the only place its lines are timestamped — the worker's own
    ``LocalStatusReporter.log`` deliberately prints bare, or a line would be
    stamped at both ends of the pipe.

    Args:
        out: The subprocess's stdout pipe.
        queue: The in-memory deque kept as a same-process fallback.
        process_state: The ``processes[name]`` entry to update.
        name: The process name, used as the durable run log's key.
    """
    for line in iter(out.readline, b''):
        line_str = line.decode('utf-8')
        print(line_str, end='') # Mirror to console

        # Update last message for UI
        process_state["last_message"] = line_str.strip()

        if "::PROGRESS::" in line_str:
            try:
                _, json_str = line_str.split("::PROGRESS::", 1)
                data = json.loads(json_str.strip())
                process_state["progress"].update(data)
            except Exception:
                queue.append(line_str)
                _log_run_line(name, line_str)
        elif "::DATA::" in line_str:
            try:
                _, json_str = line_str.split("::DATA::", 1)
                data = json.loads(json_str.strip())
                process_state["data"].update(data)
            except Exception:
                queue.append(line_str)
                _log_run_line(name, line_str)
        else:
            queue.append(line_str)
            _log_run_line(name, line_str)
    out.close()




def _log_run_line(name, line_str: str) -> None:
    """Append one worker stdout line to the durable run log."""
    if name:
        run_logs.append(name, line_str)




def monitor_process_completion(name, proc):
    """Waits for process to finish and updates stats."""
    proc.wait()

    # proc.wait() returns as soon as the process exits, but the reader thread
    # is still draining whatever is left in the pipe. Finalizing the run before
    # it finishes would close the log and silently drop the worker's last
    # lines — usually the ones that say how the run ended.
    reader = processes[name].get("log_thread")
    if reader:
        reader.join(timeout=10)

    end_time = datetime.now(UTC)
    start_time_str = processes[name].get("start_time")
    duration = 0
    if start_time_str:
        start_time = datetime.fromisoformat(start_time_str)
        duration = (end_time - start_time).total_seconds()

    outcome = "Success" if proc.returncode == 0 else "Fail"
    study_name = processes[name].get("study_name")

    # Capture the task_args BEFORE we tear down the in-memory process entry
    # below — the failure ledger records them.
    completed_task_args = dict(processes[name].get("data", {}).get("task_args", {}) or {})

    # Reload from GCS before merging so we don't clobber task-runner writes
    load_process_stats()

    # Record stats — start from any existing entry, overlay ::DATA:: emitted by the
    # process, then set the standard completion fields on top.
    merged = {**process_stats.get(name, {}), **processes[name].get("data", {})}
    merged.update({
        "last_success": end_time.isoformat() if outcome == "Success" else merged.get("last_success"),
        "last_run_end_time": end_time.isoformat(),
        "last_run_duration": duration,
        "last_run_outcome": outcome,
        "last_run_study": study_name
    })
    process_stats[name] = merged
    save_process_stats()

    # Mirror the Cloud-Tasks failure ledger in subprocess mode so local dev
    # shows the same audit trail (subprocess runs are never queue-retried, so
    # every failure here is terminal).
    if outcome == "Fail":
        task_failures.record_failure(
            task=name,
            error=f"Worker exited with code {proc.returncode}",
            status_key=name,
            disposition=task_failures.DISPOSITION_DEAD,
            task_args=completed_task_args,
            phase="subprocess",
        )

    run_logs.finalize(name, run_logs.STATE_COMPLETED if outcome == "Success"
                      else run_logs.STATE_FAILED)

    # Update global state to stopped
    _clear_graceful_stop(name)
    processes[name]["status"] = "stopped"
    processes[name]["proc"] = None
    processes[name]["start_time"] = None
    processes[name]["study_name"] = None

    # Local-mode refresh run. On Cloud Run the task runner advances the run
    # when a step's task finishes; in subprocess dev the web process is the only
    # thing that knows a worker exited, so it drives the run from here. The run
    # record was seeded by whichever endpoint started the origin, so a worker
    # started outside a run (or one belonging to a run that already ended)
    # simply finds nothing to advance.
    if not is_cloud_run():
        try:
            from web_interface.services import refresh_pipeline

            record = refresh_pipeline.load_run()
            if (record and record.get("in_flight")
                    and name in refresh_pipeline.STEP_ORDER
                    and (record.get("steps", {}).get(name) or {}).get("state")
                    in ("origin", "dispatched")):
                threading.Thread(
                    target=run_local_refresh_run,
                    args=(record["run_id"],),
                    kwargs={"finished": name, "outcome": outcome},
                    daemon=True,
                ).start()
        except Exception as exc:
            print(f"[{name}] Local refresh-run advance skipped: {exc}")


def local_pipeline_script_map() -> dict:
    """Map each downstream pipeline task name to its subprocess script path.

    Used by the local-dev sequential run driver (:func:`run_local_refresh_run`).
    Every dispatchable step of the refresh pipeline must have an entry here, or
    the local run aborts with "Unknown step". Exposed at module level so the
    invariant is unit-testable against the registry.
    """
    from fyp.fyp_config import (
        EMBEDDINGS_REFRESH_SCRIPT,
        META_REFRESH_GROUPS_SCRIPT,
        PCA_REFRESH_SCRIPT,
        RECODE_REFRESH_STUDIES_SCRIPT,
        SESSIONS_REFRESH_SCRIPT,
        TIMELINES_REFRESH_SCRIPT,
        VIDEO_MAP_REFRESH_SCRIPT,
    )

    return {
        "recode_refresh_studies": RECODE_REFRESH_STUDIES_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "pca_refresh": PCA_REFRESH_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT,
        "embeddings_refresh": EMBEDDINGS_REFRESH_SCRIPT,
        "video_map_refresh": VIDEO_MAP_REFRESH_SCRIPT,
        "sessions_refresh": SESSIONS_REFRESH_SCRIPT,
    }


def run_local_refresh_run(run_id: str, finished: str | None = None,
                          outcome: str = "Success") -> None:
    """Drive a refresh run to completion as sequential subprocesses (local dev).

    Same planner and the same pruning as Cloud Run; the only difference is the
    schedule. There is one CPU here, so the leaves that fan out in parallel on
    Cloud Run are run one after another — the decisions are identical, and the
    chart draws them as consecutive bars rather than overlapping ones.

    Args:
        run_id: The run to advance. A mismatch means the run was replaced while
            this thread was waiting, and it does nothing.
        finished: The step whose completion triggered this, for the log line.
        outcome: That step's outcome. A failure stops the run here.
    """
    from web_interface.services import refresh_pipeline

    record = refresh_pipeline.load_run()
    if not record or record.get("run_id") != run_id or not record.get("in_flight"):
        return

    if finished and outcome != "Success":
        print(f"[refresh-run] {finished} failed; stopping the run.")
        refresh_pipeline.finish_run(partial=True, failed_at=finished, run_id=run_id)
        _publish_local_run_summary(run_id)
        return

    script_map = local_pipeline_script_map()

    while True:
        record = refresh_pipeline.load_run()
        if not record or record.get("run_id") != run_id or not record.get("in_flight"):
            return
        action = refresh_pipeline.next_actions(record)
        prunes = action["prunes"]
        for step, reason in prunes.items():
            print(f"[refresh-run] Skipping {step} — {reason}.")

        if action["action"] == "finish":
            refresh_pipeline.finish_run(partial=False, prunes=prunes, run_id=run_id)
            _publish_local_run_summary(run_id)
            return

        targets = ([(action["step"], action["task_args"])] if action["action"] == "spine"
                   else list(action["leaves"]))
        stage_total = refresh_pipeline.stage_total(record["steps"])
        stage_index = refresh_pipeline.next_stage_index(record["steps"])
        fork_at = None
        if action["action"] == "fork":
            fork_at = next((n for n in reversed(refresh_pipeline.STEP_ORDER)
                            if (record["steps"].get(n) or {}).get("state")
                            in ("origin", "dispatched")), None)

        dispatched: dict[str, dict] = {}
        failed_at = None
        for step_name, step_args in targets:
            script_path = script_map.get(step_name)
            if script_path is None:
                print(f"[refresh-run] Unknown step {step_name}; stopping.")
                failed_at = step_name
                break
            success, msg = start_process(
                step_name, script_path,
                args=_task_args_to_cli(step_name, step_args),
                started_by=f"auto-pipeline (after {record.get('origin')})")
            if not success:
                print(f"[refresh-run] Failed to start {step_name}: {msg}")
                failed_at = step_name
                break
            # Seed the stage framing AFTER start_process, which resets progress
            # to {}. Subprocess ::PROGRESS:: lines only update named keys, so
            # these survive until the step finishes.
            if step_name in processes:
                processes[step_name]["progress"].update({
                    "stage_index": stage_index,
                    "stage_total": stage_total,
                    "stage_name": step_name,
                })
            dispatched[step_name] = {}

            # Wait for monitor_process_completion to finish tearing the process
            # down; once proc is None its stats have been written.
            import time as _t
            while processes.get(step_name, {}).get("proc") is not None:
                _t.sleep(0.5)

            if process_stats.get(step_name, {}).get("last_run_outcome") != "Success":
                print(f"[refresh-run] Step {step_name} failed; stopping the run.")
                failed_at = step_name
                break

        refresh_pipeline.record_dispatch(run_id, dispatched, prunes=prunes,
                                         fork_at=fork_at)
        if failed_at:
            refresh_pipeline.finish_run(partial=True, failed_at=failed_at,
                                        run_id=run_id)
            _publish_local_run_summary(run_id)
            return


def _publish_local_run_summary(run_id: str) -> None:
    """Mirror a finished local run onto the Consolidate card and the memory copy.

    Local dev overlays ``processes[...]["data"]`` on top of process_stats when
    it renders the consolidate entry, so a summary written only to the stats
    file would be shadowed by the worker's last ``::DATA::`` emission.
    """
    from web_interface.services import refresh_pipeline

    record = refresh_pipeline.load_run()
    if not record or record.get("run_id") != run_id:
        return
    if record.get("origin_kind") not in ("consolidate", "armed", "refresh_downstream"):
        return
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry["last_pipeline_summary"] = record.get("summary") or ""
    entry["last_pipeline_summary_ts"] = record.get("finished_ts")
    entry["last_pipeline_partial"] = bool(record.get("partial"))
    entry["last_pipeline_failed_at"] = record.get("failed_at")
    if not record.get("partial"):
        entry.pop("consolidation_impact", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    mem = processes.get("consolidate_enrichment", {}).get("data")
    if isinstance(mem, dict):
        mem["last_pipeline_summary"] = entry["last_pipeline_summary"]
        mem["last_pipeline_summary_ts"] = entry["last_pipeline_summary_ts"]
        mem["last_pipeline_partial"] = entry["last_pipeline_partial"]
        mem["last_pipeline_failed_at"] = entry["last_pipeline_failed_at"]
        if not record.get("partial"):
            mem.pop("consolidation_impact", None)



def _dispatch_cloud_task(name: str, task_args: dict,
                         dispatch_deadline_seconds: int | None = None,
                         schedule_delay_seconds: int | None = None) -> tuple[bool, str]:
    """Dispatch a background task via Google Cloud Tasks.

    Args:
        name: Task name (used to build the endpoint URL).
        task_args: JSON-serialisable dict forwarded as the request body.
        dispatch_deadline_seconds: Optional override for the Cloud Tasks
            ``dispatch_deadline``.  When set, Cloud Tasks will wait up to
            this many seconds for the HTTP response before considering the
            task failed.  Cloud Tasks accepts [15s, 30m] for HTTP targets and
            rejects anything else with a 400 at creation time, so the value
            is clamped to ``CLOUD_TASKS_MAX_DISPATCH_DEADLINE`` here.
        schedule_delay_seconds: Optional delay before the task is eligible to
            run (Cloud Tasks ``schedule_time``). Used by the batch annotator's
            poll phase to re-check a running job after a delay WITHOUT holding a
            task-runner instance asleep in the meantime.
    """
    try:
        from google.cloud import tasks_v2
        from google.protobuf import duration_pb2

        project = os.environ.get("GCP_PROJECT_ID")
        location = os.environ.get("CLOUD_TASKS_LOCATION")
        queue = os.environ.get("CLOUD_TASKS_QUEUE")
        service_url = os.environ.get("K_SERVICE")
        sa_email = os.environ.get("CLOUD_TASKS_SA_EMAIL")

        if not all([project, location, queue, service_url]):
            return False, "Cloud Tasks environment variables not configured"

        # Build the full Cloud Run URL for the internal task endpoint
        # K_SERVICE is just the service name; we need the full URL
        cloud_run_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "")
        if not cloud_run_url:
            # No safe fallback: a Cloud Run service URL embeds a
            # deployment-specific hash that cannot be derived from K_SERVICE.
            return False, (
                "CLOUD_RUN_SERVICE_URL is not set — it is required to dispatch "
                "Cloud Tasks (the service URL cannot be inferred from K_SERVICE)"
            )

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(project, location, queue)

        task_body = json.dumps(task_args).encode()

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{cloud_run_url}/internal/run-task/{name}",
                headers={"Content-Type": "application/json"},
                body=task_body,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=sa_email,
                ),
            ),
        )

        if dispatch_deadline_seconds:
            if dispatch_deadline_seconds > CLOUD_TASKS_MAX_DISPATCH_DEADLINE:
                print(f"[CloudTasks] {name}: dispatch deadline "
                      f"{dispatch_deadline_seconds}s exceeds the Cloud Tasks maximum; "
                      f"clamping to {CLOUD_TASKS_MAX_DISPATCH_DEADLINE}s.")
                dispatch_deadline_seconds = CLOUD_TASKS_MAX_DISPATCH_DEADLINE
            task.dispatch_deadline = duration_pb2.Duration(
                seconds=dispatch_deadline_seconds,
            )

        if schedule_delay_seconds and schedule_delay_seconds > 0:
            from datetime import timedelta as _timedelta

            from google.protobuf import timestamp_pb2
            schedule_ts = timestamp_pb2.Timestamp()
            schedule_ts.FromDatetime(datetime.now(UTC) + _timedelta(seconds=schedule_delay_seconds))
            task.schedule_time = schedule_ts

        # Cloud Tasks API can return transient 503/504. Retry a couple of
        # times with short backoff before surfacing the failure to the UI.
        import time as _time
        import traceback as _tb
        from google.api_core import exceptions as _gax_exc
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                response = client.create_task(parent=parent, task=task)
                print(f"[CloudTasks] Dispatched task for {name}: {response.name}")
                return True, "Task dispatched"
            except (_gax_exc.ServiceUnavailable,
                    _gax_exc.DeadlineExceeded,
                    _gax_exc.InternalServerError) as e:
                last_err = e
                details = getattr(e, "details", lambda: None)()
                metadata = getattr(e, "trailing_metadata", lambda: None)()
                print(f"[CloudTasks] {type(e).__name__} dispatching {name} "
                      f"(attempt {attempt + 1}/3): {e} | details={details!r} "
                      f"| metadata={metadata!r}")
                if attempt == 0:
                    _tb.print_exc()
                _time.sleep(0.5 * (2 ** attempt))
        raise last_err if last_err else RuntimeError("Cloud Tasks dispatch failed")

    except Exception as e:
        print(f"[CloudTasks] Failed to dispatch {name}: {type(e).__name__}: {e}")
        return False, f"Cloud Tasks dispatch failed: {e}"




def _drain_lease_conflict(name: str) -> str | None:
    """Return a block message when a local scrape-queue drain holds a lease.

    A local drain against the shared bucket (see web_interface/drain_lease.py)
    is invisible to this process's subprocess table AND to the GCS task-status
    check, so it gets its own guard: the platform's own scraper is blocked, and
    so is a consolidation (its queue prune would race the drain's).
    """
    from web_interface import drain_lease

    try:
        if name.startswith("queue_scraper_"):
            platform = name.removeprefix("queue_scraper_")
            lease = drain_lease.read_drain_lease(platform)
            if lease:
                return (f"Blocked: {drain_lease.describe_lease(lease)} is draining "
                        f"this queue. Wait for it to finish (or for its lease to "
                        f"expire, ~{drain_lease.LEASE_STALE_S // 60} min after it stops).")
        elif name == "consolidate_enrichment":
            leases = drain_lease.active_drain_leases()
            if leases:
                held = "; ".join(drain_lease.describe_lease(v) for v in leases.values())
                return (f"Blocked: {held} is writing scrape data right now. "
                        f"Consolidate after the drain finishes.")
    except Exception as exc:
        # The lease is a guard, not a dependency — never block starts on a
        # lease-read failure.
        print(f"Drain-lease check failed (ignoring): {exc}")
    return None






def start_process(name: str, script_path, args: list = [], study_name: str | None = None,
                  task_args: dict | None = None, started_by: str = "",
                  extra_task_args: dict | None = None) -> tuple[bool, str]:
    """Start a background process. Uses Cloud Tasks on Cloud Run for eligible processes,
    otherwise falls back to subprocess.

    Args:
        name: Registered process name.
        script_path: Path to the worker script (subprocess mode).
        args: CLI argument list for the worker.
        study_name: Study this run targets, when applicable.
        task_args: Pre-built Cloud Tasks arguments; derived from ``args`` when omitted.
        started_by: Username of the admin who launched it. Recorded in the run
            log's banner — this layer is the only one that knows who clicked,
            so the attribution is stamped here rather than inside the worker.
        extra_task_args: Keys merged into the Cloud Tasks payload after it is
            built, for context the CLI has no flag for — the refresh run's id
            and stage framing. Ignored in subprocess mode, where the web process
            that seeded the run also observes the completion and needs no
            round-trip.
    """

    # A local scrape-queue drain (laptop, FYP_FORCE_GCS) holds a lease on the
    # shared storage — refuse conflicting work while it is fresh.
    lease_msg = _drain_lease_conflict(name)
    if lease_msg:
        return False, lease_msg

    # Defense in depth: never dispatch an annotation Cloud Task when a
    # local-only backend is selected (annotation_configured gates the Start
    # button, but a mid-flight settings change could race past it).
    if is_cloud_run() and name in ("queue_annotator", "queue_annotator_batch"):
        try:
            from fyp.annotation.backends import active_backend_name, get_backend

            backend_name = active_backend_name()
            if not get_backend(backend_name).cloud_run_capable:
                return False, (f"The '{backend_name}' annotation backend runs only on a "
                               f"local machine — switch the backend to Gemini in "
                               f"Admin → Backends, or run the annotator locally.")
        except ValueError as exc:
            return False, f"Annotation backend unavailable: {exc}"

    # Same defense for the embeddings worker: a local embedding backend can
    # only run on the host machine.
    if is_cloud_run() and name == "embeddings_refresh":
        try:
            from fyp.analysis.embedding_backends import active_backend_name, get_backend

            backend_name = active_backend_name()
            if not get_backend(backend_name).cloud_run_capable:
                return False, (f"The '{backend_name}' embedding backend runs only on a "
                               f"local machine — switch the backend to Gemini in "
                               f"Admin → Backends, or run the embeddings refresh locally.")
        except ValueError as exc:
            return False, f"Embedding backend unavailable: {exc}"

    # Cloud Tasks path for eligible processes on Cloud Run
    if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
        # For study_refresh, use a study-specific status key so multiple
        # studies can refresh concurrently without colliding.
        if name == "study_refresh" and task_args and task_args.get("study_name"):
            status_key = f"study_refresh__{task_args['study_name']}"
        else:
            status_key = name

        # Check if already running via GCS status (with stale detection)
        status = read_task_status(status_key)
        if status and status.get("state") == "running":
            updated_str = status.get("updated_at", "")
            is_stale = False
            age = 0.0
            if updated_str:
                try:
                    updated_at = datetime.fromisoformat(updated_str)
                    age = (datetime.now(UTC) - updated_at).total_seconds()
                    is_stale = age > 600  # 10 min without heartbeat = likely dead
                except (ValueError, TypeError):
                    pass
            if not is_stale:
                return False, "Process already running"
            # A stale 'running' status is a corpse: the run died without ever
            # reaching the failure wrapper, which for these workers means a
            # SIGKILL (out of memory) — no traceback, no ledger entry, so
            # repeated silent deaths go unnoticed (pca_refresh died this way
            # three times, 2026-08-08/09). Dead-letter it HERE: the dispatch
            # below writes a fresh 'running' placeholder, so by the time the
            # task runner starts, the corpse is already overwritten and
            # unobservable from that side. Never raises.
            try:
                last_msg = (status.get("progress") or {}).get("message") or "—"
                task_failures.record_failure(
                    task=name,
                    error=(f"Previous run found dead: status stuck at 'running' "
                           f"with a heartbeat {age / 60:.0f} min old (last message: "
                           f"{last_msg}). No failure was recorded by the run "
                           f"itself — the process was most likely SIGKILLed "
                           f"(out of memory)."),
                    status_key=status_key,
                    disposition=task_failures.DISPOSITION_DEAD,
                    phase="presumed_oom",
                )
            except Exception as exc:
                print(f"[{name}] stale-predecessor ledger record failed: {exc}")

        # Build task args from the CLI args list
        if task_args is None:
            task_args = _cli_args_to_dict(name, args, study_name)
        if extra_task_args:
            task_args = {**task_args, **extra_task_args}

        # Attribution and run identity ride along in task_args so the worker —
        # running in the other Cloud Run service — writes into the run this
        # click opened, instead of opening a second one of its own.
        task_args["started_by"] = started_by or task_args.get("started_by") or ""
        task_args["log_run_id"] = task_args.get("log_run_id") or run_logs.new_run_id()

        success, msg = _dispatch_cloud_task(
            name, task_args,
            dispatch_deadline_seconds=dispatch_deadline_for(name, task_args))
        if success:
            run_logs.open_run(status_key, run_id=task_args["log_run_id"],
                              started_by=task_args["started_by"],
                              task_args=task_args, mode="cloud")
            # Write an immediate "running" status so the UI shows feedback
            # before the task-runner instance starts up and writes its own status.
            from web_interface.task_status import GCSStatusReporter
            placeholder = GCSStatusReporter(status_key)
            placeholder._status["state"] = "running"
            placeholder._status["start_time"] = datetime.now(UTC).isoformat()
            placeholder._status["progress"] = {"percent": 0, "message": "Starting..."}
            placeholder._status["task_args"] = {
                "batch_size": task_args.get("batch_size"),
                "max_batches": task_args.get("max_batches"),
                "platform": task_args.get("platform"),
            }
            placeholder._write_status(force=True)
        return success, msg

    # Subprocess path (local dev + non-eligible processes on Cloud Run)
    if processes[name]["proc"] is not None:
        if processes[name]["proc"].poll() is None:
            return False, "Process already running"

    env_vars = worker_env()

    # Translate task_args → CLI args when caller supplied only task_args.
    # Only done when `args` is empty, so callers that already built their
    # own CLI arg list (via api_start → _cli_args_to_dict in reverse) keep
    # working.
    if not args and task_args:
        args = list(_task_args_to_cli(name, task_args))

    cmd = [PYTHON_EXEC, "-u", str(script_path)] + args

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            env=env_vars
        )
        processes[name]["proc"] = proc
        processes[name]["status"] = "running"
        processes[name]["start_time"] = datetime.now(UTC).isoformat()
        processes[name]["study_name"] = study_name
        # Seed a "Starting..." placeholder (mirrors the Cloud Run path) so the
        # first status poll shows immediate feedback rather than an empty bar.
        processes[name]["progress"] = {"percent": 0, "message": "Starting..."}
        processes[name]["last_message"] = ""
        # Reset emitted data too — otherwise the in-memory ::DATA:: payload
        # from a previous run leaks into /api/status until the new worker
        # emits its own, making the UI show stale values (e.g. the
        # Consolidation Impact panel carrying the prior run's impact).
        _ta = dict(task_args) if task_args else _cli_args_to_dict(name, args, study_name)
        if started_by:
            _ta["started_by"] = started_by
        # Keep the full task_args dict in memory so monitor_process_completion
        # can inspect flags like auto_refresh / pipeline_remaining / stage info
        # after the subprocess exits. The UI only reads a few specific fields
        # (batch_size, max_batches) so the extra keys are harmless.
        processes[name]["data"] = {
            "task_args": dict(_ta),
        }

        # Open the durable run before the reader thread starts, so the banner
        # is the run's first line and nothing the worker prints races ahead of it.
        run_logs.open_run(name, started_by=started_by, task_args=_ta,
                          mode="subprocess")

        # Start logging thread
        t = threading.Thread(target=enqueue_output, args=(proc.stdout, processes[name]["logs"], processes[name], name))
        t.daemon = True
        t.start()
        # Kept so monitor_process_completion can wait for the pipe to drain
        # before it closes the run log.
        processes[name]["log_thread"] = t

        # Start monitoring thread
        t_mon = threading.Thread(target=monitor_process_completion, args=(name, proc))
        t_mon.daemon = True
        t_mon.start()

        return True, "Started"
    except Exception as e:
        return False, str(e)




def _cli_args_to_dict(name: str, args: list, study_name: str | None) -> dict:
    """Convert CLI arg list back to a dict for Cloud Tasks dispatch."""
    task_args: dict = {}
    if study_name:
        task_args["study_name"] = study_name

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--studies" and i + 1 < len(args):
            task_args["studies"] = args[i + 1]
            i += 2
        elif arg == "--batch-size" and i + 1 < len(args):
            task_args["batch_size"] = args[i + 1]
            i += 2
        elif arg == "--max-batches" and i + 1 < len(args):
            task_args["max_batches"] = args[i + 1]
            i += 2
        elif arg == "--platform" and i + 1 < len(args):
            task_args["platform"] = args[i + 1]
            i += 2
        elif arg == "--launched-by" and i + 1 < len(args):
            task_args["launched_by"] = args[i + 1]
            i += 2
        elif arg == "--collections" and i + 1 < len(args):
            task_args["collections"] = args[i + 1]
            i += 2
        elif arg == "--auto-refresh":
            task_args["auto_refresh"] = True
            i += 1
        elif arg in ("--n-niches", "--map-sample", "--pca-dim") and i + 1 < len(args):
            task_args[arg.lstrip("-").replace("-", "_")] = args[i + 1]
            i += 2
        elif arg == "--force":
            task_args["force_full_rebuild"] = True
            i += 1
        elif arg == "--stale-only":
            task_args["stale_only"] = True
            i += 1
        elif arg == "--skip-if-busy":
            task_args["skip_if_busy"] = True
            i += 1
        elif not arg.startswith("--"):
            # Positional arg (study_name)
            if "study_name" not in task_args:
                task_args["study_name"] = arg
            i += 1
        else:
            i += 1

    return task_args


def _task_args_to_cli(name: str, task_args: dict) -> list[str]:
    """Translate a task_args dict to a CLI argv list for subprocess mode.

    Covers only the flags each worker's __main__ block supports — keys that
    aren't mapped here (e.g. pipeline_remaining) are simply dropped in
    subprocess mode. Cloud Tasks mode forwards task_args as JSON so all keys
    survive there.
    """
    out: list[str] = []
    if task_args.get("batch_size") is not None:
        out += ["--batch-size", str(task_args["batch_size"])]
    if task_args.get("max_batches") is not None:
        out += ["--max-batches", str(task_args["max_batches"])]
    if task_args.get("platform"):
        out += ["--platform", str(task_args["platform"])]
    if task_args.get("studies"):
        out += ["--studies", str(task_args["studies"])]
    if task_args.get("collections"):
        out += ["--collections", str(task_args["collections"])]
    if task_args.get("force_consolidation"):
        out += ["--force-consolidation"]
    if task_args.get("auto_refresh"):
        out += ["--auto-refresh"]
    if task_args.get("force_full_rebuild"):
        out += ["--force"]
    if task_args.get("stale_only"):
        out += ["--stale-only"]
    if task_args.get("skip_if_busy"):
        out += ["--skip-if-busy"]
    if task_args.get("hours_back") is not None:
        out += ["--hours-back", str(task_args["hours_back"])]
    if task_args.get("collection_id"):
        out += ["--collection-id", str(task_args["collection_id"])]
    # collection_delete takes several: the flag repeats (argparse 'append').
    if task_args.get("collection_ids"):
        for _cid in task_args["collection_ids"]:
            out += ["--collection-id", str(_cid)]
    if task_args.get("run_id"):
        out += ["--run-id", str(task_args["run_id"])]
    if task_args.get("candidate_names"):
        names = task_args["candidate_names"]
        joined = ",".join(names) if isinstance(names, list) else str(names)
        out += ["--candidates", joined]
    if task_args.get("include_live"):
        out += ["--include-live"]
    if name == "ab_eval":
        # ab_eval-only structured args (JSON-encoded for the CLI). Keyed on the
        # process name because "name"/"eval_set" are too generic to map safely
        # for every worker.
        if task_args.get("arms_spec"):
            out += ["--arms-spec", json.dumps(task_args["arms_spec"])]
        if task_args.get("eval_set"):
            out += ["--eval-set", str(task_args["eval_set"])]
        if task_args.get("name"):
            out += ["--name", str(task_args["name"])]
        if task_args.get("started_by"):
            out += ["--started-by", str(task_args["started_by"])]
    if task_args.get("launched_by"):
        out += ["--launched-by", str(task_args["launched_by"])]
    # study_name is a positional in some scripts (recode/pca) — append last
    if task_args.get("study_name"):
        out += [str(task_args["study_name"])]
    return out


def stop_process(name: str) -> tuple[bool, str]:
    """Stop a running process (hard kill for subprocess, cancel for Cloud Tasks)."""
    # Cloud Tasks path
    if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
        status = read_task_status(name)
        if status and status.get("state") == "running":
            write_cancel_request(name)
            # If the heartbeat is clearly not ticking (task-runner pod dead or
            # never started), the cancel sentinel will never be consumed and
            # the next start_process would 409 until the 10-minute staleness
            # window expires. Overwrite the status with a terminal state so
            # the user can restart immediately.
            updated_str = status.get("updated_at", "")
            if updated_str:
                try:
                    updated_at = datetime.fromisoformat(updated_str)
                    age = (datetime.now(UTC) - updated_at).total_seconds()
                    if age > STUCK_STATUS_THRESHOLD_S:
                        force_clear_status(name, reason="cancelled")
                        # The run was opened by the task runner, which is gone;
                        # close it here or it stays "running" forever.
                        run_logs.finalize(name, run_logs.STATE_CANCELLED)
                        return True, "Stuck status cleared"
                except (ValueError, TypeError):
                    pass
            return True, "Cancel requested"
        return False, "Not running"

    # Subprocess path
    proc = processes[name]["proc"]
    if proc:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        processes[name]["proc"] = None
        processes[name]["status"] = "stopped"
        processes[name]["start_time"] = None
        _clear_graceful_stop(name)
        # Ahead of the monitor thread, which would otherwise record the kill's
        # non-zero exit code as a failure rather than a cancellation.
        run_logs.finalize(name, run_logs.STATE_CANCELLED)
        return True, "Stopped"
    # Process handle already cleared (e.g. by monitor thread) — clean up state
    if processes[name]["status"] in ("running", "stopping"):
        processes[name]["status"] = "stopped"
        processes[name]["start_time"] = None
        _clear_graceful_stop(name)
        run_logs.finalize(name, run_logs.STATE_CANCELLED)
        return True, "Stopped"
    return False, "Not running"




def graceful_stop_process(name: str) -> tuple[bool, str]:
    """Signal a process to stop after finishing its current batch."""
    # Cloud Tasks path: use GCS cancel sentinel
    if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
        status = read_task_status(name)
        if status and status.get("state") == "running":
            write_cancel_request(name)
            return True, "Graceful stop requested"
        return False, "Not running"

    # Subprocess path: use local file sentinel
    proc = processes[name]["proc"]
    if proc and proc.poll() is None:
        GRACEFUL_STOP_DIR.mkdir(parents=True, exist_ok=True)
        sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
        sentinel.touch()
        processes[name]["status"] = "stopping"
        return True, "Graceful stop requested"
    return False, "Not running"




def _clear_graceful_stop(name: str) -> None:
    """Remove the graceful stop sentinel file for a process."""
    sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
    if sentinel.exists():
        sentinel.unlink(missing_ok=True)




def check_graceful_stop(name: str) -> bool:
    """Check if a graceful stop has been requested for a process. Called by worker scripts."""
    sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
    return sentinel.exists()
