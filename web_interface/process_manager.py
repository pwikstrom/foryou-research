import subprocess
import threading
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
from fyp.fyp_config import PROJECT_ROOT, PYTHON_EXEC
import fyp.data_io as data_io
from web_interface.task_status import (
    is_cloud_run, read_task_status, write_cancel_request,
)


GRACEFUL_STOP_DIR = PROJECT_ROOT / "tmp" / "graceful_stop"


# Processes eligible for Cloud Tasks dispatch.
CLOUD_TASK_ELIGIBLE = {
    "consolidate_enrichment",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "study_refresh",
    "queue_annotator",
    "queue_scraper",
    "timelines_refresh",
}


# --- Global State ---
# Store process handles and logs
processes = {
    "queue_scraper": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "queue_annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "meta_refresh_groups": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "timelines_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "recode_refresh_studies": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "pca_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "consolidate_enrichment": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "study_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
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
    
    end_time = datetime.now(timezone.utc)
    start_time_str = processes[name].get("start_time")
    duration = 0
    if start_time_str:
        start_time = datetime.fromisoformat(start_time_str)
        duration = (end_time - start_time).total_seconds()

    outcome = "Success" if proc.returncode == 0 else "Fail"
    study_name = processes[name].get("study_name")

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

def _dispatch_cloud_task(name: str, task_args: dict,
                         dispatch_deadline_seconds: int | None = None) -> tuple[bool, str]:
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

        response = client.create_task(parent=parent, task=task)
        print(f"[CloudTasks] Dispatched task for {name}: {response.name}")
        return True, "Task dispatched"

    except Exception as e:
        print(f"[CloudTasks] Failed to dispatch {name}: {e}")
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
                    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
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
            placeholder._status["start_time"] = datetime.now(timezone.utc).isoformat()
            placeholder._status["progress"] = {"percent": 0, "message": "Starting..."}
            placeholder._write_status(force=True)
        return success, msg

    # Subprocess path (local dev + non-eligible processes on Cloud Run)
    if processes[name]["proc"] is not None:
        if processes[name]["proc"].poll() is None:
            return False, "Process already running"

    env_vars = os.environ.copy()
    env_vars["WEB_INTERFACE"] = "true"

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
        processes[name]["start_time"] = datetime.now(timezone.utc).isoformat()
        processes[name]["study_name"] = study_name
        processes[name]["progress"] = {}
        processes[name]["last_message"] = ""
        # Reset emitted data too — otherwise the in-memory ::DATA:: payload
        # from a previous run leaks into /api/status until the new worker
        # emits its own, making the UI show stale values (e.g. the
        # Consolidation Impact panel carrying the prior run's impact).
        processes[name]["data"] = {}

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
        elif not arg.startswith("--"):
            # Positional arg (study_name)
            if "study_name" not in task_args:
                task_args["study_name"] = arg
            i += 1
        else:
            i += 1

    return task_args


def stop_process(name: str) -> tuple[bool, str]:
    """Stop a running process (hard kill for subprocess, cancel for Cloud Tasks)."""
    # Cloud Tasks path
    if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
        status = read_task_status(name)
        if status and status.get("state") == "running":
            write_cancel_request(name)
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
