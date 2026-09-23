#!/usr/bin/env python3
"""The enrichment supervisor's scraper hold, from the worker's abort to the tick.

Until 2026-09-23 ``_scraper_blocked`` looked for ``permanent_storm_tripped`` /
``circuit_breaker_tripped`` in the last run's task status, while the workers
emitted ``permanent_storm_abort`` / ``rate_limit_abort`` — and a local drain
(where Instagram and YouTube run) never writes that status file at all. The
hold never matched; the supervisor restarted the scraper into the same wall
until the no-drain guard parked the plans. The hold now reads the platform's
scraper alert. These tests run the real batch loop into each wall, with only
the fetch layer and storage faked, and assert that the supervisor sees the
hold, that the operator's dismissal and a healthy batch release it, and that
both worker paths report an abort under the batch's own attribute name.

Usage:
    pytest tests/unit/test_supervisor_scraper_hold.py
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from fyp.scrape import scrape, scraper_alerts
from fyp.scrape.platform_scraper import SESSION_EXPIRED
from web_interface import run_enrichment_supervisor as sup

# Every guard pinned low so a batch trips it instantly — the transient-storm
# guard above the breaker, as in production (25 vs 15): a run of throttle
# verdicts is both, and the breaker must see it first.
THRESHOLD = 5
TRANSIENT_THRESHOLD = THRESHOLD + 2

ABORT_FLAGS = ("session_expired", "circuit_breaker_tripped",
               "permanent_storm_tripped", "transient_storm_tripped")


class _AlertStore:
    """In-memory stand-in for the data_io calls scraper_alerts makes."""

    def __init__(self):
        self.files = {}

    def exists(self, storage_location="", filename="", **kwargs):
        return filename in self.files

    def load_json(self, storage_location="", filename="", **kwargs):
        return self.files.get(filename)

    def update_json(self, storage_location="", filename="", mutate=None,
                    default=None, **kwargs):
        new = mutate(self.files.get(filename, default))
        if new is not None:
            self.files[filename] = new
        return new


@pytest.fixture
def alerts(monkeypatch):
    store = _AlertStore()
    monkeypatch.setattr(scraper_alerts, "data_io", store)
    return store


def _failure(category: str) -> pd.DataFrame:
    empty = pd.DataFrame()
    empty.attrs["error_type"] = category
    return empty


def _metadata_row(item_id: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "fave_count_raw": 0,
        "comment_count_raw": 0, "share_count_raw": 0, "video_downloaded": True,
    }])


def _batch(platform: str, fake_dl, n: int = THRESHOLD * 2):
    """One real, non-dry-run batch: guards, attrs and alert all live."""
    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "CIRCUIT_BREAKER_THRESHOLD", THRESHOLD), \
         patch.object(scrape, "_permanent_storm_threshold", return_value=THRESHOLD), \
         patch.object(scrape, "_transient_storm_threshold", return_value=TRANSIENT_THRESHOLD), \
         patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                      lambda: None), \
         patch.object(scrape, "check_existing_media", return_value={}), \
         patch.object(scrape.data_io, "save_json", lambda **kw: None):
        results, _, _ = scrape.download_video_threads(
            interesting_videos=[f"v{i}" for i in range(n)], max_workers=1,
            dry_run=False, platform=platform)
    return results


# platform, the failure every item hits, the attrs flag it trips, the alert kind
WALLS = [
    ("tiktok", "removed", "permanent_storm_tripped", scraper_alerts.KIND_PERMANENT_STORM),
    ("tiktok", "unknown", "transient_storm_tripped", scraper_alerts.KIND_TRANSIENT_STORM),
    ("tiktok", "rate_limited", "circuit_breaker_tripped", scraper_alerts.KIND_CIRCUIT_BREAKER),
    ("tiktok", "bot_check", "circuit_breaker_tripped", scraper_alerts.KIND_CIRCUIT_BREAKER),
    ("instagram", SESSION_EXPIRED, "session_expired", scraper_alerts.KIND_SESSION_EXPIRED),
]


@pytest.mark.parametrize("platform,category,flag,kind", WALLS,
                         ids=[f"{w[0]}-{w[1]}" for w in WALLS])
def test_every_abort_holds_the_supervisor_until_dismissed(alerts, platform, category,
                                                          flag, kind):
    assert sup._scraper_blocked(platform) is None

    results = _batch(platform, lambda video_id=None, **kw: _failure(category))

    assert results.attrs[flag] is True
    assert sup._scraper_blocked(platform) == kind
    assert sup._scraper_blocked("youtube") is None, "the hold is per platform"

    # The blocked plan's journal line: "clear the alert on the Scrape page,
    # then arm again". Dismissing must be enough.
    scraper_alerts.clear_alert(platform, reason="dismissed by admin")
    assert sup._scraper_blocked(platform) is None


def test_a_healthy_batch_releases_the_hold(alerts):
    _batch("tiktok", lambda video_id=None, **kw: _failure("unknown"))
    assert sup._scraper_blocked("tiktok") == scraper_alerts.KIND_TRANSIENT_STORM

    _batch("tiktok", lambda video_id=None, **kw: _metadata_row(video_id), n=2)
    assert sup._scraper_blocked("tiktok") is None


def test_a_stale_status_flag_without_an_alert_does_not_hold(alerts):
    """YouTube's task-status file still carried a July Cloud Run run's rate-limit
    flag in September; reading it would have parked every YouTube plan with no
    alert to clear."""
    stale = {"state": "completed", "data": {flag: True for flag in ABORT_FLAGS}
             | {"rate_limit_abort": True, "permanent_storm_abort": True}}
    with patch("web_interface.task_status.read_task_status", return_value=stale):
        assert sup._scraper_blocked("youtube") is None


def test_the_drain_blocks_the_platform_plans_on_a_real_alert(alerts, monkeypatch):
    from fyp.scrape import scrape_queues
    from web_interface.services import collection_enrichment as ce
    from web_interface.services import enrichment_journal as journal

    _batch("tiktok", lambda video_id=None, **kw: _failure("rate_limited"))

    saved, recorded = {}, []
    monkeypatch.setattr(scrape_queues, "queue_lengths", lambda: {"tiktok": 12})
    monkeypatch.setattr(sup, "_scrape_lane_busy", lambda platform: False)
    monkeypatch.setattr(sup, "_unavailable_here", lambda platform: None)
    monkeypatch.setattr(sup, "_start", lambda *a, **k: pytest.fail("the scraper was restarted"))
    monkeypatch.setattr(ce, "save_plan", lambda cid, patch_: saved.update({cid: patch_}))
    monkeypatch.setattr(journal, "record", lambda kind, text, **kw: recorded.append(text))

    plans = {"c1": {"platform": "tiktok"}, "c2": {"platform": "instagram"}}
    assert sup._drain(_Reporter(), plans) is None

    assert saved == {"c1": {"state": ce.STATE_BLOCKED,
                            "last_error": "scraper circuit_breaker"}}
    assert len(recorded) == 1 and "circuit breaker" in recorded[0]
    assert "clear the alert" in recorded[0]


# --------------------------------------------------------------------------- #
# Both worker paths report an abort under the batch attribute's own name
# --------------------------------------------------------------------------- #

class _Reporter:
    def __init__(self):
        self.lines, self.data = [], []

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, *args, **kwargs):
        pass

    def emit_data(self, payload):
        self.data.append(payload)

    def check_cancelled(self):
        return False


def _aborted_threads(flag):
    def threads(interesting_videos=None, **kwargs):
        frame = pd.DataFrame()
        for k in ABORT_FLAGS + ("memory_stop",):
            frame.attrs[k] = k == flag
        return frame, [], list(interesting_videos)
    return threads


def _emitted_flags(reporter) -> set[str]:
    return {k for payload in reporter.data for k, v in payload.items()
            if v is True and k != "chain"}


@pytest.mark.parametrize("flag", ABORT_FLAGS)
def test_the_local_loop_emits_the_attrs_key(flag):
    reporter = _Reporter()
    with patch.object(scrape, "download_video_threads", side_effect=_aborted_threads(flag)), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: (len(i), 0)), \
         patch.object(scrape.scrape_queues, "charge_media_retry",
                      side_effect=lambda p, retry, resolved: []):
        scrape.scraper_loop_from_list(video_list=["v0", "v1"], batch_size=2,
                                      platform="tiktok", reporter=reporter)
    assert _emitted_flags(reporter) == {flag}


@pytest.mark.parametrize("flag", ABORT_FLAGS)
def test_the_cloud_batch_emits_the_attrs_key(flag, tmp_path):
    import fyp.scrape as fyp_scrape
    from fyp.scrape import scrape_queues
    from tests.unit.test_scrape_retry_budget import _fake_data_io, _HealthyScraper
    from web_interface.run_queue_scraper import run_queue_scraper

    io = _fake_data_io(str(tmp_path))
    io.save_json(data=["v0", "v1"], filename=scrape_queues.queue_filename("tiktok"))
    reporter = _Reporter()
    with patch.object(scrape_queues, "_data_io", return_value=io), \
         patch.object(scrape_queues, "migrate_legacy_queue", lambda platform: None), \
         patch.object(fyp_scrape, "download_video_threads", _aborted_threads(flag)), \
         patch.object(fyp_scrape, "record_failed_scrapes", lambda items, **kw: None), \
         patch("fyp.platform_scraper.get_scraper", lambda platform: _HealthyScraper()), \
         patch("web_interface.run_queue_scraper._journal_scrape_finished",
               lambda **kw: None):
        assert run_queue_scraper(reporter, {"platform": "tiktok"}) is None
    assert _emitted_flags(reporter) == {flag}
