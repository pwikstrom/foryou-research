"""
Queue annotator: process TikTok videos through Gemini for machine annotation.

On Cloud Run this runs as a single-batch Cloud Task that self-chains to the
next batch until the queue is exhausted, the user's max_batches limit is
reached, or the user cancels.

Locally it runs all batches in a single subprocess (same as before).
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter

# Safety validation: reject batch sizes that risk timing out.
# Each video ~60-90s via Gemini with 50 concurrent workers.
# Formula: batch_size * 90 / 50 * 1.5 safety margin.  Must fit in 3600s.
MAX_BATCH_SIZE = 2000
_SECONDS_PER_VIDEO = 90
_WORKERS = 50
_SAFETY_MARGIN = 1.5


def _estimate_seconds(batch_size: int) -> float:
    return batch_size * _SECONDS_PER_VIDEO / _WORKERS * _SAFETY_MARGIN


def _dispatch_deadline_for(batch_size: int) -> int:
    """Return the Cloud Tasks dispatch_deadline in seconds."""
    return 3600 if batch_size > 1000 else 1800


def run_queue_annotator(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Process one batch of annotation and optionally return chain info.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Must contain 'batch_size'. Optional: 'max_batches',
                   'chunk_index', 'initial_total'.

    Returns:
        dict with ``chain=True`` and ``next_task_args`` if another batch
        should be dispatched, or ``None`` when the work is done.
    """
    import fyp.data_io as data_io
    from fyp.machine_annotation import annotate_from_video_id_list

    if not task_args:
        task_args = {}

    batch_size: int = int(task_args.get("batch_size", 500))
    max_batches: int | None = task_args.get("max_batches")
    if max_batches is not None:
        max_batches = int(max_batches)
    chunk_index: int = int(task_args.get("chunk_index", 0))
    # Captured from the queue length on chain #1 and carried forward so that
    # progress framing stays stable across chains even as the queue shrinks
    # under pruning. Absent on the first chain or legacy in-flight tasks —
    # defaulted below from the current queue length.
    initial_total: int = int(task_args.get("initial_total", 0))

    # ---- Validate batch size ----
    if batch_size > MAX_BATCH_SIZE:
        est = _estimate_seconds(batch_size)
        raise ValueError(
            f"batch_size {batch_size} rejected: estimated {est:.0f}s exceeds "
            f"the 3600s Cloud Tasks timeout. Maximum is {MAX_BATCH_SIZE}."
        )

    # ---- Load the annotation queue ----
    target_cache_file = "to_annotate.json"
    if not data_io.exists(storage_location="cache", filename=target_cache_file):
        reporter.log("No to_annotate.json found in cache. Nothing to do.")
        return None

    video_list: list[str] = data_io.load_json(storage_location="cache", filename=target_cache_file)
    if not video_list:
        reporter.log("Annotation queue is empty.")
        return None

    total_queue = len(video_list)
    if initial_total <= 0:
        initial_total = total_queue
    reporter.log(f"Loaded {total_queue:,} videos from queue (initial_total={initial_total:,}).")

    # ---- Slice this batch from the head of the pruned queue ----
    batch = video_list[:batch_size]
    if not batch:
        reporter.log("Queue empty at start of batch. Nothing to do.")
        return None

    already_done = max(0, initial_total - total_queue)
    overall_total = max(initial_total, already_done + len(batch))

    if max_batches is not None:
        total_batches = max_batches
    else:
        total_batches = (initial_total + batch_size - 1) // batch_size
    display_total_batches = max(total_batches, chunk_index + 1)
    batch_label = f"{chunk_index + 1}/{display_total_batches}"

    reporter.log(
        f"Batch {batch_label}: processing {len(batch):,} videos "
        f"(done {already_done:,}/{overall_total:,})"
    )

    # ---- Annotate ----
    ok_ids, fail_ids = annotate_from_video_id_list(
        fine_list=batch,
        verbose=False,
        dry_run=False,
        batch_label=batch_label,
        cumulative_done=already_done,
        cumulative_total=overall_total,
        reporter=reporter,
    )

    # ---- Update queue: remove successful + failed items ----
    # Reload fresh to avoid clobbering concurrent writes. Mirrors the
    # scraper's prune at run_queue_scraper.py.
    fresh_queue = data_io.load_json(storage_location="cache", filename=target_cache_file)
    items_to_remove: set[str] = set(ok_ids) | set(fail_ids)
    pruned_this_batch = 0
    if isinstance(fresh_queue, list):
        updated_queue = [v for v in fresh_queue if v not in items_to_remove]
        pruned_this_batch = len(fresh_queue) - len(updated_queue)
        if pruned_this_batch > 0:
            data_io.save_json(data=updated_queue, storage_location="cache", filename=target_cache_file)
        queue_remaining = len(updated_queue)
    else:
        queue_remaining = max(0, total_queue - len(batch))

    reporter.emit_data({"annotate_queue_len": queue_remaining})
    reporter.log(
        f"Batch {batch_label} complete. "
        f"{len(ok_ids)} OK, {len(fail_ids)} fail. "
        f"Queue: {queue_remaining:,} remaining."
    )

    # ---- Check whether to chain ----
    if reporter.check_cancelled():
        reporter.log("Cancellation requested. Stopping after this batch.")
        return None

    next_chunk = chunk_index + 1
    if max_batches is not None and next_chunk >= max_batches:
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        return None

    if queue_remaining == 0:
        reporter.log("Queue exhausted.")
        return None

    # Safety: if this batch pruned nothing (e.g. refinement failed for the
    # whole batch), chaining would re-process the same head slice and loop.
    # Stop so the user can investigate or the next run picks them up fresh.
    if pruned_this_batch == 0:
        reporter.log(
            "No items pruned from this batch (refinement failure?). "
            "Stopping chain to avoid an infinite retry loop."
        )
        return None

    # More work remains — request a chain dispatch
    next_task_args = {
        "batch_size": batch_size,
        "max_batches": max_batches,
        "chunk_index": next_chunk,
        "initial_total": initial_total,
    }
    reporter.log(f"Chaining to next batch (chunk_index={next_chunk})...")
    return {
        "chain": True,
        "next_task_args": next_task_args,
        "dispatch_deadline_seconds": _dispatch_deadline_for(batch_size),
    }




if __name__ == "__main__":
    import argparse
    import atexit
    import concurrent.futures.thread as _ft

    from fyp.machine_annotation import queue_annotation_loop
    from web_interface.task_status import LocalStatusReporter

    # When a Gemini worker hangs, call_machine_threads() marks it DNF and
    # returns without waiting. The ThreadPoolExecutor's atexit hook would
    # otherwise block process exit until the stuck thread finishes — so drop
    # it so the subprocess can exit cleanly.
    atexit.unregister(_ft._python_exit)

    parser = argparse.ArgumentParser(description="Run queue annotator")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches (default: unlimited)")

    args = parser.parse_args()

    print("Starting Queue Annotator")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    reporter = LocalStatusReporter("queue_annotator")
    try:
        queue_annotation_loop(
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            verbose=False,
            dry_run=False,
            reporter=reporter,
            cancellation_check=reporter.check_cancelled,
        )
        reporter.complete()
        print("Queue annotation process completed.")
        os._exit(0)
    except Exception as e:
        reporter.fail(str(e))
        print(f"Queue annotation process failed: {e}")
        import traceback
        traceback.print_exc()
        os._exit(1)
