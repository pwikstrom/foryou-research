"""
Queue scraper: download short-video metadata and media for one platform.

Each platform has its own queue (``to_scrape_<platform>.json``) and its own
worker process (``queue_scraper_<platform>``); the platform rides along in
``task_args`` and is carried through self-chaining.

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

# Upper safety cap on a single Cloud Task batch. Steady-state scrape memory is
# flat (~0.5 GiB for hundreds of items — there is no per-item leak). The
# historical OOM (a 500-item TikTok batch dying at ~215 items) was moviepy
# slideshow assembly rendering photo posts at native resolution, now bounded at
# the root by SLIDESHOW_MAX_DIMENSION in fyp.scrape. Remaining guards:
# download_video_threads runs a memory safety valve on a background timer
# (saves completed work and defers the rest if container memory nears the
# limit), and tiktok_dl caps a single download's size. This batch cap is a
# secondary blast-radius bound — larger batches just recycle the container less
# often; the remainder of the queue drains by self-chaining to the next batch.
MAX_BATCH_SIZE = 1000
_DISPATCH_DEADLINE = 1800


def run_queue_scraper(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Process one batch of scraping and optionally return chain info.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional dict with 'platform', 'batch_size', 'max_batches',
                   'chunk_index', 'initial_total'.

    Returns:
        dict with ``chain=True`` and ``next_task_args`` if another batch
        should be dispatched, or ``None`` when the work is done.
    """
    import fyp.scrape_queues as scrape_queues
    from fyp.platform_scraper import get_scraper
    from fyp.scrape import download_video_threads

    if not task_args:
        task_args = {}

    platform: str = str(task_args.get("platform") or "") or scrape_queues.default_platform()
    batch_size: int = min(int(task_args.get("batch_size", 500)), MAX_BATCH_SIZE)
    max_batches: int | None = task_args.get("max_batches")
    if max_batches is not None:
        max_batches = int(max_batches)
    chunk_index: int = int(task_args.get("chunk_index", 0))
    # ``initial_total`` is captured on chain #1 from the queue length and
    # carried forward unchanged, giving stable progress framing across chains.
    # Absent on the first chain (or on old in-flight tasks that used the
    # ``videos_processed`` scheme) — default from the current queue below.
    initial_total: int = int(task_args.get("initial_total", 0))
    # Job-wide OK/fail totals carried forward across self-chained batches so the
    # progress line shows totals, not batch-local counts.
    cumulative_ok: int = int(task_args.get("cumulative_ok", 0))
    cumulative_fail: int = int(task_args.get("cumulative_fail", 0))

    # ---- Load the per-platform scrape queue (migrates a legacy queue) ----
    video_list: list[str] = scrape_queues.load_scrape_queue(platform)
    if not video_list:
        reporter.log(f"Scrape queue for '{platform}' is empty.")
        return None

    total_queue = len(video_list)
    if initial_total <= 0:
        initial_total = total_queue
    # Frame the bar/pending against THIS run's target when the run is capped with
    # max_batches, not the whole queue. Mirrors the local loop's total_items.
    # An uncapped run frames against the whole queue. initial_total stays = full
    # queue for the already_done math below, and is carried across chains
    # unchanged, so run_target is stable per chain.
    if max_batches is not None:
        run_target = min(initial_total, max_batches * batch_size)
    else:
        run_target = initial_total
    reporter.log(
        f"Loaded {total_queue:,} items from '{platform}' queue (initial_total={initial_total:,})."
    )

    # ---- Platform health check ----
    # Surface obvious auth/session problems before kicking off the batch —
    # e.g. an expired TikTok sessionid means every scrape gets a 403 from the
    # platform's bot wall. Platforms with nothing to report return None.
    health = get_scraper(platform).health_check()
    if health is not None:
        status = health.get('status')
        message = health.get('message')
        if status in ('expired', 'missing'):
            reporter.log(f"WARNING: {platform} auth/health {status} — {message}")
            reporter.log("Continuing without authentication. Expect elevated failure rate.")
        elif status in ('expiring_soon', 'stale'):
            reporter.log(f"NOTE: {platform} auth/health {status} — {message}")
        else:
            reporter.log(f"{platform} health: {message}")

    # ---- Slice this batch from the head of the pruned queue ----
    # Prior chains removed completed items, so the queue's head is always the
    # next work to do. No offset arithmetic needed.
    batch = video_list[:batch_size]
    if not batch:
        reporter.log("Queue empty at start of batch. Nothing to do.")
        return None

    # Items completed in prior chains (for display and progress framing).
    # Cap at initial_total so a queue that grew mid-chain (user re-queueing)
    # doesn't report negative progress.
    already_done = max(0, initial_total - total_queue)
    overall_total = max(run_target, already_done + len(batch))

    if max_batches is not None:
        total_batches = max_batches
    else:
        total_batches = (initial_total + batch_size - 1) // batch_size
    # Avoid "3/2"-style labels if chain_index drifts past total_batches
    # (possible with transient-failure retries).
    display_total_batches = max(total_batches, chunk_index + 1)
    batch_label = f"{chunk_index + 1}/{display_total_batches}"

    reporter.log(
        f"Batch {batch_label}: scraping {len(batch):,} videos "
        f"(done {already_done:,}/{overall_total:,})"
    )

    pct_before = int(already_done / overall_total * 100) if overall_total else 0
    reporter.update_progress(pct_before,
        f"Batch {batch_label}: scraping {len(batch):,} videos")
    reporter.emit_data({"threads": 4})

    def _on_threads_change(n: int) -> None:
        reporter.emit_data({"threads": n})

    # ---- Scrape ----
    # The monitor thread inside download_video_threads owns the live progress
    # line (it has throughput / processing count / ETA); pass the reporter and
    # the job-wide OK/fail carry-over so it can render totals.
    results_df, permanent_failed, transient_failed = download_video_threads(
        interesting_videos=batch,
        max_workers=8,
        verbose=False,
        dry_run=False,
        batch_label=batch_label,
        cumulative_done=already_done,
        cumulative_total=overall_total,
        cumulative_ok=cumulative_ok,
        cumulative_fail=cumulative_fail,
        reporter=reporter,
        on_concurrency_change=_on_threads_change,
        platform=platform,
    )

    # The memory safety valve stopped this batch early (container memory near
    # the limit). The completed rows below are still saved and pruned; the
    # deferred items stay queued and are picked up by the next chain, which
    # runs in a freshly recycled worker with reset memory. This is NOT a
    # circuit-breaker abort — chaining continues normally.
    if results_df.attrs.get('memory_stop'):
        reporter.log(
            "Batch stopped early by the memory safety valve — completed items "
            "saved; remaining items deferred to the next (fresh) batch."
        )

    good_ids = []
    if not results_df.empty and "item_id" in results_df.columns:
        good_ids = results_df["item_id"].to_list()

    pct_after = int((already_done + len(batch)) / overall_total * 100) if overall_total else 100
    reporter.update_progress(pct_after,
        f"Batch {batch_label} done: {len(good_ids)} OK, "
        f"{len(permanent_failed)} permanent fail, {len(transient_failed)} transient")

    # ---- Update queue: remove successful + permanently failed items ----
    # prune_scrape_queue reloads fresh to avoid clobbering concurrent writes.
    # Transient failures are excluded: they include metadata-only rows whose
    # media phase failed transiently (also present in good_ids) — those must
    # stay queued for a media retry.
    items_to_remove: set[str] = (set(good_ids) | set(permanent_failed)) - set(transient_failed)
    pruned_this_batch, queue_remaining = scrape_queues.prune_scrape_queue(platform, items_to_remove)

    reporter.emit_data({"scrape_queue_len": queue_remaining})
    reporter.log(
        f"Batch {batch_label} complete. "
        f"{len(good_ids)} OK, {len(permanent_failed)} permanent fail, "
        f"{len(transient_failed)} transient (will retry). "
        f"Queue: {queue_remaining:,} remaining."
    )

    # ---- Check whether to chain ----
    if results_df.attrs.get('circuit_breaker_tripped'):
        reporter.log(
            "Rate-limit circuit breaker tripped — the platform is throttling "
            "this session. Stopping the chain; unfinished items stay in the "
            "queue. Re-run the scraper later."
        )
        reporter.emit_data({"rate_limit_abort": True})
        return None

    if results_df.attrs.get('permanent_storm_tripped'):
        reporter.log(
            f"Permanent-failure storm detected "
            f"({results_df.attrs.get('permanent_storm_category')}): every item is "
            f"failing with the same permanent verdict, so the scraper or its "
            f"session is likely broken — not the items. Stopping the chain; the "
            f"affected items stay queued. A scraper alert was raised on the "
            f"enrichment page — revise the scraper before re-running."
        )
        reporter.emit_data({"permanent_storm_abort": True})
        return None

    if results_df.attrs.get('transient_storm_tripped'):
        reporter.log(
            f"Transient-failure storm detected "
            f"({results_df.attrs.get('transient_storm_category')}): every item is "
            f"failing with the same retryable verdict, so the platform has likely "
            f"changed something and the scraper is broken — grinding on would churn "
            f"the queue at zero yield. Stopping the chain; the items stay queued. "
            f"A scraper alert was raised on the enrichment page — revise the "
            f"scraper (or wait for an upstream fix) before re-running."
        )
        reporter.emit_data({"transient_storm_abort": True})
        return None

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

    # Safety: if this batch pruned nothing (e.g. every item was a transient
    # failure), chaining would re-process the exact same head slice and loop
    # forever. Stop chaining so the user can intervene or the next run picks
    # them up fresh.
    if pruned_this_batch == 0:
        reporter.log(
            f"No items pruned from this batch ({len(transient_failed)} transient). "
            f"Stopping chain to avoid an infinite retry loop."
        )
        return None

    next_task_args = {
        "platform": platform,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "chunk_index": next_chunk,
        "initial_total": initial_total,
        "cumulative_ok": cumulative_ok + len(good_ids),
        "cumulative_fail": cumulative_fail + len(permanent_failed),
    }
    reporter.log(f"Chaining to next batch (chunk_index={next_chunk})...")
    return {
        "chain": True,
        "next_task_args": next_task_args,
        "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
    }




if __name__ == "__main__":
    import argparse

    import fyp.scrape_queues as scrape_queues
    from fyp.scrape import queue_scraper_loop
    from web_interface.drain_lease import DrainLease
    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Run queue scraper")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches (default: unlimited)")
    parser.add_argument("--platform", type=str, default=None, help="Platform queue to drain (default: contract default)")

    args = parser.parse_args()

    platform = args.platform or scrape_queues.default_platform()
    process_name = f"queue_scraper_{platform}"

    print(f"Starting Queue Scraper for platform '{platform}'")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    reporter = LocalStatusReporter(process_name)
    try:
        # The lease marks this drain on the shared storage so the Cloud Run UI
        # refuses to start the same platform's scraper (or a consolidation)
        # while it runs. Inert in pure-local dev (separate storage).
        with DrainLease(platform):
            queue_scraper_loop(
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                verbose=False,
                dry_run=False,
                reporter=reporter,
                cancellation_check=reporter.check_cancelled,
                platform=platform,
                process_name=process_name,
            )
        reporter.complete()
        print("Queue scraping process completed.")

    except Exception as e:
        reporter.fail(str(e))
        print(f"Queue scraping process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
