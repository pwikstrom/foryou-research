import traceback
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
import web_interface.auth as auth
from fyp.core import logging_setup
from web_interface import activity_log, run_logs, task_failures
from fyp.fyp_config import (
    CONSOLIDATE_ENRICHMENT_SCRIPT,
    DEMO_DATASET_SCRIPT,
    EMBEDDINGS_REFRESH_SCRIPT,
    META_REFRESH_GROUPS_SCRIPT,
    PCA_REFRESH_SCRIPT,
    QUEUE_ANNOTATOR_BATCH_SCRIPT,
    QUEUE_ANNOTATOR_SCRIPT,
    QUEUE_SCRAPER_SCRIPT,
    RECODE_REFRESH_STUDIES_SCRIPT,
    SESSIONS_REFRESH_SCRIPT,
    TIMELINES_REFRESH_SCRIPT,
    VIDEO_MAP_REFRESH_SCRIPT,
)

from ..permissions import user_has_permission
from ..process_manager import (
    CLOUD_TASK_ELIGIBLE,
    SCRAPER_PROCESS_NAMES,
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
    stamp_task_status,
)

process_bp = Blueprint('process_bp', __name__)

# A forked leaf (meta/pca/timelines) that has not reached a fresh "running" or
# terminal state within this many seconds of the fan-out is treated as having
# failed to start — e.g. a Cloud Run 429 dropped the task. The grace must exceed
# a worst-case cold start so a merely-slow boot is not flagged, AND the queue's
# retry backoff: since 2026-07 the queue re-delivers a dropped task (min-backoff
# 60s, doubling), so a leaf can legitimately boot several minutes late. Declaring
# it failed earlier than that would write a "pipeline aborted" summary for a run
# that then goes on to succeed.
FORK_START_GRACE_SECONDS = 600


# Tasks that are safe for the QUEUE to retry after a failed attempt: pure
# recomputations that rewrite their artifacts from source data, so a partial
# run leaves nothing to reconcile. Everything else deliberately stays
# single-attempt — see the reasons below — and its failure goes straight to
# the ledger instead:
#   queue_scraper_* / queue_annotator  — the queue prune is the claim; a retry
#       either re-scrapes/re-annotates (real money) or loses the batch, and
#       circuit-breaker / permanent-storm aborts must never be retried.
#   queue_annotator_batch              — a retried submit could submit (and
#       pay for) the same Gemini batch job twice.
#   consolidate_enrichment             — a retry would double-fire the
#       downstream refresh pipeline.
#   collection_delete                  — destructive and partially-applied.
#   ingest_refresh                     — ledger-guarded but partial writes.
#   embeddings_refresh                 — idempotent per shard, but a retry
#       re-spends embedding credits.
#   ab_eval                            — has its own 409 concurrency gate.
QUEUE_RETRY_SAFE: set[str] = {
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "study_refresh",
    "timelines_refresh",
    "sequence_refresh",
    "sessions_refresh",
    "collection_metadata_refresh",
    "video_map_refresh",
    "retokenise_hashtags",
    "benchmark_parquet_read",
    "aio_fetch",
    # Deterministic generator with fixed output filenames — a retry simply
    # overwrites the same artifacts.
    "demo_dataset",
}

# Total attempts the app is willing to see for a retry-safe task. Must not
# exceed the queue's own --max-attempts (see scripts/configure_task_queue.sh);
# whichever is smaller wins, and the app-side bound is what stops a retry
# storm if the queue is reconfigured upward.
MAX_APP_RETRIES = 4

@process_bp.route('/api/start/<name>', methods=['POST'])
@auth.admin_required
def api_start(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400

    # Refuse to start the annotator when Gemini is not configured — otherwise the
    # worker boots, finds no client, and fails every item. A pure config check
    # (no network); if the import itself fails, google-genai isn't installed, so
    # Gemini is likewise unavailable. Not a 409 (that drives the "already
    # running" dialog), so the client surfaces the reason instead.
    if name in ("queue_annotator", "queue_annotator_batch"):
        try:
            from fyp.annotation.machine_annotation import annotation_configured
            gemini_ok, gemini_reason = annotation_configured()
        except Exception as exc:
            gemini_ok, gemini_reason = False, (
                "Gemini annotation is unavailable: the google-genai library "
                f"could not be loaded ({exc})."
            )
        if not gemini_ok:
            return jsonify({"status": "error", "message": gemini_reason}), 400

    # Same gate for the embeddings worker: refuse to start it when the active
    # embedding backend isn't usable (missing credentials for Gemini, missing
    # deps/model for a local backend). video_map_refresh stays ungated — it
    # only reads the store, and niche naming degrades to term-based labels.
    if name == "embeddings_refresh":
        try:
            from fyp.analysis.embedding_backends import active_backend_name, get_backend
            avail = get_backend(active_backend_name()).availability()
            embed_ok, embed_reason = avail.ok, avail.reason
        except Exception as exc:
            embed_ok, embed_reason = False, f"Embedding backend unavailable: {exc}"
        if not embed_ok:
            return jsonify({"status": "error", "message": embed_reason}), 400

    data = request.json or {}
    args = []
    
    if "study_name" in data:
        args.append(data["study_name"])

    if name in ["downloader", "annotator", "queue_annotator", "queue_annotator_batch", "embeddings_refresh"] or name.startswith("queue_scraper_"):
        # batch_size / max_batches go straight into a worker argv — validate
        # and bound them here rather than trusting the client blindly.
        for key, flag, upper in (("batch_size", "--batch-size", 5000),
                                 ("max_batches", "--max-batches", None)):
            raw = data.get(key)
            if raw is None or not str(raw).strip():
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                return jsonify({"status": "error",
                                "message": f"{key} must be an integer"}), 400
            if value < 1 or (upper is not None and value > upper):
                bound = f"1-{upper}" if upper is not None else ">= 1"
                return jsonify({"status": "error",
                                "message": f"{key} must be {bound}"}), 400
            args.extend([flag, str(value)])

    # Capture the launching user (their username is their email) so the async
    # batch annotator can email them at submit / batch / done milestones. Threaded
    # through task_args and re-emitted across the worker's self-chain.
    if name == "queue_annotator_batch" and getattr(current_user, "username", None):
        args.extend(["--launched-by", str(current_user.username)])

    # The platform is encoded in the process name (queue_scraper_<platform>) —
    # single source of truth for which queue the worker drains.
    if name.startswith("queue_scraper_"):
        args.extend(["--platform", name.removeprefix("queue_scraper_")])

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
        if data.get("reset_labels"):
            args.append("--reset-labels")
        for flag, key in (
            ("--n-niches", "n_niches"),
            ("--map-sample", "map_sample"),
            ("--pca-dim", "pca_dim"),
        ):
            if data.get(key) and str(data[key]).strip():
                args.extend([flag, str(data[key])])

    study_name = data.get("study_name") 

    script_map = {
        **{scraper_name: QUEUE_SCRAPER_SCRIPT for scraper_name in SCRAPER_PROCESS_NAMES},
        "queue_annotator": QUEUE_ANNOTATOR_SCRIPT,
        "queue_annotator_batch": QUEUE_ANNOTATOR_BATCH_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT,
        "recode_refresh_studies": RECODE_REFRESH_STUDIES_SCRIPT,
        "pca_refresh": PCA_REFRESH_SCRIPT,
        "consolidate_enrichment": CONSOLIDATE_ENRICHMENT_SCRIPT,
        "embeddings_refresh": EMBEDDINGS_REFRESH_SCRIPT,
        "video_map_refresh": VIDEO_MAP_REFRESH_SCRIPT,
        "sessions_refresh": SESSIONS_REFRESH_SCRIPT,
        "demo_dataset": DEMO_DATASET_SCRIPT,
    }
    
    success, msg = start_process(name, script_map[name], args, study_name=study_name,
                                 started_by=getattr(current_user, "username", ""))
    if success:
        activity_log.record(
            actor=getattr(current_user, "username", ""),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="start_process",
            target=name,
            details={"args": args, "study_name": study_name},
        )
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409


@process_bp.route('/api/stop/<name>', methods=['POST'])
@auth.admin_required
def api_stop(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    success, msg = stop_process(name)
    if success:
        activity_log.record(
            actor=getattr(current_user, "username", ""),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="stop_process",
            target=name,
        )
    return jsonify({"status": "success" if success else "error", "message": msg})


@process_bp.route('/api/stop_graceful/<name>', methods=['POST'])
@auth.admin_required
def api_stop_graceful(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400

    success, msg = graceful_stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})


def _redact_status_for_viewer(status_data: dict) -> dict:
    """Strip operational detail from the status payload for plain viewers.

    The header badge needs run states for every logged-in user, but
    ``task_args`` and ``last_run_study`` leak study names outside the caller's
    access set — only users holding a Data Management permission (or admins)
    see them.
    """
    is_admin_attr = getattr(current_user, "is_admin", False)
    is_admin = is_admin_attr() if callable(is_admin_attr) else bool(is_admin_attr)
    if is_admin or user_has_permission(current_user, 'tab.data_management'):
        return status_data
    for entry in status_data.values():
        entry.pop("task_args", None)
        entry.pop("last_run_study", None)
    return status_data






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
    return jsonify(_redact_status_for_viewer(status_data))


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


def _resolve_log_key(name: str) -> str | None:
    """Map a URL segment to a durable run-log key, or None when unknown.

    Accepts a plain process name and the keyed form some tasks use for their
    status file (``study_refresh__<study>``) — reading those used to be
    impossible, because the lookup was done with the bare process name and
    always missed.

    Args:
        name: The ``<name>`` segment from the request path.

    Returns:
        A validated storage key, or None when it names no known process or
        would not be safe as a filename.
    """
    if not run_logs.valid_key(name):
        return None
    if name in processes:
        return name
    if "__" in name and name.split("__", 1)[0] in processes:
        return name
    return None




@process_bp.route('/api/logs/clear/<name>', methods=['POST'])
@auth.admin_required
def api_clear_logs(name):
    """Delete a process's whole run history (all retained runs)."""
    key = _resolve_log_key(name)
    if key is None:
        return jsonify({"error": "Unknown process"}), 400

    if key in processes:
        processes[key]["logs"].clear()
    run_logs.clear(key)
    return jsonify({"status": "success"})


@process_bp.route('/api/logs/<name>', methods=['GET'])
@auth.admin_required
def api_logs(name):
    """Return a run's log lines, plus the run list for the modal's picker.

    Query args:
        run: A specific run id; the newest run when omitted.
        since: Cursor from a previous response's ``next_since``, so a polling
            client appends new lines instead of re-downloading the whole log.
    """
    key = _resolve_log_key(name)
    if key is None:
        return jsonify({"error": "Unknown process"}), 400

    run_id = (request.args.get("run") or "").strip()
    try:
        since = max(0, int(request.args.get("since") or 0))
    except (TypeError, ValueError):
        since = 0

    payload = run_logs.read(key, run_id=run_id, since=since)

    if not payload["runs"]:
        # Pre-migration runs, and the deploy window where an old worker is
        # still writing logs into its status file.
        legacy = ""
        if is_cloud_run() and key in CLOUD_TASK_ELIGIBLE:
            gcs_status = read_task_status(key) or {}
            legacy = "\n".join(gcs_status.get("logs", []))
        if not legacy and key in processes:
            legacy = "".join(processes[key]["logs"])
        return jsonify({"logs": legacy, "next_since": 0, "reset": True,
                        "run_id": "", "run": None, "runs": [], "key": key})

    # `logs` stays a newline-joined string: the async-annotator card feed reads
    # this same endpoint and splits on newlines.
    return jsonify({
        "logs": "\n".join(payload["lines"]),
        "next_since": payload["next_since"],
        "reset": payload["reset"],
        "run_id": (payload["run"] or {}).get("run_id", ""),
        "run": payload["run"],
        "runs": payload["runs"],
        "key": key,
    })




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

    from web_interface.run_ab_eval import run_ab_eval
    from web_interface.run_aio_fetch import run_aio_fetch
    from web_interface.run_benchmark_parquet_read import run_benchmark_parquet_read
    from web_interface.run_collection_delete import run_collection_delete
    from web_interface.run_collection_metadata_refresh import run_collection_metadata_refresh
    from web_interface.run_consolidate_enrichment import run_consolidate_enrichment
    from web_interface.run_demo_dataset import run_demo_dataset
    from web_interface.run_embeddings_refresh import run_embeddings_refresh
    from web_interface.run_ingest_refresh import run_ingest_refresh
    from web_interface.run_meta_refresh_groups import run_meta_refresh_groups
    from web_interface.run_pca_refresh import run_pca_refresh
    from web_interface.run_queue_annotator import run_queue_annotator
    from web_interface.run_queue_annotator_batch import run_queue_annotator_batch
    from web_interface.run_queue_scraper import run_queue_scraper
    from web_interface.run_recode_refresh_studies import run_recode_refresh_studies
    from web_interface.run_retokenise_hashtags import run_retokenise_hashtags
    from web_interface.run_sequence_refresh import run_sequence_refresh
    from web_interface.run_sessions_refresh import run_sessions_refresh
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
        # One entry per platform; the bare name is a transition alias so an
        # in-flight chain dispatched before the per-platform rename still runs
        # (it defaults to the contract's default platform).
        **{scraper_name: run_queue_scraper for scraper_name in SCRAPER_PROCESS_NAMES},
        "queue_scraper": run_queue_scraper,
        "timelines_refresh": run_timelines_refresh,
        "ingest_refresh": run_ingest_refresh,
        "aio_fetch": run_aio_fetch,
        "collection_metadata_refresh": run_collection_metadata_refresh,
        "collection_delete": run_collection_delete,
        "benchmark_parquet_read": run_benchmark_parquet_read,
        "sequence_refresh": run_sequence_refresh,
        "sessions_refresh": run_sessions_refresh,
        "embeddings_refresh": run_embeddings_refresh,
        "video_map_refresh": run_video_map_refresh,
        "retokenise_hashtags": run_retokenise_hashtags,
        "ab_eval": run_ab_eval,
        "demo_dataset": run_demo_dataset,
    })


def _get_status_key(name: str, task_args: dict) -> str:
    """Get the GCS status key for a task. For study_refresh, includes the study name
    so multiple studies can refresh concurrently."""
    if name == "study_refresh":
        study_name = task_args.get("study_name", "unknown")
        return f"study_refresh__{study_name}"
    return name


def _pipeline_actor(task_args: dict, parent: str) -> str:
    """Return the attribution string for a step this pipeline dispatched.

    A downstream step is not launched by a person, but it is *traceable* to
    one — so the banner reads e.g. ``patrik (via consolidate_enrichment)``
    rather than losing the trail at the first hop.

    Args:
        task_args: The dispatching step's arguments.
        parent: The name of the dispatching step.
    """
    origin = (task_args.get("started_by") or "").strip()
    if not origin:
        return f"auto-pipeline (after {parent})"
    if "(via " in origin:
        return origin  # already traced; don't nest the annotation
    return f"{origin} (via {parent})"




def _run_task_with_stats(name: str, task_args: dict, retry_count: int = 0) -> bool:
    """Execute a task function and update process_stats on completion.

    Args:
        name: registered task name.
        task_args: the task's arguments.
        retry_count: Cloud Tasks' ``X-CloudTasks-TaskRetryCount`` for this
            attempt (0 on the first try).

    Returns:
        True when the task succeeded (or handed off to a chain link), False
        when it failed. ``internal_run_task`` turns a False into an HTTP 503
        for retry-safe tasks so Cloud Tasks retries them.

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

    # Echo the caller's batch/platform selections back into the status file so the
    # UI can repopulate the (disabled) batch inputs while the task runs. Without
    # this the worker's start()/resume() writes a status with no task_args, so the
    # scraper/annotator cards blank their max-batches box to the "Inf" placeholder
    # the moment the real task-runner instance takes over from the dispatch
    # placeholder — the "resets to Inf" bug seen only on Cloud Run.
    max_batches = task_args.get("max_batches")
    # np.inf serializes to invalid JSON ("Infinity") and would break the poll's
    # response.json(); an unbounded run is best conveyed as null (→ "Inf" placeholder).
    if isinstance(max_batches, float) and max_batches == float("inf"):
        max_batches = None
    surface_task_args = {
        "batch_size": task_args.get("batch_size"),
        "max_batches": max_batches,
        "platform": task_args.get("platform"),
    }
    reporter._status["task_args"] = surface_task_args

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

    # Chain continuation: resume from existing GCS state so progress is preserved.
    # The batch annotator's poll phase re-dispatches itself with chunk_index still
    # 0 for the whole first chunk (submit -> poll -> poll ...); without resuming on
    # phase=="poll" every poll would call start() and wipe the accumulated log
    # buffer, so a single-chunk run would show almost no history in the card feed.
    # Adopt the run opened at dispatch (the web service is the only place that
    # knows who clicked Start), so one click yields one run rather than a
    # dispatch run plus a worker run — and so every link of a self-chain writes
    # into the same continuous log.
    run_logs.attach_run(status_key, run_id=task_args.get("log_run_id", ""),
                        started_by=task_args.get("started_by", ""),
                        task_args=task_args, mode="cloud")

    if task_args.get("chunk_index", 0) > 0 or task_args.get("phase") == "poll":
        reporter.resume()
    else:
        reporter.start()
    # resume() replaces _status wholesale from the prior chain link, so re-assert
    # the surface args (a prior link wrote them, but be explicit) — a no-op write
    # here; the next status write carries them.
    reporter._status["task_args"] = surface_task_args

    start_time = datetime.now(UTC)
    study_name = task_args.get("study_name")
    chain_result: dict | None = None
    # True once a cross-task chain has been dispatched (the dispatch IS this
    # step's hand-off, so the pipeline-advance block below must not also fire).
    dispatched_cross_task = False

    # Tee the fyp package's own narration into the run log. On Cloud Run the
    # task runner is nobody's subprocess, so without this the UI showed only
    # the worker's explicit reporter.log calls — four lines for a consolidation
    # that logs hundreds.
    log_sink = run_logs.ReporterLogHandler(status_key)
    logging_setup.add_sink(log_sink)

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
                dispatched_cross_task = True
            else:
                # Same-task chain: keep the status file "running" so the
                # next link inherits it. Forward any pipeline metadata from
                # the incoming task_args so self-chains don't lose pipeline
                # context (e.g. timelines_refresh batching across many
                # collections while inside the consolidate pipeline). The
                # fanout/leaf/fork-timestamp keys must survive too, so a
                # self-chaining terminal leaf still runs the completion barrier
                # on its final batch.
                # log_run_id and started_by ride along too, so every batch of a
                # self-chaining scraper or annotator appends to one continuous
                # run instead of starting a fresh, unattributed log per link.
                for k in ("pipeline_remaining", "pipeline_stage_total",
                          "pipeline_stage_index", "pipeline_fanout",
                          "pipeline_leaves", "pipeline_fork_ts",
                          "log_run_id", "started_by"):
                    if k in task_args and k not in next_args:
                        next_args[k] = task_args[k]
                success, msg = _dispatch_cloud_task(
                    name, next_args,
                    dispatch_deadline_seconds=deadline,
                    schedule_delay_seconds=chain_result.get("next_dispatch_delay_seconds"),
                )
                if success:
                    reporter.log(f"Chained to next batch: {msg}")
                else:
                    reporter.fail(f"Chain dispatch failed: {msg}")
                    # A broken hand-off used to leave no stats row at all —
                    # the ledger is the only durable trace of it.
                    task_failures.record_failure(
                        task=name, error=f"Chain dispatch failed: {msg}",
                        status_key=status_key, retry_count=retry_count,
                        disposition=task_failures.DISPOSITION_DEAD,
                        task_args=task_args, phase="chain_dispatch",
                    )
                # Stop the heartbeat so it doesn't race with the next chain
                # link's reporter writing to the same GCS status file.
                reporter._stop_heartbeat()
                # Flush and hand the still-open run to the next link. Without
                # this, everything logged since the last throttled write —
                # up to five seconds of it — was dropped on every hop.
                run_logs.detach(status_key)
                # Return without writing completion stats — the chain
                # continues (on success) or the failure is recorded above.
                return success
        else:
            reporter.complete()
            outcome = "Success"
    except Exception as e:
        reporter.fail(f"{e}\n{traceback.format_exc()}")
        outcome = "Fail"
        chain_result = None
        # Retry-safe tasks get another queue attempt until the app-side bound
        # is reached; everything else is terminal on the first failure.
        will_retry = (name in QUEUE_RETRY_SAFE
                      and retry_count < MAX_APP_RETRIES - 1
                      and not reporter.check_cancelled())
        task_failures.record_failure(
            task=name, error=f"{e}\n{traceback.format_exc()}",
            status_key=status_key, retry_count=retry_count,
            disposition=(task_failures.DISPOSITION_RETRYING if will_retry
                         else task_failures.DISPOSITION_DEAD),
            task_args=task_args, phase="run",
        )
    finally:
        # Runs on the chain hop's early return too — a stale sink would keep
        # forwarding the next task's output into this task's run.
        logging_setup.remove_sink(log_sink)

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

    # ---- Pipeline advance. The downstream pipeline is an out-tree: a linear
    # spine (consolidate → embeddings → video_map → recode) that fans out at
    # recode into the independent terminal leaves (meta ‖ pca ‖ timelines). So:
    #   - a spine step advances the single next spine step (pipeline_remaining);
    #   - the recode step fans out to every leaf at once (pipeline_fanout);
    #   - a leaf checks whether it is the last leaf to finish (the barrier) and,
    #     if so, writes the summary;
    #   - a non-leaf failure aborts the pipeline; a leaf failure does not abort
    #     its (independent, already-dispatched) siblings.
    pipeline_remaining = task_args.get("pipeline_remaining") or []
    pipeline_fanout = task_args.get("pipeline_fanout") or []
    pipeline_leaves = task_args.get("pipeline_leaves") or []
    is_leaf = name in pipeline_leaves

    if is_leaf:
        # Terminal leaf (success OR failure): the forked pipeline is finished
        # once every leaf is in a terminal state. This leaf has already written
        # its own terminal status (complete()/fail() above), so the barrier only
        # reads its siblings' own status files — no shared mutable counter, so no
        # lost-update race on process_stats.json.
        _maybe_finish_forked_pipeline(
            pipeline_leaves, fork_ts=task_args.get("pipeline_fork_ts")
        )
    elif outcome == "Success" and not dispatched_cross_task:
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
            next_args["started_by"] = _pipeline_actor(task_args, name)
            next_args["log_run_id"] = run_logs.new_run_id()

            success, msg = _dispatch_cloud_task(next_name, next_args)
            if success:
                print(f"[{name}] Pipeline: advanced to {next_name}: {msg}")
                run_logs.open_run(next_name, run_id=next_args["log_run_id"],
                                  started_by=next_args["started_by"],
                                  task_args=next_args, mode="cloud")
                _set_pipeline_in_flight(True)
            else:
                print(f"[{name}] Pipeline advance to {next_name} failed: {msg}")
                _set_pipeline_in_flight(False)
                _write_pipeline_summary_cloud(partial=True, failed_at=next_name)
        elif pipeline_fanout:
            # Fork point (recode): dispatch every terminal leaf concurrently.
            # The leaves are mutually independent (distinct readers, distinct
            # outputs), so no join is needed for dispatch — only the leaf
            # barrier, below, to detect when the whole fan-out has finished. A
            # single fork timestamp lets each leaf ignore a sibling's stale
            # "completed" status left over from a previous pipeline run.
            fork_ts = datetime.now(UTC).isoformat()
            leaf_stage_index = (
                int(pipeline_stage_index or 1) + 1 if pipeline_stage_index is not None else None
            )
            leaf_stage = {
                "stage_index": leaf_stage_index,
                "stage_total": pipeline_stage_total,
            }
            any_dispatch_failed = False
            for child in pipeline_fanout:
                child_name = child["task"]
                child_args = dict(child.get("task_args") or {})
                child_args["pipeline_leaves"] = pipeline_leaves
                child_args["pipeline_fork_ts"] = fork_ts
                child_args["pipeline_stage_total"] = pipeline_stage_total
                child_args["pipeline_stage_index"] = leaf_stage_index
                child_args["started_by"] = _pipeline_actor(task_args, name)
                child_args["log_run_id"] = run_logs.new_run_id()
                # Stamp the leaf "queued" BEFORE dispatching so its card shows a
                # definitive this-run status (not a stale one from a previous
                # run). When the task actually boots it overwrites this with
                # "running"; if it is dropped (429, no retry) it stays "queued"
                # and the grace check below flips it to "failed".
                stamp_task_status(
                    child_name, "queued", "Queued — waiting for a worker…",
                    stage=leaf_stage,
                )
                success, msg = _dispatch_cloud_task(child_name, child_args)
                if success:
                    print(f"[{name}] Pipeline: forked {child_name}: {msg}")
                    run_logs.open_run(child_name, run_id=child_args["log_run_id"],
                                      started_by=child_args["started_by"],
                                      task_args=child_args, mode="cloud")
                else:
                    print(f"[{name}] Pipeline fork of {child_name} failed: {msg}")
                    stamp_task_status(
                        child_name, "failed",
                        "Couldn't start — the task could not be queued for a worker.",
                        error=f"Dispatch failed: {msg}", stage=leaf_stage,
                    )
                    any_dispatch_failed = True
            # Record the fork so the completion barrier and the status-poll
            # backstop can detect a leaf that was dispatched but never started.
            _record_pipeline_fork(pipeline_leaves, fork_ts)
            if any_dispatch_failed:
                _set_pipeline_in_flight(False)
                _write_pipeline_summary_cloud(partial=True, failed_at=name)
            else:
                _set_pipeline_in_flight(True)
        elif pipeline_stage_index is not None:
            # Final step of a fully-linear pipeline (no fan-out) and it succeeded.
            _set_pipeline_in_flight(False)
            _write_pipeline_summary_cloud(partial=False)
    elif outcome != "Success":
        # A spine step (non-leaf) failed → abort the pipeline. (A leaf failure is
        # handled by the barrier above, which must not abort independent siblings.)
        if pipeline_remaining or pipeline_fanout or pipeline_stage_index is not None:
            print(f"[{name}] Step failed; aborting pipeline.")
            _set_pipeline_in_flight(False)
            _write_pipeline_summary_cloud(partial=True, failed_at=name)

    # Server-side auto-fire of an armed Consolidate & Refresh. Arming promises
    # the pipeline runs once the enrichment queues finish, but the original
    # trigger was a browser poll POST — which fails silently when the tab's CSRF
    # token has expired (queues often run >1h) or the tab is closed. Firing here,
    # on the task-runner at terminal worker completion, makes it browser-independent.
    if name == "queue_annotator" or name.startswith("queue_scraper"):
        _maybe_autofire_armed_consolidate(name)

    return outcome == "Success"


def _maybe_autofire_armed_consolidate(just_finished: str) -> None:
    """Dispatch an armed Consolidate & Refresh once the enrichment queues idle.

    Called from a terminal ``queue_scraper`` / ``queue_annotator`` completion on
    the task-runner. No-ops unless ``consolidate_enrichment.auto_armed`` is set.
    Defers while the *other* enrichment worker is still running (that worker runs
    this same check when it finishes) and while a consolidate is already in
    flight. The armed flag is cleared before dispatch so two near-simultaneous
    finishers can't both fire; a failed dispatch re-arms for a later retry.

    Args:
        just_finished: The worker whose completion triggered this check.
    """
    from ..process_manager import _dispatch_cloud_task

    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    if not entry.get("auto_armed"):
        return

    # The other enrichment workers may still be running on separate task-runner
    # instances — read their GCS status (single source of truth across instances).
    others = [w for w in SCRAPER_PROCESS_NAMES + ["queue_annotator"] if w != just_finished]
    for worker in others:
        st = read_task_status(worker) or {}
        if (st.get("state") or "").lower() == "running":
            updated = st.get("updated_at") or ""
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds()
                if age <= 600:
                    return
            except (ValueError, TypeError):
                return  # Malformed heartbeat — treat as running, be safe.

    # Don't double-fire onto an already-running consolidate.
    cs = read_task_status("consolidate_enrichment") or {}
    if (cs.get("state") or "").lower() == "running":
        return

    # Defer while a local scrape-queue drain holds a lease on the shared
    # storage (its queue prunes would race the consolidation). The arm stays
    # set, so the next worker completion — or a manual trigger — re-checks.
    try:
        from web_interface import drain_lease
        if drain_lease.active_drain_leases():
            print(f"[{just_finished}] Armed consolidate deferred: local drain lease active.")
            return
    except Exception:
        pass

    # Claim the armed flag (clear before dispatch) so a concurrent finisher
    # observing the same idle state can't also fire.
    force = bool(entry.get("auto_armed_force"))
    auto_refresh = bool(entry.get("auto_armed_auto_refresh"))
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    task_args: dict = {}
    if force:
        task_args["force_consolidation"] = True
    if auto_refresh:
        task_args["auto_refresh"] = True

    success, msg = _dispatch_cloud_task("consolidate_enrichment", task_args)
    if success:
        print(f"[{just_finished}] Armed Consolidate & Refresh fired: {msg}")
        _set_pipeline_in_flight(True)
    else:
        print(f"[{just_finished}] Armed consolidate dispatch failed: {msg}")
        # Re-arm so the other finisher or a manual trigger can retry.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry["auto_armed"] = True
        entry["auto_armed_force"] = force
        entry["auto_armed_auto_refresh"] = auto_refresh
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()


def _record_pipeline_fork(leaves: list[str], fork_ts: str) -> None:
    """Persist the active fan-out so the status-poll backstop can resolve it.

    Stored on the consolidate_enrichment stats entry as ``pipeline_fork`` =
    ``{"leaves": [...], "fork_ts": "..."}``. Cleared by the barrier when the
    fan-out finishes (see :func:`_maybe_finish_forked_pipeline`).
    """
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry["pipeline_fork"] = {"leaves": list(leaves), "fork_ts": fork_ts}
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()


def _clear_pipeline_fork() -> None:
    """Remove the recorded fan-out once it has finished (or been resolved)."""
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    if "pipeline_fork" in entry:
        entry.pop("pipeline_fork", None)
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()


def resolve_forked_pipeline() -> None:
    """Status-poll backstop: resolve a fan-out whose leaf-completion events have
    all fired but a dropped leaf still left it un-finalized.

    The barrier (:func:`_maybe_finish_forked_pipeline`) is event-driven — it runs
    when a leaf completes. If every surviving leaf finishes BEFORE the grace
    window elapses, no later event re-checks the dropped leaf, so the fan-out
    would hang. The web-service status poll calls this on each tick; once the
    grace window passes it flips the dropped leaf to "failed" and finalizes.
    """
    load_process_stats()
    fork = process_stats.get("consolidate_enrichment", {}).get("pipeline_fork")
    if not fork:
        return
    _maybe_finish_forked_pipeline(fork.get("leaves") or [], fork.get("fork_ts"))


def _maybe_finish_forked_pipeline(leaves: list[str], fork_ts: str | None = None) -> None:
    """Finalize the fan-out once every forked leaf has reached a terminal state.

    Called both by each completing leaf (event-driven) and by the status-poll
    backstop (time-driven). Completion is read from each leaf's own GCS status
    file via :func:`read_task_status` — single-writer and strongly consistent —
    so there is no lost-update race that a shared counter in the cross-service
    ``process_stats.json`` would suffer. The summary/flag writes are idempotent,
    so a double-fire by two leaves finishing at once is harmless.

    A leaf counts as finished for THIS run only when its status is terminal AND
    its ``updated_at`` is at/after ``fork_ts`` (so a stale terminal status from a
    previous run does not trip the barrier early). A leaf that was dispatched but
    never reached a fresh "running"/terminal state within
    ``FORK_START_GRACE_SECONDS`` of the fork is treated as failed-to-start (a 429
    drop) and stamped "failed" so its card stops looking like it is still waiting.

    Args:
        leaves: The full leaf set forked from recode (task names).
        fork_ts: ISO timestamp of the fan-out, the freshness lower bound.
    """
    fork_dt = None
    if fork_ts:
        try:
            fork_dt = datetime.fromisoformat(fork_ts)
        except (ValueError, TypeError):
            fork_dt = None

    grace_exceeded = (
        fork_dt is not None
        and (datetime.now(UTC) - fork_dt).total_seconds() > FORK_START_GRACE_SECONDS
    )
    terminal_states = {"completed", "failed", "cancelled", "error"}
    failed: list[str] = []
    for leaf in leaves:
        status = read_task_status(leaf) or {}
        state = (status.get("state") or "").lower()

        updated_dt = None
        updated = status.get("updated_at")
        if updated:
            try:
                updated_dt = datetime.fromisoformat(updated)
            except (ValueError, TypeError):
                updated_dt = None
        fresh = fork_dt is None or (updated_dt is not None and updated_dt >= fork_dt)

        if state in terminal_states and fresh:
            if state != "completed":
                failed.append(leaf)
            continue  # this leaf is done for this run

        # Not a fresh-terminal leaf. A genuinely-running leaf keeps heartbeating,
        # so never kill state=="running" — only a leaf still stuck "queued"/stale
        # past the grace window counts as failed-to-start (dropped by a 429).
        if grace_exceeded and state != "running":
            stamp_task_status(
                leaf, "failed",
                "Couldn't start — no worker was available, so the task was "
                "dropped. The other steps ran; retry this one.",
                error="Task was not initiated (HTTP 429 / no instance, no retry).",
            )
            task_failures.record_failure(
                task=leaf,
                error="Task was not initiated (HTTP 429 / no instance, no retry).",
                disposition=task_failures.DISPOSITION_DEAD,
                phase="fork",
            )
            failed.append(leaf)
            continue

        return  # still within grace, or running — not done yet

    # Every leaf has reached a terminal state for THIS run — the fan-out is done.
    _clear_pipeline_fork()
    _set_pipeline_in_flight(False)
    _write_pipeline_summary_cloud(
        partial=bool(failed),
        failed_at=",".join(failed) if failed else None,
    )


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
        "embeddings_refresh",
        "video_map_refresh",
        "recode_refresh_studies",
        "meta_refresh_groups",
        "pca_refresh",
        "timelines_refresh",
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
    # Structured outcome so the UI can style the summary (green ✓ vs amber ⚠)
    # and annotate the impact panel with where the chain stopped.
    entry["last_pipeline_partial"] = bool(partial)
    entry["last_pipeline_failed_at"] = failed_at
    # On a fully-successful pipeline every affected study/collection has been
    # refreshed, so the consolidation impact is fully resolved — clear it
    # deterministically here instead of waiting for the timestamp-based
    # staleness heuristic to notice on a later poll. On a partial/aborted run we
    # keep the impact so "Refresh All Affected" stays offered.
    if not partial:
        entry.pop("consolidation_impact", None)
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

    # Cloud Tasks counts attempts for us; 0 on the first delivery.
    try:
        retry_count = int(request.headers.get("X-CloudTasks-TaskRetryCount", 0))
    except (TypeError, ValueError):
        retry_count = 0

    _ensure_task_functions_loaded()
    if name not in TASK_FUNCTIONS:
        return jsonify({"error": f"Unknown task: {name}"}), 404

    task_args = request.json or {}

    # Run synchronously -- Cloud Tasks will wait for the response.
    ok = _run_task_with_stats(name, task_args, retry_count=retry_count)

    if ok:
        return "OK", 200

    # A failure is only worth another delivery when the task is safe to re-run
    # from scratch AND the app-side attempt bound is not yet reached. 503 asks
    # Cloud Tasks to retry with backoff; 200 acks the failure as terminal (the
    # ledger already holds the dead-letter record either way).
    if name in QUEUE_RETRY_SAFE and retry_count < MAX_APP_RETRIES - 1:
        return f"Task {name} failed; retry requested", 503
    return "Task failed (terminal)", 200

