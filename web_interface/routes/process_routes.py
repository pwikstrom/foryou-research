"""Worker control and execution routes.

The admin start/stop/status/log APIs, and the internal Cloud Tasks endpoint
(``/internal/run-task/<name>``). The endpoint only authenticates and answers
the queue; running the worker, recording its stats and advancing self-chains
and the refresh pipeline is ``web_interface/tasks/runtime.py``."""

import json
import threading
import time
from datetime import UTC

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from web_interface.services import activity_log
from web_interface.tasks import run_logs, worker_registry

from ..auth.permissions import admin_required, user_has_permission
from ..services import refresh_pipeline
from ..tasks import runtime
from ..tasks.process_manager import (
    CLOUD_TASK_ELIGIBLE,
    graceful_stop_process,
    load_process_stats,
    process_stats,
    processes,
    start_process,
    stop_process,
)
from ..tasks.task_status import (
    CANCEL_SUFFIX,
    STATUS_PREFIX,
    is_cloud_run,
    read_task_status,
)

process_bp = Blueprint("process_bp", __name__)


@process_bp.route("/api/start/<name>", methods=["POST"])
@admin_required
def api_start(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    if not worker_registry.WORKERS[name].generic_start:
        # Workers with their own launch endpoint (study refresh, ingestion,
        # deletes, reports, ...) need arguments this card door cannot supply.
        return jsonify({"error": f"{name} is started from its own endpoint"}), 400

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
            gemini_ok, gemini_reason = (
                False,
                (
                    "Gemini annotation is unavailable: the google-genai library "
                    f"could not be loaded ({exc})."
                ),
            )
        if not gemini_ok:
            return jsonify(
                {"status": "error", "error": gemini_reason, "message": gemini_reason}
            ), 400

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
            return jsonify({"status": "error", "error": embed_reason, "message": embed_reason}), 400

    # One refresh run at a time. Two runs would interleave writes to the same
    # caches — and the second would plan against inputs the first is still
    # rewriting. The cards grey out while a run is in flight; this is the
    # server-side half of that, and a distinct code from the 409 that means
    # "this worker is already running" so the client explains rather than
    # offering to stop and retry.
    if name in refresh_pipeline.STEP_ORDER and refresh_pipeline.run_in_flight():
        run = refresh_pipeline.load_run() or {}
        origin = run.get("origin_label") or run.get("origin") or "another step"
        return jsonify(
            {
                "status": "busy",
                "message": (
                    f"A refresh run started from {origin} is still in "
                    f"progress. Wait for it to finish before starting "
                    f"another step."
                ),
            }
        ), 423

    data = request.json or {}
    args = []

    if "study_name" in data:
        args.append(data["study_name"])

    if name in [
        "downloader",
        "annotator",
        "queue_annotator",
        "queue_annotator_batch",
        "embeddings_refresh",
    ] or name.startswith("queue_scraper_"):
        # batch_size / max_batches go straight into a worker argv — validate
        # and bound them here rather than trusting the client blindly.
        for key, flag, upper in (
            ("batch_size", "--batch-size", 5000),
            ("max_batches", "--max-batches", None),
        ):
            raw = data.get(key)
            if raw is None or not str(raw).strip():
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                return jsonify(
                    {
                        "status": "error",
                        "error": f"{key} must be an integer",
                        "message": f"{key} must be an integer",
                    }
                ), 400
            if value < 1 or (upper is not None and value > upper):
                bound = f"1-{upper}" if upper is not None else ">= 1"
                return jsonify(
                    {
                        "status": "error",
                        "error": f"{key} must be {bound}",
                        "message": f"{key} must be {bound}",
                    }
                ), 400
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

    # Plan the run before starting the origin, so the chart can show what is
    # coming from the first poll instead of appearing only once the first
    # dependent has been dispatched.
    started_by = getattr(current_user, "username", "")
    run_task_args = None
    record = None
    if name in refresh_pipeline.STEP_ORDER:
        # A consolidation only cascades when the caller asked it to; every other
        # step's whole purpose is to feed the ones below it.
        mode = (
            "refresh"
            if name != "consolidate_enrichment" or data.get("auto_refresh")
            else "consolidate_only"
        )
        record = refresh_pipeline.plan_run(
            name,
            kind="card",
            started_by=started_by,
            mode=mode,
            origin_task_args=dict(data),
            provisional=True,
        )
        refresh_pipeline.seed_run(record)
        run_task_args = {
            "pipeline_run_id": record["run_id"],
            "pipeline_stage_index": 1,
            "pipeline_stage_total": record["stage_total"],
        }

    success, msg = start_process(
        name,
        worker_registry.worker_module(name),
        args,
        study_name=study_name,
        started_by=started_by,
        extra_task_args=run_task_args,
    )
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
        return jsonify({"status": "error", "error": msg, "message": msg}), 409


@process_bp.route("/api/stop/<name>", methods=["POST"])
@admin_required
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


@process_bp.route("/api/stop_graceful/<name>", methods=["POST"])
@admin_required
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
    if is_admin or user_has_permission(current_user, "tab.data_management"):
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
        from fyp.core.fyp_config import fyp_cf

        bucket = fyp_cf["data_io"].get("bucket")
        gcs_prefix = fyp_cf["gcs_paths"].get("cache", "")
        if bucket is None or not gcs_prefix:
            return statuses
        prefix = f"{gcs_prefix}/{STATUS_PREFIX}/"
        for blob in bucket.list_blobs(prefix=prefix):
            fname = blob.name.split("/")[-1]
            if not fname.endswith(".json") or fname.endswith(CANCEL_SUFFIX):
                continue
            try:
                statuses[fname[: -len(".json")]] = json.loads(blob.download_as_bytes())
            except Exception:
                continue
    except Exception:
        pass
    return statuses


@process_bp.route("/api/status", methods=["GET"])
@login_required
def api_status():
    if is_cloud_run():
        with _status_cache_lock:
            now = time.monotonic()
            if _status_cache["payload"] is None or now - _status_cache["ts"] >= _STATUS_CACHE_TTL:
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
                if key.startswith("study_refresh__") and s.get("state") == "running"
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
                        or datetime.fromisoformat(updated_str) > datetime.fromisoformat(stats_end)
                    )
                except (ValueError, TypeError):
                    newer = False
                if not newer:
                    gcs_status = None

            if gcs_status and gcs_status.get("state") in ("running", "queued", "failed", "error"):
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
        if p_data["proc"] and p_data["proc"].poll() is not None and state == "running":
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


@process_bp.route("/api/status/study_refresh/<study_name>", methods=["GET"])
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
            return jsonify(
                {
                    "state": gcs_status.get("state", "unknown"),
                    "progress": gcs_status.get("progress", {}),
                    "data": gcs_status.get("data", {}),
                    "last_run_outcome": stats_entry.get("last_run_outcome"),
                }
            )

    # Local dev: read the in-process status dict populated by the background
    # thread spawned from save_study.
    from web_interface.tasks.task_status import read_local_thread_status

    local_status = read_local_thread_status(status_key)
    if local_status:
        return jsonify(
            {
                "state": local_status.get("state", "unknown"),
                "progress": local_status.get("progress", {}),
                "data": local_status.get("data", {}),
                "last_run_outcome": None,
            }
        )

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


@process_bp.route("/api/logs/clear/<name>", methods=["POST"])
@admin_required
def api_clear_logs(name):
    """Delete a process's whole run history (all retained runs)."""
    key = _resolve_log_key(name)
    if key is None:
        return jsonify({"error": "Unknown process"}), 400

    if key in processes:
        processes[key]["logs"].clear()
    run_logs.clear(key)
    return jsonify({"status": "success"})


@process_bp.route("/api/logs/<name>", methods=["GET"])
@admin_required
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
        return jsonify(
            {
                "logs": legacy,
                "next_since": 0,
                "reset": True,
                "run_id": "",
                "run": None,
                "runs": [],
                "key": key,
            }
        )

    # `logs` stays a newline-joined string: the async-annotator card feed reads
    # this same endpoint and splits on newlines.
    return jsonify(
        {
            "logs": "\n".join(payload["lines"]),
            "next_since": payload["next_since"],
            "reset": payload["reset"],
            "run_id": (payload["run"] or {}).get("run_id", ""),
            "run": payload["run"],
            "runs": payload["runs"],
            "key": key,
        }
    )


# ---------------------------------------------------------------------------
# Internal endpoint: receives Cloud Tasks HTTP requests.
# Lives in a separate blueprint so it can be fully CSRF-exempted.
# ---------------------------------------------------------------------------

internal_bp = Blueprint("internal_bp", __name__)


@internal_bp.route("/internal/run-task/<name>", methods=["POST"])
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

    runtime.ensure_task_functions_loaded()
    if name not in runtime.TASK_FUNCTIONS:
        return jsonify({"error": f"Unknown task: {name}"}), 404

    task_args = request.json or {}

    # Run synchronously -- Cloud Tasks will wait for the response.
    ok = runtime.run_task_with_stats(name, task_args, retry_count=retry_count)

    if ok:
        return "OK", 200

    # A failure is only worth another delivery when the task is safe to re-run
    # from scratch AND the app-side attempt bound is not yet reached. 503 asks
    # Cloud Tasks to retry with backoff; 200 acks the failure as terminal (the
    # ledger already holds the dead-letter record either way).
    if name in runtime.QUEUE_RETRY_SAFE and retry_count < runtime.MAX_APP_RETRIES - 1:
        return f"Task {name} failed; retry requested", 503
    return "Task failed (terminal)", 200
