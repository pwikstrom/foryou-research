#!/usr/bin/env python3
"""Live smoke test: scrape one public YouTube video (manual, uses network).

Verifies metadata extraction, the duration cap (long videos stay
metadata-only), and — with --media on a short video — the 720p DASH-merge
download. Local dev uses Chrome cookies.

Usage:
    python tests/repro_youtube_scrape.py [VIDEO_ID] [--media]
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyp.platform_scraper import get_scraper

# "Me at the zoo" — 19s, public since 2005; override on the CLI.
DEFAULT_VIDEO_ID = "jNQXAC9IVRw"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    video_id = args[0] if args else DEFAULT_VIDEO_ID
    save_media = "--media" in sys.argv

    scraper = get_scraper("youtube", verbose=True)
    print(f"URL: {scraper.item_url(video_id)}")
    print(f"Health: {scraper.health_check()}")
    print(f"Duration cap: {scraper.media_duration_cap()}s")

    save_path = tempfile.mkdtemp(prefix="yt_repro_")
    raw = scraper.fetch(video_id, save_media=save_media, save_path=save_path, verbose=True)

    if raw.empty:
        print(f"FETCH FAILED: [{raw.attrs.get('error_type')}] {raw.attrs.get('error_detail')}")
        print(f"classified: {scraper.classify_error(raw.attrs.get('error_type'))}")
        sys.exit(1)

    print("\n--- raw row ---")
    print(raw.T)
    assert raw.loc[0, "item_id"] == video_id

    canonical = scraper.canonicalize_batch(scraper.prepare_raw_batch(raw.copy()))
    print("\n--- canonical row (selected) ---")
    cols = ["item_id", "desc", "create_time", "duration", "author_name", "play_count",
            "yt_like_count", "yt_channel_follower_count", "yt_categories",
            "faves_per_K_play", "comments_per_K_play",
            "source_platform", "scrape_status", "scrape_contract_version"]
    print(canonical[[c for c in cols if c in canonical.columns]].T)

    if save_media:
        print(f"\nMedia dir {save_path}: {os.listdir(save_path)}")
    print("\nOK: YouTube live scrape round-trip")


if __name__ == "__main__":
    main()
