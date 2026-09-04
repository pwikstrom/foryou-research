"""Batch queue annotator: annotate via the Gemini Batch API (async, ~50% cheaper).

A submit -> poll state machine that becomes the default path for non-urgent /
bulk annotation. The actual annotation runs on Google's batch infrastructure
(off task-runner); this worker only does short bookkeeping tasks around it and
the result flows back through the SAME raw -> refine -> (separate) consolidate ->
study-refresh path as the synchronous annotator. The synchronous path remains
the urgent fallback.

One self-chained ``run`` phase drives a TABLE of concurrent jobs (the jobs run
on Google's infrastructure, so N in flight cost this worker nothing): each
chain link polls every job once, ingests + refines the finished ones, then
submits new jobs from the queue while slots are free (``MAX_CONCURRENT_JOBS``),
and re-dispatches itself after ``_POLL_DELAY_S`` via Cloud Tasks
``schedule_time`` (no instance held asleep in between). Submitting CLAIMS the
slice out of ``to_annotate.json`` (so neither a sync run nor another batch run
re-annotates the same items while the async job runs — and the enrichment
supervisor's handoff reads the job table to skip in-flight ids too). Annotation
wall time for a multi-job backlog is therefore ~one job turnaround, not one
per job. The legacy ``submit``/``poll`` phases are adapters for chains that
were in flight when this shape shipped.

Claim/restore safety: claimed items are removed from the queue only after a
successful submit, and restored if the job fails or an item comes back
unprocessed. If a poll chain is orphaned (this worker is deliberately excluded
from ``process_routes.QUEUE_RETRY_SAFE`` — a retried submit could pay for the
same batch job twice), the claimed-but-unannotated items are re-discovered by the next
``calculate_to_annotate`` (they remain scraped-but-not-annotated).

LIVE SPIKE (run before any bulk use — needs GCS media + batch access; cannot run
in a local/no-GCS environment):
    Submit a 5-video batch via the submit phase, inspect the output JSONL, and
    confirm (a) gemini-3-flash-preview is batch-enabled on the project and
    (b) the request/response key casing in fyp/machine_annotation_batch.py
    (generationConfig / responseSchema / systemInstruction / mediaResolution and
    the output candidates/usageMetadata shape). Adjust the builders if needed,
    then re-run the offline equivalence test before scaling up.
"""

import datetime as _dt
import math
import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.mail_utils import send_batch_annotation_email_async
from web_interface.task_status import TaskStatusReporter

JOB_STATE_FILE = "annotate_batch_job.json"
QUEUE_FILE = "to_annotate.json"

# Default slice per batch job. Batch tolerates far larger slices than the
# synchronous path (no 50-worker / 3600s ceiling); kept moderate to keep the
# JSONL well under the 1 GB / 200k-request limits and turnaround reasonable.
DEFAULT_BATCH_SIZE = 2000

# Concurrent Gemini jobs the run keeps in flight. Turnaround is a fixed cost
# per job weakly related to its size, so N serial jobs waste (N-1) turnarounds;
# concurrent jobs cost the same money. The cap bounds the ids carried in the
# chain's task_args (~170 KB at 4 x 2000, well under the 1 MB Cloud Tasks
# payload limit) and the blast radius of an orphaned chain (claimed items are
# only re-discovered by the next annotate-queue calculation). Overridable per
# run via task_args["max_concurrent_jobs"].
MAX_CONCURRENT_JOBS = 4

# States that mean "still working" — keep polling.
_RUNNING_STATES = {
    "JOB_STATE_PENDING", "JOB_STATE_QUEUED", "JOB_STATE_RUNNING", "JOB_STATE_PAUSED",
}

# Delay between poll checks. The job runs on Google's infra; we re-dispatch a
# short poll task after this delay (Cloud Tasks schedule_time on Cloud Run; a
# plain sleep in the local __main__ loop) rather than holding a task-runner
# instance asleep.
_POLL_DELAY_S = 120


def _ts_label() -> str:
    return "".join(c for c in str(_dt.datetime.now()) if c in "0123456789")


def _delay_phrase(seconds: int) -> str:
    """'2 minutes' / '90 seconds', for the "checking again in ..." chain line."""
    if seconds >= 60 and seconds % 60 == 0:
        mins = seconds // 60
        return "1 minute" if mins == 1 else f"{mins} minutes"
    return f"{seconds} seconds"


def _log(reporter, message: str) -> None:
    """reporter.log for the in-card feed.

    Stamping now happens once, centrally, in ``run_logs.append`` — this used to
    prefix its own time read from a ``misc.timezone`` config key that does not
    exist (it is ``TIME_ZONE``), so every stamp silently fell back to naive
    local time, which on Cloud Run is UTC rather than the project timezone.
    """
    reporter.log(message)


def _total_batches(initial_total, batch_size, max_batches) -> int:
    """Estimated batch count for the run (the queue can grow, so it is an estimate)."""
    batch_size = int(batch_size or 0)
    if batch_size <= 0:
        return 1
    est = max(1, math.ceil(int(initial_total or 0) / batch_size))
    if max_batches:
        est = min(est, int(max_batches))
    return est


def _claim_from_queue(data_io, ids) -> int:
    """Remove ``ids`` from the annotation queue. Returns the count removed.

    Atomic read-modify-write: ids appended by another process while the batch
    was being prepared are never clobbered by the claim.
    """
    claimed = {str(i) for i in ids}
    counts = {"removed": 0}

    def _mutate(fresh):
        fresh = fresh if isinstance(fresh, list) else []
        remaining = [v for v in fresh if str(v) not in claimed]
        counts["removed"] = len(fresh) - len(remaining)
        if not counts["removed"]:
            return None  # nothing to claim — skip the write
        return remaining

    data_io.update_json(
        storage_location="cache", filename=QUEUE_FILE, mutate=_mutate, default=[]
    )
    return counts["removed"]


def _restore_to_queue(data_io, ids) -> int:
    """Append ``ids`` back to the queue (skipping ones already present)."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    counts = {"added": 0}

    def _mutate(fresh):
        fresh = fresh if isinstance(fresh, list) else []
        present = {str(v) for v in fresh}
        additions = [i for i in ids if i not in present]
        counts["added"] = len(additions)
        if not additions:
            return None
        return fresh + additions

    data_io.update_json(
        storage_location="cache", filename=QUEUE_FILE, mutate=_mutate, default=[]
    )
    return counts["added"]


def _clear_job_state(data_io) -> None:
    """Delete the persisted job-state file at every terminal exit (best-effort).

    The "N in batch" indicator reads ``submitted_ids`` from this file (gated by
    "is the worker running"); deleting it on every terminal path means a stale
    file can never resurrect a claimed count after the run ends.
    """
    try:
        if data_io.exists(storage_location="cache", filename=JOB_STATE_FILE):
            data_io.remove(storage_location="cache", filename=JOB_STATE_FILE)
    except Exception:
        pass


def _notify(task_args, kind, **details) -> None:
    """Fire-and-forget email to the run's launcher (no-op if unknown/invalid).

    ``launched_by`` is the launching user's username (their email). Sending is
    async and self-guarding, so a mail outage never blocks the pipeline.
    """
    to_email = (task_args or {}).get("launched_by")
    if not to_email:
        return
    try:
        send_batch_annotation_email_async(to_email, kind, **details)
    except Exception as exc:
        print(f"[queue_annotator_batch] email notify failed: {exc}")


def _poll_one_job(reporter, run, job, batch, data_io):
    """Poll one in-flight job; ingest it if finished.

    Mutates ``run`` (totals, submit_halted) and returns True when the job is
    terminal (caller drops it from the table). Every failure path restores
    exactly THIS job's claimed ids and halts further submits — the other jobs
    keep polling and draining, so one bad job never strands its siblings.
    """
    from fyp.machine_annotation import refine_one_raw_annotation_batch

    label = f"Batch {int(job.get('batch_no') or 0)}"
    submitted_ids = job.get("submitted_ids") or []

    state = batch.poll_batch_job(job["job_name"])
    _log(reporter, f"{label}: Gemini job state is {state}.")

    if state in _RUNNING_STATES:
        return False

    if state in batch._TERMINAL_FAIL:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: Gemini job ended in {state}; restored {restored:,} "
                       f"claimed video(s) to the queue. No further jobs will be submitted.")
        _notify(run, "failed", error=f"Gemini batch job ended in {state}")
        run["submit_halted"] = True
        return True

    # SUCCEEDED / PARTIALLY_SUCCEEDED: ingest -> refine. Guard the whole block:
    # if download/ingest/refine throws, restore this job's claimed slice (an
    # unguarded crash here is what stranded a claimed batch in prod), notify
    # the launcher, and let the remaining jobs keep draining.
    _log(reporter, f"{label}: Gemini job succeeded — ingesting results...")
    try:
        raw_filename = batch.download_and_ingest(job["output_uri"], submitted_ids)
        refined = refine_one_raw_annotation_batch(raw_json_filename=raw_filename, verbose=False)
    except Exception as exc:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: ingest/refine crashed ({exc}); restored {restored:,} "
                       f"claimed video(s). No further jobs will be submitted.")
        _notify(run, "failed", error=str(exc))
        run["submit_halted"] = True
        return True

    if refined is None or refined.empty:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: refinement produced nothing; restored {restored:,} "
                       f"video(s). No further jobs will be submitted, to avoid a loop.")
        _notify(run, "failed", error="Refinement produced no rows")
        run["submit_halted"] = True
        return True

    ok_ids = refined.loc[refined["annotated_ok"].fillna(False).astype(bool), "item_id"].astype(str).tolist()
    fail_ids = refined.loc[refined.get("annotated_fail", False).fillna(False).astype(bool), "item_id"].astype(str).tolist() \
        if "annotated_fail" in refined.columns else []
    # ok + fail stay claimed (definitively processed). Items that were submitted
    # but never came back (DNF / missing from output) are re-queued for retry —
    # matching the synchronous worker, which leaves un-refined items in the queue.
    accounted = set(ok_ids) | set(fail_ids)
    unprocessed = [str(i) for i in submitted_ids if str(i) not in accounted]
    requeued = _restore_to_queue(data_io, unprocessed)

    run["total_ok"] = int(run.get("total_ok", 0)) + len(ok_ids)
    run["total_fail"] = int(run.get("total_fail", 0)) + len(fail_ids)
    run["_completed_this_link"].append(
        {"ok": len(ok_ids), "fail": len(fail_ids), "requeued": requeued})
    remaining = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    _log(reporter, f"{label} done: {len(ok_ids):,} annotated, {len(fail_ids):,} failed, "
                   f"{requeued:,} re-queued. {len(remaining):,} still pending in the queue.")
    return True


def _submit_more_jobs(reporter, run, batch, data_io) -> None:
    """Fill free job slots from the queue's head. Mutates ``run``.

    Submit-then-claim per slice: an exception before the submit succeeds leaves
    the queue untouched. A submit exception stops submitting for THIS link only
    (the chain retries next link) — unlike a failed JOB, which halts submits for
    the rest of the run.
    """
    cap = int(run.get("max_concurrent_jobs") or MAX_CONCURRENT_JOBS)
    batch_size = int(run.get("batch_size", DEFAULT_BATCH_SIZE))
    max_batches = run.get("max_batches")

    while len(run["jobs"]) < cap and not run.get("submit_halted"):
        if max_batches is not None and int(run.get("chunk_index", 0)) >= int(max_batches):
            break
        queue = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
        slice_ids = [str(v) for v in queue[:batch_size]]
        if not slice_ids:
            break
        if int(run.get("initial_total", 0)) <= 0:
            run["initial_total"] = len(queue)

        batch_no = int(run.get("chunk_index", 0)) + 1
        total_batches = _total_batches(run.get("initial_total", 0), batch_size, max_batches)
        if not run.get("_announced"):
            run["_announced"] = True
            _log(reporter, f"Starting async annotation: {run['initial_total']:,} video(s) "
                           f"to process in up to {total_batches} batch(es) of "
                           f"{batch_size:,}, at most {cap} job(s) in flight.")

        ts_label = _ts_label()
        _log(reporter, f"Batch {batch_no}: building + uploading JSONL "
                       f"for {len(slice_ids):,} video(s)...")
        try:
            jsonl_uri, submitted_ids = batch.build_and_upload_jsonl(slice_ids, ts_label)
            job_name, output_uri = batch.submit_batch_job(jsonl_uri, ts_label)
        except Exception as exc:
            _log(reporter, f"Batch {batch_no}: submit failed ({exc}); queue untouched — "
                           f"will retry on the next chain link.")
            run["_submit_error"] = str(exc)
            break
        _log(reporter, f"Batch {batch_no}: submitted {len(submitted_ids):,} video(s) "
                       f"to the Gemini batch service (job {job_name}).")

        claimed = _claim_from_queue(data_io, submitted_ids)
        _log(reporter, f"Claimed {claimed:,} video(s) out of the queue — "
                       f"{len(submitted_ids):,} now in batch {batch_no}.")
        run["jobs"].append({
            "job_name": job_name,
            "output_uri": output_uri,
            "jsonl_uri": jsonl_uri,
            "submitted_ids": submitted_ids,
            "ts_label": ts_label,
            "batch_no": batch_no,
            "submitted_at": _dt.datetime.now(_dt.UTC).isoformat(),
        })
        run["chunk_index"] = batch_no

        # "Submitted" email once per run — per-job progress is "batch_done".
        if not run.get("notified_submitted"):
            run["notified_submitted"] = True
            _notify(run, "submitted", n_items=len(submitted_ids))


def _journal_finished(run: dict, remaining: int, reason: str) -> None:
    """The run's one history line, written where its totals are final."""
    try:
        from web_interface.services import enrichment_journal as journal

        ok, fail = int(run.get("total_ok") or 0), int(run.get("total_fail") or 0)
        batches = int(run.get("chunk_index") or 0)
        message = (f"Annotation finished — {ok:,} annotated, {fail:,} failed "
                   f"across {batches:,} batch job(s)")
        message += f"; {remaining:,} still queued" if remaining else "; queue empty"
        if reason and not reason.lower().startswith("queue is now empty"):
            message += f" ({reason})"
        journal.record("annotate.finished", message,
                       actor=run.get("started_by") or None, worker="queue_annotator_batch",
                       ok=ok, fail=fail, batches=batches, queue_remaining=remaining,
                       reason=reason)
    except Exception:
        pass


def _run_phase(reporter, task_args, batch, data_io):
    """One chain link of the job-table state machine.

    Poll every in-flight job once (ingesting the finished ones), then top up
    free slots from the queue, persist the table, and chain — or finish when
    the table is empty and nothing is left to submit.
    """
    run = dict(task_args)
    run["jobs"] = [dict(j) for j in (run.get("jobs") or [])]
    run.setdefault("total_ok", 0)
    run.setdefault("total_fail", 0)
    run.setdefault("chunk_index", 0)
    run["_completed_this_link"] = []
    # Re-announce only on a fresh run; adapters mark continuations announced.
    run.setdefault("_announced", bool(run["jobs"]) or int(run.get("chunk_index") or 0) > 0)

    if reporter.check_cancelled():
        _log(reporter, f"Cancellation requested; leaving {len(run['jobs'])} in-flight "
                       f"job(s) and their claimed items as-is.")
        _clear_job_state(data_io)
        return None

    if not run["jobs"] and not data_io.exists(storage_location="cache", filename=QUEUE_FILE):
        reporter.log("No to_annotate.json found in cache. Nothing to do.")
        return None

    # ---- Poll + ingest ----
    still_running = []
    for job in run["jobs"]:
        try:
            terminal = _poll_one_job(reporter, run, job, batch, data_io)
        except Exception as exc:
            # A transient poll error (network, API hiccup) keeps the job in the
            # table — the next link re-polls it.
            _log(reporter, f"Batch {job.get('batch_no')}: poll failed ({exc}); will retry.")
            terminal = False
        if not terminal:
            still_running.append(job)
    run["jobs"] = still_running

    # ---- Top up free slots ----
    _submit_more_jobs(reporter, run, batch, data_io)

    # ---- Reflect state in the card + the job-state mirror file ----
    remaining = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    claimed_total = sum(len(j.get("submitted_ids") or []) for j in run["jobs"])
    reporter.emit_data({
        "annotate_queue_len": len(remaining),
        "annotate_claimed_len": claimed_total,
    })

    # ---- Terminal? ----
    if not run["jobs"] and run.get("_submit_error"):
        # Nothing in flight to wait for AND submission is failing: retrying on a
        # 120s chain would loop on a broken backend forever. The queue was left
        # untouched, so stopping loses nothing.
        _log(reporter, "Stopping: no job in flight and the submit is failing "
                       f"({run['_submit_error']}).")
        _notify(run, "failed", error=f"Batch submit failed: {run['_submit_error']}")
        _journal_finished(run, len(remaining),
                          f"stopped — the batch submit is failing ({run['_submit_error']})")
        _clear_job_state(data_io)
        return None

    if not run["jobs"]:
        max_batches = run.get("max_batches")
        terminal_reason = None
        if run.get("submit_halted"):
            terminal_reason = "A job failed, so no further jobs were submitted."
        elif max_batches is not None and int(run.get("chunk_index", 0)) >= int(max_batches):
            terminal_reason = f"Reached the max-batches limit ({max_batches})."
        elif not remaining:
            terminal_reason = "Queue is now empty." if run.get("chunk_index") else None

        if not run.get("chunk_index"):
            reporter.log("Annotation queue is empty.")
            _clear_job_state(data_io)
            return None
        if terminal_reason is not None:
            total_ok, total_fail = int(run["total_ok"]), int(run["total_fail"])
            _log(reporter, f"All done — {terminal_reason} Processed {total_ok:,} annotated, "
                           f"{total_fail:,} failed across {int(run['chunk_index'])} batch(es). "
                           f"Run a Consolidate & Refresh to fold the new annotations in.")
            # The per-job "failed" mails already covered a run with no results.
            if total_ok or total_fail:
                _notify(run, "completed", total_ok=total_ok, total_fail=total_fail)
            _journal_finished(run, len(remaining), terminal_reason.rstrip("."))
            _clear_job_state(data_io)
            return None

    # More to do (jobs in flight, or the queue refilled while slots were full):
    # notify finished jobs, persist the table, chain to the next link.
    for done in run["_completed_this_link"]:
        _notify(run, "batch_done", ok=done["ok"], fail=done["fail"],
                requeued=done["requeued"], remaining=len(remaining))

    next_args = {k: v for k, v in run.items() if not k.startswith("_")}
    next_args["phase"] = "run"
    next_args["format"] = 2
    data_io.save_json(data=next_args, storage_location="cache", filename=JOB_STATE_FILE)

    # This link ends here — the next poll is a separately dispatched task
    # ~_POLL_DELAY_S later, on a fresh instance. Only the first link of a
    # backlog actually starts a new batch, so the generic "Chained to next
    # batch" read as if a batch were being submitted every two minutes.
    wait = _delay_phrase(_POLL_DELAY_S)
    n_jobs = len(run["jobs"])
    chain_msg = (f"{n_jobs} job(s) still in flight — checking again in {wait}."
                 if n_jobs else
                 f"No job in flight — trying the queue again in {wait}.")
    return {
        "chain": True,
        "next_task_args": next_args,
        "dispatch_deadline_seconds": 1800,
        "next_dispatch_delay_seconds": _POLL_DELAY_S,
        "chain_log_message": chain_msg,
    }


def _legacy_args_to_run(task_args) -> dict:
    """Map a pre-table chain link's args into the ``run`` shape.

    ``submit`` (fresh run or the between-jobs link of an old chain) starts with
    an empty table; ``poll`` (an old chain's in-flight job, live across the
    deploy) wraps that job into a one-entry table so nothing is stranded.
    """
    common = {
        "phase": "run",
        "batch_size": int(task_args.get("batch_size", DEFAULT_BATCH_SIZE)),
        "chunk_index": int(task_args.get("chunk_index", 0)),
        "initial_total": int(task_args.get("initial_total", 0)),
        "max_batches": task_args.get("max_batches"),
        "launched_by": task_args.get("launched_by"),
        "total_ok": int(task_args.get("total_ok", 0)),
        "total_fail": int(task_args.get("total_fail", 0)),
    }
    if task_args.get("phase") == "poll" and task_args.get("job_name"):
        common["jobs"] = [{
            "job_name": task_args.get("job_name"),
            "output_uri": task_args.get("output_uri"),
            "jsonl_uri": task_args.get("jsonl_uri"),
            "submitted_ids": task_args.get("submitted_ids") or [],
            "ts_label": "",
            "batch_no": int(task_args.get("chunk_index", 0)) + 1,
            "submitted_at": "",
        }]
        # The old poll phase's chunk_index counted COMPLETED jobs; the table
        # counts submitted ones, and this job is submitted.
        common["chunk_index"] = int(task_args.get("chunk_index", 0)) + 1
        common["notified_submitted"] = True
    else:
        common["jobs"] = []
    return common


def run_queue_annotator_batch(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Process one phase of the batch-annotation state machine.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: ``phase`` ('run', or the legacy 'submit' | 'poll' shapes)
            plus the run's job table and accounting.

    Returns:
        A chain dict (next link) or ``None`` when the work is done / failed.
    """
    import fyp.data_io as data_io
    import fyp.machine_annotation_batch as batch
    from fyp.annotation.backends import active_backend_name

    task_args = task_args or {}

    # Batch mode is intrinsically Gemini (Batch API + GCS JSONL) — refuse
    # cleanly when another backend is selected instead of failing mid-flight.
    backend = active_backend_name()
    if backend != "gemini":
        reporter.log(f"Batch annotation only supports the Gemini backend "
                     f"(active backend: {backend}). Switch the backend in "
                     f"Admin → Backends or use the live annotator.")
        return None

    phase = task_args.get("phase", "run")
    if phase in ("submit", "poll"):
        task_args = _legacy_args_to_run(task_args)
        phase = "run"
    if phase == "run":
        return _run_phase(reporter, task_args, batch, data_io)
    reporter.log(f"Unknown batch phase '{phase}'.")
    return None




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Run batch queue annotator (submit + poll to done)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="Items per submitted batch job (default: %(default)s)")
    parser.add_argument("--max-batches", type=int, default=None,
                        help="Max batches (default: unlimited)")
    parser.add_argument("--launched-by", type=str, default=None,
                        help="Email of the launching user, for milestone notifications.")
    args = parser.parse_args()

    reporter = LocalStatusReporter("queue_annotator_batch")
    next_args = {"phase": "run", "batch_size": args.batch_size,
                 "max_batches": args.max_batches, "launched_by": args.launched_by}
    try:
        while next_args is not None:
            result = run_queue_annotator_batch(reporter, next_args)
            if not result or not result.get("chain"):
                break
            # No Cloud Tasks locally: honour the poll delay with a plain sleep so
            # the local loop polls the job to completion.
            delay = result.get("next_dispatch_delay_seconds")
            if delay:
                time.sleep(delay)
            next_args = result["next_task_args"]
        reporter.complete()
        print("Batch queue annotation completed.")
    except Exception as exc:
        reporter.fail(str(exc))
        import traceback
        traceback.print_exc()
        sys.exit(1)
