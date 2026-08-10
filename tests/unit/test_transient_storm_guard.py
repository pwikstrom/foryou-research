#!/usr/bin/env python3
"""Tests for the transient-failure storm guard in the scrape batch loop.

Covers the 2026-08-10 incident where TikTok deployed a new bot-challenge wall
that made yt-dlp fail every item with "No video formats found!" → the
retryable "unknown" category. Neither the rate-limit circuit breaker (throttle
categories only) nor the permanent-storm guard (permanent verdicts only) could
see it, so the worker churned the whole queue at 0% yield, burning 3 yt-dlp
attempts per item and self-chaining batch after batch. The guard treats a run
of consecutive *identical* transient classifications as a broken platform or
scraper: the batch aborts, the batch loop stops chaining, and a persistent
scraper alert is raised. The items are already transient, so they stay queued
as-is — no demotion is needed.

Usage:
    python tests/unit/test_transient_storm_guard.py
    pytest tests/unit/test_transient_storm_guard.py
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from fyp.scrape import scrape
from fyp.youtube_dl import YouTubeScraper


STORM_THRESHOLD = 5  # small test threshold, patched over the config accessor




def _failure(category: str) -> pd.DataFrame:
    """An empty fetch result carrying a failure category, like a real miss."""
    empty = pd.DataFrame()
    empty.attrs["error_type"] = category
    return empty




def _metadata_row(item_id: str) -> pd.DataFrame:
    """A >10-column single-row frame like a real fetch result."""
    return pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "fave_count_raw": 0,
        "comment_count_raw": 0, "share_count_raw": 0, "video_downloaded": True,
    }])




def _run_batch(ids, fake_dl, platform, max_workers=2, dry_run=True):
    """Run download_video_threads with the fetch layer faked out.

    The transient-storm threshold is pinned to STORM_THRESHOLD, version
    registration is a no-op, and YouTube's 1.5s inter-request pacing is
    disabled so the threaded batch runs instantly.
    """
    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "_transient_storm_threshold", return_value=STORM_THRESHOLD), \
         patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                      lambda: None), \
         patch.object(YouTubeScraper, "inter_request_delay", return_value=0.0):
        return scrape.download_video_threads(
            interesting_videos=ids, max_workers=max_workers,
            dry_run=dry_run, platform=platform)




@pytest.mark.parametrize("platform", ["tiktok", "instagram", "youtube"])
def test_transient_storm_trips(platform):
    """A homogeneous transient storm aborts; every id stays queued."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("unknown")

    results, perm, trans = _run_batch(ids, fake_dl, platform)

    assert results.attrs.get("transient_storm_tripped") is True
    assert results.attrs.get("transient_storm_category") == "transient:unknown"
    assert results.attrs.get("permanent_storm_tripped") is False
    assert results.attrs.get("circuit_breaker_tripped") is False
    assert perm == [], f"storm ids must not be marked permanent: {perm}"
    assert set(trans) == set(ids), "all storm/aborted ids must stay queued"
    print(f"PASS: transient storm trips ({platform})")




def test_heterogeneous_transients_do_not_trip():
    """Mixed transient categories are ordinary flakiness — no storm verdict."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]
    calls = {"n": 0}

    def fake_dl(video_id=None, **kwargs):
        calls["n"] += 1
        return _failure("unknown" if calls["n"] % 2 else "network")

    # max_workers=1: sequential fetches keep the alternation consecutive.
    results, perm, trans = _run_batch(ids, fake_dl, "tiktok", max_workers=1)

    assert results.attrs.get("transient_storm_tripped") is False
    assert perm == []
    assert set(trans) == set(ids)
    print("PASS: heterogeneous transients do not trip")




def test_subthreshold_homogeneous_run_does_not_trip():
    """A short run of one transient category is below suspicion."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD - 1)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("unknown")

    results, perm, trans = _run_batch(ids, fake_dl, "tiktok")

    assert results.attrs.get("transient_storm_tripped") is False
    assert set(trans) == set(ids)
    print("PASS: sub-threshold homogeneous run does not trip")




def test_success_resets_the_run():
    """Interleaved successes prove the session works — the count restarts."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 3)]
    calls = {"n": 0}

    def fake_dl(video_id=None, **kwargs):
        calls["n"] += 1
        # A success every STORM_THRESHOLD-th item keeps every homogeneous
        # run strictly below the threshold.
        if calls["n"] % STORM_THRESHOLD == 0:
            return _metadata_row(video_id)
        return _failure("unknown")

    results, perm, trans = _run_batch(ids, fake_dl, "tiktok", max_workers=1)

    assert results.attrs.get("transient_storm_tripped") is False
    assert len(results) == len(ids) // STORM_THRESHOLD
    print("PASS: success resets the run")




def test_permanent_storm_takes_precedence_unchanged():
    """A homogeneous permanent storm still trips its own guard, not this one."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("removed")

    with patch.object(scrape, "_permanent_storm_threshold", return_value=STORM_THRESHOLD):
        results, perm, trans = _run_batch(ids, fake_dl, "instagram")

    assert results.attrs.get("permanent_storm_tripped") is True
    assert results.attrs.get("transient_storm_tripped") is False
    print("PASS: permanent storm takes precedence unchanged")




def test_transient_storm_raises_alert():
    """A transient storm raises a persistent, user-visible scraper alert."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]
    alerts = []

    def fake_dl(video_id=None, **kwargs):
        return _failure("unknown")

    with patch.object(scrape, "check_existing_media", return_value={}), \
         patch.object(scrape.data_io, "save_json", side_effect=lambda **kw: None), \
         patch.object(scrape.scraper_alerts, "raise_alert",
                      side_effect=lambda **kw: alerts.append(kw)), \
         patch.object(scrape.scraper_alerts, "clear_alert", side_effect=Exception):
        results, perm, trans = _run_batch(
            ids, fake_dl, "tiktok", max_workers=1, dry_run=False)

    assert results.attrs.get("transient_storm_tripped") is True
    assert len(alerts) == 1
    assert alerts[0]["platform"] == "tiktok"
    assert alerts[0]["kind"] == scrape.scraper_alerts.KIND_TRANSIENT_STORM
    assert alerts[0]["category"] == "transient:unknown"
    print("PASS: transient storm raises an alert")




def test_batch_loop_stops_on_transient_storm():
    """A transient-storm verdict stops the batch loop; nothing is pruned."""
    ids = [f"v{i}" for i in range(4)]
    calls = {"n": 0}
    prunes = []

    def fake_threads(interesting_videos=None, **kwargs):
        calls["n"] += 1
        empty = pd.DataFrame()
        empty.attrs["circuit_breaker_tripped"] = False
        empty.attrs["permanent_storm_tripped"] = False
        empty.attrs["transient_storm_tripped"] = True
        empty.attrs["transient_storm_category"] = "transient:unknown"
        empty.attrs["memory_stop"] = False
        return empty, [], list(interesting_videos)

    with patch.object(scrape, "download_video_threads", side_effect=fake_threads), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: prunes.append(set(i)) or (len(i), 0)):
        good, perm, trans = scrape.scraper_loop_from_list(
            video_list=ids, batch_size=2, platform="tiktok")

    assert calls["n"] == 1, "the loop must stop after the storm batch"
    assert prunes == [], f"transient ids must stay in the queue: {prunes}"
    assert perm == [] and set(trans) == set(ids[:2])
    print("PASS: batch loop stops on a transient storm")




if __name__ == "__main__":
    for p in ("tiktok", "instagram", "youtube"):
        test_transient_storm_trips(p)
    test_heterogeneous_transients_do_not_trip()
    test_subthreshold_homogeneous_run_does_not_trip()
    test_success_resets_the_run()
    test_permanent_storm_takes_precedence_unchanged()
    test_transient_storm_raises_alert()
    test_batch_loop_stops_on_transient_storm()
    print("All transient-storm guard tests passed.")
