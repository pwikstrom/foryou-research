import json
import os
import subprocess
import threading
from collections import deque
from datetime import UTC, datetime

import fyp.data_io as data_io
from fyp.fyp_config import PROJECT_ROOT, PYTHON_EXEC
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
_LONG_RUNNING_DEADLINES = {
    "pca_refresh": 1800,
    "recode_refresh_studies": 1800,
    "sessions_refresh": 1800,
    "timelines_refresh": 1800,
    "embeddings_refresh": 1800,
    "queue_annotator_batch": 1800,
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
        batch_size = int((task_args or {}).get("batch_size", 500))
        return 3600 if batch_size > 1000 else 1800
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
    "ops_report": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
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

    # Capture the task_args + emitted data BEFORE we tear down the in-memory
    # process entry below — the auto-refresh orchestrator needs both.
    completed_task_args = dict(processes[name].get("data", {}).get("task_args", {}) or {})
    completed_emitted_data = dict(processes[name].get("data", {}) or {})

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

    # Local-mode pipeline auto-dispatch. After the consolidate subprocess
    # completes successfully, if it was requested with auto_refresh=True and
    # emitted a non-empty impact, sequentially fire the stale downstream
    # refresh subprocesses. Cloud Run runs this via the Cloud Tasks chain in
    # _run_task_with_stats — this local path covers `python web_interface/
    # fyp_data_hub.py` dev runs.
    if (
        name == "consolidate_enrichment"
        and outcome == "Success"
        and not is_cloud_run()
        and bool(completed_task_args.get("auto_refresh"))
    ):
        impact = completed_emitted_data.get("consolidation_impact")
        if impact:
            t = threading.Thread(
                target=_run_local_downstream_pipeline,
                args=(impact,),
                daemon=True,
            )
            t.start()

    # After a local video-map rebuild requested with auto_refresh, refresh all
    # study caches so the new niches reach the analysis tabs (Cloud Run does
    # this via the Cloud Tasks chain returned by run_video_map_refresh).
    if (
        name == "video_map_refresh"
        and outcome == "Success"
        and not is_cloud_run()
        and bool(completed_task_args.get("auto_refresh"))
    ):
        t = threading.Thread(
            target=_run_local_video_map_downstream,
            daemon=True,
        )
        t.start()


def _run_local_downstream_pipeline(impact: dict) -> None:
    """Sequentially dispatch stale downstream refreshes after a consolidate.

    Used only in local dev mode — Cloud Run chains via Cloud Tasks in
    _run_task_with_stats.
    """
    from web_interface.run_consolidate_enrichment import (
        _build_downstream_pipeline,
        build_pipeline_summary,
    )

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        return

    def _summary(steps_ran: list[str], aborted_at: str | None) -> str:
        if aborted_at:
            return (
                f"Pipeline aborted at '{aborted_at}'. "
                + (build_pipeline_summary(impact, steps_ran) if steps_ran else "No steps completed.")
            )
        return build_pipeline_summary(impact, steps_ran)

    _run_local_pipeline(pipeline, summary_owner="consolidate_enrichment", summary_fn=_summary)


def _run_local_video_map_downstream() -> None:
    """Refresh all study caches after a local video-map rebuild.

    A rebuild remaps every video's niche, so every study/collection is refreshed
    (no filter). Used only in local dev mode — Cloud Run chains via Cloud Tasks.
    """
    from web_interface.run_video_map_refresh import _DOWNSTREAM_PIPELINE

    def _summary(steps_ran: list[str], aborted_at: str | None) -> str:
        if aborted_at:
            return f"Niche refresh aborted at '{aborted_at}' ({len(steps_ran)} step(s) completed)."
        return f"Refreshed all study caches with the rebuilt niches ({len(steps_ran)} step(s))."

    _run_local_pipeline(
        list(_DOWNSTREAM_PIPELINE), summary_owner="video_map_refresh", summary_fn=_summary
    )


def local_pipeline_script_map() -> dict:
    """Map each downstream pipeline task name to its subprocess script path.

    Used by the local-dev sequential orchestrator (:func:`_run_local_pipeline`).
    Every task that can appear in the consolidate or video-map downstream
    pipeline must have an entry here, or the local pipeline aborts with
    "Unknown step". Exposed at module level so the invariant is unit-testable.
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


def _run_local_pipeline(pipeline: list, summary_owner: str, summary_fn) -> None:
    """Run a downstream refresh pipeline as sequential subprocesses (local dev).

    Sets process_stats['consolidate_enrichment']['pipeline_in_flight'] so the UI
    poll keeps showing stage progress between steps (the semantic-space banner
    and consolidate panel both read that flag), and clears it when done. Writes
    the final summary into ``summary_owner``'s stats entry.

    Args:
        pipeline: Ordered list of ``{"task", "task_args"}`` steps.
        summary_owner: process_stats key to receive the final summary.
        summary_fn: ``(steps_ran, aborted_at) -> str`` summary builder.
    """
    script_map = local_pipeline_script_map()

    total_stages = 1 + len(pipeline)  # the trigger task itself was stage 1
    _set_pipeline_in_flight(True)
    steps_ran: list[str] = []
    aborted_at: str | None = None

    try:
        for i, step in enumerate(pipeline):
            step_name = step["task"]
            step_args = step.get("task_args") or {}
            script_path = script_map.get(step_name)
            if script_path is None:
                print(f"[pipeline] Unknown step {step_name}; aborting.")
                break

            # Build CLI args for the subprocess from task_args.
            cli_args: list = []
            if step_args.get("studies"):
                cli_args += ["--studies", str(step_args["studies"])]
            if step_args.get("collections"):
                cli_args += ["--collections", str(step_args["collections"])]
            if step_args.get("stale_only"):
                cli_args.append("--stale-only")
            if step_args.get("skip_if_busy"):
                cli_args.append("--skip-if-busy")

            stage_index = i + 2  # stage 1 was the trigger task

            success, msg = start_process(
                step_name, script_path, args=cli_args,
                started_by=f"auto-pipeline (after {summary_owner})")
            if not success:
                print(f"[pipeline] Failed to start {step_name}: {msg}")
                break

            # Seed stage info AFTER start_process (which resets progress={}).
            # Subprocess ::PROGRESS:: lines only .update() specific keys, so
            # these stage fields persist until the step finishes.
            if step_name in processes:
                processes[step_name]["progress"].update({
                    "stage_index": stage_index,
                    "stage_total": total_stages,
                    "stage_name": step_name,
                })

            # Wait for monitor_process_completion to fully tear the process
            # down (proc handle cleared) rather than racing with it on
            # proc.wait(). Once proc is None, stats have been written.
            import time as _t
            while processes.get(step_name, {}).get("proc") is not None:
                _t.sleep(0.5)

            outcome = process_stats.get(step_name, {}).get("last_run_outcome")
            if outcome != "Success":
                print(f"[pipeline] Step {step_name} outcome={outcome}; aborting pipeline.")
                aborted_at = step_name
                break
            steps_ran.append(step_name)
    finally:
        # Write the final pipeline summary so the UI has a persistent statement
        # of what the pipeline actually did.
        from datetime import datetime as _dt
        load_process_stats()
        entry = process_stats.get(summary_owner, {})
        entry["last_pipeline_summary"] = summary_fn(steps_ran, aborted_at)
        entry["last_pipeline_summary_ts"] = _dt.now(UTC).isoformat()
        # Structured outcome (mirrors _write_pipeline_summary_cloud) so the UI
        # styles the summary and impact panel correctly in local dev too.
        entry["last_pipeline_partial"] = bool(aborted_at)
        entry["last_pipeline_failed_at"] = aborted_at
        # A fully-successful consolidate downstream pipeline resolves the stored
        # impact — clear it so "Refresh All Affected" stops being offered.
        if not aborted_at and summary_owner == "consolidate_enrichment":
            entry.pop("consolidation_impact", None)
        process_stats[summary_owner] = entry
        save_process_stats()

        # Keep the in-memory ::DATA:: copy in sync with the authoritative entry.
        # The enrichment-stats / step-view endpoints overlay it on top of
        # process_stats, so a lingering consolidation_impact (or stale pipeline
        # flags) here would re-show the impact panel after the pipeline cleared
        # it. No-op on Cloud Run (no in-process subprocess data).
        mem = processes.get(summary_owner, {}).get("data")
        if isinstance(mem, dict):
            mem["last_pipeline_summary"] = entry["last_pipeline_summary"]
            mem["last_pipeline_summary_ts"] = entry["last_pipeline_summary_ts"]
            mem["last_pipeline_partial"] = entry["last_pipeline_partial"]
            mem["last_pipeline_failed_at"] = entry["last_pipeline_failed_at"]
            if not aborted_at and summary_owner == "consolidate_enrichment":
                mem.pop("consolidation_impact", None)

        _set_pipeline_in_flight(False)


def _set_pipeline_in_flight(value: bool) -> None:
    """Flip the pipeline_in_flight flag in process_stats (local-mode helper).

    Mirrors _set_pipeline_in_flight in process_routes.py (the Cloud Tasks
    path). Kept here so the subprocess monitor can set it without importing
    the routes module (which would create a circular import).
    """
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    if value:
        entry["pipeline_in_flight"] = True
    else:
        entry.pop("pipeline_in_flight", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

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
            task failed.  Max 1800s for HTTP targets on the default queue
            config, but can go up to 1800s (30 min) or 3600s with
            appropriate queue settings.
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
                  task_args: dict | None = None, started_by: str = "") -> tuple[bool, str]:
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

    env_vars = os.environ.copy()
    env_vars["WEB_INTERFACE"] = "true"

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
