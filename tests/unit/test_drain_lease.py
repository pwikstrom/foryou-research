"""Verify the local-drain lease (web_interface/drain_lease.py) and the
start_process guard built on it.

Covers: freshness/staleness of read_drain_lease, DrainLease acquire/release
lifecycle (file written with holder identity, removed on exit), and
process_manager._drain_lease_conflict blocking the leased platform's scraper
and consolidation while leaving other processes unaffected.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web_interface import drain_lease


def _fake_io(store: dict):
    """A data_io stand-in backed by an in-memory dict keyed on filename."""

    class FakeIO:
        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            return filename in store

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            return json.loads(store[filename])

        @staticmethod
        def save_json(data=None, storage_location="cache", filename="", verbose=False):
            store[filename] = json.dumps(data)

        @staticmethod
        def remove(storage_location="cache", filename="", verbose=False):
            del store[filename]

    return FakeIO


def _lease_payload(platform: str, age_seconds: float) -> str:
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return json.dumps({
        "platform": platform, "host": "laptop", "user": "patrik",
        "pid": 4242, "started_at": ts, "heartbeat_at": ts,
    })






def test_read_drain_lease_fresh_stale_missing():
    store = {}
    with patch.object(drain_lease, "_data_io", return_value=_fake_io(store)):
        assert drain_lease.read_drain_lease("youtube") is None

        store["local_drain_youtube.json"] = _lease_payload("youtube", age_seconds=10)
        lease = drain_lease.read_drain_lease("youtube")
        assert lease and lease["host"] == "laptop"

        store["local_drain_youtube.json"] = _lease_payload(
            "youtube", age_seconds=drain_lease.LEASE_STALE_S + 5
        )
        assert drain_lease.read_drain_lease("youtube") is None, "stale lease must be ignored"

        store["local_drain_youtube.json"] = "not json {"
        assert drain_lease.read_drain_lease("youtube") is None






def test_drain_lease_lifecycle_writes_and_removes():
    store = {}
    with patch.object(drain_lease, "_data_io", return_value=_fake_io(store)):
        with drain_lease.DrainLease("youtube"):
            lease = json.loads(store["local_drain_youtube.json"])
            assert lease["platform"] == "youtube"
            assert lease["host"] and lease["user"] and lease["pid"]
            assert drain_lease.read_drain_lease("youtube") is not None
        assert "local_drain_youtube.json" not in store, "lease must be released on exit"






def test_drain_lease_released_even_when_body_raises():
    store = {}
    with patch.object(drain_lease, "_data_io", return_value=_fake_io(store)):
        try:
            with drain_lease.DrainLease("youtube"):
                raise RuntimeError("drain crashed")
        except RuntimeError:
            pass
        assert "local_drain_youtube.json" not in store






def test_start_process_guard_blocks_leased_platform_and_consolidate():
    from web_interface import process_manager as pm

    fresh = json.loads(_lease_payload("youtube", age_seconds=10))
    with patch.object(drain_lease, "read_drain_lease",
                      side_effect=lambda p: fresh if p == "youtube" else None), \
         patch.object(drain_lease, "active_drain_leases",
                      return_value={"youtube": fresh}):
        msg = pm._drain_lease_conflict("queue_scraper_youtube")
        assert msg and "laptop" in msg and "youtube" in msg

        assert pm._drain_lease_conflict("queue_scraper_tiktok") is None, \
            "other platforms' scrapers stay startable"

        msg = pm._drain_lease_conflict("consolidate_enrichment")
        assert msg and "laptop" in msg

        assert pm._drain_lease_conflict("pca_refresh") is None, \
            "unrelated processes are unaffected"






def test_start_process_guard_open_when_no_lease():
    from web_interface import process_manager as pm

    with patch.object(drain_lease, "read_drain_lease", return_value=None), \
         patch.object(drain_lease, "active_drain_leases", return_value={}):
        assert pm._drain_lease_conflict("queue_scraper_youtube") is None
        assert pm._drain_lease_conflict("consolidate_enrichment") is None
