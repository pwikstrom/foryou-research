#!/usr/bin/env python3
"""Regression tests for the YouTube media-download failure handling.

Covers the 2026-07-05 incident where YouTube's session rate-limit response
("Video unavailable. … The current session has been rate-limited…") was
classified as permanent ``removed``, and failed media downloads were silently
saved as scrape-ok metadata-only rows and dequeued forever.

Run: python tests/test_youtube_media_retry.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd

from fyp import youtube_dl
from fyp.platform_scraper import THROTTLE_CATEGORIES
from fyp.youtube_dl import YouTubeScraper, _classify_error


PROD_RATE_LIMIT_MSG = (
    "ERROR: [youtube] q6fq4_uP7aM: Video unavailable. This content isn't "
    "available, try again later. The current session has been rate-limited "
    "by YouTube for up to an hour. It is recommended to use `-t sleep` to "
    "add a delay between video requests to avoid exceeding the rate limit."
)


def test_classification() -> None:
    """The rate-limit-disguised-as-unavailable message must stay transient."""
    category, _ = _classify_error(Exception(PROD_RATE_LIMIT_MSG))
    assert category == "rate_limited", f"prod rate-limit message → {category}"
    assert category in THROTTLE_CATEGORIES

    category, _ = _classify_error(Exception("ERROR: [youtube] xx: Video unavailable"))
    assert category == "removed", f"plain removal message → {category}"

    category, _ = _classify_error(
        Exception("Sign in to confirm you're not a bot"))
    assert category == "bot_check", f"bot wall message → {category}"

    # YouTube uses a typographic apostrophe in the real message (seen in prod
    # 2026-07-06 — the ASCII pattern alone classified it as unknown).
    category, _ = _classify_error(
        Exception("ERROR: [youtube] xyz: Sign in to confirm you’re not a bot. "
                  "Use --cookies-from-browser or --cookies for the authentication."))
    assert category == "bot_check", f"curly-apostrophe bot wall → {category}"

    scraper = YouTubeScraper()
    assert scraper.classify_error("rate_limited") == "transient:rate_limited"
    assert scraper.classify_error("removed") == "permanent:removed"
    print("OK  classification")






def test_media_failure_attrs() -> None:
    """fetch() must stamp media_error_type when metadata succeeds but media fails."""
    fake_info = {
        "description": "a short",
        "duration": 30,
        "channel_id": "UCx",
        "uploader_id": "@x",
        "channel": "X",
        "view_count": 100,
        "like_count": 1,
        "comment_count": 0,
        "channel_follower_count": 10,
        "categories": [],
    }

    scraper = YouTubeScraper()
    with patch.object(youtube_dl, "_extract_metadata", return_value=(fake_info, None)), \
         patch.object(youtube_dl, "_download_media",
                      return_value=(False, "rate_limited", "simulated")):
        row = scraper.fetch("abc123def45", save_media=True, save_path="/tmp")

    assert isinstance(row, pd.DataFrame) and not row.empty
    assert row.loc[0, "video_downloaded"] == False  # noqa: E712
    assert row.attrs.get("media_error_type") == "rate_limited"
    assert not scraper.classify_error(row.attrs["media_error_type"]).startswith("permanent")

    with patch.object(youtube_dl, "_extract_metadata", return_value=(fake_info, None)), \
         patch.object(youtube_dl, "_download_media",
                      return_value=(True, None, "")):
        row = scraper.fetch("abc123def45", save_media=True, save_path="/tmp")

    assert row.loc[0, "video_downloaded"] == True  # noqa: E712
    assert "media_error_type" not in row.attrs
    print("OK  media-failure attrs")






def _fake_metadata_row(item_id: str, media_error: str | None = None) -> pd.DataFrame:
    """A >10-column single-row frame like a real fetch result."""
    row = pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "yt_author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "yt_like_count": 0,
        "yt_comment_count": 0, "yt_channel_follower_count": 0,
        "yt_categories": "", "video_downloaded": False,
    }])
    if media_error is not None:
        row.attrs["media_error_type"] = media_error
        row.attrs["media_error_detail"] = "simulated"
    return row






def test_orchestrator_media_retry_and_breaker() -> None:
    """Transient media failures stay retryable; a rate-limit storm aborts the batch."""
    # Patch on the owning module: fyp/scrape.py became the fyp.scrape package
    # (Phase 8); download_video_threads resolves download_single_video via its
    # own module globals, so the patch must land on fyp.scrape.scrape.
    from fyp.scrape import scrape

    # --- transient media failure: row returned AND id marked transient ---
    def fake_dl(video_id=None, **kwargs):
        return _fake_metadata_row(video_id, media_error="rate_limited")

    with patch.object(scrape, "download_single_video", side_effect=fake_dl):
        results, perm, trans = scrape.download_video_threads(
            interesting_videos=["vid_a", "vid_b"], max_workers=2,
            dry_run=True, platform="youtube")

    assert set(trans) == {"vid_a", "vid_b"}, f"media-failed ids not transient: {trans}"
    assert perm == []
    assert not results.empty and set(results["item_id"]) == {"vid_a", "vid_b"}
    print("OK  transient media failure keeps items retryable")

    # --- circuit breaker: a storm of rate_limited failures aborts the batch ---
    def fake_dl_storm(video_id=None, **kwargs):
        empty = pd.DataFrame()
        empty.attrs["error_type"] = "rate_limited"
        return empty

    n_items = scrape.CIRCUIT_BREAKER_THRESHOLD * 3
    ids = [f"v{i}" for i in range(n_items)]
    with patch.object(scrape, "download_single_video", side_effect=fake_dl_storm):
        results, perm, trans = scrape.download_video_threads(
            interesting_videos=ids, max_workers=2,
            dry_run=True, platform="youtube")

    assert results.attrs.get("circuit_breaker_tripped") is True
    assert perm == []
    assert set(trans) == set(ids), "aborted/failed items must all stay queued"
    print("OK  circuit breaker trips and keeps everything queued")






if __name__ == "__main__":
    test_classification()
    test_media_failure_attrs()
    test_orchestrator_media_retry_and_breaker()
    print("All tests passed.")
