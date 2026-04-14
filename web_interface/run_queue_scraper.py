# -*- coding: utf-8 -*-
"""
Queue scraper: download TikTok video metadata and media via yt-dlp.

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


# Each video ~10s with 4 threads → 500 videos ≈ 1250s.
# 1800s dispatch deadline leaves comfortable margin.
MAX_BATCH_SIZE = 500
_DISPATCH_DEADLINE = 1800


def run_queue_scraper(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Process one batch of scraping and optionally return chain info.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional dict with 'batch_size', 'max_batches',
                   'chunk_index', 'videos_processed'.

    Returns:
        dict with ``chain=True`` and ``next_task_args`` if another batch
        should be dispatched, or ``None`` when the work is done.
    """
    import fyp.data_io as data_io
    from fyp.scrape import download_video_threads

    if not task_args:
        task_args = {}

    batch_size: int = min(int(task_args.get("batch_size", 500)), MAX_BATCH_SIZE)
    max_batches: int | None = task_args.get("max_batches")
    if max_batches is not None:
        max_batches = int(max_batches)
    chunk_index: int = int(task_args.get("chunk_index", 0))
    videos_processed: int = int(task_args.get("videos_processed", 0))

    # ---- Load the scrape queue ----
    target_cache_file = "to_scrape.json"
    if not data_io.exists(storage_location="cache", filename=target_cache_file):
        reporter.log("No to_scrape.json found in cache. Nothing to do.")
        return None

    video_list: list[str] = data_io.load_json(storage_location="cache", filename=target_cache_file)
    if not video_list:
        reporter.log("Scrape queue is empty.")
        return None

    total_queue = len(video_list)
    reporter.log(f"Loaded {total_queue:,} videos from queue.")

    # ---- Slice this batch ----
    offset = videos_processed
    batch = video_list[offset : offset + batch_size]
    if not batch:
        reporter.log("No more videos to process at this offset.")
        return None

    if max_batches is not None:
        total_batches = max_batches
    else:
        total_batches = (total_queue + batch_size - 1) // batch_size
    overall_total = min(total_queue, total_batches * batch_size)
    batch_label = f"{chunk_index + 1}/{total_batches}"

    reporter.log(
        f"Batch {batch_label}: scraping {len(batch):,} videos "
        f"(offset {offset:,}, overall {videos_processed:,}/{overall_total:,})"
    )

    pct_before = int(videos_processed / overall_total * 100) if overall_total else 0
    reporter.update_progress(pct_before,
        f"Batch {batch_label}: scraping {len(batch):,} videos")
    reporter.emit_data({"threads": 8})

    done_in_batch = 0
    ok_in_batch = 0
    fail_in_batch = 0

    def _on_threads_change(n: int) -> None:
        reporter.emit_data({"threads": n})

    def _on_video_done(idx: int, ok: bool, error_cat: str | None) -> None:
        nonlocal done_in_batch, ok_in_batch, fail_in_batch
        done_in_batch += 1
        if ok:
            ok_in_batch += 1
        else:
            fail_in_batch += 1
        completed = videos_processed + done_in_batch
        pct = int(completed / overall_total * 100) if overall_total else 0
        reporter.update_progress(pct,
            f"Batch {batch_label}: {done_in_batch}/{len(batch)} "
            f"({ok_in_batch} OK, {fail_in_batch} fail)")

    # ---- Scrape ----
    results_df, permanent_failed, transient_failed = download_video_threads(
        interesting_videos=batch,
        max_workers=8,
        verbose=False,
        dry_run=False,
        batch_label=batch_label,
        cumulative_done=videos_processed,
        cumulative_total=overall_total,
        on_concurrency_change=_on_threads_change,
        on_video_done=_on_video_done,
    )

    good_ids = []
    if not results_df.empty and "item_id" in results_df.columns:
        good_ids = results_df["item_id"].to_list()

    new_videos_processed = videos_processed + len(batch)
    pct_after = int(new_videos_processed / overall_total * 100) if overall_total else 100
    reporter.update_progress(pct_after,
        f"Batch {batch_label} done: {len(good_ids)} OK, "
        f"{len(permanent_failed)} permanent fail, {len(transient_failed)} transient")

    # ---- Update queue: remove successful + permanently failed items ----
    # Reload fresh to avoid clobbering concurrent writes
    fresh_queue = data_io.load_json(storage_location="cache", filename=target_cache_file)
    if isinstance(fresh_queue, list):
        items_to_remove = set(good_ids + permanent_failed)
        updated_queue = [v for v in fresh_queue if v not in items_to_remove]
        if len(updated_queue) < len(fresh_queue):
            data_io.save_json(data=updated_queue, storage_location="cache", filename=target_cache_file)
        queue_remaining = len(updated_queue)
    else:
        queue_remaining = max(0, total_queue - new_videos_processed)

    reporter.emit_data({"scrape_queue_len": queue_remaining})
    reporter.log(
        f"Batch {batch_label} complete. "
        f"{len(good_ids)} OK, {len(permanent_failed)} permanent fail, "
        f"{len(transient_failed)} transient (will retry). "
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

    if new_videos_processed >= total_queue:
        reporter.log("Queue exhausted.")
        return None

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
        "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
    }




if __name__ == "__main__":
    import argparse
    from web_interface.task_status import LocalStatusReporter
    from fyp.scrape import queue_scraper_loop

    parser = argparse.ArgumentParser(description="Run queue scraper")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches (default: unlimited)")

    args = parser.parse_args()

    print(f"Starting Queue Scraper")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    reporter = LocalStatusReporter("queue_scraper")
    try:
        queue_scraper_loop(
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            verbose=False,
            dry_run=False
        )
        reporter.complete()
        print("Queue scraping process completed.")

    except Exception as e:
        reporter.fail(str(e))
        print(f"Queue scraping process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
