"""Worker/pipeline status helpers shared by the management endpoints.

Pure moves from ``web_interface/routes/management_routes.py`` (Phase 7b) —
worker liveness checks, the consolidate-pipeline step view, per-platform
cookie-health caching, and the acting-user lookup.
"""

import threading
import time as _time
from datetime import UTC, datetime

from flask_login import current_user

from fyp.platform_scraper import get_scraper

from ..process_manager import process_stats, processes
from ..task_status import is_cloud_run, read_task_status


def _actor() -> str:
    """Return the username of the acting user, or empty string if unauthenticated."""
    try:
        return current_user.username if current_user.is_authenticated else ""
    except Exception:
        return ""






# Downstream refresh steps considered by the auto-pipeline, in the order they
# are dispatched. Keep in sync with _PIPELINE_STEPS_ORDER in
# run_consolidate_enrichment.py. Ordering matters: embeddings feed video_map
# (the niches), video_map feeds recode, and recode produces the recoded datasets
# that meta_refresh_groups / pca_refresh consume. This list is used only to
# check whether any pipeline step is currently running, so membership matters
# more than order, but the two lists are kept identical to avoid drift.
PIPELINE_STEPS_ORDER = [
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
]


def _is_worker_running(name: str) -> bool:
    """True if a worker (subprocess or Cloud Task) is currently running.

    Consults the in-memory subprocess state *and* the GCS status file, with
    the same stale-heartbeat detection used by /api/status. Safe to call from
    any endpoint that needs to gate behaviour on worker activity.
    """
    proc_state = processes.get(name, {})
    proc = proc_state.get("proc")
    if proc is not None and proc.poll() is None:
        return True

    if is_cloud_run():
        gcs_status = read_task_status(name)
        if gcs_status and gcs_status.get("state") == "running":
            updated_str = gcs_status.get("updated_at") or ""
            try:
                updated_at = datetime.fromisoformat(updated_str)
                age = (datetime.now(UTC) - updated_at).total_seconds()
                if age <= 600:
                    return True
            except (ValueError, TypeError):
                # No / malformed heartbeat — treat as running to be safe.
                return True

    return False


def _workers_blocking_consolidate() -> list[str]:
    """Return the names of scraper/annotator workers currently running."""
    blocking = []
    for name in ("queue_scraper", "queue_annotator"):
        if _is_worker_running(name):
            blocking.append(name)
    return blocking


# Per-platform cookie-health cache. cookie_health() does a GCS blob.reload()
# (one HTTP round-trip per platform) to read the file age, so we cache the result
# for a few minutes — the enrichment-stats endpoint is polled every ~2s during a
# consolidate run and there is no value in re-probing GCS that often.
_COOKIE_HEALTH_TTL_SEC = 300
_cookie_health_cache: dict[str, tuple[float, dict]] = {}
_cookie_health_lock = threading.Lock()


def _cached_cookie_health(platform: str) -> dict:
    """Return a platform's cookie health, cached for ``_COOKIE_HEALTH_TTL_SEC``.

    Delegates to the platform scraper's ``health_check`` hook (which passes the
    correct per-platform session-cookie name). Never raises: a probe failure
    degrades to an ``unknown`` status so one bad platform can't 500 the tab.

    Args:
        platform: platform key, e.g. ``"tiktok"``.

    Returns:
        The ``cookie_health`` dict (``status``/``message``/``file_age_days``/
        ``session_days_left``/``session_expires_at``/…), or an ``unknown``-status
        stub when the probe fails or the scraper exposes no health hook.
    """
    now = _time.time()
    with _cookie_health_lock:
        cached = _cookie_health_cache.get(platform)
        if cached and (now - cached[0]) < _COOKIE_HEALTH_TTL_SEC:
            return cached[1]

    try:
        health = get_scraper(platform).health_check()
        if not health:
            health = {"status": "unknown", "message": "No cookie health available"}
    except Exception as e:
        health = {"status": "unknown", "message": f"Cookie health probe failed: {e}"}

    with _cookie_health_lock:
        _cookie_health_cache[platform] = (now, health)
    return health


def _build_pipeline_step_view(pipeline_active: bool) -> list[dict]:
    """Build an ordered per-step view of the last/active consolidate pipeline.

    Returns one dict per step (``consolidate_enrichment`` plus every step in the
    persisted ``pipeline_plan``) with keys ``step``, ``label``, ``state``,
    ``percent``, ``message`` and ``ran_at``. ``state`` is one of ``running``,
    ``queued``, ``success``, ``failed``, ``skipped`` or ``pending``. Live state
    comes from each step's GCS status file (Cloud Run); terminal outcomes fall
    back to ``process_stats``. Returns ``[]`` when no plan has been recorded so
    the UI hides the list.

    Args:
        pipeline_active: Whether a consolidate pipeline is currently in flight
            (``pipeline_in_flight`` or any step running). Drives the
            pending-vs-skipped distinction for steps that have not run.
    """
    from web_interface.run_consolidate_enrichment import _PIPELINE_STAGE_LABELS

    # Merge the in-memory ::DATA:: copy: in local/subprocess mode the consolidate
    # worker's pipeline_plan lives in processes[...]["data"] until the process
    # completes, so reading process_stats alone would miss it mid-run.
    entry = {
        **process_stats.get("consolidate_enrichment", {}),
        **(processes.get("consolidate_enrichment", {}).get("data", {}) or {}),
    }
    plan = entry.get("pipeline_plan") or {}
    # Show the list whenever a plan record exists — even one with no downstream
    # steps yet. A marker seeded at dispatch (steps=[]) makes the "Consolidate
    # enrichment data" step appear immediately (live from its status file) so the
    # user isn't left staring at a bare "Consolidation running…" text line while
    # consolidation runs; downstream steps stream in once the worker computes the
    # real plan. Only a truly absent plan hides the list.
    if not plan:
        return []
    steps = plan.get("steps") or []

    started_dt = None
    started_ts = plan.get("started_ts")
    if started_ts:
        try:
            started_dt = datetime.fromisoformat(started_ts)
        except (ValueError, TypeError):
            started_dt = None

    cloud = is_cloud_run()
    view: list[dict] = []
    for step in ["consolidate_enrichment"] + steps:
        ps = process_stats.get(step, {})
        label = _PIPELINE_STAGE_LABELS.get(step, step)

        # Live status: a fresh running/queued state wins. On Cloud Run this comes
        # from the per-step GCS status file; locally from the in-memory process
        # entry (the web service runs the local pipeline thread in-process).
        live_state = None
        percent = None
        message = None
        if cloud:
            st = read_task_status(step) or {}
            raw = (st.get("state") or "").lower()
            fresh = True
            updated = st.get("updated_at")
            if started_dt and updated:
                try:
                    fresh = datetime.fromisoformat(updated) >= started_dt
                except (ValueError, TypeError):
                    fresh = True
            if fresh and raw in ("running", "queued"):
                live_state = raw
                prog = st.get("progress") or {}
                percent = prog.get("percent")
                message = prog.get("message")
        else:
            if (processes.get(step, {}) or {}).get("status") == "running":
                live_state = "running"
                prog = (processes.get(step, {}) or {}).get("progress") or {}
                percent = prog.get("percent")
                message = prog.get("message")

        # Did this step reach a terminal state as part of THIS pipeline run?
        end = ps.get("last_run_end_time")
        end_dt = None
        if end:
            try:
                end_dt = datetime.fromisoformat(end)
            except (ValueError, TypeError):
                end_dt = None
        ran_this_run = end_dt is not None and (started_dt is None or end_dt >= started_dt)

        if live_state:
            state = live_state
        elif ran_this_run:
            state = "success" if ps.get("last_run_outcome") == "Success" else "failed"
        else:
            # Never ran this round: pending while the pipeline is still active,
            # otherwise skipped (aborted before reaching it / dropped by a 429).
            state = "pending" if pipeline_active else "skipped"

        view.append({
            "step": step,
            "label": label,
            "state": state,
            "percent": percent if state == "running" else None,
            "message": message if state == "running" else None,
            "ran_at": end if ran_this_run else None,
        })

    return view
