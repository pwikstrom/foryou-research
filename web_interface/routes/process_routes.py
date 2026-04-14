import threading
import traceback

from flask import Blueprint, jsonify, request
from flask_login import login_required
import web_interface.auth as auth
import fyp.data_io as data_io
from fyp.fyp_config import (
    QUEUE_SCRAPER_SCRIPT, QUEUE_ANNOTATOR_SCRIPT, META_REFRESH_VIEWER_SCRIPT,
    META_REFRESH_GROUPS_SCRIPT, TIMELINES_REFRESH_SCRIPT, RECODE_REFRESH_STUDIES_SCRIPT,
    PCA_REFRESH_SCRIPT, CONSOLIDATE_ENRICHMENT_SCRIPT
)
from ..process_manager import (
    processes, process_stats, load_process_stats, save_process_stats,
    start_process, stop_process, graceful_stop_process,
    CLOUD_TASK_ELIGIBLE,
)
from ..task_status import (
    is_cloud_run, read_task_status, GCSStatusReporter,
)

process_bp = Blueprint('process_bp', __name__)

@process_bp.route('/api/start/<name>', methods=['POST'])
@auth.admin_required
def api_start(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    data = request.json or {}
    args = []
    
    if "study_name" in data:
        args.append(data["study_name"])

    if name in ["downloader", "annotator", "queue_scraper", "queue_annotator"]:
        if data.get("batch_size") and str(data["batch_size"]).strip():
             args.extend(["--batch-size", str(data["batch_size"])])
        if data.get("max_batches") and str(data["max_batches"]).strip():
             args.extend(["--max-batches", str(data["max_batches"])])

    if name == "timelines_refresh" and data.get("collections"):
        args.extend(["--collections", str(data["collections"])])
    if name in ["recode_refresh_studies", "pca_refresh"] and data.get("studies"):
        args.extend(["--studies", str(data["studies"])])

    study_name = data.get("study_name") 

    script_map = {
        "queue_scraper": QUEUE_SCRAPER_SCRIPT,
        "queue_annotator": QUEUE_ANNOTATOR_SCRIPT,
        "meta_refresh_viewer": META_REFRESH_VIEWER_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT,
        "recode_refresh_studies": RECODE_REFRESH_STUDIES_SCRIPT,
        "pca_refresh": PCA_REFRESH_SCRIPT,
        "consolidate_enrichment": CONSOLIDATE_ENRICHMENT_SCRIPT
    }
    
    success, msg = start_process(name, script_map[name], args, study_name=study_name)
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409


@process_bp.route('/api/stop/<name>', methods=['POST'])
@auth.admin_required
def api_stop(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    success, msg = stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})


@process_bp.route('/api/stop_graceful/<name>', methods=['POST'])
@auth.admin_required
def api_stop_graceful(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400

    success, msg = graceful_stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})


@process_bp.route('/api/status', methods=['GET'])
@login_required
def api_status():
    # Reload process_stats from GCS so we see task-runner writes
    if is_cloud_run():
        load_process_stats()

    status_data = {}

    # study_refresh uses keyed status files (study_refresh__<study>).
    # Scan GCS for any running study_refresh task to surface in the global badge.
    _study_refresh_gcs = None
    if is_cloud_run():
        try:
            bucket = data_io.fyp_cf['data_io'].get('bucket')
            gcs_prefix = data_io.fyp_cf['gcs_paths'].get('cache', '')
            if bucket and gcs_prefix:
                prefix = f"{gcs_prefix}/task_status/study_refresh__"
                for blob in bucket.list_blobs(prefix=prefix):
                    sr_status = read_task_status(
                        blob.name.split("/")[-1].replace(".json", "")
                    )
                    if sr_status and sr_status.get("state") == "running":
                        _study_refresh_gcs = sr_status
                        break
        except Exception:
            pass

    for name, p_data in processes.items():
        gcs_status = None

        # Cloud Tasks path: read status from GCS for eligible processes
        if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
            if name == "study_refresh":
                gcs_status = _study_refresh_gcs
            else:
                gcs_status = read_task_status(name)
            if gcs_status and gcs_status.get("state") == "running":
                # Check for stale status (task timed out without updating)
                updated_str = gcs_status.get("updated_at", "")
                if updated_str:
                    from datetime import datetime, timezone
                    try:
                        updated_at = datetime.fromisoformat(updated_str)
                        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                        if age > 600:  # 10 min without heartbeat = likely dead
                            gcs_status = None
                    except (ValueError, TypeError):
                        pass

            if gcs_status and gcs_status.get("state") == "running":
                stats_entry = process_stats.get(name, {})
                status_data[name] = {
                    "state": gcs_status["state"],
                    "progress": gcs_status.get("progress", {}),
                    "data": gcs_status.get("data", {}),
                    "start_time": gcs_status.get("start_time"),
                    "last_message": gcs_status.get("progress", {}).get("message", ""),
                    "last_success": stats_entry.get("last_success"),
                    "last_run_end_time": stats_entry.get("last_run_end_time"),
                    "last_run_duration": stats_entry.get("last_run_duration"),
                    "last_run_outcome": stats_entry.get("last_run_outcome"),
                    "last_run_study": stats_entry.get("last_run_study"),
                }
                continue

        # Subprocess path (local dev + non-eligible + idle Cloud Tasks processes)
        state = p_data["status"]
        if p_data["proc"]:
            if p_data["proc"].poll() is not None:
                if state == "running":
                    state = "stopped"

        stats_entry = process_stats.get(name, {})
        if gcs_status:
            # Completed/failed Cloud Task: merge GCS emitted data with process_stats
            progress_field = gcs_status.get("progress", {})
            data_field = {**stats_entry, **gcs_status.get("data", {})}
        else:
            progress_field = p_data["progress"]
            data_field = p_data["data"]

        status_data[name] = {
            "state": state,
            "progress": progress_field,
            "data": data_field,
            "start_time": p_data["start_time"],
            "last_message": p_data.get("last_message", ""),
            "last_success": stats_entry.get("last_success"),
            "last_run_end_time": stats_entry.get("last_run_end_time"),
            "last_run_duration": stats_entry.get("last_run_duration"),
            "last_run_outcome": stats_entry.get("last_run_outcome"),
            "last_run_study": stats_entry.get("last_run_study"),
        }
    return jsonify(status_data)


@process_bp.route('/api/status/study_refresh/<study_name>', methods=['GET'])
@login_required
def api_study_refresh_status(study_name: str):
    """Get status of a single-study refresh task."""
    status_key = f"study_refresh__{study_name}"

    if is_cloud_run():
        gcs_status = read_task_status(status_key)
        if gcs_status:
            # Stale detection
            if gcs_status.get("state") == "running":
                updated_str = gcs_status.get("updated_at", "")
                if updated_str:
                    from datetime import datetime, timezone
                    try:
                        updated_at = datetime.fromisoformat(updated_str)
                        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                        if age > 600:
                            gcs_status["state"] = "failed"
                            gcs_status["error"] = "Task timed out"
                    except (ValueError, TypeError):
                        pass

            stats_entry = process_stats.get(status_key, {})
            return jsonify({
                "state": gcs_status.get("state", "unknown"),
                "progress": gcs_status.get("progress", {}),
                "data": gcs_status.get("data", {}),
                "last_run_outcome": stats_entry.get("last_run_outcome"),
            })

    return jsonify({"state": "unknown"})


@process_bp.route('/api/logs/clear/<name>', methods=['POST'])
@auth.admin_required
def api_clear_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    processes[name]["logs"].clear()
    return jsonify({"status": "success"})


@process_bp.route('/api/logs/<name>', methods=['GET'])
@login_required
def api_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400

    # Cloud Tasks path: return logs from GCS status file
    if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
        gcs_status = read_task_status(name)
        if gcs_status:
            log_lines = gcs_status.get("logs", [])
            return jsonify({"logs": "\n".join(log_lines)})

    # Subprocess path
    logs = list(processes[name]["logs"])
    return jsonify({"logs": "".join(logs)})




# ---------------------------------------------------------------------------
# Internal endpoint: receives Cloud Tasks HTTP requests.
# Lives in a separate blueprint so it can be fully CSRF-exempted.
# ---------------------------------------------------------------------------

internal_bp = Blueprint('internal_bp', __name__)

# Registry of task functions for Cloud Tasks execution.
_task_functions_loaded = False
TASK_FUNCTIONS: dict[str, callable] = {}


def _ensure_task_functions_loaded() -> None:
    """Lazily load task functions on first use."""
    global _task_functions_loaded
    if _task_functions_loaded:
        return
    _task_functions_loaded = True

    from web_interface.run_consolidate_enrichment import run_consolidate_enrichment
    from web_interface.run_recode_refresh_studies import run_recode_refresh_studies
    from web_interface.run_meta_refresh_viewer import run_meta_refresh_viewer
    from web_interface.run_meta_refresh_groups import run_meta_refresh_groups
    from web_interface.run_pca_refresh import run_pca_refresh
    from web_interface.run_study_refresh import run_study_refresh
    from web_interface.run_queue_annotator import run_queue_annotator
    from web_interface.run_queue_scraper import run_queue_scraper
    from web_interface.run_timelines_refresh import run_timelines_refresh

    TASK_FUNCTIONS.update({
        "consolidate_enrichment": run_consolidate_enrichment,
        "recode_refresh_studies": run_recode_refresh_studies,
        "meta_refresh_viewer": run_meta_refresh_viewer,
        "meta_refresh_groups": run_meta_refresh_groups,
        "pca_refresh": run_pca_refresh,
        "study_refresh": run_study_refresh,
        "queue_annotator": run_queue_annotator,
        "queue_scraper": run_queue_scraper,
        "timelines_refresh": run_timelines_refresh,
    })


def _get_status_key(name: str, task_args: dict) -> str:
    """Get the GCS status key for a task. For study_refresh, includes the study name
    so multiple studies can refresh concurrently."""
    if name == "study_refresh":
        study_name = task_args.get("study_name", "unknown")
        return f"study_refresh__{study_name}"
    return name


def _run_task_with_stats(name: str, task_args: dict) -> None:
    """Execute a task function and update process_stats on completion.

    If the task function returns a dict with ``chain=True``, a follow-up
    Cloud Task is dispatched and the reporter is kept in "running" state
    (no complete/stats write) so the next link inherits the same status key.
    """
    from datetime import datetime, timezone
    from ..process_manager import _dispatch_cloud_task

    status_key = _get_status_key(name, task_args)
    reporter = GCSStatusReporter(status_key)

    # Chain continuation: resume from existing GCS state so progress is preserved
    if task_args.get("chunk_index", 0) > 0:
        reporter.resume()
    else:
        reporter.start()

    start_time = datetime.now(timezone.utc)
    study_name = task_args.get("study_name")
    chain_result: dict | None = None

    try:
        _ensure_task_functions_loaded()
        task_func = TASK_FUNCTIONS[name]
        chain_result = task_func(reporter=reporter, task_args=task_args)

        if isinstance(chain_result, dict) and chain_result.get("chain"):
            # Dispatch next Cloud Task in the chain.
            # Do NOT call reporter.complete() — the next task inherits the
            # same GCS status key and keeps reporting as "running".
            next_args = chain_result["next_task_args"]
            deadline = chain_result.get("dispatch_deadline_seconds")
            success, msg = _dispatch_cloud_task(name, next_args,
                                                dispatch_deadline_seconds=deadline)
            if success:
                reporter.log(f"Chained to next batch: {msg}")
            else:
                reporter.fail(f"Chain dispatch failed: {msg}")
            # Stop the heartbeat so it doesn't race with the next chain
            # link's reporter writing to the same GCS status file.
            reporter._stop_heartbeat()
            # Return without writing completion stats — the chain
            # continues (on success) or the failure is recorded above.
            return

        reporter.complete()
        outcome = "Success"
    except Exception as e:
        reporter.fail(f"{e}\n{traceback.format_exc()}")
        outcome = "Fail"
        chain_result = None

    # Update process_stats (same logic as monitor_process_completion)
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    load_process_stats()
    merged = {**process_stats.get(status_key, {}), **reporter._status.get("data", {})}
    merged.update({
        "last_success": end_time.isoformat() if outcome == "Success" else merged.get("last_success"),
        "last_run_end_time": end_time.isoformat(),
        "last_run_duration": duration,
        "last_run_outcome": outcome,
        "last_run_study": study_name,
    })
    process_stats[status_key] = merged
    save_process_stats()


@internal_bp.route('/internal/run-task/<name>', methods=['POST'])
def internal_run_task(name: str):
    """Endpoint called by Google Cloud Tasks to execute a background task.
    The internal_bp blueprint is CSRF-exempted since Cloud Tasks authenticates
    via OIDC token, not browser cookies."""

    # Validate the request comes from Cloud Tasks (OIDC token present)
    auth_header = request.headers.get("Authorization", "")
    if is_cloud_run() and not auth_header.startswith("Bearer "):
        return "Unauthorized", 401

    _ensure_task_functions_loaded()
    if name not in TASK_FUNCTIONS:
        return jsonify({"error": f"Unknown task: {name}"}), 404

    task_args = request.json or {}

    # Run synchronously -- Cloud Tasks will wait for the response.
    _run_task_with_stats(name, task_args)

    return "OK", 200

