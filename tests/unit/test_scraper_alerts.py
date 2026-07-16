#!/usr/bin/env python3
"""Tests for the persistent scraper-alert store (fyp/scrape/scraper_alerts.py).

Covers the raise/refresh/clear lifecycle against a fake in-memory data_io
(the real store lives in cache/scraper_alerts.json), and the non-raising
guarantees — alerting must never break scraping.

Usage:
    python tests/unit/test_scraper_alerts.py
    pytest tests/unit/test_scraper_alerts.py
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fyp.scrape import scraper_alerts




class FakeDataIO:
    """In-memory stand-in for data_io's load_json/update_json pair."""

    def __init__(self):
        self.files = {}
        self.writes = 0

    def load_json(self, storage_location="", filename="", **kwargs):
        return self.files.get(filename)

    def update_json(self, storage_location="", filename="", mutate=None,
                    default=None, **kwargs):
        current = self.files.get(filename, default)
        new = mutate(current)
        if new is not None:
            self.files[filename] = new
            self.writes += 1




def test_raise_load_clear_roundtrip():
    fake = FakeDataIO()
    with patch.object(scraper_alerts, "data_io", fake):
        scraper_alerts.raise_alert(
            "instagram", scraper_alerts.KIND_PERMANENT_STORM,
            category="permanent:removed", count=15, message="storm")

        alerts = scraper_alerts.load_alerts()
        assert set(alerts) == {"instagram"}
        entry = alerts["instagram"]
        assert entry["kind"] == "permanent_storm"
        assert entry["category"] == "permanent:removed"
        assert entry["count"] == 15
        assert entry["occurrences"] == 1
        assert entry["raised_at"] and entry["raised_at"] == entry["last_seen"]

        scraper_alerts.clear_alert("instagram", reason="test")
        assert scraper_alerts.load_alerts() == {}
    print("PASS: raise/load/clear roundtrip")




def test_reraise_same_kind_keeps_raised_at_and_counts():
    fake = FakeDataIO()
    with patch.object(scraper_alerts, "data_io", fake):
        scraper_alerts.raise_alert("youtube", scraper_alerts.KIND_PERMANENT_STORM,
                                   category="permanent:removed")
        first = scraper_alerts.load_alerts()["youtube"]["raised_at"]
        scraper_alerts.raise_alert("youtube", scraper_alerts.KIND_PERMANENT_STORM,
                                   category="permanent:removed")

        entry = scraper_alerts.load_alerts()["youtube"]
        assert entry["occurrences"] == 2
        assert entry["raised_at"] == first, "re-raise must keep the original raised_at"
    print("PASS: re-raise keeps raised_at and bumps occurrences")




def test_alerts_are_per_platform():
    fake = FakeDataIO()
    with patch.object(scraper_alerts, "data_io", fake):
        scraper_alerts.raise_alert("instagram", scraper_alerts.KIND_PERMANENT_STORM)
        scraper_alerts.raise_alert("tiktok", scraper_alerts.KIND_PERMANENT_STORM)
        scraper_alerts.clear_alert("instagram")
        assert set(scraper_alerts.load_alerts()) == {"tiktok"}
    print("PASS: alerts are per-platform")




def test_clear_without_active_alert_writes_nothing():
    fake = FakeDataIO()
    with patch.object(scraper_alerts, "data_io", fake):
        scraper_alerts.clear_alert("instagram")
        assert fake.writes == 0, "clearing a non-existent alert must not write"
    print("PASS: no-op clear writes nothing")




def test_never_raises_on_storage_failure():
    class BrokenDataIO:
        def load_json(self, **kwargs):
            raise RuntimeError("storage down")

        def update_json(self, **kwargs):
            raise RuntimeError("storage down")

    with patch.object(scraper_alerts, "data_io", BrokenDataIO()):
        assert scraper_alerts.load_alerts() == {}
        scraper_alerts.raise_alert("instagram", scraper_alerts.KIND_PERMANENT_STORM)
        scraper_alerts.clear_alert("instagram")
    print("PASS: storage failures never raise")




if __name__ == "__main__":
    test_raise_load_clear_roundtrip()
    test_reraise_same_kind_keeps_raised_at_and_counts()
    test_alerts_are_per_platform()
    test_clear_without_active_alert_writes_nothing()
    test_never_raises_on_storage_failure()
    print("All scraper-alert tests passed.")
