"""A permanent verdict on the MEDIA leg must never prune an item.

Covers the 2026-09-18 incident: a rate-limited YouTube session answered a bare
"Video unavailable" for the stream fetch of videos whose metadata had just
scraped fine. The classifier read it as ``removed`` (permanent); the
orchestrator saved the metadata row, counted it as a success and pruned the id
— 187 live videos written as scrape-ok rows with no media, 181 of them gone
from the queue for good. The permanent-storm guard tripped on those very
verdicts but "demoted 0", because it only re-examined failures and these were
results.

Three behaviours are pinned here:
  * every media failure keeps the id queued (``media_retry_ids``), whatever the
    category, and a media-leg storm still trips the guard;
  * the cross-run media-retry budget bounds those retries;
  * the batch deadline stops new downloads but keeps the rows of the ones in
    flight, instead of writing them all off and blocking on them anyway.

Run: pytest tests/unit/test_media_leg_permanent_verdict.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from fyp.scrape import scrape, scrape_queues
from fyp.youtube_dl import YouTubeScraper

STORM_THRESHOLD = 5


def _metadata_row(item_id: str, media_error: str | None = None) -> pd.DataFrame:
    """A >10-column single-row frame like a real fetch result."""
    row = pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "yt_author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "yt_like_count": 0,
        "yt_comment_count": 0, "yt_channel_follower_count": 0,
        "yt_categories": "", "video_downloaded": media_error is None,
    }])
    if media_error is not None:
        row.attrs["media_error_type"] = media_error
        row.attrs["media_error_detail"] = "simulated"
    return row


def _run_batch(ids, fake_dl, max_workers=2, **extra_patches):
    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "_permanent_storm_threshold", return_value=STORM_THRESHOLD), \
         patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                      lambda: None), \
         patch.object(YouTubeScraper, "inter_request_delay", return_value=0.0):
        return scrape.download_video_threads(
            interesting_videos=ids, max_workers=max_workers,
            dry_run=True, platform="youtube")


# --------------------------------------------------------------------------- #
# Media-leg verdicts
# --------------------------------------------------------------------------- #

def test_permanent_media_verdict_keeps_item_queued():
    """'removed' on the media leg: row saved, id transient, never permanent."""
    ids = ["vid_a", "vid_b"]

    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id, media_error="removed")

    results, perm, trans = _run_batch(ids, fake_dl)

    assert perm == [], f"media-leg verdict must never be permanent: {perm}"
    assert set(trans) == set(ids), "media-failed ids must stay queued"
    assert set(results["item_id"]) == set(ids), "the metadata rows are still saved"
    assert set(results.attrs["media_retry_ids"]) == set(ids)
    assert results.attrs["permanent_storm_tripped"] is False


def test_media_leg_storm_trips_guard_and_keeps_everything_queued():
    """A run of identical media-leg permanent verdicts is a session storm."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 3)]

    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id, media_error="removed")

    results, perm, trans = _run_batch(ids, fake_dl)

    assert results.attrs["permanent_storm_tripped"] is True
    assert results.attrs["permanent_storm_category"] == "permanent:removed"
    assert perm == []
    assert set(trans) == set(ids), "storm + aborted ids must all stay queued"
    # Every saved row is also flagged for a media retry.
    assert set(results.attrs["media_retry_ids"]) == set(results["item_id"])


def test_media_success_is_not_a_retry():
    ids = ["ok_1", "ok_2"]

    def fake_dl(video_id=None, **kwargs):
        return _metadata_row(video_id)

    results, perm, trans = _run_batch(ids, fake_dl)
    assert trans == [] and perm == []
    assert results.attrs["media_retry_ids"] == []


# --------------------------------------------------------------------------- #
# Media-retry budget (sidecar helper)
# --------------------------------------------------------------------------- #

def _fake_data_io(tmp: str):
    class FakeIO:
        @staticmethod
        def _p(filename):
            return os.path.join(tmp, filename)

        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            return os.path.exists(FakeIO._p(filename))

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            with open(FakeIO._p(filename)) as f:
                return json.load(f)

        @staticmethod
        def update_json(storage_location="cache", filename="", mutate=None,
                        default=None, max_retries=6, verbose=False):
            path = FakeIO._p(filename)
            current = json.loads(json.dumps(default)) if default is not None else None
            if os.path.exists(path):
                with open(path) as f:
                    current = json.load(f)
            new_value = mutate(current)
            if new_value is None:
                return None
            with open(path, "w") as f:
                json.dump(new_value, f)
            return new_value

    return FakeIO


def test_charge_media_retry_bounds_and_clears():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _fake_data_io(tmp)
        with patch.object(scrape_queues, "_data_io", return_value=fake):
            n = scrape_queues.MAX_MEDIA_RETRY_STRIKES
            for _strike in range(1, n):
                assert scrape_queues.charge_media_retry("youtube", ["a", "b"]) == []
            # 'a' resolves (media landed); 'b' strikes out.
            exhausted = scrape_queues.charge_media_retry("youtube", ["b"], resolved_ids=["a"])
            assert exhausted == ["b"]
            side = fake.load_json(filename=scrape_queues.media_strikes_filename("youtube"))
            assert side == {}, f"resolved and exhausted ids leave the sidecar: {side}"
            # A fresh strike after exhaustion starts from one again.
            assert scrape_queues.charge_media_retry("youtube", ["b"]) == []


# --------------------------------------------------------------------------- #
# Batch deadline
# --------------------------------------------------------------------------- #

def test_deadline_keeps_in_flight_rows_and_defers_the_rest():
    """Past the deadline: in-flight downloads land and are kept; un-started
    items come back transient (batch_aborted), not written off as timeouts."""
    ids = [f"d{i}" for i in range(8)]

    def slow_dl(video_id=None, **kwargs):
        time.sleep(0.6)
        return _metadata_row(video_id)

    with patch.object(scrape, "_batch_deadline_cap", return_value=1):
        results, perm, trans = _run_batch(ids, slow_dl, max_workers=2)

    assert results.attrs["batch_deadline_hit"] is True
    saved = set(results["item_id"])
    assert len(saved) >= 2, f"rows that finished before/at the deadline are kept: {saved}"
    assert saved | set(trans) == set(ids)
    assert saved.isdisjoint(trans)
    assert perm == []
    # No storm/breaker flag rides along with a plain deadline.
    assert results.attrs["circuit_breaker_tripped"] is False
    assert results.attrs["permanent_storm_tripped"] is False


if __name__ == "__main__":
    test_permanent_media_verdict_keeps_item_queued()
    test_media_leg_storm_trips_guard_and_keeps_everything_queued()
    test_media_success_is_not_a_retry()
    test_charge_media_retry_bounds_and_clears()
    test_deadline_keeps_in_flight_rows_and_defers_the_rest()
    print("All tests passed.")
