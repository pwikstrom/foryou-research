"""Batch queue annotator: annotate via the Gemini Batch API (async, ~50% cheaper).

A submit -> poll state machine that becomes the default path for non-urgent /
bulk annotation. The actual annotation runs on Google's batch infrastructure
(off task-runner); this worker only does short bookkeeping tasks around it and
the result flows back through the SAME raw -> refine -> (separate) consolidate ->
study-refresh path as the synchronous annotator. The synchronous path remains
the urgent fallback.

Phases (carried in ``task_args['phase']``, self-chained like queue_annotator):
  * ``submit`` — slice the queue, build + upload the JSONL, submit the batch job,
    then CLAIM the slice out of ``to_annotate.json`` (so neither a sync run nor
    another batch run re-annotates the same items while the async job runs),
    persist job state, and chain to ``poll`` after a short delay.
  * ``poll``   — check the job ONCE; if still running, re-dispatch another poll
    task after ``_POLL_DELAY_S`` via Cloud Tasks ``schedule_time`` (no instance
    held asleep in between); on success ingest -> refine, re-queue any
    unprocessed items, then chain back to ``submit``; on failure restore the
    claimed items to the queue and stop.

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


def _submit_phase(reporter, task_args, batch, data_io):
    """Slice the queue, submit a batch job, claim the slice, chain to poll."""
    batch_size = int(task_args.get("batch_size", DEFAULT_BATCH_SIZE))
    chunk_index = int(task_args.get("chunk_index", 0))
    max_batches = task_args.get("max_batches")
    initial_total = int(task_args.get("initial_total", 0))

    if not data_io.exists(storage_location="cache", filename=QUEUE_FILE):
        reporter.log("No to_annotate.json found in cache. Nothing to do.")
        return None
    queue = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    if not queue:
        reporter.log("Annotation queue is empty.")
        return None
    if initial_total <= 0:
        initial_total = len(queue)

    slice_ids = [str(v) for v in queue[:batch_size]]
    if not slice_ids:
        reporter.log("Queue empty at start of batch.")
        return None

    total_batches = _total_batches(initial_total, batch_size, max_batches)
    batch_no = chunk_index + 1
    if chunk_index == 0:
        _log(reporter, f"Starting async annotation: {initial_total:,} video(s) to process "
                       f"in up to {total_batches} batch(es) of {batch_size:,}.")

    ts_label = _ts_label()
    _log(reporter, f"Batch {batch_no} of {total_batches}: building + uploading JSONL "
                   f"for {len(slice_ids):,} video(s)...")
    # Build + upload + submit FIRST. If any of this throws, the queue is left
    # untouched (the items will be retried), so we only claim after success.
    jsonl_uri, submitted_ids = batch.build_and_upload_jsonl(slice_ids, ts_label)
    job_name, output_uri = batch.submit_batch_job(jsonl_uri, ts_label)
    _log(reporter, f"Batch {batch_no} of {total_batches}: submitted {len(submitted_ids):,} "
                   f"video(s) to the Gemini batch service (job {job_name}).")

    claimed = _claim_from_queue(data_io, submitted_ids)
    remaining_after_claim = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    _log(reporter, f"Claimed {claimed:,} video(s) out of the queue — "
                   f"{len(remaining_after_claim):,} still pending, {len(submitted_ids):,} now in this batch.")
    # Reflect the claim in the card immediately: the pending count drops NOW and
    # the "N in batch job" indicator appears, without waiting for the (hours-long)
    # job to finish. The stats endpoint is the reload authority for both.
    reporter.emit_data({
        "annotate_queue_len": len(remaining_after_claim),
        "annotate_claimed_len": len(submitted_ids),
    })

    # "Submitted" email only on the first chunk — otherwise a multi-chunk run
    # would email once per chunk. Per-chunk progress is the "batch_done" email.
    if chunk_index == 0:
        _notify(task_args, "submitted", n_items=len(submitted_ids))

    job_state = {
        "phase": "poll",
        "job_name": job_name,
        "output_uri": output_uri,
        "jsonl_uri": jsonl_uri,
        "submitted_ids": submitted_ids,
        "chunk_index": chunk_index,
        "initial_total": initial_total,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "launched_by": task_args.get("launched_by"),
        "total_ok": int(task_args.get("total_ok", 0)),
        "total_fail": int(task_args.get("total_fail", 0)),
    }
    data_io.save_json(data=job_state, storage_location="cache", filename=JOB_STATE_FILE)
    return {
        "chain": True,
        "next_task_args": job_state,
        "dispatch_deadline_seconds": 600,
        "next_dispatch_delay_seconds": _POLL_DELAY_S,
    }


def _poll_phase(reporter, task_args, batch, data_io):
    """Check the job once; reschedule, or ingest + refine + re-queue + chain."""
    from fyp.machine_annotation import refine_one_raw_annotation_batch

    job_name = task_args.get("job_name")
    output_uri = task_args.get("output_uri")
    submitted_ids = task_args.get("submitted_ids", [])
    batch_no = int(task_args.get("chunk_index", 0)) + 1
    total_batches = _total_batches(task_args.get("initial_total", 0),
                                   task_args.get("batch_size", DEFAULT_BATCH_SIZE),
                                   task_args.get("max_batches"))
    label = f"Batch {batch_no} of {total_batches}"
    if not job_name:
        reporter.log("Poll phase missing job_name; nothing to do.")
        return None

    if reporter.check_cancelled():
        _log(reporter, "Cancellation requested; leaving the job and claimed items as-is.")
        _clear_job_state(data_io)
        return None

    state = batch.poll_batch_job(job_name)
    _log(reporter, f"{label}: Gemini job state is {state}.")

    if state in _RUNNING_STATES:
        # Not done — re-poll later WITHOUT holding this instance asleep.
        return {
            "chain": True,
            "next_task_args": dict(task_args, phase="poll"),
            "dispatch_deadline_seconds": 600,
            "next_dispatch_delay_seconds": _POLL_DELAY_S,
        }

    if state in batch._TERMINAL_FAIL:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: Gemini job ended in {state}; restored {restored:,} claimed video(s) to the queue. Stopping.")
        _notify(task_args, "failed", error=f"Gemini batch job ended in {state}")
        _clear_job_state(data_io)
        return None

    # SUCCEEDED / PARTIALLY_SUCCEEDED: ingest -> refine. Guard the whole block:
    # if download/ingest/refine throws, restore the claimed slice to the queue
    # (an unguarded crash here is what stranded a claimed batch in prod), notify
    # the launcher, and stop gracefully.
    _log(reporter, f"{label}: Gemini job succeeded — ingesting results...")
    try:
        raw_filename = batch.download_and_ingest(output_uri, submitted_ids)
        refined = refine_one_raw_annotation_batch(raw_json_filename=raw_filename, verbose=False)
    except Exception as exc:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: ingest/refine crashed ({exc}); restored {restored:,} claimed video(s). Stopping.")
        _notify(task_args, "failed", error=str(exc))
        _clear_job_state(data_io)
        return None

    if refined is None or refined.empty:
        restored = _restore_to_queue(data_io, submitted_ids)
        _log(reporter, f"{label}: refinement produced nothing; restored {restored:,} video(s). Stopping to avoid a loop.")
        _notify(task_args, "failed", error="Refinement produced no rows")
        _clear_job_state(data_io)
        return None

    ok_ids = refined.loc[refined["annotated_ok"].fillna(False).astype(bool), "item_id"].astype(str).tolist()
    fail_ids = refined.loc[refined.get("annotated_fail", False).fillna(False).astype(bool), "item_id"].astype(str).tolist() \
        if "annotated_fail" in refined.columns else []
    # ok + fail stay claimed (definitively processed). Items that were submitted
    # but never came back (DNF / missing from output) are re-queued for retry —
    # matching the synchronous worker, which leaves un-refined items in the queue.
    accounted = set(ok_ids) | set(fail_ids)
    unprocessed = [str(i) for i in submitted_ids if str(i) not in accounted]
    requeued = _restore_to_queue(data_io, unprocessed)

    remaining = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    # This slice is now ingested — nothing is reserved until the next submit.
    reporter.emit_data({"annotate_queue_len": len(remaining), "annotate_claimed_len": 0})
    _log(
        reporter,
        f"{label} done: {len(ok_ids):,} annotated, {len(fail_ids):,} failed, "
        f"{requeued:,} re-queued. {len(remaining):,} still pending in the queue."
    )

    # Running totals across chunks, for the terminal "completed" email.
    total_ok = int(task_args.get("total_ok", 0)) + len(ok_ids)
    total_fail = int(task_args.get("total_fail", 0)) + len(fail_ids)

    next_chunk = int(task_args.get("chunk_index", 0)) + 1
    max_batches = task_args.get("max_batches")
    terminal_reason = None
    if max_batches is not None and next_chunk >= int(max_batches):
        terminal_reason = f"Reached the max-batches limit ({max_batches})."
    elif not remaining:
        terminal_reason = "Queue is now empty."

    if terminal_reason is not None:
        # Whole run finished: send the terminal "completed" email (which subsumes
        # this last chunk's per-batch email) and clear the job state.
        _log(reporter, f"All done — {terminal_reason} Processed {total_ok:,} annotated, "
                       f"{total_fail:,} failed across {batch_no} batch(es). "
                       f"Run a Consolidate & Refresh to fold the new annotations in.")
        _notify(task_args, "completed", total_ok=total_ok, total_fail=total_fail)
        _clear_job_state(data_io)
        return None

    # More chunks remain: notify this chunk's completion, then chain to the next
    # submit (carrying launcher identity + running totals across the self-chain).
    _notify(task_args, "batch_done", ok=len(ok_ids), fail=len(fail_ids),
            requeued=requeued, remaining=len(remaining))

    return {"chain": True, "next_task_args": {
        "phase": "submit", "batch_size": int(task_args.get("batch_size", DEFAULT_BATCH_SIZE)),
        "chunk_index": next_chunk, "initial_total": int(task_args.get("initial_total", 0)),
        "max_batches": max_batches,
        "launched_by": task_args.get("launched_by"),
        "total_ok": total_ok, "total_fail": total_fail,
    }, "dispatch_deadline_seconds": 1800}


def run_queue_annotator_batch(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Process one phase of the batch-annotation state machine.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: ``phase`` ('submit' | 'poll') plus phase-specific state.

    Returns:
        A chain dict (next phase) or ``None`` when the work is done / failed.
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

    phase = task_args.get("phase", "submit")
    if phase == "submit":
        return _submit_phase(reporter, task_args, batch, data_io)
    if phase == "poll":
        return _poll_phase(reporter, task_args, batch, data_io)
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
    next_args = {"phase": "submit", "batch_size": args.batch_size,
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
