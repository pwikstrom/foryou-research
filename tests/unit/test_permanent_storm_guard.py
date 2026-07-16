#!/usr/bin/env python3
"""Tests for the permanent-failure storm guard in the scrape batch loop.

Covers the 2026-07-16 incident where a flagged Instagram session made yt-dlp
return HTTP 404 for every item ("removed" → permanent), and the whole batch
was pruned from to_scrape_instagram.json as permanently failed even though the
posts were live. The guard treats a run of consecutive *identical* permanent
classifications as a broken session: the batch aborts, the affected ids are
demoted to transient (kept queued, not recorded as failed), and the batch loop
stops chaining. The guard is generic — it keys off classify_error()'s
"permanent:<category>" strings, so it covers all platform scrapers.

Usage:
    python tests/unit/test_permanent_storm_guard.py
    pytest tests/unit/test_permanent_storm_guard.py
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

    The storm threshold is pinned to STORM_THRESHOLD, version registration is
    a no-op, and YouTube's 1.5s inter-request pacing is disabled so the
    threaded batch runs instantly.
    """
    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "_permanent_storm_threshold", return_value=STORM_THRESHOLD), \
         patch.object(scrape.scrape_versioning, "ensure_current_version_registered",
                      lambda: None), \
         patch.object(YouTubeScraper, "inter_request_delay", return_value=0.0):
        return scrape.download_video_threads(
            interesting_videos=ids, max_workers=max_workers,
            dry_run=dry_run, platform=platform)




@pytest.mark.parametrize("platform", ["tiktok", "instagram", "youtube"])
def test_storm_trips_and_demotes(platform):
    """A homogeneous permanent storm aborts and keeps every id queued."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("removed")

    results, perm, trans = _run_batch(ids, fake_dl, platform)

    assert results.attrs.get("permanent_storm_tripped") is True
    assert results.attrs.get("permanent_storm_category") == "permanent:removed"
    assert results.attrs.get("circuit_breaker_tripped") is False
    assert perm == [], f"storm ids must not be marked permanent: {perm}"
    assert set(trans) == set(ids), "all storm/aborted ids must stay queued"
    print(f"PASS: storm trips and demotes ({platform})")




def test_heterogeneous_permanents_do_not_trip():
    """Mixed permanent categories are genuine verdicts — pruned as usual."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 2)]
    calls = {"n": 0}

    def fake_dl(video_id=None, **kwargs):
        calls["n"] += 1
        return _failure("removed" if calls["n"] % 2 else "private")

    # max_workers=1: sequential fetches keep the alternation consecutive.
    results, perm, trans = _run_batch(ids, fake_dl, "instagram", max_workers=1)

    assert results.attrs.get("permanent_storm_tripped") is False
    assert set(perm) == set(ids), "heterogeneous permanents must prune normally"
    assert trans == []
    print("PASS: heterogeneous permanents do not trip")




def test_subthreshold_homogeneous_run_does_not_trip():
    """A short run of one permanent category is below suspicion."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD - 1)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("removed")

    results, perm, trans = _run_batch(ids, fake_dl, "instagram")

    assert results.attrs.get("permanent_storm_tripped") is False
    assert set(perm) == set(ids)
    assert trans == []
    print("PASS: sub-threshold homogeneous run does not trip")




def test_demotion_is_category_precise():
    """Permanent verdicts from other categories keep their normal handling."""
    n_private = 3
    ids = [f"v{i}" for i in range(n_private + STORM_THRESHOLD + 4)]
    calls = {"n": 0}

    def fake_dl(video_id=None, **kwargs):
        calls["n"] += 1
        return _failure("private" if calls["n"] <= n_private else "removed")

    results, perm, trans = _run_batch(ids, fake_dl, "instagram", max_workers=1)

    assert results.attrs.get("permanent_storm_tripped") is True
    assert results.attrs.get("permanent_storm_category") == "permanent:removed"
    assert len(perm) == n_private, f"non-storm permanents must survive: {perm}"
    assert len(trans) == len(ids) - n_private
    assert set(perm) | set(trans) == set(ids)
    print("PASS: demotion is category-precise")




def test_rate_limit_breaker_unaffected():
    """The throttle breaker still trips on its own, without a storm verdict."""
    ids = [f"v{i}" for i in range(scrape.CIRCUIT_BREAKER_THRESHOLD * 2)]

    def fake_dl(video_id=None, **kwargs):
        return _failure("rate_limited")

    results, perm, trans = _run_batch(ids, fake_dl, "instagram")

    assert results.attrs.get("circuit_breaker_tripped") is True
    assert results.attrs.get("permanent_storm_tripped") is False
    assert perm == []
    assert set(trans) == set(ids)
    print("PASS: rate-limit breaker unaffected")




def test_storm_ids_excluded_from_failed_record():
    """Demoted ids never reach the failed-scrapes JSON, and an alert is raised."""
    n_ok = 3
    ids = [f"v{i}" for i in range(n_ok + STORM_THRESHOLD + 4)]
    calls = {"n": 0}
    saved = {}
    alerts = []

    def fake_dl(video_id=None, **kwargs):
        calls["n"] += 1
        if calls["n"] <= n_ok:
            return _metadata_row(video_id)
        return _failure("removed")

    def fake_save_json(data=None, **kwargs):
        saved["failed"] = list(data)

    with patch.object(scrape, "check_existing_media", return_value={}), \
         patch.object(scrape, "_canonicalize_recode_save",
                      side_effect=lambda results, *a, **k: results), \
         patch.object(scrape.data_io, "save_json", side_effect=fake_save_json), \
         patch.object(scrape.scraper_alerts, "raise_alert",
                      side_effect=lambda **kw: alerts.append(kw)), \
         patch.object(scrape.scraper_alerts, "clear_alert", side_effect=Exception):
        results, perm, trans = _run_batch(
            ids, fake_dl, "instagram", max_workers=1, dry_run=False)

    assert results.attrs.get("permanent_storm_tripped") is True
    assert len(results) == n_ok
    assert perm == []
    # Sequential run: n_ok successes, then STORM_THRESHOLD 'removed' failures
    # (demoted, excluded from the failed record), then batch-aborted items
    # (recorded, exactly like the rate-limit breaker records them).
    n_aborted = len(ids) - n_ok - STORM_THRESHOLD
    assert len(saved.get("failed", [])) == n_aborted, (
        f"demoted storm ids must be excluded from the failed record: {saved}")
    # The storm raises a persistent, user-visible scraper alert.
    assert len(alerts) == 1
    assert alerts[0]["platform"] == "instagram"
    assert alerts[0]["category"] == "permanent:removed"
    print("PASS: storm ids excluded from the failed record; alert raised")




def test_healthy_batch_clears_alert():
    """A batch with real results and no storm auto-clears the platform alert."""
    ids = ["v0", "v1"]
    cleared = []

    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id)

    with patch.object(scrape, "check_existing_media", return_value={}), \
         patch.object(scrape, "_canonicalize_recode_save",
                      side_effect=lambda results, *a, **k: results), \
         patch.object(scrape.data_io, "save_json", side_effect=Exception), \
         patch.object(scrape.scraper_alerts, "raise_alert", side_effect=Exception), \
         patch.object(scrape.scraper_alerts, "clear_alert",
                      side_effect=lambda platform, **kw: cleared.append(platform)):
        results, perm, trans = _run_batch(
            ids, fake_dl, "instagram", dry_run=False)

    assert results.attrs.get("permanent_storm_tripped") is False
    assert len(results) == len(ids)
    assert cleared == ["instagram"]
    print("PASS: healthy batch clears the alert")




def test_batch_loop_stops_and_does_not_prune():
    """A storm verdict stops the batch loop and nothing suspect is pruned."""
    ids = [f"v{i}" for i in range(4)]
    calls = {"n": 0}
    prunes = []

    def fake_threads(interesting_videos=None, **kwargs):
        calls["n"] += 1
        empty = pd.DataFrame()
        empty.attrs["circuit_breaker_tripped"] = False
        empty.attrs["permanent_storm_tripped"] = True
        empty.attrs["permanent_storm_category"] = "permanent:removed"
        empty.attrs["memory_stop"] = False
        return empty, [], list(interesting_videos)

    with patch.object(scrape, "download_video_threads", side_effect=fake_threads), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: prunes.append(set(i)) or (len(i), 0)):
        good, perm, trans = scrape.scraper_loop_from_list(
            video_list=ids, batch_size=2, platform="instagram")

    assert calls["n"] == 1, "the loop must stop after the storm batch"
    assert prunes == [], f"suspect ids must stay in the queue: {prunes}"
    assert perm == [] and set(trans) == set(ids[:2])

    # Control: without a storm, permanent failures prune as before.
    calls["n"] = 0

    def fake_threads_normal(interesting_videos=None, **kwargs):
        calls["n"] += 1
        empty = pd.DataFrame()
        empty.attrs["circuit_breaker_tripped"] = False
        empty.attrs["permanent_storm_tripped"] = False
        empty.attrs["memory_stop"] = False
        return empty, list(interesting_videos), []

    with patch.object(scrape, "download_video_threads", side_effect=fake_threads_normal), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: prunes.append(set(i)) or (len(i), 0)):
        scrape.scraper_loop_from_list(video_list=ids, batch_size=2, platform="instagram")

    assert calls["n"] == 2 and prunes == [set(ids)]
    print("PASS: batch loop stops on a storm and skips the prune")




if __name__ == "__main__":
    for p in ("tiktok", "instagram", "youtube"):
        test_storm_trips_and_demotes(p)
    test_heterogeneous_permanents_do_not_trip()
    test_subthreshold_homogeneous_run_does_not_trip()
    test_demotion_is_category_precise()
    test_rate_limit_breaker_unaffected()
    test_storm_ids_excluded_from_failed_record()
    test_healthy_batch_clears_alert()
    test_batch_loop_stops_and_does_not_prune()
    print("All permanent-storm guard tests passed.")
