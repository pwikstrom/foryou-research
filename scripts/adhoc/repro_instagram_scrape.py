#!/usr/bin/env python3
"""Live smoke test: scrape one public Instagram reel (manual, uses network).

Verifies with real traffic that (1) the /p/ URL form serves a reel shortcode,
(2) item_id is stamped from the requested shortcode (not yt-dlp's numeric
media pk), and (3) the canonical frame carries per-K rates. Local dev uses
Chrome cookies — be logged in to Instagram in Chrome.

Usage:
    python tests/repro_instagram_scrape.py [SHORTCODE] [--media]
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyp.platform_scraper import get_scraper

# A long-lived public reel (Instagram's own account); override on the CLI.
DEFAULT_SHORTCODE = "C2Ejk2Pu4dP"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    shortcode = args[0] if args else DEFAULT_SHORTCODE
    save_media = "--media" in sys.argv

    scraper = get_scraper("instagram", verbose=True)
    print(f"URL: {scraper.item_url(shortcode)}")
    print(f"Health: {scraper.health_check()}")

    save_path = tempfile.mkdtemp(prefix="ig_repro_")
    raw = scraper.fetch(shortcode, save_media=save_media, save_path=save_path, verbose=True)

    if raw.empty:
        print(f"FETCH FAILED: [{raw.attrs.get('error_type')}] {raw.attrs.get('error_detail')}")
        print(f"classified: {scraper.classify_error(raw.attrs.get('error_type'))}")
        sys.exit(1)

    print("\n--- raw row ---")
    print(raw.T)
    assert raw.loc[0, "item_id"] == shortcode, (
        f"item_id drifted: {raw.loc[0, 'item_id']!r} != requested {shortcode!r}"
    )

    canonical = scraper.canonicalize_batch(scraper.prepare_raw_batch(raw.copy()))
    print("\n--- canonical row (selected) ---")
    cols = ["item_id", "desc", "create_time", "duration", "author_name", "play_count",
            "ig_like_count", "faves_per_K_play", "comments_per_K_play",
            "source_platform", "scrape_status", "scrape_contract_version"]
    print(canonical[[c for c in cols if c in canonical.columns]].T)

    if save_media:
        print(f"\nMedia dir {save_path}: {os.listdir(save_path)}")
    print("\nOK: Instagram live scrape round-trip")


if __name__ == "__main__":
    main()
