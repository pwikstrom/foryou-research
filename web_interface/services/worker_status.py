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

from ..process_manager import SCRAPER_PROCESS_NAMES, process_stats, processes
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
# that meta_refresh_groups / pca_refresh consume. Both membership and order are
# load-bearing — this is the liveness check AND the forecast plan the consolidate
# dispatch seeds, which is what the user reads off the step list before the
# worker has computed the real one.
PIPELINE_STEPS_ORDER = [
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
    "sessions_refresh",
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
    """Return the names of scraper/annotator workers currently running.

    Scraper processes are per-platform (``queue_scraper_<platform>``, derived
    from the scrape contract) — the list must come from
    ``SCRAPER_PROCESS_NAMES``, not a hardcoded legacy name.
    """
    blocking = []
    for name in [*SCRAPER_PROCESS_NAMES, "queue_annotator", "queue_annotator_batch"]:
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


def consolidate_entry_view() -> dict:
    """The consolidate entry as the UI should read it, across both run modes.

    ``processes["consolidate_enrichment"]["data"]`` is the SUBPROCESS
    ``::DATA::`` mirror: in local dev it carries the worker's emissions while
    the process is alive, before ``monitor_process_completion`` folds them into
    ``process_stats``, so it must win there. On Cloud Run the worker runs in the
    other service and nothing in this process ever updates that dict — but the
    dispatch endpoints still seed it, so overlaying it there pinned whichever
    web instance served the click to the marker it wrote (``steps: []``) while
    every other instance served the real plan. The browser polls every 2s, the
    poll lands on a different instance each time, and the step list flips
    between one row and the full pipeline. Reading it only in subprocess mode
    removes the per-instance divergence entirely.
    """
    entry = dict(process_stats.get("consolidate_enrichment", {}))
    if not is_cloud_run():
        entry.update(processes.get("consolidate_enrichment", {}).get("data", {}) or {})
    return entry


def _build_pipeline_step_view(pipeline_active: bool) -> list[dict]:
    """Build an ordered per-step view of the last/active consolidate pipeline.

    Returns one dict per step (``consolidate_enrichment`` plus every step in the
    persisted ``pipeline_plan``) with keys ``step``, ``label``, ``state``,
    ``percent``, ``message``, ``ran_at`` and ``provisional``. ``state`` is one of
    ``running``, ``queued``, ``success``, ``failed``, ``skipped`` or ``pending``.
    Live state comes from each step's GCS status file (Cloud Run); terminal
    outcomes fall back to ``process_stats``. ``provisional`` marks a row that
    belongs to the dispatch-time forecast rather than the plan the consolidation
    computed. Returns ``[]`` when no plan has been recorded so the UI hides the
    list.

    Args:
        pipeline_active: Whether a consolidate pipeline is currently in flight
            (``pipeline_in_flight`` or any step running). Drives the
            pending-vs-skipped distinction for steps that have not run — as does
            an unconfirmed forecast plan, which keeps its steps pending until the
            consolidation that would confirm them has ended.
    """
    from web_interface.run_consolidate_enrichment import _PIPELINE_STAGE_LABELS

    entry = consolidate_entry_view()
    plan = entry.get("pipeline_plan") or {}
    # Show the list whenever a plan record exists. The marker seeded at dispatch
    # carries the FORECAST pipeline (every downstream step, flagged provisional)
    # so the whole list is visible with live pending/running/success from the
    # first poll — the user should not have to wait out the consolidation to
    # learn what is coming. Once the worker computes the real plan it replaces
    # the forecast and any step this run doesn't need drops off. Only a truly
    # absent plan hides the list.
    if not plan:
        return []
    steps = plan.get("steps") or []
    provisional = bool(plan.get("provisional"))

    started_dt = None
    started_ts = plan.get("started_ts")
    if started_ts:
        try:
            started_dt = datetime.fromisoformat(started_ts)
        except (ValueError, TypeError):
            started_dt = None

    # A forecast plan describes a run that is by definition still ahead of
    # itself, so its un-run steps stay "pending" until the consolidation that
    # will confirm them has finished — without this they read as "skipped"
    # in the seconds between dispatch and the consolidate step's first status
    # write, and again in the gap before the real plan lands.
    consolidate_end = process_stats.get("consolidate_enrichment", {}).get("last_run_end_time")
    consolidate_done = False
    if consolidate_end:
        try:
            consolidate_done = (started_dt is None
                                or datetime.fromisoformat(consolidate_end) >= started_dt)
        except (ValueError, TypeError):
            consolidate_done = False
    steps_pending = pipeline_active or (provisional and not consolidate_done)

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
            state = "pending" if steps_pending else "skipped"

        view.append({
            "step": step,
            "label": label,
            "state": state,
            "percent": percent if state == "running" else None,
            "message": message if state == "running" else None,
            "ran_at": end if ran_this_run else None,
            # True while this row is part of the dispatch-time forecast rather
            # than the plan the consolidation actually computed.
            "provisional": provisional and step != "consolidate_enrichment",
        })

    return view
