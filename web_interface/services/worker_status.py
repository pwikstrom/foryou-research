"""Worker/pipeline status helpers shared by the management endpoints.

Pure moves from ``web_interface/routes/management_routes.py`` (Phase 7b) —
worker liveness checks, the consolidate-pipeline step view, per-platform
cookie-health caching, and the acting-user lookup.
"""

import threading
import time as _time
from datetime import UTC, datetime, timedelta

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






# The dispatchable refresh steps, in dependency order. The graph itself lives in
# services/refresh_pipeline; this alias stays because the endpoints, the
# supervisor and the tests read it as the liveness set ("is any pipeline worker
# running right now").
from .refresh_pipeline import DOWNSTREAM_ORDER as PIPELINE_STEPS_ORDER  # noqa: E402
from .refresh_pipeline import LABELS as _PIPELINE_STAGE_LABELS  # noqa: E402


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


def refresh_run_view() -> dict | None:
    """The current refresh run as the page header reads it, or None.

    Names where the run came from and how it ended, so the chart can say
    "Started from Semantic Map by patrik" rather than implying every run is a
    consolidation.
    """
    from .refresh_pipeline import SHORT_LABELS, load_run

    record = load_run(reload=False)
    if not record:
        return None
    return {
        "run_id": record.get("run_id"),
        "origin": record.get("origin"),
        "origin_label": record.get("origin_label") or SHORT_LABELS.get(
            record.get("origin", ""), record.get("origin")),
        "origin_kind": record.get("origin_kind"),
        "started_by": record.get("started_by") or "",
        "started_ts": record.get("started_ts"),
        "finished_ts": record.get("finished_ts"),
        "in_flight": bool(record.get("in_flight")),
        "provisional": bool(record.get("provisional")),
        "mode": record.get("mode") or "refresh",
        "fork_at": record.get("fork_at"),
        "summary": record.get("summary"),
        "partial": bool(record.get("partial")),
        "failed_at": record.get("failed_at"),
        "reason": record.get("reason"),
    }


def _build_pipeline_step_view(pipeline_active: bool) -> list[dict]:
    """Build an ordered per-step view of the current/last refresh run.

    Returns one dict per step — ``consolidate_enrichment`` followed by the whole
    canonical ``PIPELINE_STEPS_ORDER``, ALWAYS, so the chart keeps the same shape
    run to run — with keys ``step``, ``label``, ``state``, ``percent``,
    ``message``, ``ran_at``, ``reason``, ``is_origin``, ``plan_mode`` and
    ``provisional``.

    ``state`` is one of:

    ``running`` / ``queued`` / ``success`` / ``failed``
        What the step is doing, read from its own status file.
    ``pending``
        Planned, not started yet.
    ``pruned``
        Planned, then skipped because its upstream reported no change —
        ``reason`` says which. This is the ordinary outcome of a run whose
        inputs barely moved, and the chart should read as a decision, not a gap.
    ``not_planned``
        Nothing in this run feeds it.
    ``upstream``
        It comes BEFORE this run's origin — a semantic-map run does not
        re-embed. Not part of the run at all.
    ``skipped``
        Planned and never ran: the run was aborted or a dispatch was dropped.

    The three inert states (``pruned``, ``not_planned``, ``upstream``) read no
    status file and carry no timing, so work running outside this run — a
    hand-started refresh, a sessions run chained from a study save — can never
    be painted into it or widen its time axis.

    Live state comes from each step's GCS status file (Cloud Run); terminal
    outcomes fall back to ``process_stats``. Returns ``[]`` when no run has ever
    been recorded, so the UI hides the chart rather than showing eight greyed
    rows that assert a run happened and needed nothing.

    Args:
        pipeline_active: Whether a refresh run is in flight (its own flag, or
            any step running). Drives the pending-vs-skipped distinction for
            steps that have not run.
    """
    from .refresh_pipeline import load_run

    record = load_run(reload=False)
    if not record:
        return []

    steps_plan = record.get("steps") or {}
    provisional = bool(record.get("provisional"))
    plan_mode = record.get("mode") or "refresh"
    origin = record.get("origin")

    # Every canonical step gets a row every run, so the chart's shape never
    # moves with what a given run happened to need. A planned step outside the
    # canonical order (shouldn't happen, but a record is data) is appended so it
    # can never lose its row.
    rows = ["consolidate_enrichment"] + list(PIPELINE_STEPS_ORDER)
    rows += [s for s in steps_plan if s not in rows]

    started_dt = None
    started_ts = record.get("started_ts")
    if started_ts:
        try:
            started_dt = datetime.fromisoformat(started_ts)
        except (ValueError, TypeError):
            started_dt = None

    # A provisional plan describes a run still ahead of itself, so its un-run
    # steps stay "pending" until the origin has finished — otherwise they read
    # as "skipped" in the seconds between dispatch and the first status write.
    origin_done = False
    origin_end = process_stats.get(origin, {}).get("last_run_end_time") if origin else None
    if origin_end:
        try:
            origin_done = (started_dt is None
                           or datetime.fromisoformat(origin_end) >= started_dt)
        except (ValueError, TypeError):
            origin_done = False
    steps_pending = pipeline_active or (provisional and not origin_done)

    cloud = is_cloud_run()
    view: list[dict] = []
    for step in rows:
        ps = process_stats.get(step, {})
        label = _PIPELINE_STAGE_LABELS.get(step, step)
        plan_state = (steps_plan.get(step) or {}).get("state") or "not_planned"

        # Inert rows: no status read and no process_stats fallback. A step can
        # run outside this run while it is in flight (a sessions refresh chained
        # from a study save) and that foreign work must not be drawn as part of
        # the run, nor stretch its axis.
        if plan_state in ("pruned", "not_planned", "upstream"):
            view.append({
                "step": step, "label": label, "state": plan_state,
                "reason": (steps_plan.get(step) or {}).get("reason"),
                "is_origin": False,
                "percent": None, "message": None, "ran_at": None,
                "started_at": None, "ended_at": None, "queued_at": None,
                "duration_s": None, "plan_mode": plan_mode, "provisional": False,
            })
            continue

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

        # Wall-clock bounds for the pipeline chart, which measures time rather
        # than progress. A live step's start comes from its status file; a
        # finished step's from its recorded end minus its recorded duration —
        # for a self-chaining step (timelines, sessions) that duration spans
        # the whole chain, so the bar does too. A queued leaf has no start yet,
        # only the moment it was stamped queued at the fork.
        started_at = None
        ended_at = None
        queued_at = None
        duration_s = None
        if state == "running":
            if cloud:
                started_at = st.get("start_time")
            else:
                local_start = (processes.get(step, {}) or {}).get("start_time")
                started_at = (local_start.isoformat() if hasattr(local_start, "isoformat")
                              else local_start)
        elif state == "queued":
            queued_at = st.get("updated_at") if cloud else None
        elif ran_this_run:
            ended_at = end
            dur = ps.get("last_run_duration")
            try:
                duration_s = float(dur) if dur is not None else None
                if duration_s is not None and end_dt is not None:
                    started_at = (end_dt - timedelta(seconds=duration_s)).isoformat()
            except (TypeError, ValueError):
                duration_s = None

        view.append({
            "step": step,
            "label": label,
            "state": state,
            "reason": (steps_plan.get(step) or {}).get("reason"),
            "is_origin": step == origin,
            "percent": percent if state == "running" else None,
            "message": message if state == "running" else None,
            "ran_at": end if ran_this_run else None,
            "started_at": started_at,
            "ended_at": ended_at,
            "queued_at": queued_at,
            "duration_s": duration_s,
            # Repeated on every row so the renderer can word the unplanned rows
            # ("not needed" vs "not requested") without an envelope.
            "plan_mode": plan_mode,
            # True while this row is part of the plan made at dispatch rather
            # than one the run has confirmed by getting there.
            "provisional": provisional and step != origin,
        })

    return view
