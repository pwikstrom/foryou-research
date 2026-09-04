"""Drive the automatic enrichment loop for armed collections.

One short tick. It plans and dispatches at most one thing, then returns — it never
scrapes, annotates or consolidates itself, and it never self-chains. That keeps it
far inside the default Cloud Tasks dispatch deadline, so the duplicate-chain trap
(a link outliving its deadline and being re-dispatched from scratch) cannot apply.

The queues, the workers and the consolidation pipeline are global singletons, so
this is a conductor, not an executor: it feeds
``cache/to_scrape_<platform>.json`` and ``cache/to_annotate.json`` and starts the
existing workers, one at a time.

THE CYCLE, per tick::

    0. GATE      any enrichment/consolidate worker running, or a drain lease held
                 -> do nothing (whoever is running will trigger the next tick)
    1. DRAIN     scrape queue non-empty -> start the platform's scraper
                 annotation queue non-empty -> start the annotator
    2. SETTLE    worker results not yet consolidated -> consolidate
                 after scraping   : auto_refresh False (only enrichment_status is
                                    needed, to decide the handoff)
                 after annotating : auto_refresh True  (the one full downstream
                                    refresh of the cycle)
    3. HANDOFF   items now provably scraped -> append to the annotation queue and
                 charge them to the collection's budget
    4. PLAN      cut the next A+B slice for one collection -> scrape queue

which unrolls to plan -> scrape -> consolidate -> annotate -> consolidate -> plan,
exactly the loop an operator runs by hand today.

Triggers: a terminal scraper/annotator completion, the end of a consolidation, and
an hourly Cloud Scheduler heartbeat that restarts a loop stalled by a lost
dispatch. All three are safe to fire concurrently — the tick re-reads the queues
and task status, and every ledger write is a compare-and-set.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import fyp.data_io as data_io
from web_interface.services import collection_enrichment as ce
from web_interface.task_status import TaskStatusReporter

# Consecutive cycles a collection may enqueue work without a single new scrape
# landing before its plan parks itself. Named here for the worker's own log copy;
# the threshold lives with the rest of the plan semantics.
_MAX_STALLS = ce.MAX_STALLS


def _admin_kill_switch() -> bool:
    """True when automatic enrichment is enabled site-wide.

    Default off: an unattended loop that spends money should be something an
    operator turns on deliberately, after watching one collection run.
    """
    try:
        from web_interface import admin_settings
        return bool(admin_settings.get_setting("auto_enrichment_enabled"))
    except Exception:
        return False


def _hard_gate() -> list[str]:
    """What blocks the WHOLE tick: consolidation, pipeline steps, drain leases.

    Deliberately narrower than the old all-workers gate: a running scraper or
    annotator no longer freezes the loop — those are per-LANE concerns (see
    ``_scrape_lane_busy`` / ``_annotate_lane_busy``), which is what lets the
    next cycle's scrape run inside the current cycle's annotation window.
    """
    from web_interface.services.worker_status import (
        PIPELINE_STEPS_ORDER, _is_worker_running,
    )

    blocking = []
    for name in ["consolidate_enrichment", *PIPELINE_STEPS_ORDER]:
        if _is_worker_running(name):
            blocking.append(name)
    try:
        from web_interface import drain_lease
        blocking += [f"local drain ({p})" for p in sorted(drain_lease.active_drain_leases())]
    except Exception:
        pass
    return blocking


def _scrape_lane_busy(platform: str) -> bool:
    """True while this platform's scraper is running."""
    from web_interface.services.worker_status import _is_worker_running
    return _is_worker_running(f"queue_scraper_{platform}")


def _annotate_lane_busy() -> bool:
    """True while either annotator variant is running.

    Both variants gate the lane: the synchronous annotator's queue prune is
    not lost-update safe against the batch worker's claims, so the two must
    never overlap — and the lane check is what enforces that here.
    """
    from web_interface.services.worker_status import _is_worker_running
    return _is_worker_running("queue_annotator") or _is_worker_running("queue_annotator_batch")


def _in_flight_annotation_ids() -> set[str]:
    """Ids claimed by the batch annotator's in-flight Gemini jobs.

    Enrichment status knows nothing about claims, so a handoff computed from
    it would re-queue (and re-pay for) items a job is annotating RIGHT NOW.
    Reads both job-state shapes: the format-2 job table and the legacy
    single-job file a pre-table chain may still be carrying.
    """
    try:
        state = data_io.load_json(storage_location="cache",
                                  filename="annotate_batch_job.json")
    except Exception:
        return set()
    if not isinstance(state, dict):
        return set()
    jobs = state.get("jobs")
    if isinstance(jobs, list):
        return {str(i) for j in jobs if isinstance(j, dict)
                for i in (j.get("submitted_ids") or [])}
    return {str(i) for i in (state.get("submitted_ids") or [])}


def _pipeline_in_flight() -> bool:
    """True in the gap between one pipeline step ending and the next booting."""
    from web_interface.process_manager import load_process_stats, process_stats
    load_process_stats()
    return bool(process_stats.get("consolidate_enrichment", {}).get("pipeline_in_flight"))


def _unconsolidated() -> str | None:
    """Which worker has produced results the consolidation has not yet seen.

    Returns ``"scrape"``, ``"annotate"`` or None. The comparison mirrors the
    Data Management banner: newest worker success versus ``last_consolidation``.
    """
    from web_interface.process_manager import (
        SCRAPER_PROCESS_NAMES, load_process_stats, process_stats,
    )
    load_process_stats()
    consolidated = process_stats.get("consolidate_enrichment", {}).get("last_consolidation") or ""

    def _newest(names) -> str:
        return max((process_stats.get(n, {}).get("last_success") or "" for n in names),
                   default="")

    scraped = _newest(SCRAPER_PROCESS_NAMES + ["queue_scraper"])
    annotated = _newest(["queue_annotator", "queue_annotator_batch"])

    # ISO-8601 timestamps sort lexically. Annotation wins a tie: it is the step
    # whose consolidation must carry the full downstream refresh.
    if annotated and annotated > consolidated:
        return "annotate"
    if scraped and scraped > consolidated:
        return "scrape"
    return None


def _annotator_process() -> str:
    """The annotator to run: the async batch worker when the backend supports it.

    The Gemini Batch API is roughly half the price of the synchronous path,
    which matters for a loop meant to run unattended for days. Any doubt about
    the capability falls back to the synchronous annotator, which works with
    every backend.
    """
    try:
        from fyp.annotation.backends import active_backend_name, get_backend
        if get_backend(active_backend_name()).supports_batch_mode:
            return "queue_annotator_batch"
    except Exception:
        pass
    return "queue_annotator"


# Subprocess-mode script paths for the workers this tick starts. On Cloud Run
# start_process dispatches a Cloud Task and ignores the path; locally it spawns
# the script, so both modes need an entry here.
def _script_for(name: str):
    from fyp.fyp_config import (
        CONSOLIDATE_ENRICHMENT_SCRIPT, QUEUE_ANNOTATOR_BATCH_SCRIPT,
        QUEUE_ANNOTATOR_SCRIPT, QUEUE_SCRAPER_SCRIPT,
    )
    if name.startswith("queue_scraper"):
        return QUEUE_SCRAPER_SCRIPT
    return {
        "queue_annotator": QUEUE_ANNOTATOR_SCRIPT,
        "queue_annotator_batch": QUEUE_ANNOTATOR_BATCH_SCRIPT,
        "consolidate_enrichment": CONSOLIDATE_ENRICHMENT_SCRIPT,
    }[name]


def _start(name: str, task_args: dict | None = None) -> tuple[bool, str]:
    from web_interface.process_manager import start_process
    args = []
    if name.startswith("queue_scraper_"):
        args = ["--platform", name[len("queue_scraper_"):]]
    return start_process(name, _script_for(name), args, task_args=task_args or {},
                         started_by="enrichment_supervisor")


def _scraper_blocked(platform: str) -> str | None:
    """A storm/circuit-breaker abort the operator has to clear, if any.

    The scraper already raises these; the supervisor must not keep restarting it
    into a bot wall.
    """
    from web_interface.task_status import read_task_status
    status = read_task_status(f"queue_scraper_{platform}") or {}
    data = status.get("data") or {}
    for flag in ("permanent_storm_tripped", "circuit_breaker_tripped"):
        if data.get(flag):
            return flag
    return None


def run_enrichment_supervisor(reporter: TaskStatusReporter,
                              task_args: dict | None = None) -> dict | None:
    """Advance the enrichment loop by one step. Never raises into the task runner."""
    task_args = task_args or {}
    started = time.perf_counter()
    forced = str(task_args.get("collection_id") or "")

    if not forced and not _admin_kill_switch():
        reporter.log("Automatic enrichment is switched off site-wide "
                     "(Admin -> Settings -> auto_enrichment_enabled).")
        reporter.emit_data({"action": "disabled"})
        return None

    blocking = _hard_gate()
    if blocking or _pipeline_in_flight():
        reporter.log(f"Busy, nothing to do: {', '.join(blocking) or 'pipeline in flight'}.")
        reporter.emit_data({"action": "busy", "blocking": blocking})
        return None

    # Backstop: a deferred refresh older than FINALIZE_BACKSTOP_H fires even
    # mid-plan (and even with nothing armed — a quiet finalize whose dispatch
    # failed must not strand the debt behind "No armed collections").
    backstop = _finalize(reporter, require_backstop=True)
    if backstop:
        reporter.update_progress(100, backstop.get("message") or backstop["action"])
        reporter.emit_data(backstop)
        return None

    plans = ce.armed_plans()
    if forced:
        entry = ce.get_plan(forced)
        plans = {forced: entry} if entry else {}
    if not plans:
        quiet = _finalize(reporter)
        if quiet:
            reporter.update_progress(100, quiet.get("message") or quiet["action"])
            reporter.emit_data(quiet)
            return None
        reporter.log("No armed collections.")
        reporter.emit_data({"action": "idle"})
        return None

    reporter.log(f"{len(plans)} armed collection(s).")
    outcome = (_drain(reporter, plans)
               or _settle(reporter)
               or _handoff(reporter, plans)
               or _plan(reporter, plans)
               or _finalize(reporter)
               or {"action": "nothing_to_do"})

    # A handoff fills the annotation queue but, on its own, leaves the annotator
    # for a later tick — and ticks fire only at terminal worker completions, so
    # this tick is the cycle BOUNDARY and must perform the whole move itself:
    # start the annotator on the handed-off backlog, then cut and start the next
    # scrape slice so it runs inside the annotation window.
    if outcome.get("action") == "handoff":
        message = (f"Queued {outcome.get('queued')} item(s) for annotation")
        follow = _drain(reporter, plans)
        if follow:
            message += " and started the annotator"
            outcome = {**follow, "handoff_queued": outcome.get("queued")}
        # The next slice: scrape-lane only — the annotator just dispatched may
        # not read as running yet, and a second annotate start would double it.
        plan_out = _plan(reporter, plans)
        if plan_out and plan_out.get("action") == "plan":
            # The in-memory entry may predate its first cycle and lack the
            # platform the ledger save just recorded — patch it in so the
            # armed-platforms check sees the queue we just filled.
            pcid = plan_out.get("collection_id")
            plans = {**plans, pcid: {**(plans.get(pcid) or {}),
                                     "platform": plan_out.get("platform")}}
            drain2 = _drain(reporter, plans, lanes=("scrape",))
            if drain2 and drain2.get("action") == "scrape":
                message += (f"; queued the next slice "
                            f"({plan_out.get('queued')} item(s)) and started the scraper")
            else:
                message += f"; queued the next slice ({plan_out.get('queued')} item(s))"
        outcome = {**outcome, "message": message + "."}

    reporter.update_progress(100, outcome.get("message") or outcome["action"])
    reporter.emit_data(outcome)
    reporter.log(f"[TIMING] enrichment_supervisor total={time.perf_counter() - started:.1f}s")
    # Returning None: this worker never chains. The loop is advanced by the next
    # trigger (a worker completion, a consolidation, or the hourly heartbeat),
    # which keeps every tick short and re-checks the world from scratch.
    return None


# --------------------------------------------------------------------------- #
# Step 1 — drain the queues
# --------------------------------------------------------------------------- #

def _queue_stalled(reporter, plans: dict, guard_key: str, queue_len: int,
                   label: str) -> bool:
    """No-drain guard for either queue: park the plans when a queue's length is
    unchanged across repeated supervisor-started worker runs.

    Covers every "worker runs but the queue never shrinks" mode in one place:
    annotation results that cannot be refined, a scraper whose failures are all
    transient (no storm flag, nothing pruned), a bot wall the storm detector
    missed. Two identical lengths in a row = the third attempt is not made.
    Found live in the first local end-to-end run (annotate side).

    Returns:
        True when the caller must NOT start the worker (plans were parked).
    """
    guard = ce.get_meta(guard_key) or {}
    strikes = (int(guard.get("strikes") or 0) + 1
               if int(guard.get("len") or -1) == queue_len else 0)
    if strikes >= 2:
        reporter.log(f"The {label} queue is not draining ({queue_len} item(s) "
                     f"across {strikes + 1} runs) — parking all armed plans.")
        for cid in plans:
            ce.save_plan(cid, {"state": ce.STATE_BLOCKED,
                               "last_error": f"{label} queue not draining"})
        ce.set_meta(guard_key, None)
        return True
    ce.set_meta(guard_key, {"len": queue_len, "strikes": strikes})
    return False


def _drain(reporter, plans: dict, lanes: tuple = ("scrape", "annotate")) -> dict | None:
    """Start workers for whichever queues hold work and whose lane is free.

    The two lanes are independent: a scraper can start while the annotator is
    mid-job and vice versa (they share no files — the scraper writes scrape
    parquets and its own queue, the annotator claims from ``to_annotate.json``
    and writes refined annotation parquets). At most one scraper plus the
    annotator per tick. The stall guards are evaluated ONLY when the lane is
    free — a queue that is merely waiting for its busy worker is not stalled,
    so the guards can never strike while jobs are legitimately in flight.

    Args:
        reporter: Status reporter.
        plans: The armed plans this tick serves.
        lanes: Which lanes to consider — the boundary move restricts the
            post-plan drain to ("scrape",) because the annotator it just
            started may not show as running yet (double-dispatch guard).
    """
    from fyp.scrape import scrape_queues

    scrape_outcome = None
    if "scrape" in lanes:
        lengths = scrape_queues.queue_lengths()
        # Serve only platforms an armed collection actually uses, so a leftover
        # queue from manual admin work does not keep the loop busy forever.
        armed_platforms = {str(e.get("platform") or "") for e in plans.values()}
        for platform, count in sorted(lengths.items()):
            if not count:
                # A drained queue clears its stall guard — otherwise a later
                # fill that happens to match the stale guarded length would
                # strike spuriously and park healthy plans.
                if ce.get_meta(f"scrape_guard_{platform}") is not None:
                    ce.set_meta(f"scrape_guard_{platform}", None)
                continue
            if platform not in armed_platforms:
                continue
            if _scrape_lane_busy(platform):
                continue  # already being drained
            tripped = _scraper_blocked(platform)
            if tripped:
                reporter.log(f"Scraper for '{platform}' is held off: {tripped}. "
                             f"Pausing the affected plans.")
                for cid, entry in plans.items():
                    if str(entry.get("platform") or "") == platform:
                        ce.save_plan(cid, {"state": ce.STATE_BLOCKED,
                                           "last_error": f"scraper {tripped}"})
                continue
            platform_plans = {cid: e for cid, e in plans.items()
                              if str(e.get("platform") or "") == platform}
            if _queue_stalled(reporter, platform_plans or plans,
                              f"scrape_guard_{platform}", count, f"{platform} scrape"):
                return {"action": "scrape_stalled", "platform": platform,
                        "queued": count,
                        "message": f"The {platform} scrape queue is not draining; plans parked."}
            ok, msg = _start(f"queue_scraper_{platform}")
            reporter.log(f"Scraping {count} queued {platform} item(s): {msg}")
            scrape_outcome = {"action": "scrape", "platform": platform, "queued": count,
                              "started": ok, "message": f"Started the {platform} scraper."}
            break

    annotate_outcome = None
    if "annotate" in lanes:
        queue = data_io.load_json(storage_location="cache",
                                  filename=ce.ANNOTATE_QUEUE_FILENAME) or []
        if isinstance(queue, list) and queue:
            if _annotate_lane_busy():
                pass  # already being drained (or claimed into in-flight jobs)
            elif _queue_stalled(reporter, plans, "annotate_guard", len(queue),
                                "annotation"):
                return {"action": "annotate_stalled", "queued": len(queue),
                        "message": "The annotation queue is not draining; plans parked."}
            else:
                name = _annotator_process()
                ok, msg = _start(name)
                reporter.log(f"Annotating {len(queue)} queued item(s) via {name}: {msg}")
                annotate_outcome = {"action": "annotate", "queued": len(queue),
                                    "started": ok, "message": f"Started {name}."}
        else:
            # An empty queue clears the guard: whatever was stuck has drained.
            if ce.get_meta("annotate_guard") is not None:
                ce.set_meta("annotate_guard", None)

    if scrape_outcome and annotate_outcome:
        return {**scrape_outcome,
                "message": f"{scrape_outcome['message']} {annotate_outcome['message']}"}
    return scrape_outcome or annotate_outcome


# --------------------------------------------------------------------------- #
# Step 2 — make results visible
# --------------------------------------------------------------------------- #

def _settle(reporter) -> dict | None:
    """Consolidate when a worker's results are not yet in enrichment status.

    Always core-only (``auto_refresh=False``): all the loop needs from a
    consolidation is ``enrichment_status.parquet`` — the expensive downstream
    chain is deferred and runs once, at finalize, when the loop goes quiet (or
    when the staleness backstop expires). The consolidation itself records the
    deferred impact.

    Consolidation requires every worker quiet. When results are pending but a
    lane is still busy, the tick STOPS here (``waiting_consolidate``) rather
    than falling through to HANDOFF/PLAN on stale enrichment status — that
    stop is what bounds cross-cycle overlap to one cycle.
    """
    from web_interface.services.worker_status import _workers_blocking_consolidate

    kind = _unconsolidated()
    if not kind:
        return None
    blocking = _workers_blocking_consolidate()
    if blocking:
        reporter.log(f"Results from {kind} await consolidation; waiting for "
                     f"{', '.join(blocking)} to finish first.")
        return {"action": "waiting_consolidate", "after": kind,
                "blocking": blocking,
                "message": f"Waiting for {', '.join(blocking)} before consolidating."}
    # plan_deferred marks this debt as the LOOP's, so _finalize may spend it.
    # An operator's own consolidate-without-refresh writes the same ledger entry
    # without the flag and is left alone.
    ok, msg = _start("consolidate_enrichment",
                     {"auto_refresh": False, "plan_deferred": True})
    reporter.log(f"Consolidating after {kind} (downstream refresh deferred): {msg}")
    return {"action": "consolidate", "after": kind, "auto_refresh": False,
            "started": ok, "message": f"Consolidating after {kind}."}


# --------------------------------------------------------------------------- #
# Finalize — the one full downstream refresh per plan
# --------------------------------------------------------------------------- #

# How stale the analyses may get mid-plan before the deferred refresh fires
# anyway. The quiet-path finalize normally settles the debt when every armed
# plan is done; the backstop covers plans that run for days, stall, or are
# never finished.
FINALIZE_BACKSTOP_H = 24


def _finalize(reporter, require_backstop: bool = False) -> dict | None:
    """Dispatch the deferred downstream pipeline when its moment has come.

    Quiet path (``require_backstop=False``): reached when DRAIN/SETTLE/
    HANDOFF/PLAN all found nothing to do — the loop is going quiet, so the
    accumulated impact gets its one full refresh. Backstop path: checked right
    after the hard gate on every tick, fires even mid-plan once the debt is
    older than ``FINALIZE_BACKSTOP_H`` (safe while lanes are busy — the
    pipeline steps read consolidated stores, not raw worker output).
    """
    from web_interface.services import downstream_refresh

    deferred = downstream_refresh.get_deferred_impact()
    if not deferred:
        return None
    if not deferred.get("from_plan"):
        # The operator consolidated and chose not to refresh. That choice is
        # theirs to reverse with "Refresh All Affected"; spending it here
        # overrode it silently 3.5 minutes later (2026-09-04). The impact panel
        # keeps the debt visible, so nothing is lost by waiting.
        return None
    if not require_backstop:
        # "Quiet" means the LOOP is quiet, not merely this tick: a tick where
        # everything is WAITING (scraper mid-run, jobs in flight, nothing to
        # start) also falls through to here, and refreshing then would block
        # the loop behind the pipeline for the rest of the cycle — observed
        # live 2026-09-01, one tick after a boundary move.
        from web_interface.services.worker_status import _workers_blocking_consolidate
        if _workers_blocking_consolidate():
            return None
    if require_backstop:
        ref = downstream_refresh.last_full_refresh() or deferred.get("deferred_since")
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(str(ref))).total_seconds() / 3600
        except (ValueError, TypeError):
            age_h = None
        if age_h is not None and age_h < FINALIZE_BACKSTOP_H:
            return None
        reporter.log(f"Deferred-refresh backstop: the analyses are "
                     f"{age_h:.0f}h stale — refreshing now, mid-plan." if age_h is not None
                     else "Deferred-refresh backstop: staleness unknown — refreshing now.")

    status, msg = downstream_refresh.dispatch_downstream_refresh(None)
    if status == "started":
        reporter.log(f"Deferred downstream refresh dispatched "
                     f"(impact spanned {deferred.get('runs', '?')} consolidation(s)).")
        return {"action": "finalize",
                "message": "Started the deferred analysis refresh."}
    if status == "noop":
        # The debt builds an empty pipeline (e.g. its studies were deleted) —
        # settle it so the loop does not retry forever.
        downstream_refresh.settle_deferred_impact()
        return None
    reporter.log(f"Deferred refresh not started ({status}): {msg}")
    return None


# --------------------------------------------------------------------------- #
# Step 3 — scrape -> annotate handoff
# --------------------------------------------------------------------------- #

def _handoff(reporter, plans: dict) -> dict | None:
    """Queue every armed collection's newly scraped items for annotation.

    This is the only place items enter the annotation queue. It reads enrichment
    status (never a cycle's intent — an unscraped id in that queue is refined as
    ``annotated_fail`` and burnt for good). It sweeps every scraped-but-
    unannotated video of the collection, bounded by the plan's annotation
    target — the cheapest step toward the target, always taken before any new
    scraping (this step outranks the plan step, so the ordering is free).
    """
    # Ids the batch annotator has claimed into in-flight Gemini jobs are
    # invisible to enrichment status (still scraped-but-unannotated there);
    # re-queueing one would annotate — and pay for — it twice.
    claimed = _in_flight_annotation_ids()
    total = 0
    served = []
    for cid, entry in plans.items():
        try:
            result = ce.handoff_scraped(cid, entry)
        except Exception as exc:
            reporter.log(f"Handoff check for {cid} failed: {exc}")
            continue
        ready = [i for i in result["ready"] if str(i) not in claimed]
        if len(ready) != len(result["ready"]):
            reporter.log(f"{cid}: {len(result['ready']) - len(ready)} item(s) already "
                         f"in an in-flight annotation job — not re-queued.")
        if not ready:
            # Still persist the pruned in-flight set: resolved ids (annotated
            # or permanently failed) must leave it even on a no-op handoff.
            if result["in_flight"] != list(entry.get("in_flight") or []):
                ce.save_plan(cid, {"in_flight": result["in_flight"]})
            continue
        n = ce.queue_for_annotation(ready)
        ce.save_plan(cid, {
            "spent_items": int(entry.get("spent_items") or 0) + n,
            "in_flight": result["in_flight"],
            "stall_count": 0,
            "last_error": None,
        })
        reporter.log(f"{cid}: {n} scraped item(s) handed to annotation.")
        total += n
        served.append(cid)
    if not total:
        return None
    return {"action": "handoff", "queued": total, "collections": served,
            "message": f"Queued {total} item(s) for annotation."}


# --------------------------------------------------------------------------- #
# Step 4 — cut the next slice
# --------------------------------------------------------------------------- #

def _auto_cycle_items(entry: dict, activity, status) -> int:
    """Size an Auto plan's cycle: fill the annotation lane, never overshoot.

    ``min(target headroom, one full set of concurrent Gemini jobs)`` — a cycle
    whose annotation is always ~one job turnaround, with a scrape that roughly
    fills that window. Headroom subtracts PENDING work (queued or claimed into
    an in-flight job) because enrichment status has not seen it yet; without
    that, every overlapped cycle would re-count the previous cycle's items.

    Returns:
        The effective cycle_items; 0 when pending work already covers the
        target (the caller skips the slice, not the plan).
    """
    from web_interface.run_queue_annotator_batch import (
        DEFAULT_BATCH_SIZE, MAX_CONCURRENT_JOBS,
    )

    settings = {**ce.DEFAULT_SETTINGS, **(entry.get("settings") or {})}
    target = int(settings.get("annotation_target") or 0)
    annotated = ce._annotated_unique(activity, status)
    collection_ids = {str(i) for i in activity["item_id"].unique()}
    queue = data_io.load_json(storage_location="cache",
                              filename=ce.ANNOTATE_QUEUE_FILENAME) or []
    pending = ({str(i) for i in queue} | _in_flight_annotation_ids()) & collection_ids
    headroom = max(0, target - annotated - len(pending))
    if headroom <= 0:
        return 0
    return min(headroom, MAX_CONCURRENT_JOBS * DEFAULT_BATCH_SIZE, 20_000)


def _plan(reporter, plans: dict) -> dict | None:
    """Serve one collection: cut its next A+B slice into the scrape queue.

    One collection per cycle keeps the scrape queue legible and bounds how much
    the loop can commit before the next consolidation reports back.
    """
    from fyp.scrape import scrape_queues

    scrapeable = set(scrape_queues.registered_platforms())
    # Round-robin by least-recently-served, so several armed collections make
    # progress together instead of the first one monopolising the loop.
    order = sorted(plans.items(), key=lambda kv: str(kv[1].get("last_cycle_at") or ""))

    for cid, entry in order:
        try:
            activity = ce.load_activity(cid)
            if activity is None or activity.empty:
                ce.save_plan(cid, {"state": ce.STATE_DONE,
                                   "last_error": "no viewing activity"})
                reporter.log(f"{cid}: no viewing activity; plan closed.")
                continue

            platform = ce.collection_platform(activity)
            if platform not in scrapeable:
                ce.save_plan(cid, {"state": ce.STATE_BLOCKED, "platform": platform,
                                   "last_error": f"no scraper registered for '{platform}'"})
                reporter.log(f"{cid}: no scraper for '{platform}'; plan blocked.")
                continue
            if _scrape_lane_busy(platform):
                # The platform's scraper is mid-run; cutting another slice now
                # would stretch that run and skew its stall accounting. The
                # boundary tick after it finishes serves this collection.
                reporter.log(f"{cid}: the {platform} scraper is still running; "
                             f"slice deferred.")
                continue

            settings = {**ce.DEFAULT_SETTINGS, **(entry.get("settings") or {})}
            auto_items = None
            status = None
            if settings.get("cycle_items_auto"):
                status = ce.load_status(activity["item_id"].unique())
                auto_items = _auto_cycle_items(entry, activity, status)
                if auto_items == 0:
                    # Target headroom is fully covered by pending work (queued
                    # or in an in-flight job) — cutting more would overshoot.
                    reporter.log(f"{cid}: pending annotations already cover the "
                                 f"target; no new slice this cycle.")
                    continue
                entry = {**entry,
                         "settings": {**settings, "cycle_items": auto_items}}
                reporter.log(f"{cid}: auto items-per-cycle = {auto_items:,}.")

            result = ce.plan_cycle(cid, entry, activity=activity, status=status)
            items = result["item_ids"]

            if not items:
                ce.save_plan(cid, {"state": ce.STATE_DONE, "platform": platform,
                                   "a_cursor": result["a_cursor"],
                                   "b_cursor": result["b_cursor"],
                                   "finished_at": ce.now_iso()})
                reporter.log(f"{cid}: nothing left to enrich; plan complete.")
                _notify_owner(reporter, cid, entry)
                continue

            # A cycle that enqueues work but never produces a scrape is chasing
            # something unscrapeable (a bot wall, deleted posts). Park it rather
            # than let it spend forever.
            stalls = int(entry.get("stall_count") or 0)
            if stalls >= _MAX_STALLS:
                ce.save_plan(cid, {"state": ce.STATE_BLOCKED,
                                   "last_error": f"no scrape progress in {stalls} cycles"})
                reporter.log(f"{cid}: no scrape progress in {stalls} cycles; plan blocked.")
                continue

            scrape_queues.append_to_scrape_queue(platform, items)
            ce.save_plan(cid, {
                # Informational: what Auto resolved to this cycle — the
                # panel's disabled input displays it (None in manual mode).
                **({"last_auto_cycle_items": auto_items}
                   if auto_items is not None else {}),
                # The plan's record of queued scrapes — what stall detection
                # reads. (It no longer scopes the handoff, which sweeps the
                # whole collection's scraped-but-unannotated set.)
                "in_flight": sorted(set(str(i) for i in (entry.get("in_flight") or []))
                                    | set(items)),
                "platform": platform,
                "a_cursor": result["a_cursor"],
                "b_cursor": result["b_cursor"],
                "cycles": int(entry.get("cycles") or 0) + 1,
                "stall_count": stalls + 1,   # cleared by the next successful handoff
                "last_cycle_at": ce.now_iso(),
                "last_batch": {"a": result["a"], "b": result["b"],
                               "total": len(items)},
                "last_error": None,
            })
            reporter.log(f"{cid}: queued {len(items)} item(s) to scrape "
                         f"({result['b']} deep-dive, {result['a']} spread); "
                         f"back to {result['b_cursor']} / {result['a_cursor']}.")
            return {"action": "plan", "collection_id": cid, "queued": len(items),
                    "a": result["a"], "b": result["b"], "platform": platform,
                    "message": f"Queued {len(items)} item(s) for {cid}."}
        except Exception as exc:
            reporter.log(f"Planning for {cid} failed: {exc}")
            ce.save_plan(cid, {"last_error": str(exc)})
    return None


def _notify_owner(reporter, cid: str, entry: dict) -> None:
    """Tell the participant their collection is enriched, if they opted in."""
    owner = str(entry.get("owner") or "")
    if not owner or entry.get("notified"):
        return
    try:
        from web_interface import mail_utils
        from web_interface.security import user_manager

        user = user_manager.get_user(owner)
        if not user:
            return
        # consent_to_contact is the participant's own switch; an unset value
        # reads as consent absent, but the plan is still closed either way.
        if (user.profile or {}).get("consent_to_contact") and mail_utils.is_email(owner):
            n = int(entry.get("spent_items") or 0)
            mail_utils.send_first_batch_ready_email_async(owner, cid, n)
            reporter.log(f"{cid}: notified {owner}.")
        user_manager.update_user_settings(owner, {"hub_tour_real_data_pending": True})
        ce.save_plan(cid, {"notified": True, "notified_at": ce.now_iso()})
    except Exception as exc:
        reporter.log(f"{cid}: owner notification failed: {exc}")


if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    run_worker(
        run_enrichment_supervisor,
        "enrichment_supervisor",
        arg_specs=[
            (("--collection-id",), {"dest": "collection_id", "default": "",
                                    "help": "Serve only this collection, "
                                            "ignoring the site-wide switch."}),
        ],
        make_task_args=lambda args: ({"collection_id": args.collection_id}
                                     if args.collection_id else {}),
        description="Advance the automatic enrichment loop by one step.",
    )
