import traceback
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

import fyp.data_io as data_io
import web_interface.auth as auth
from fyp.fyp_config import (
    CONSOLIDATE_ENRICHMENT_SCRIPT,
    EMBEDDINGS_REFRESH_SCRIPT,
    META_REFRESH_GROUPS_SCRIPT,
    PCA_REFRESH_SCRIPT,
    QUEUE_ANNOTATOR_SCRIPT,
    QUEUE_SCRAPER_SCRIPT,
    RECODE_REFRESH_STUDIES_SCRIPT,
    TIMELINES_REFRESH_SCRIPT,
    VIDEO_MAP_REFRESH_SCRIPT,
)

from ..process_manager import (
    CLOUD_TASK_ELIGIBLE,
    graceful_stop_process,
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
    stop_process,
)
from ..task_status import (
    GCSStatusReporter,
    is_cloud_run,
    read_task_status,
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

    if name in ["downloader", "annotator", "queue_scraper", "queue_annotator", "embeddings_refresh"]:
        if data.get("batch_size") and str(data["batch_size"]).strip():
             args.extend(["--batch-size", str(data["batch_size"])])
        if data.get("max_batches") and str(data["max_batches"]).strip():
             args.extend(["--max-batches", str(data["max_batches"])])

    if name == "timelines_refresh" and data.get("collections"):
        args.extend(["--collections", str(data["collections"])])
    if name in ["recode_refresh_studies", "pca_refresh"] and data.get("studies"):
        args.extend(["--studies", str(data["studies"])])
    if name == "recode_refresh_studies" and data.get("force_full_rebuild"):
        args.append("--force")

    if name == "video_map_refresh":
        # A map rebuild remaps every video's niche, so default to refreshing all
        # study caches afterwards (the new niches must reach the analysis tabs).
        # Callers can opt out with {"auto_refresh": false}.
        if data.get("auto_refresh", True):
            args.append("--auto-refresh")
        for flag, key in (
            ("--n-niches", "n_niches"),
            ("--map-sample", "map_sample"),
            ("--pca-dim", "pca_dim"),
        ):
            if data.get(key) and str(data[key]).strip():
                args.extend([flag, str(data[key])])

    study_name = data.get("study_name") 

    script_map = {
        "queue_scraper": QUEUE_SCRAPER_SCRIPT,
        "queue_annotator": QUEUE_ANNOTATOR_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT,
        "recode_refresh_studies": RECODE_REFRESH_STUDIES_SCRIPT,
        "pca_refresh": PCA_REFRESH_SCRIPT,
        "consolidate_enrichment": CONSOLIDATE_ENRICHMENT_SCRIPT,
        "embeddings_refresh": EMBEDDINGS_REFRESH_SCRIPT,
        "video_map_refresh": VIDEO_MAP_REFRESH_SCRIPT
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
                    from datetime import datetime
                    try:
                        updated_at = datetime.fromisoformat(updated_str)
                        age = (datetime.now(UTC) - updated_at).total_seconds()
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
                    "task_args": gcs_status.get("task_args", {}),
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
            "task_args": p_data.get("data", {}).get("task_args", {}),
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
                    from datetime import datetime
                    try:
                        updated_at = datetime.fromisoformat(updated_str)
                        age = (datetime.now(UTC) - updated_at).total_seconds()
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

    # Local dev: read the in-process status dict populated by the background
    # thread spawned from save_study.
    from web_interface.task_status import read_local_thread_status
    local_status = read_local_thread_status(status_key)
    if local_status:
        return jsonify({
            "state": local_status.get("state", "unknown"),
            "progress": local_status.get("progress", {}),
            "data": local_status.get("data", {}),
            "last_run_outcome": None,
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

    from web_interface.run_aio_fetch import run_aio_fetch
    from web_interface.run_benchmark_parquet_read import run_benchmark_parquet_read
    from web_interface.run_collection_delete import run_collection_delete
    from web_interface.run_collection_metadata_refresh import run_collection_metadata_refresh
    from web_interface.run_consolidate_enrichment import run_consolidate_enrichment
    from web_interface.run_embeddings_refresh import run_embeddings_refresh
    from web_interface.run_ingest_refresh import run_ingest_refresh
    from web_interface.run_meta_refresh_groups import run_meta_refresh_groups
    from web_interface.run_pca_refresh import run_pca_refresh
    from web_interface.run_queue_annotator import run_queue_annotator
    from web_interface.run_queue_annotator_batch import run_queue_annotator_batch
    from web_interface.run_queue_scraper import run_queue_scraper
    from web_interface.run_recode_refresh_studies import run_recode_refresh_studies
    from web_interface.run_sequence_refresh import run_sequence_refresh
    from web_interface.run_study_refresh import run_study_refresh
    from web_interface.run_timelines_refresh import run_timelines_refresh
    from web_interface.run_video_map_refresh import run_video_map_refresh

    TASK_FUNCTIONS.update({
        "consolidate_enrichment": run_consolidate_enrichment,
        "recode_refresh_studies": run_recode_refresh_studies,
        "meta_refresh_groups": run_meta_refresh_groups,
        "pca_refresh": run_pca_refresh,
        "study_refresh": run_study_refresh,
        "queue_annotator": run_queue_annotator,
        "queue_annotator_batch": run_queue_annotator_batch,
        "queue_scraper": run_queue_scraper,
        "timelines_refresh": run_timelines_refresh,
        "ingest_refresh": run_ingest_refresh,
        "aio_fetch": run_aio_fetch,
        "collection_metadata_refresh": run_collection_metadata_refresh,
        "collection_delete": run_collection_delete,
        "benchmark_parquet_read": run_benchmark_parquet_read,
        "sequence_refresh": run_sequence_refresh,
        "embeddings_refresh": run_embeddings_refresh,
        "video_map_refresh": run_video_map_refresh,
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

    Chain contract:
    - If the task function returns ``{"chain": True, "next_task_args": ...}``,
      a follow-up Cloud Task is dispatched. When ``next_task`` is present,
      it dispatches a different task (cross-task chain, used by the
      consolidate-→-downstream pipeline); otherwise it chains to the same task.
    - When a task completes naturally and ``pipeline_remaining`` in task_args
      is non-empty, the next pipeline step is dispatched. Each step sees its
      own status key; stage metadata is forwarded in task_args and applied
      via ``reporter.set_stage`` so subsequent update_progress calls carry
      pipeline framing automatically.
    """
    from datetime import datetime

    from ..process_manager import _dispatch_cloud_task

    status_key = _get_status_key(name, task_args)
    reporter = GCSStatusReporter(status_key)

    # Apply pipeline stage framing (forwarded from the consolidate dispatcher).
    # This makes every subsequent update_progress call carry stage_* fields
    # without each worker needing to know it's part of a pipeline.
    pipeline_stage_index = task_args.get("pipeline_stage_index")
    pipeline_stage_total = task_args.get("pipeline_stage_total")
    if pipeline_stage_index is not None or name == "consolidate_enrichment":
        reporter.set_stage(
            stage_index=pipeline_stage_index,
            stage_total=pipeline_stage_total,
            stage_name=name,
        )

    # Chain continuation: resume from existing GCS state so progress is preserved
    if task_args.get("chunk_index", 0) > 0:
        reporter.resume()
    else:
        reporter.start()

    start_time = datetime.now(UTC)
    study_name = task_args.get("study_name")
    chain_result: dict | None = None

    try:
        _ensure_task_functions_loaded()
        # Refresh var_schema from disk/GCS if it changed since this task
        # runner last loaded it.  Long-lived task-runner containers would
        # otherwise stick to whichever schema they imported at startup,
        # silently producing stale recodes and stale sidecar hashes after
        # an admin edit on the web service.
        try:
            from fyp.fyp_config import reload_var_schema_if_changed
            reload_var_schema_if_changed()
        except Exception as e:
            print(f"[task {name}] reload_var_schema_if_changed failed: {e}")
        task_func = TASK_FUNCTIONS[name]
        chain_result = task_func(reporter=reporter, task_args=task_args)

        if isinstance(chain_result, dict) and chain_result.get("chain"):
            # Dispatch next Cloud Task in the chain. ``next_task`` (if
            # provided) switches to a different task type — used by the
            # consolidate → downstream-refresh pipeline. Same-task chains
            # (scraper/annotator batching) inherit the current task name
            # and status key.
            next_task_name = chain_result.get("next_task") or name
            next_args = chain_result["next_task_args"]
            deadline = chain_result.get("dispatch_deadline_seconds")
            cross_task = next_task_name != name

            if cross_task:
                # Cross-task chain: complete the current status file so the
                # step shows as Success, then dispatch a fresh task.
                reporter.complete()
                outcome = "Success"
                success, msg = _dispatch_cloud_task(next_task_name, next_args,
                                                    dispatch_deadline_seconds=deadline)
                if success:
                    print(f"[{name}] Pipeline: dispatched {next_task_name}: {msg}")
                    # Mark the pipeline as in-flight so the UI keeps polling
                    # through the gap between steps (step N completes before
                    # step N+1 boots and writes its own "running" status).
                    _set_pipeline_in_flight(True)
                else:
                    print(f"[{name}] Pipeline dispatch of {next_task_name} failed: {msg}")
                    _set_pipeline_in_flight(False)
                # Fall through to stats update (below) with outcome=Success.
                chain_result = None
            else:
                # Same-task chain: keep the status file "running" so the
                # next link inherits it. Forward any pipeline metadata from
                # the incoming task_args so self-chains don't lose pipeline
                # context (e.g. timelines_refresh batching across many
                # collections while inside the consolidate pipeline).
                for k in ("pipeline_remaining", "pipeline_stage_total", "pipeline_stage_index"):
                    if k in task_args and k not in next_args:
                        next_args[k] = task_args[k]
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
        else:
            reporter.complete()
            outcome = "Success"
    except Exception as e:
        reporter.fail(f"{e}\n{traceback.format_exc()}")
        outcome = "Fail"
        chain_result = None

    # Update process_stats (same logic as monitor_process_completion)
    end_time = datetime.now(UTC)
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

    # ---- Pipeline advance: after a step completes successfully, dispatch the
    # next step in the pipeline (if any). Failures abort the pipeline.
    if outcome == "Success":
        pipeline_remaining = task_args.get("pipeline_remaining") or []
        if pipeline_remaining:
            next_step = pipeline_remaining[0]
            next_remaining = pipeline_remaining[1:]
            next_name = next_step["task"]
            next_args = dict(next_step.get("task_args") or {})
            next_args["pipeline_remaining"] = next_remaining
            next_args["pipeline_stage_total"] = pipeline_stage_total
            next_args["pipeline_stage_index"] = (
                int(pipeline_stage_index or 1) + 1 if pipeline_stage_index is not None else None
            )

            success, msg = _dispatch_cloud_task(next_name, next_args)
            if success:
                print(f"[{name}] Pipeline: advanced to {next_name}: {msg}")
                _set_pipeline_in_flight(True)
            else:
                print(f"[{name}] Pipeline advance to {next_name} failed: {msg}")
                _set_pipeline_in_flight(False)
                _write_pipeline_summary_cloud(partial=True, failed_at=next_name)
        elif pipeline_stage_index is not None:
            # This was the final step of a pipeline and it succeeded.
            _set_pipeline_in_flight(False)
            _write_pipeline_summary_cloud(partial=False)
    else:
        # Step failed — pipeline (if any) is aborted. Log so the failure is
        # visible in the task runner's console.
        if task_args.get("pipeline_remaining") or pipeline_stage_index is not None:
            print(f"[{name}] Step failed; aborting pipeline.")
            _set_pipeline_in_flight(False)
            _write_pipeline_summary_cloud(partial=True, failed_at=name)


def _write_pipeline_summary_cloud(partial: bool = False, failed_at: str | None = None) -> None:
    """Write a human-readable pipeline summary into consolidate_stats.

    Called at the end of the Cloud Tasks pipeline chain. Inspects each
    downstream step's last_run_end_time against the consolidate step's
    last_run_end_time to determine which steps ran as part of this
    pipeline; a step "ran" when its end_time is newer.
    """
    from web_interface.run_consolidate_enrichment import build_pipeline_summary

    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    impact = entry.get("consolidation_impact")
    consol_end = entry.get("last_run_end_time")
    consol_end_dt = None
    if consol_end:
        try:
            consol_end_dt = datetime.fromisoformat(consol_end)
        except (ValueError, TypeError):
            pass

    candidate_steps = [
        "recode_refresh_studies",
        "meta_refresh_groups",
        "pca_refresh",
        "timelines_refresh",
        "embeddings_refresh",
    ]
    steps_ran: list[str] = []
    for step in candidate_steps:
        step_end = process_stats.get(step, {}).get("last_run_end_time")
        if not step_end or not consol_end_dt:
            continue
        try:
            step_end_dt = datetime.fromisoformat(step_end)
        except (ValueError, TypeError):
            continue
        if step_end_dt > consol_end_dt:
            steps_ran.append(step)

    summary = build_pipeline_summary(impact, steps_ran)
    if partial:
        suffix = f" (Pipeline aborted at '{failed_at}'.)" if failed_at else " (Pipeline aborted.)"
        summary = summary + suffix
    entry["last_pipeline_summary"] = summary
    entry["last_pipeline_summary_ts"] = datetime.now(UTC).isoformat()
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()


def _set_pipeline_in_flight(value: bool) -> None:
    """Record whether a consolidate→downstream pipeline is currently in flight.

    Used by the UI to keep polling across the brief gap between one step
    completing and the next step's Cloud Task booting up and writing a
    'running' status file. Stored under the consolidate_enrichment stats
    entry so the enrichment-stats endpoint can read it without extra state.
    """
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    if value:
        entry["pipeline_in_flight"] = True
    else:
        entry.pop("pipeline_in_flight", None)
    process_stats["consolidate_enrichment"] = entry
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

