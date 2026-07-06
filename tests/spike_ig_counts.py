#!/usr/bin/env python3
"""Verification/repro for Instagram view/play-count supplementation.

Phase-0 established that yt-dlp returns no view_count for reels, the post-page
HTML embeds none (``_fetch_page_counts`` is a no-op for reels), and instaloader's
GraphQL ``from_shortcode`` path is now blocked — but Instagram's authenticated
``api/v1/media/{pk}/info/`` endpoint still returns ``play_count`` /
``ig_play_count`` / ``like_count`` / ``comment_count``. That endpoint is what
``fyp.instagram_dl._fetch_media_info_counts`` now calls.

This script exercises that helper against a handful of REAL reel shortcodes and
prints, per shortcode, the media-info counts beside yt-dlp's (which has the
like/comment counts but no view_count) so the recovered play_count can be
sanity-checked. Read-only w.r.t. the pipeline; hits Instagram with the research
account, so it runs a small sample with the helper's own inter-call jitter.

Usage:
    python tests/spike_ig_counts.py                  # 5 ids from the IG queue
    python tests/spike_ig_counts.py --n 8            # first 8 queue ids
    python tests/spike_ig_counts.py CODE1 CODE2 ...  # explicit shortcodes
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# In local dev, scraper_cookies.requests_cookiejar() only returns the prod
# cookie file when YTDLP_COOKIE_FILE_INSTAGRAM points at it (Cloud Run gets it
# from GCS). Wire it to the /tmp copy so the helper is tested against the EXACT
# cookie jar production uses. Harmless if already set or absent.
_PROD_COOKIE = "/tmp/instagram_cookies.txt"
if (not os.environ.get("K_SERVICE")
        and not os.environ.get("YTDLP_COOKIE_FILE_INSTAGRAM")
        and os.path.exists(_PROD_COOKIE)):
    os.environ["YTDLP_COOKIE_FILE_INSTAGRAM"] = _PROD_COOKIE

from fyp import fyp_config

fyp_config.initialize()

from fyp import instagram_dl, scrape_queues


DEFAULT_N = 5


def _yt_counts(url, code):
    info, fail = instagram_dl._extract_metadata(url, code)
    if info is None:
        return {"error": (fail.attrs.get("error_type") if fail is not None else "no-info")}
    return {
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
    }


def main():
    argv = sys.argv[1:]
    n = DEFAULT_N
    codes = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--n":
            n = int(argv[i + 1]); i += 2; continue
        if a.startswith("-"):
            i += 1; continue
        codes.append(a); i += 1

    if not codes:
        codes = list(scrape_queues.load_scrape_queue("instagram"))[:n]
    print(f"Testing {len(codes)} shortcodes: {codes}\n")

    play_hits = 0
    for idx, code in enumerate(codes, 1):
        url = instagram_dl.InstagramScraper().item_url(code)
        pk = instagram_dl._shortcode_to_mediaid(code)
        print(f"[{idx}/{len(codes)}] {code}  (pk={pk})")

        try:
            yt = _yt_counts(url, code)
        except Exception as exc:
            yt = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"   yt-dlp     : {yt}")

        try:
            mi = instagram_dl._fetch_media_info_counts(code)
        except Exception as exc:
            mi = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"   media-info : {mi}")
        if isinstance(mi, dict) and isinstance(mi.get("play_count"), int):
            play_hits += 1
        print()

    n_codes = len(codes)
    print("=" * 70)
    print(f"SUMMARY: media-info play_count present for {play_hits}/{n_codes} shortcodes")
    print(f"         circuit breaker open: {instagram_dl._media_info_circuit_open()}")
    print("=" * 70)


if __name__ == "__main__":
    main()
