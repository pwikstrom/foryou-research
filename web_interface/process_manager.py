import json
import os
import subprocess
import threading
from collections import deque
from datetime import UTC, datetime

import fyp.data_io as data_io
from fyp.fyp_config import PROJECT_ROOT, PYTHON_EXEC
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


# Processes eligible for Cloud Tasks dispatch.
CLOUD_TASK_ELIGIBLE = {
    "consolidate_enrichment",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "study_refresh",
    "queue_annotator",
    "queue_annotator_batch",
    "queue_scraper",
    "timelines_refresh",
    "ingest_refresh",
    "aio_fetch",
    "collection_metadata_refresh",
    "collection_delete",
    "benchmark_parquet_read",
    "sequence_refresh",
    "embeddings_refresh",
    "video_map_refresh",
}


# --- Global State ---
# Store process handles and logs
processes = {
    "queue_scraper": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
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
    "embeddings_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "video_map_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
}

process_stats = {}



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




def save_process_stats():
    try:
        data_io.save_json(data=process_stats, storage_location="cache", filename="process_stats.json")
    except Exception as e:
        print(f"Failed to save process stats: {e}")




def enqueue_output(out, queue, process_state):
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
        elif "::DATA::" in line_str:
            try:
                _, json_str = line_str.split("::DATA::", 1)
                data = json.loads(json_str.strip())
                process_state["data"].update(data)
            except Exception:
                queue.append(line_str)
        else:
            queue.append(line_str)
    out.close()

def monitor_process_completion(name, proc):
    """Waits for process to finish and updates stats."""
    proc.wait()

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
    from fyp.fyp_config import (
        EMBEDDINGS_REFRESH_SCRIPT,
        META_REFRESH_GROUPS_SCRIPT,
        PCA_REFRESH_SCRIPT,
        RECODE_REFRESH_STUDIES_SCRIPT,
        TIMELINES_REFRESH_SCRIPT,
    )

    script_map = {
        "recode_refresh_studies": RECODE_REFRESH_STUDIES_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "pca_refresh": PCA_REFRESH_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT,
        "embeddings_refresh": EMBEDDINGS_REFRESH_SCRIPT,
    }

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

            stage_index = i + 2  # stage 1 was the trigger task

            success, msg = start_process(step_name, script_path, args=cli_args)
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
        process_stats[summary_owner] = entry
        save_process_stats()
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
            # Fallback: construct from K_SERVICE (only works with default URLs)
            region = location.replace("australia-", "")  # approximate
            cloud_run_url = f"https://{service_url}-powk2i6raq-ts.a.run.app"

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




def start_process(name: str, script_path, args: list = [], study_name: str | None = None,
                  task_args: dict | None = None) -> tuple[bool, str]:
    """Start a background process. Uses Cloud Tasks on Cloud Run for eligible processes,
    otherwise falls back to subprocess."""

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
            if updated_str:
                try:
                    updated_at = datetime.fromisoformat(updated_str)
                    age = (datetime.now(UTC) - updated_at).total_seconds()
                    is_stale = age > 600  # 10 min without heartbeat = likely dead
                except (ValueError, TypeError):
                    pass
            if not is_stale:
                return False, "Process already running"

        # Build task args from the CLI args list
        if task_args is None:
            task_args = _cli_args_to_dict(name, args, study_name)

        # Set dispatch_deadline for self-chaining processes
        deadline = None
        if name == "queue_annotator" and task_args:
            batch_size = int(task_args.get("batch_size", 500))
            deadline = 3600 if batch_size > 1000 else 1800
        elif name == "queue_annotator_batch":
            # Submit + poll phases are short relative to the (async) job itself;
            # poll re-chains on its own wall-clock budget.
            deadline = 1800
        elif name == "queue_scraper":
            deadline = 1800

        success, msg = _dispatch_cloud_task(name, task_args,
                                            dispatch_deadline_seconds=deadline)
        if success:
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
        processes[name]["progress"] = {}
        processes[name]["last_message"] = ""
        # Reset emitted data too — otherwise the in-memory ::DATA:: payload
        # from a previous run leaks into /api/status until the new worker
        # emits its own, making the UI show stale values (e.g. the
        # Consolidation Impact panel carrying the prior run's impact).
        _ta = task_args if task_args else _cli_args_to_dict(name, args, study_name)
        # Keep the full task_args dict in memory so monitor_process_completion
        # can inspect flags like auto_refresh / pipeline_remaining / stage info
        # after the subprocess exits. The UI only reads a few specific fields
        # (batch_size, max_batches) so the extra keys are harmless.
        processes[name]["data"] = {
            "task_args": dict(_ta),
        }

        # Start logging thread
        t = threading.Thread(target=enqueue_output, args=(proc.stdout, processes[name]["logs"], processes[name]))
        t.daemon = True
        t.start()

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
    if task_args.get("hours_back") is not None:
        out += ["--hours-back", str(task_args["hours_back"])]
    if task_args.get("collection_id"):
        out += ["--collection-id", str(task_args["collection_id"])]
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
        return True, "Stopped"
    # Process handle already cleared (e.g. by monitor thread) — clean up state
    if processes[name]["status"] in ("running", "stopping"):
        processes[name]["status"] = "stopped"
        processes[name]["start_time"] = None
        _clear_graceful_stop(name)
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
