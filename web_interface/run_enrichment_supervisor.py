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


def _busy() -> list[str]:
    """Anything that must finish before the tick may act."""
    from web_interface.services.worker_status import (
        _is_worker_running, _workers_blocking_consolidate,
    )
    from web_interface.services.worker_status import PIPELINE_STEPS_ORDER

    blocking = list(_workers_blocking_consolidate())
    for name in ["consolidate_enrichment", *PIPELINE_STEPS_ORDER]:
        if _is_worker_running(name):
            blocking.append(name)
    try:
        from web_interface import drain_lease
        blocking += [f"local drain ({p})" for p in sorted(drain_lease.active_drain_leases())]
    except Exception:
        pass
    return blocking


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

    blocking = _busy()
    if blocking or _pipeline_in_flight():
        reporter.log(f"Busy, nothing to do: {', '.join(blocking) or 'pipeline in flight'}.")
        reporter.emit_data({"action": "busy", "blocking": blocking})
        return None

    plans = ce.armed_plans()
    if forced:
        entry = ce.get_plan(forced)
        plans = {forced: entry} if entry else {}
    if not plans:
        reporter.log("No armed collections.")
        reporter.emit_data({"action": "idle"})
        return None

    reporter.log(f"{len(plans)} armed collection(s).")
    outcome = (_drain(reporter, plans)
               or _settle(reporter)
               or _handoff(reporter, plans)
               or _plan(reporter, plans)
               or {"action": "nothing_to_do"})

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


def _drain(reporter, plans: dict) -> dict | None:
    """Start a worker if either queue holds work. One worker, then return."""
    from fyp.scrape import scrape_queues

    lengths = scrape_queues.queue_lengths()
    # Serve only platforms an armed collection actually uses, so a leftover queue
    # from manual admin work does not keep the loop busy forever.
    armed_platforms = {str(e.get("platform") or "") for e in plans.values()}
    for platform, count in sorted(lengths.items()):
        if not count:
            # A drained queue clears its stall guard — otherwise a later fill
            # that happens to match the stale guarded length would strike
            # spuriously and park healthy plans.
            if ce.get_meta(f"scrape_guard_{platform}") is not None:
                ce.set_meta(f"scrape_guard_{platform}", None)
            continue
        if platform not in armed_platforms:
            continue
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
        return {"action": "scrape", "platform": platform, "queued": count,
                "started": ok, "message": f"Started the {platform} scraper."}

    queue = data_io.load_json(storage_location="cache",
                              filename=ce.ANNOTATE_QUEUE_FILENAME) or []
    if isinstance(queue, list) and queue:
        if _queue_stalled(reporter, plans, "annotate_guard", len(queue),
                          "annotation"):
            return {"action": "annotate_stalled", "queued": len(queue),
                    "message": "The annotation queue is not draining; plans parked."}
        name = _annotator_process()
        ok, msg = _start(name)
        reporter.log(f"Annotating {len(queue)} queued item(s) via {name}: {msg}")
        return {"action": "annotate", "queued": len(queue), "started": ok,
                "message": f"Started {name}."}
    else:
        # An empty queue clears the guard: whatever was stuck has drained.
        if ce.get_meta("annotate_guard") is not None:
            ce.set_meta("annotate_guard", None)
    return None


# --------------------------------------------------------------------------- #
# Step 2 — make results visible
# --------------------------------------------------------------------------- #

def _settle(reporter) -> dict | None:
    """Consolidate when a worker's results are not yet in enrichment status.

    The post-scrape consolidation deliberately skips the downstream pipeline: all
    it has to produce is ``scraped_ok``, which is what the handoff reads. Only the
    post-annotation one pays for embeddings -> video_map -> recode -> the four
    leaves, so a cycle runs that expensive chain once rather than twice.
    """
    kind = _unconsolidated()
    if not kind:
        return None
    auto_refresh = (kind == "annotate")
    ok, msg = _start("consolidate_enrichment", {"auto_refresh": auto_refresh})
    reporter.log(f"Consolidating after {kind} "
                 f"(downstream refresh {'on' if auto_refresh else 'off'}): {msg}")
    return {"action": "consolidate", "after": kind, "auto_refresh": auto_refresh,
            "started": ok, "message": f"Consolidating after {kind}."}


# --------------------------------------------------------------------------- #
# Step 3 — scrape -> annotate handoff
# --------------------------------------------------------------------------- #

def _handoff(reporter, plans: dict) -> dict | None:
    """Queue every armed collection's newly scraped items for annotation.

    This is the only place items enter the annotation queue, and it reads
    enrichment status rather than what a cycle intended to scrape. An unscraped id
    in that queue resolves no media, is refined as ``annotated_fail`` and is then
    pruned as permanently failed — the item is burnt for good.
    """
    total = 0
    served = []
    for cid, entry in plans.items():
        try:
            ready = ce.handoff_scraped(cid, entry)
        except Exception as exc:
            reporter.log(f"Handoff check for {cid} failed: {exc}")
            continue
        if not ready:
            continue
        n = ce.queue_for_annotation(ready)
        ce.save_plan(cid, {
            "spent_items": int(entry.get("spent_items") or 0) + n,
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

            result = ce.plan_cycle(cid, entry, activity=activity)
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
                    "a": result["a"], "b": result["b"],
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
