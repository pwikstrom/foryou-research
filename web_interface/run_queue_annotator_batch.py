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
unprocessed. If a poll chain is orphaned (e.g. a task dies under the queue's
max-attempts=1), the claimed-but-unannotated items are re-discovered by the next
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

import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

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
    import datetime as _dt
    return "".join(c for c in str(_dt.datetime.now()) if c in "0123456789")


def _claim_from_queue(data_io, ids) -> int:
    """Remove ``ids`` from the annotation queue. Returns the count removed."""
    claimed = {str(i) for i in ids}
    fresh = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    remaining = [v for v in fresh if str(v) not in claimed]
    removed = len(fresh) - len(remaining)
    if removed:
        data_io.save_json(data=remaining, storage_location="cache", filename=QUEUE_FILE)
    return removed


def _restore_to_queue(data_io, ids) -> int:
    """Append ``ids`` back to the queue (skipping ones already present)."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    fresh = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    present = {str(v) for v in fresh}
    additions = [i for i in ids if i not in present]
    if additions:
        data_io.save_json(data=fresh + additions, storage_location="cache", filename=QUEUE_FILE)
    return len(additions)


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

    ts_label = _ts_label()
    reporter.log(f"Submit: building + uploading JSONL for {len(slice_ids):,} videos...")
    # Build + upload + submit FIRST. If any of this throws, the queue is left
    # untouched (the items will be retried), so we only claim after success.
    jsonl_uri, submitted_ids = batch.build_and_upload_jsonl(slice_ids, ts_label)
    job_name, output_uri = batch.submit_batch_job(jsonl_uri, ts_label)
    reporter.log(f"Submitted batch job {job_name} ({len(submitted_ids):,} items).")

    claimed = _claim_from_queue(data_io, submitted_ids)
    reporter.log(f"Claimed {claimed} item(s) out of the annotation queue (reserved for this job).")

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
    if not job_name:
        reporter.log("Poll phase missing job_name; nothing to do.")
        return None

    if reporter.check_cancelled():
        reporter.log("Cancellation requested; leaving the job and claimed items as-is.")
        return None

    state = batch.poll_batch_job(job_name)
    reporter.log(f"Batch job {job_name} state: {state}")

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
        reporter.log(f"Batch job {job_name} ended in {state}; restored {restored} claimed item(s) to the queue. Stopping.")
        return None

    # SUCCEEDED / PARTIALLY_SUCCEEDED: ingest -> refine.
    reporter.log("Batch succeeded. Ingesting output...")
    raw_filename = batch.download_and_ingest(output_uri, submitted_ids)
    refined = refine_one_raw_annotation_batch(raw_json_filename=raw_filename, verbose=False)
    if refined is None or refined.empty:
        restored = _restore_to_queue(data_io, submitted_ids)
        reporter.log(f"Refinement produced nothing; restored {restored} item(s). Stopping to avoid a loop.")
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
    reporter.emit_data({"annotate_queue_len": len(remaining)})
    reporter.log(
        f"Ingested batch: {len(ok_ids)} OK, {len(fail_ids)} fail, {requeued} re-queued. "
        f"Queue: {len(remaining):,} remaining."
    )

    next_chunk = int(task_args.get("chunk_index", 0)) + 1
    max_batches = task_args.get("max_batches")
    if max_batches is not None and next_chunk >= int(max_batches):
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        return None
    if not remaining:
        reporter.log("Queue exhausted.")
        return None

    return {"chain": True, "next_task_args": {
        "phase": "submit", "batch_size": int(task_args.get("batch_size", DEFAULT_BATCH_SIZE)),
        "chunk_index": next_chunk, "initial_total": int(task_args.get("initial_total", 0)),
        "max_batches": max_batches,
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

    task_args = task_args or {}
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
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=None)
    args = parser.parse_args()

    reporter = LocalStatusReporter("queue_annotator_batch")
    next_args = {"phase": "submit", "batch_size": args.batch_size, "max_batches": args.max_batches}
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
