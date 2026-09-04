import json
import threading
import time
import traceback
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import web_interface.auth as auth
from fyp.core import logging_setup
from web_interface import activity_log, run_logs, task_failures
from fyp.fyp_config import (
    CONSOLIDATE_ENRICHMENT_SCRIPT,
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
from ..services import refresh_pipeline
from ..process_manager import (
    CLOUD_TASK_ELIGIBLE,
    SCRAPER_PROCESS_NAMES,
    dispatch_deadline_for,
    graceful_stop_process,
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
    stop_process,
)
from ..task_status import (
    CANCEL_SUFFIX,
    GCSStatusReporter,
    STATUS_PREFIX,
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
# One constant with the sweep's queued-delivery grace: both answer "how long
# may a dispatched task go undelivered before we call it lost", and the
# queue's own maxRetryDuration is the only honest answer to either.
from ..services.refresh_pipeline import QUEUED_DELIVERY_GRACE_SECONDS as FORK_START_GRACE_SECONDS  # noqa: E402


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
#   embeddings_refresh                 — NOT idempotent: shards are uuid-named
#       appends, so a retried live link would write a duplicate shard
#       (2026-08-14 twin-shard incident). The worker's single-flight lease
#       makes a redelivered link exit cleanly, and a retry would also
#       re-spend embedding credits.
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
    # Fully idempotent: every tick re-reads the queues/status from scratch and
    # dispatches at most one worker, which start_process refuses if already
    # running. A retried tick is at worst a no-op.
    "enrichment_supervisor",
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

    # One refresh run at a time. Two runs would interleave writes to the same
    # caches — and the second would plan against inputs the first is still
    # rewriting. The cards grey out while a run is in flight; this is the
    # server-side half of that, and a distinct code from the 409 that means
    # "this worker is already running" so the client explains rather than
    # offering to stop and retry.
    if name in refresh_pipeline.STEP_ORDER and refresh_pipeline.run_in_flight():
        run = refresh_pipeline.load_run() or {}
        origin = run.get("origin_label") or run.get("origin") or "another step"
        return jsonify({
            "status": "busy",
            "message": (f"A refresh run started from {origin} is still in "
                        f"progress. Wait for it to finish before starting "
                        f"another step."),
        }), 423

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
    if name == "sessions_refresh":
        if data.get("stale_only"):
            args.append("--stale-only")
        if data.get("collections"):
            args.extend(["--collections", str(data["collections"])])
    if name in ["recode_refresh_studies", "pca_refresh"] and data.get("studies"):
        args.extend(["--studies", str(data["studies"])])
    if name == "recode_refresh_studies" and data.get("force_full_rebuild"):
        args.append("--force")

    if name == "consolidate_enrichment":
        # The Consolidate card posts to its own endpoint; this is the plain-API
        # door. Honour the same flag so both agree: without it the consolidation
        # records its impact as deferred debt and nothing downstream runs.
        if data.get("force_consolidation") or data.get("force"):
            args.append("--force-consolidation")
        if data.get("auto_refresh"):
            args.append("--auto-refresh")

    if name == "video_map_refresh":
        # No --auto-refresh here any more: what a rebuilt map makes stale is the
        # refresh pipeline's decision, and it is made from what the rebuild
        # reports (how many videos actually changed niche) rather than assumed.
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
    }
    
    # Plan the run before starting the origin, so the chart can show what is
    # coming from the first poll instead of appearing only once the first
    # dependent has been dispatched.
    started_by = getattr(current_user, "username", "")
    run_task_args = None
    record = None
    if name in refresh_pipeline.STEP_ORDER:
        # A consolidation only cascades when the caller asked it to; every other
        # step's whole purpose is to feed the ones below it.
        mode = ("refresh" if name != "consolidate_enrichment" or data.get("auto_refresh")
                else "consolidate_only")
        record = refresh_pipeline.plan_run(
            name, kind="card", started_by=started_by, mode=mode,
            origin_task_args=dict(data), provisional=True)
        refresh_pipeline.seed_run(record)
        run_task_args = {
            "pipeline_run_id": record["run_id"],
            "pipeline_stage_index": 1,
            "pipeline_stage_total": record["stage_total"],
        }

    success, msg = start_process(name, script_map[name], args, study_name=study_name,
                                 started_by=started_by,
                                 extra_task_args=run_task_args)
    if not success and record is not None:
        # Nothing started, so nothing will ever finish this run — leaving the
        # record in flight would lock every card until the stale-flag sweep.
        refresh_pipeline.clear_run()
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






# /api/status is polled every few seconds by every open tab, and on Cloud Run
# a naive build costs dozens of GCS round-trips (process_stats + one status
# file per eligible process). Two defenses, applied only on Cloud Run so local
# dev keeps its free, always-fresh in-memory path:
#   1. All task-status files are fetched with ONE list_blobs pass instead of a
#      per-process exists()+load_json() pair.
#   2. The assembled payload is cached for _STATUS_CACHE_TTL seconds under a
#      single-flight lock, so concurrent polls from many tabs share one build.
# The cache holds the UNREDACTED payload; redaction happens per request on a
# per-entry copy (it pops top-level keys only).
_STATUS_CACHE_TTL = 3.0
_status_cache: dict = {"payload": None, "ts": 0.0}
_status_cache_lock = threading.Lock()


def _read_all_task_statuses() -> dict[str, dict]:
    """Fetch every task_status/*.json from GCS in a single listing pass.

    Returns {status_key: status_dict}, where status_key is the filename stem
    (e.g. "pca_refresh", "study_refresh__mystudy"). Cancel-request files are
    skipped. Unreadable blobs are skipped rather than failing the poll.
    """
    statuses: dict[str, dict] = {}
    try:
        # Lazy config import, matching data_io's own idiom. NOTE: the scan
        # this replaced read ``data_io.fyp_cf`` — an attribute that does not
        # exist — so its blanket except made it silently return nothing.
        from fyp.fyp_config import fyp_cf

        bucket = fyp_cf['data_io'].get('bucket')
        gcs_prefix = fyp_cf['gcs_paths'].get('cache', '')
        if bucket is None or not gcs_prefix:
            return statuses
        prefix = f"{gcs_prefix}/{STATUS_PREFIX}/"
        for blob in bucket.list_blobs(prefix=prefix):
            fname = blob.name.split("/")[-1]
            if not fname.endswith(".json") or fname.endswith(CANCEL_SUFFIX):
                continue
            try:
                statuses[fname[: -len(".json")]] = json.loads(
                    blob.download_as_bytes()
                )
            except Exception:
                continue
    except Exception:
        pass
    return statuses


@process_bp.route('/api/status', methods=['GET'])
@login_required
def api_status():
    if is_cloud_run():
        with _status_cache_lock:
            now = time.monotonic()
            if (
                _status_cache["payload"] is None
                or now - _status_cache["ts"] >= _STATUS_CACHE_TTL
            ):
                _status_cache["payload"] = _build_status_payload()
                _status_cache["ts"] = time.monotonic()
            # Per-entry copies: redaction pops top-level keys and must never
            # mutate the cached payload another user's request will receive.
            status_data = {k: dict(v) for k, v in _status_cache["payload"].items()}
    else:
        status_data = _build_status_payload()
    return jsonify(_redact_status_for_viewer(status_data))


def _build_status_payload() -> dict:
    # Reload process_stats from GCS so we see task-runner writes
    if is_cloud_run():
        load_process_stats()

    status_data = {}

    # One listing pass for every task-status file (Cloud Run only). Also
    # surfaces study_refresh, which uses keyed status files
    # (study_refresh__<study>): any running one shows in the global badge.
    gcs_statuses: dict[str, dict] = {}
    _study_refresh_gcs = None
    if is_cloud_run():
        gcs_statuses = _read_all_task_statuses()
        _study_refresh_gcs = next(
            (
                s
                for key, s in gcs_statuses.items()
                if key.startswith("study_refresh__")
                and s.get("state") == "running"
            ),
            None,
        )

    for name, p_data in processes.items():
        gcs_status = None

        # Cloud Tasks path: read status from GCS for eligible processes
        if is_cloud_run() and name in CLOUD_TASK_ELIGIBLE:
            if name == "study_refresh":
                gcs_status = _study_refresh_gcs
            else:
                gcs_status = gcs_statuses.get(name)
            if gcs_status and gcs_status.get("state") in ("running", "queued"):
                # Check for stale status (task timed out without updating).
                # Applies to "queued" too: a queued stamp has no heartbeat, so
                # a leaf dropped before the fork grace check could flip it
                # would otherwise show "queued" forever.
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

            # A "failed" status (worker .fail() or a dropped dispatch stamped by
            # the fork grace check) only wins while it is NEWER than the last
            # recorded run end — a completed later run supersedes it. A dropped
            # dispatch writes no stats row, so it keeps surfacing as failed;
            # a worker failure writes last_run_end_time moments after fail(),
            # so it falls through to the idle path with outcome=Fail.
            if gcs_status and gcs_status.get("state") in ("failed", "error"):
                from datetime import datetime
                stats_end = process_stats.get(name, {}).get("last_run_end_time")
                updated_str = gcs_status.get("updated_at", "")
                try:
                    newer = bool(updated_str) and (
                        not stats_end
                        or datetime.fromisoformat(updated_str)
                        > datetime.fromisoformat(stats_end)
                    )
                except (ValueError, TypeError):
                    newer = False
                if not newer:
                    gcs_status = None

            if gcs_status and gcs_status.get("state") in ("running", "queued",
                                                          "failed", "error"):
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
                    "error": gcs_status.get("error"),
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
    return status_data


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
    from web_interface.run_embeddings_refresh import run_embeddings_refresh
    from web_interface.run_ingest_refresh import run_ingest_refresh
    from web_interface.run_meta_refresh_groups import run_meta_refresh_groups
    from web_interface.run_enrichment_supervisor import run_enrichment_supervisor
    from web_interface.run_ops_report import run_ops_report
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
        # Deliberately NOT in QUEUE_RETRY_SAFE: a queue retry would re-send
        # the report email. A failed run lands in the task-failures ledger.
        "ops_report": run_ops_report,
        "enrichment_supervisor": run_enrichment_supervisor,
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
    if "(via " in origin or origin.startswith("auto-pipeline"):
        return origin  # already traced; don't nest the annotation
    return f"{origin} (via {parent})"




# A status heartbeat older than this marks the run as dead — the same 600 s
# rule /api/status and _is_worker_running apply.
_STALE_HEARTBEAT_SECONDS = 600


def _ledger_stale_predecessor(name: str, status_key: str) -> None:
    """Dead-letter a prior run that died without a failure record (OOM SIGKILL).

    A SIGKILL bypasses the failure wrapper entirely: no ``task_failures``
    entry is written and the status file stays ``state="running"`` with a
    frozen heartbeat — the UI's stale rule shows it as dead, but the
    dead-letter ledger doesn't, so repeated silent deaths are easy to miss
    (this happened twice with pca_refresh, 2026-08-08/09). Called when a NEW
    run of the same key starts (chunk 0): if the previous status is a stale
    ``running`` corpse, record it before ``reporter.start()`` overwrites it.
    Never raises — bookkeeping must not block the new run.
    """
    try:
        prior = read_task_status(status_key)
        if not prior or prior.get("state") != "running":
            return
        updated_str = prior.get("updated_at") or ""
        try:
            age = (datetime.now(UTC)
                   - datetime.fromisoformat(updated_str)).total_seconds()
        except (ValueError, TypeError):
            return  # malformed heartbeat: can't prove it's a corpse
        if age <= _STALE_HEARTBEAT_SECONDS:
            return
        task_failures.record_failure(
            task=name,
            error=(f"Previous run found dead: status stuck at 'running' with a "
                   f"heartbeat {age / 60:.0f} min old (last message: "
                   f"{prior.get('message') or '—'!s}). No failure was recorded "
                   f"by the run itself — the process was most likely "
                   f"SIGKILLed (out of memory)."),
            status_key=status_key,
            disposition=task_failures.DISPOSITION_DEAD,
            phase="presumed_oom",
        )
    except Exception as exc:
        print(f"[{name}] stale-predecessor ledger check failed: {exc}")




def _chain_run_start(link_start: datetime, status_start: str | None) -> datetime:
    """The whole run's start time, spanning every link of a self-chain.

    A self-chaining task boots one process per link, so the final link's local
    ``link_start`` is only minutes old even when the run began an hour ago. The
    status file's ``start_time`` — written by link 0's ``start()`` and carried
    forward by every ``resume()`` — is the run's true origin, so the recorded
    ``last_run_duration`` must be measured from it, not from the last batch.

    Args:
        link_start: When THIS link's task function started.
        status_start: The reporter status's ``start_time`` (ISO string), if any.

    Returns:
        The earlier of the two instants; ``link_start`` when the status value
        is absent or unparsable.
    """
    if status_start:
        try:
            return min(link_start, datetime.fromisoformat(status_start))
        except (ValueError, TypeError):
            pass
    return link_start






def _merge_run_stats(existing: dict, run_data: dict, *, name: str, task_args: dict,
                     outcome: str, end_time: datetime, duration: float,
                     study_name: str | None) -> dict:
    """The process_stats entry for a task after one run of it.

    A run's emitted data is merged over the stored entry and the last-run
    fields are refreshed — except for the weekly shadow verification, which
    runs under the consolidate key (so the supervisor's gate serialises it) but
    is not a consolidation. It records under ``last_verify_*`` instead, so it
    never becomes the card's "Last: … OK" line (13 min right after a 42 s
    consolidation read as "fired twice" on 2026-09-03) and never bumps
    ``last_success``, which staleness checks read as "data consolidated".
    """
    merged = {**existing, **run_data}
    if name == "consolidate_enrichment" and task_args.get("verify_consolidation"):
        merged.update({
            "last_verify_end_time": end_time.isoformat(),
            "last_verify_duration": duration,
            "last_verify_outcome": outcome,
        })
        return merged
    merged.update({
        "last_success": end_time.isoformat() if outcome == "Success" else merged.get("last_success"),
        "last_run_end_time": end_time.isoformat(),
        "last_run_duration": duration,
        "last_run_outcome": outcome,
        "last_run_study": study_name,
    })
    return merged


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
        _ledger_stale_predecessor(name, status_key)
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
    cancelled = False

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
            # Dispatch next Cloud Task in the chain. ``next_task`` (if provided)
            # switches to a different task type; same-task chains (scraper /
            # annotator / embeddings batching) inherit the current task name and
            # status key. A worker that names its own successor has done the
            # hand-off itself, so the refresh run must not also advance —
            # ``dispatched_cross_task`` is what says so below.
            next_task_name = chain_result.get("next_task") or name
            next_args = chain_result["next_task_args"]
            # A worker may name its own chain deadline; otherwise fall back to
            # the shared table rather than to Cloud Tasks' 600s default.
            deadline = (chain_result.get("dispatch_deadline_seconds")
                        or dispatch_deadline_for(next_task_name, next_args))
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
                for k in ("pipeline_run_id", "pipeline_remaining",
                          "pipeline_stage_total", "pipeline_stage_index",
                          "pipeline_fanout", "pipeline_leaves",
                          "pipeline_fork_ts", "log_run_id", "started_by"):
                    if k in task_args and k not in next_args:
                        next_args[k] = task_args[k]
                success, msg = _dispatch_cloud_task(
                    name, next_args,
                    dispatch_deadline_seconds=deadline,
                    schedule_delay_seconds=chain_result.get("next_dispatch_delay_seconds"),
                )
                if success:
                    # A worker that chains for a reason other than "here comes
                    # the next batch" (the batch annotator re-polling the same
                    # Gemini job, say) can supply its own wording.
                    reporter.log(chain_result.get("chain_log_message")
                                 or f"Chained to next batch: {msg}")
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
            # Read the cancel sentinel BEFORE complete() clears it. Every worker
            # returns None after a cancellation, which is indistinguishable from
            # an ordinary finish once the flag is gone — and a cancelled step
            # must stop the run, not advance it onto stale inputs.
            cancelled = reporter.check_cancelled()
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
    duration = (end_time - _chain_run_start(
        start_time, reporter._status.get("start_time"))).total_seconds()

    load_process_stats()
    process_stats[status_key] = _merge_run_stats(
        process_stats.get(status_key, {}), reporter._status.get("data", {}),
        name=name, task_args=task_args, outcome=outcome, end_time=end_time,
        duration=duration, study_name=study_name)
    save_process_stats()

    # ---- Refresh-run advance. Every finished step asks the planner what, if
    # anything, depends on what it just produced. See services/refresh_pipeline.
    # Skipped when the worker dispatched its own successor: that hand-off has
    # already happened, and advancing too would run the next step twice.
    if not dispatched_cross_task:
        _advance_refresh_run(name, task_args, outcome, cancelled)

    # Server-side auto-fire of an armed Consolidate & Refresh. Arming promises
    # the pipeline runs once the enrichment queues finish, but the original
    # trigger was a browser poll POST — which fails silently when the tab's CSRF
    # token has expired (queues often run >1h) or the tab is closed. Firing here,
    # on the task-runner at terminal worker completion, makes it browser-independent.
    if name in ("queue_annotator", "queue_annotator_batch") or name.startswith("queue_scraper"):
        # The armed consolidate keeps priority: it is an explicit operator
        # request, and the supervisor would only ask for the same consolidation
        # one tick later anyway.
        if not _maybe_autofire_armed_consolidate(name):
            _tick_enrichment_supervisor(name)
    elif name == "consolidate_enrichment":
        # A finished consolidation is what lets the enrichment loop take its
        # next step (handoff, new slice, or settle). Ticked HERE — after the
        # save_process_stats() above — and not inside the task, so the tick's
        # _unconsolidated() check reads the fresh last_consolidation instead of
        # re-firing a full no-op consolidation against the stale one (the
        # observed double-run). No armed-autofire check here — firing an armed
        # consolidate off a consolidate completion would loop.
        _tick_enrichment_supervisor(name)

    return outcome == "Success"


def _maybe_autofire_armed_consolidate(just_finished: str) -> bool:
    """Dispatch an armed Consolidate & Refresh once the enrichment queues idle.

    Called from a terminal ``queue_scraper`` / ``queue_annotator`` completion on
    the task-runner. No-ops unless ``consolidate_enrichment.auto_armed`` is set.
    Defers while the *other* enrichment worker is still running (that worker runs
    this same check when it finishes) and while a consolidate is already in
    flight. The armed flag is cleared before dispatch so two near-simultaneous
    finishers can't both fire; a failed dispatch re-arms for a later retry.

    Args:
        just_finished: The worker whose completion triggered this check.

    Returns:
        True when a consolidate was actually dispatched, so the caller can skip
        the enrichment-supervisor tick rather than dispatch a redundant task off
        the same worker completion.
    """
    from ..process_manager import _dispatch_cloud_task

    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    if not entry.get("auto_armed"):
        # Say so: 2026-09-03 two armed refreshes failed to fire and every exit
        # here was silent, so the record could not tell which one it was.
        print(f"[{just_finished}] Armed consolidate: no arm flag in process_stats "
              f"(keys: {sorted(k for k in entry if 'arm' in k) or 'none'}).")
        return False

    # The other enrichment workers may still be running on separate task-runner
    # instances — read their GCS status (single source of truth across instances).
    others = [w for w in SCRAPER_PROCESS_NAMES + ["queue_annotator", "queue_annotator_batch"]
              if w != just_finished]
    for worker in others:
        st = read_task_status(worker) or {}
        if (st.get("state") or "").lower() == "running":
            updated = st.get("updated_at") or ""
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds()
                if age <= 600:
                    print(f"[{just_finished}] Armed consolidate deferred: {worker} still "
                          f"running (heartbeat {age:.0f}s ago).")
                    return False
            except (ValueError, TypeError):
                print(f"[{just_finished}] Armed consolidate deferred: {worker} running "
                      f"with an unreadable heartbeat {updated!r}.")
                return False  # Malformed heartbeat — treat as running, be safe.

    # Don't double-fire onto an already-running consolidate.
    cs = read_task_status("consolidate_enrichment") or {}
    if (cs.get("state") or "").lower() == "running":
        print(f"[{just_finished}] Armed consolidate deferred: a consolidation is already "
              f"running (since {cs.get('start_time')}).")
        return False

    # Defer while a local scrape-queue drain holds a lease on the shared
    # storage (its queue prunes would race the consolidation). The arm stays
    # set, so the next worker completion — or a manual trigger — re-checks.
    try:
        from web_interface import drain_lease
        if drain_lease.active_drain_leases():
            print(f"[{just_finished}] Armed consolidate deferred: local drain lease active.")
            return False
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

    # Plan the run before dispatching, exactly as the button does, so an armed
    # refresh that fires while nobody is watching is charted like any other.
    record = refresh_pipeline.plan_run(
        "consolidate_enrichment", kind="armed",
        started_by=f"auto-pipeline (armed, after {just_finished})",
        mode="refresh" if auto_refresh else "consolidate_only",
        origin_task_args=task_args, provisional=bool(auto_refresh))
    refresh_pipeline.seed_run(record)
    task_args["pipeline_run_id"] = record["run_id"]
    task_args["pipeline_stage_index"] = 1
    task_args["pipeline_stage_total"] = record["stage_total"]

    success, msg = _dispatch_cloud_task(
        "consolidate_enrichment", task_args,
        dispatch_deadline_seconds=dispatch_deadline_for("consolidate_enrichment", task_args))
    if success:
        print(f"[{just_finished}] Armed Consolidate & Refresh fired: {msg}")
        return True
    else:
        print(f"[{just_finished}] Armed consolidate dispatch failed: {msg}")
        refresh_pipeline.clear_run()
        # Re-arm so the other finisher or a manual trigger can retry.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry["auto_armed"] = True
        entry["auto_armed_force"] = force
        entry["auto_armed_auto_refresh"] = auto_refresh
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
    return False


def _tick_enrichment_supervisor(just_finished: str) -> None:
    """Advance the automatic enrichment loop after a worker finishes.

    Fire-and-forget: the loop must never be able to fail somebody else's task
    completion. The tick is a no-op unless the site switch is on and some
    collection is armed, and it re-checks every precondition itself, so a
    spurious call costs one cheap Cloud Task and nothing else.

    Args:
        just_finished: The worker whose completion triggered this.
    """
    try:
        from web_interface.services import collection_enrichment as ce
        if not ce.armed_plans():
            return
        from ..process_manager import _dispatch_cloud_task, dispatch_deadline_for
        success, msg = _dispatch_cloud_task(
            "enrichment_supervisor", {},
            dispatch_deadline_seconds=dispatch_deadline_for("enrichment_supervisor", {}))
        print(f"[{just_finished}] Enrichment supervisor tick: {msg}" if success else
              f"[{just_finished}] Enrichment supervisor tick failed to dispatch: {msg}")
    except Exception as exc:
        print(f"[{just_finished}] Enrichment supervisor tick skipped: {exc}")


def _advance_refresh_run(name: str, task_args: dict, outcome: str,
                         cancelled: bool = False) -> None:
    """Move the refresh run forward now that ``name`` has finished.

    The run's shape is planned when it starts; this decides, one completion at a
    time, which of the remaining steps still have anything to do. A step whose
    upstream reports no change is pruned with the reason shown in the chart
    rather than dispatched — that is the whole point of planning a run rather
    than hard-wiring a chain.

    Dispatch is an out-tree: the first spine step that still has work runs
    alone (it can change what the later steps see), and when no spine step is
    left every remaining leaf fans out together. The fan-out's completion is
    detected by :func:`_maybe_finish_forked_pipeline`, which reads each leaf's
    own status file rather than a shared counter.

    Args:
        name: The step that just finished.
        task_args: Its arguments — ``pipeline_run_id`` is what ties it to a run.
        outcome: ``"Success"`` or ``"Fail"``.
        cancelled: The operator cancelled this step.
    """
    from ..process_manager import _dispatch_cloud_task

    run_id = task_args.get("pipeline_run_id")
    leaves = task_args.get("pipeline_leaves") or []

    if not run_id:
        # A chain that predates the run record: the sessions refresh a study
        # save appends, or a task queued before this deploy. Advance it exactly
        # as before and leave the run record alone — it is not part of a run,
        # and the chart correctly shows it as foreign work.
        _advance_legacy_chain(name, task_args, outcome)
        return

    if name in leaves:
        _maybe_finish_forked_pipeline(
            leaves, fork_ts=task_args.get("pipeline_fork_ts"), run_id=run_id)
        return

    if cancelled:
        print(f"[{name}] Refresh run {run_id}: cancelled; stopping the run.")
        record = refresh_pipeline.finish_run(partial=True, failed_at=name,
                                             reason="cancelled", run_id=run_id)
        _publish_run_summary(record)
        return

    if outcome != "Success":
        print(f"[{name}] Refresh run {run_id}: step failed; stopping the run.")
        record = refresh_pipeline.finish_run(partial=True, failed_at=name,
                                             run_id=run_id)
        _publish_run_summary(record)
        return

    record = refresh_pipeline.load_run()
    if not record or record.get("run_id") != run_id:
        print(f"[{name}] Refresh run {run_id} is no longer the current run "
              f"({(record or {}).get('run_id')}); not advancing.")
        return

    action = refresh_pipeline.next_actions(record)  # prunes applied to the copy
    prunes = action["prunes"]
    for step, reason in prunes.items():
        print(f"[{name}] Refresh run: skipping {step} — {reason}.")

    if action["action"] == "finish":
        record = refresh_pipeline.finish_run(partial=False, prunes=prunes,
                                             run_id=run_id)
        _publish_run_summary(record)
        print(f"[{name}] Refresh run finished: {(record or {}).get('summary')}")
        return

    stage_total = refresh_pipeline.stage_total(record["steps"])
    stage_index = refresh_pipeline.next_stage_index(record["steps"])
    actor = _pipeline_actor(task_args, name)

    def _child_args(step: str, step_args: dict) -> dict:
        args = dict(step_args or {})
        args["pipeline_run_id"] = run_id
        args["pipeline_stage_total"] = stage_total
        args["pipeline_stage_index"] = stage_index
        args["started_by"] = actor
        args["log_run_id"] = run_logs.new_run_id()
        return args

    if action["action"] == "spine":
        next_name = action["step"]
        next_args = _child_args(next_name, action["task_args"])
        # Stamp "queued" BEFORE dispatch, as the fork leaves already are. Without
        # it a spine task the queue holds back (a 429 with no free runner,
        # redelivered minutes later) is invisible to every liveness check —
        # nothing running, nothing completed, record untouched — and the
        # abandoned-run sweep kills the run while its next step is on its way.
        stamp_task_status(
            next_name, "queued", "Queued — waiting for a worker…",
            stage={"stage_index": stage_index, "stage_total": stage_total},
        )
        success, msg = _dispatch_cloud_task(
            next_name, next_args,
            dispatch_deadline_seconds=dispatch_deadline_for(next_name, next_args))
        if success:
            print(f"[{name}] Refresh run: advanced to {next_name}: {msg}")
            run_logs.open_run(next_name, run_id=next_args["log_run_id"],
                              started_by=actor, task_args=next_args, mode="cloud")
            refresh_pipeline.record_dispatch(run_id, {next_name: {}}, prunes=prunes)
        else:
            print(f"[{name}] Refresh run: advance to {next_name} failed: {msg}")
            record = refresh_pipeline.finish_run(partial=True, failed_at=next_name,
                                                 prunes=prunes, run_id=run_id)
            _publish_run_summary(record)
        return

    # Fan-out: the remaining leaves are mutually independent (distinct readers,
    # distinct outputs), so they are dispatched together and only their
    # completion needs detecting. One fork timestamp lets each leaf ignore a
    # sibling's stale terminal status from an earlier run.
    fork_ts = datetime.now(UTC).isoformat()
    leaf_names = [leaf for leaf, _ in action["leaves"]]
    leaf_stage = {"stage_index": stage_index, "stage_total": stage_total}
    dispatched: dict[str, dict] = {}
    any_failed = False
    for leaf, leaf_args in action["leaves"]:
        child_args = _child_args(leaf, leaf_args)
        child_args["pipeline_leaves"] = leaf_names
        child_args["pipeline_fork_ts"] = fork_ts
        # Stamp "queued" BEFORE dispatch so the card shows a definitive
        # this-run status rather than a stale one. A booting task overwrites it
        # with "running"; a dropped one stays queued and the grace check below
        # flips it to failed.
        stamp_task_status(leaf, "queued", "Queued — waiting for a worker…",
                          stage=leaf_stage)
        success, msg = _dispatch_cloud_task(
            leaf, child_args,
            dispatch_deadline_seconds=dispatch_deadline_for(leaf, child_args))
        if success:
            print(f"[{name}] Refresh run: forked {leaf}: {msg}")
            run_logs.open_run(leaf, run_id=child_args["log_run_id"],
                              started_by=actor, task_args=child_args, mode="cloud")
            dispatched[leaf] = {}
        else:
            print(f"[{name}] Refresh run: fork of {leaf} failed: {msg}")
            stamp_task_status(
                leaf, "failed",
                "Couldn't start — the task could not be queued for a worker.",
                error=f"Dispatch failed: {msg}", stage=leaf_stage)
            any_failed = True

    refresh_pipeline.record_dispatch(
        run_id, dispatched, prunes=prunes, fork_at=name,
        fork={"leaves": leaf_names, "fork_ts": fork_ts})
    if any_failed:
        record = refresh_pipeline.finish_run(partial=True, failed_at=name,
                                             run_id=run_id)
        _publish_run_summary(record)


def _advance_legacy_chain(name: str, task_args: dict, outcome: str) -> None:
    """Advance a plain ``pipeline_remaining`` chain that carries no run record.

    Two callers reach this: a study save, which appends a sessions refresh to
    its own study refresh, and any task that was already queued when the run
    record shipped. Linear, no fan-out, no chart.
    """
    from ..process_manager import _dispatch_cloud_task

    remaining = task_args.get("pipeline_remaining") or []
    if outcome != "Success" or not remaining:
        return
    next_step = remaining[0]
    next_name = next_step["task"]
    next_args = dict(next_step.get("task_args") or {})
    next_args["pipeline_remaining"] = remaining[1:]
    next_args["started_by"] = _pipeline_actor(task_args, name)
    next_args["log_run_id"] = run_logs.new_run_id()
    success, msg = _dispatch_cloud_task(
        next_name, next_args,
        dispatch_deadline_seconds=dispatch_deadline_for(next_name, next_args))
    if success:
        print(f"[{name}] Chained to {next_name}: {msg}")
        run_logs.open_run(next_name, run_id=next_args["log_run_id"],
                          started_by=next_args["started_by"],
                          task_args=next_args, mode="cloud")
    else:
        print(f"[{name}] Chain to {next_name} failed: {msg}")


def _publish_run_summary(record: dict | None) -> None:
    """Mirror a finished run's outcome onto the Consolidate card.

    Only for runs that started from a consolidation (or from "Refresh All
    Affected", which replays one): that card is where the operator reads
    "what did the last consolidation actually refresh?". A run started from a
    worker card reports through the chart's own header instead, so it must not
    overwrite the consolidation's standing summary.
    """
    if not record or record.get("origin_kind") not in (
            "consolidate", "armed", "refresh_downstream"):
        return
    refreshed = refresh_pipeline.run_refreshed_anything(record)
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    # A consolidate-only run already wrote its own summary, and that one knows
    # something this record does not: whether there was an impact to defer. The
    # generic "nothing needed refreshing" would overwrite it with a claim the
    # deferred ledger contradicts.
    if record.get("mode") != "consolidate_only":
        entry["last_pipeline_summary"] = record.get("summary") or ""
        entry["last_pipeline_summary_ts"] = record.get("finished_ts")
        entry["last_pipeline_partial"] = bool(record.get("partial"))
        entry["last_pipeline_failed_at"] = record.get("failed_at")
    # Only a run that actually rebuilt something has consumed the impact. A
    # partial run leaves it so "Refresh All Affected" stays offered — and so
    # does a run that refreshed nothing at all, whether because it was told not
    # to (consolidate-only) or because every step was pruned. Dropping it there
    # threw away the operator's only record of what still needs rebuilding.
    if refreshed and not record.get("partial"):
        entry.pop("consolidation_impact", None)
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
    record = refresh_pipeline.load_run()
    fork = (record or {}).get("fork")
    if not record or not record.get("in_flight") or not fork:
        return
    _maybe_finish_forked_pipeline(fork.get("leaves") or [], fork.get("fork_ts"),
                                  run_id=record.get("run_id"))


def _maybe_finish_forked_pipeline(leaves: list[str], fork_ts: str | None = None,
                                  run_id: str | None = None) -> None:
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
        leaves: The full leaf set that was forked (task names).
        fork_ts: ISO timestamp of the fan-out, the freshness lower bound.
        run_id: The refresh run being finished. A mismatch means a newer run has
            replaced this one, and the stale barrier must not close it.
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
    record = refresh_pipeline.finish_run(
        partial=bool(failed),
        failed_at=",".join(failed) if failed else None,
        run_id=run_id,
    )
    _publish_run_summary(record)


def _set_pipeline_in_flight(value: bool) -> None:
    """Record whether a refresh run is currently occupying the pipeline.

    Kept as a thin alias over the run record: the UI polls it to keep watching
    across the gap between one step completing and the next one booting, and
    the enrichment supervisor's hard gate reads the legacy mirror of it on the
    consolidate entry.
    """
    refresh_pipeline.set_in_flight(value)


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

