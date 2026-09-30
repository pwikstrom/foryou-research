"""The shared timestamp helpers keep the formats their eleven copies produced.

Stored records (task failures, alerts, run logs, activity logs, the enrichment
journal) are compared and parsed as strings, so each family must stay
byte-identical: UTC with microseconds, UTC to the second, and the configured
local zone to the second.
"""

from datetime import UTC, datetime

import pytest

import fyp.core.runtime as runtime

FROZEN = datetime(2026, 9, 30, 1, 2, 3, 456789, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN.astimezone(tz) if tz else FROZEN.replace(tzinfo=None)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(runtime, "datetime", _FrozenDatetime)


def test_utc_now_iso_formats(frozen):
    assert runtime.utc_now_iso() == "2026-09-30T01:02:03.456789+00:00"
    assert runtime.utc_now_iso(timespec="seconds") == "2026-09-30T01:02:03+00:00"


def test_local_now_iso_uses_the_configured_zone(frozen, monkeypatch):
    monkeypatch.setattr(runtime, "cf", lambda: {"misc": {"TIME_ZONE": "Australia/Brisbane"}})
    assert runtime.local_now_iso() == "2026-09-30T11:02:03+10:00"


@pytest.mark.parametrize(
    "config",
    [dict, lambda: {"misc": {"TIME_ZONE": "Not/AZone"}}, lambda: 1 / 0],
)
def test_local_now_iso_falls_back_to_utc(frozen, monkeypatch, config):
    monkeypatch.setattr(runtime, "cf", config)
    assert runtime.local_now_iso() == "2026-09-30T01:02:03+00:00"


def test_every_module_helper_is_the_shared_one():
    import fyp.annotation.human_eval as human_eval
    import fyp.ingest.structure_sentinel as structure_sentinel
    import fyp.scrape.scraper_alerts as scraper_alerts
    from web_interface.services import (
        activity_log,
        admin_notes,
        collection_enrichment,
        enrichment_journal,
        refresh_pipeline,
        system_health,
    )
    from web_interface.tasks import run_logs, task_failures

    for fn in (
        structure_sentinel._now_iso,
        scraper_alerts._now_iso,
        task_failures._now_iso,
        system_health._now_iso,
        collection_enrichment.now_iso,
        refresh_pipeline._now,
    ):
        assert fn is runtime.utc_now_iso
    for fn in (human_eval._now_iso, enrichment_journal._now_iso):
        assert fn.func is runtime.utc_now_iso and fn.keywords == {"timespec": "seconds"}
    for fn in (run_logs._now_iso, activity_log._now_iso, admin_notes._now_iso):
        assert fn is runtime.local_now_iso
