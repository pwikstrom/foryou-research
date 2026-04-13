# -*- coding: utf-8 -*-
"""
Queue annotator: process TikTok videos through Gemini for machine annotation.

On Cloud Run this runs as a single-batch Cloud Task that self-chains to the
next batch until the queue is exhausted, the user's max_batches limit is
reached, or the user cancels.

Locally it runs all batches in a single subprocess (same as before).
"""

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
                   'chunk_index', 'videos_processed'.

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
    videos_processed: int = int(task_args.get("videos_processed", 0))

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
    reporter.log(f"Loaded {total_queue:,} videos from queue.")

    # ---- Slice this batch ----
    offset = videos_processed
    batch = video_list[offset : offset + batch_size]
    if not batch:
        reporter.log("No more videos to process at this offset.")
        return None

    # Compute overall totals for progress reporting
    if max_batches is not None:
        total_batches = max_batches
    else:
        total_batches = (total_queue + batch_size - 1) // batch_size
    overall_total = min(total_queue, total_batches * batch_size)
    batch_label = f"{chunk_index + 1}/{total_batches}"

    reporter.log(
        f"Batch {batch_label}: processing {len(batch):,} videos "
        f"(offset {offset:,}, overall {videos_processed:,}/{overall_total:,})"
    )

    # ---- Annotate ----
    annotate_from_video_id_list(
        fine_list=batch,
        verbose=False,
        dry_run=False,
        batch_label=batch_label,
        cumulative_done=videos_processed,
        cumulative_total=overall_total,
        reporter=reporter,
    )

    new_videos_processed = videos_processed + len(batch)
    queue_remaining = total_queue - new_videos_processed
    reporter.emit_data({"annotate_queue_len": max(0, queue_remaining)})
    reporter.log(f"Batch {batch_label} complete. {new_videos_processed:,} processed, {max(0, queue_remaining):,} remaining.")

    # ---- Check whether to chain ----
    if reporter.check_cancelled():
        reporter.log("Cancellation requested. Stopping after this batch.")
        return None

    next_chunk = chunk_index + 1
    if max_batches is not None and next_chunk >= max_batches:
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        return None

    if new_videos_processed >= total_queue:
        reporter.log("Queue exhausted.")
        return None

    # More work remains — request a chain dispatch
    next_task_args = {
        "batch_size": batch_size,
        "max_batches": max_batches,
        "chunk_index": next_chunk,
        "videos_processed": new_videos_processed,
    }
    reporter.log(f"Chaining to next batch (chunk_index={next_chunk})...")
    return {
        "chain": True,
        "next_task_args": next_task_args,
        "dispatch_deadline_seconds": _dispatch_deadline_for(batch_size),
    }




if __name__ == "__main__":
    import argparse
    from web_interface.task_status import LocalStatusReporter
    from fyp.machine_annotation import queue_annotation_loop

    parser = argparse.ArgumentParser(description="Run queue annotator")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches (default: unlimited)")

    args = parser.parse_args()

    print(f"Starting Queue Annotator")
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
    except Exception as e:
        reporter.fail(str(e))
        print(f"Queue annotation process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
