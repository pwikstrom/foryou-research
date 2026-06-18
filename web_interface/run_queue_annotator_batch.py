"""Batch queue annotator: annotate via the Gemini Batch API (async, ~50% cheaper).

A submit -> poll state machine that becomes the default path for non-urgent /
bulk annotation. Unlike the synchronous ``run_queue_annotator`` (50 live workers
per Cloud Task), this submits a batch job, polls it to completion, ingests the
output into ``machine_annotations_raw`` (in the exact synchronous raw shape, so
the marker-driven refinement is reused unchanged), refines + prunes the queue,
then chains to the next slice. The synchronous path remains the urgent fallback.

Phases (carried in ``task_args['phase']``, self-chained like queue_annotator):
  * ``submit`` — slice the queue, build + upload the JSONL, submit the batch job,
    persist job state, chain to ``poll``.
  * ``poll``   — poll the job; while running, poll-loop within a wall-clock
    budget then re-chain ``poll``; on success ingest -> refine -> prune queue,
    then chain back to ``submit`` for the next slice; on failure, stop.

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
# Per-poll-task wall-clock budget before re-chaining another poll task, and the
# interval between polls within a task.
_POLL_TASK_BUDGET_S = 1500
_POLL_INTERVAL_S = 60


def _ts_label() -> str:
    import datetime as _dt
    return "".join(c for c in str(_dt.datetime.now()) if c in "0123456789")


def _submit_phase(reporter, task_args, batch, data_io):
    """Slice the queue, submit a batch job, persist state, chain to poll."""
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

    slice_ids = queue[:batch_size]
    if not slice_ids:
        reporter.log("Queue empty at start of batch.")
        return None

    ts_label = _ts_label()
    reporter.log(f"Submit: building + uploading JSONL for {len(slice_ids):,} videos...")
    jsonl_uri, submitted_ids = batch.build_and_upload_jsonl(slice_ids, ts_label)
    job_name, output_uri = batch.submit_batch_job(jsonl_uri, ts_label)
    reporter.log(f"Submitted batch job {job_name} ({len(submitted_ids):,} items).")

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
    return {"chain": True, "next_task_args": job_state, "dispatch_deadline_seconds": 1800}


def _poll_phase(reporter, task_args, batch, data_io):
    """Poll the batch job; ingest + refine + prune + chain to next slice on success."""
    from fyp.machine_annotation import refine_one_raw_annotation_batch

    job_name = task_args.get("job_name")
    output_uri = task_args.get("output_uri")
    submitted_ids = task_args.get("submitted_ids", [])
    if not job_name:
        reporter.log("Poll phase missing job_name; nothing to do.")
        return None

    deadline = time.time() + _POLL_TASK_BUDGET_S
    state = batch.poll_batch_job(job_name)
    while state in _RUNNING_STATES and time.time() < deadline:
        if reporter.check_cancelled():
            reporter.log("Cancellation requested while polling batch job.")
            return None
        time.sleep(_POLL_INTERVAL_S)
        state = batch.poll_batch_job(job_name)
    reporter.log(f"Batch job {job_name} state: {state}")

    if state in _RUNNING_STATES:
        # Still running after this task's budget — re-chain another poll task.
        return {"chain": True, "next_task_args": dict(task_args, phase="poll"),
                "dispatch_deadline_seconds": 1800}

    if state in batch._TERMINAL_FAIL:
        reporter.log(f"Batch job {job_name} ended in {state}; stopping (no chain).")
        return None

    # SUCCEEDED / PARTIALLY_SUCCEEDED: ingest -> refine -> prune.
    reporter.log("Batch succeeded. Ingesting output...")
    raw_filename = batch.download_and_ingest(output_uri, submitted_ids)
    refined = refine_one_raw_annotation_batch(raw_json_filename=raw_filename, verbose=False)
    if refined is None or refined.empty:
        reporter.log("Refinement produced nothing; stopping to avoid a loop.")
        return None

    ok_ids = refined.loc[refined["annotated_ok"].fillna(False).astype(bool), "item_id"].astype(str).tolist()
    fail_ids = refined.loc[refined.get("annotated_fail", False).fillna(False).astype(bool), "item_id"].astype(str).tolist() \
        if "annotated_fail" in refined.columns else []
    remove = set(ok_ids) | set(fail_ids) | set(map(str, submitted_ids))

    fresh = data_io.load_json(storage_location="cache", filename=QUEUE_FILE) or []
    updated = [v for v in fresh if str(v) not in remove]
    pruned = len(fresh) - len(updated)
    if pruned > 0:
        data_io.save_json(data=updated, storage_location="cache", filename=QUEUE_FILE)
    reporter.emit_data({"annotate_queue_len": len(updated)})
    reporter.log(f"Ingested batch: {len(ok_ids)} OK, {len(fail_ids)} fail. Queue: {len(updated):,} remaining.")

    next_chunk = int(task_args.get("chunk_index", 0)) + 1
    max_batches = task_args.get("max_batches")
    if max_batches is not None and next_chunk >= int(max_batches):
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        return None
    if not updated:
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
            next_args = result["next_task_args"]
        reporter.complete()
        print("Batch queue annotation completed.")
    except Exception as exc:
        reporter.fail(str(exc))
        import traceback
        traceback.print_exc()
        sys.exit(1)
