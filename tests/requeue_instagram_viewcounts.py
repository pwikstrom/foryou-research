"""One-off requeue of Instagram items scraped without view counts.

Historical Instagram rows were scraped before the page-JSON count
supplementation existed (fyp.instagram_dl._fetch_page_counts), so they carry
the -1 missing-count sentinel in play_count and every *_per_K_play is NA.
Those rows are scrape_status="ok", so the normal queue builder never revisits
them — this script re-queues their item ids on the Instagram scrape queue.

With the newest-scrape-wins consolidation dedup in place, the re-scraped rows
supersede the old ones at the next Consolidate & Refresh. A re-scrape that
still finds no view count is harmless: the row comes back with -1 again and
the derivation masks keep the metrics NA.

Usage:
    python tests/requeue_instagram_viewcounts.py           # dry run
    python tests/requeue_instagram_viewcounts.py --apply   # queue the ids
"""
import sys

from fyp import fyp_config

fyp_config.initialize()

import pandas as pd

from fyp import data_io, scrape_queues

APPLY = "--apply" in sys.argv


def main() -> None:
    fn = "scrapes_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=fn):
        print(f"{fn} not found; nothing to do")
        return

    df = data_io.load_parquet(storage_location="recoded", filename=fn)
    needed = {"source_platform", "scrape_status", "play_count", "item_id"}
    missing = needed - set(df.columns)
    if missing:
        print(f"{fn} is missing columns {sorted(missing)}; nothing to do")
        return

    mask = (
        (df["source_platform"] == "instagram")
        & (df["scrape_status"] == "ok")
        & (df["play_count"] < 0).fillna(False)
    )
    ids = df.loc[mask, "item_id"].dropna().unique().tolist()
    print(f"Instagram rows scraped ok but without a view count: {len(ids)}")
    if not ids:
        return

    if not APPLY:
        print("Dry run (pass --apply to queue). First ids:", ids[:10])
        return

    added = scrape_queues.append_to_scrape_queue("instagram", ids)
    print(f"Queued {added} item(s) on {scrape_queues.queue_filename('instagram')}.")
    print("Start the Instagram scraper from the enrichment tab to drain the queue.")


main()
