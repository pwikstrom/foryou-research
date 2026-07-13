"""Unit tests for the system-health service (no network, all probes mocked).

Covers test-item picking, the platform-check classification matrix, the
format-consistency (fill-profile) comparison, the media reachability probe,
the Gemini ping, overall aggregation, the boot staleness gate, and the
one-run-at-a-time concurrency guard.
"""

import json

import pandas as pd
import pytest

from fyp.scrape.platform_scraper import empty_fail
from web_interface.services import system_health as sh


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Reset in-memory state and redirect persistence to a per-test file."""
    monkeypatch.setattr(sh, "_current", None)
    monkeypatch.setattr(sh, "_HEALTH_PATH", str(tmp_path / "system_health.json"))
    monkeypatch.setattr(sh.data_io, "exists", lambda **kwargs: False)
    yield






class FakeScraper:
    """Configurable stand-in for a platform scraper."""

    def __init__(self, fetch_result=None, canonical_columns=None,
                 canonicalize_error=None, probe_target=None, probe_error=None):
        self._fetch_result = fetch_result
        self._canonical_columns = canonical_columns
        self._canonicalize_error = canonicalize_error
        self._probe_target = probe_target
        self._probe_error = probe_error


    def fetch(self, item_id, *, save_media, save_path, stream_to_bucket=None, verbose=False):
        assert save_media is False
        return self._fetch_result


    def prepare_raw_batch(self, df):
        return df


    def canonicalize_batch(self, df):
        if self._canonicalize_error is not None:
            raise self._canonicalize_error
        cols = self._canonical_columns if self._canonical_columns is not None else df.columns
        return pd.DataFrame([{c: "x" for c in cols}])


    def classify_error(self, error_type):
        if error_type is None:
            return "ok"
        permanent = {"not_found", "private", "removed"}
        bucket = "permanent" if error_type in permanent else "transient"
        return f"{bucket}:{error_type}"


    def media_probe_url(self, item_id):
        if self._probe_error is not None:
            raise self._probe_error
        return self._probe_target






def _status_frame():
    return pd.DataFrame(
        {"source_platform": ["tiktok", "instagram", "tiktok"],
         "scraped_ok": [True, True, True]},
        index=pd.Index(["111", "222", "333"], name="item_id"))






def _patch_scraper(monkeypatch, scraper):
    monkeypatch.setattr(sh, "get_scraper", lambda platform: scraper)






def test_overall_precedence():
    assert sh._overall({"a": {"status": "ok"}}) == "ok"
    assert sh._overall({"a": {"status": "ok"}, "b": {"status": "warn"}}) == "warn"
    assert sh._overall({"a": {"status": "warn"}, "b": {"status": "fail"}}) == "fail"
    assert sh._overall({}) == "fail"






def test_pick_test_item_takes_last_matching_row():
    df = _status_frame()
    assert sh._pick_test_item(df, "tiktok") == "333"
    assert sh._pick_test_item(df, "instagram") == "222"
    assert sh._pick_test_item(df, "youtube") is None
    assert sh._pick_test_item(None, "tiktok") is None
    assert sh._pick_test_item(pd.DataFrame(), "tiktok") is None






def test_check_platform_ok(monkeypatch):
    raw = pd.DataFrame([{"desc": "hello"}])
    _patch_scraper(monkeypatch, FakeScraper(fetch_result=raw, canonical_columns=["desc"]))
    result = sh._check_platform("tiktok", _status_frame(), expected_fields=["desc"])
    assert result["status"] == "ok"
    assert "all 1 expected fields filled" in result["message"]
    assert result["item_id"] == "333"
    assert result["duration_s"] is not None
    assert result["media"]["status"] == "skipped"






def test_check_platform_fill_drift_warns(monkeypatch):
    raw = pd.DataFrame([{"desc": "hello"}])
    _patch_scraper(monkeypatch, FakeScraper(fetch_result=raw, canonical_columns=["desc"]))
    result = sh._check_platform("tiktok", _status_frame(),
                                expected_fields=["desc", "play_count"])
    assert result["status"] == "warn"
    assert "1 of 2 expected fields filled OK" in result["detail"]
    assert "play_count" in result["detail"]






def test_check_platform_canonicalization_failure_fails(monkeypatch):
    raw = pd.DataFrame([{"desc": "hello"}])
    _patch_scraper(monkeypatch, FakeScraper(
        fetch_result=raw, canonicalize_error=ValueError("bad dtype")))
    result = sh._check_platform("tiktok", _status_frame(), expected_fields=[])
    assert result["status"] == "fail"
    assert "bad dtype" in result["detail"]






@pytest.mark.parametrize("error_type,expected_status", [
    ("bot_check", "warn"),
    ("rate_limited", "warn"),
    ("network", "warn"),       # other transient
    ("not_found", "fail"),     # permanent
])
def test_check_platform_fetch_failure_classification(monkeypatch, error_type, expected_status):
    _patch_scraper(monkeypatch, FakeScraper(fetch_result=empty_fail(error_type, "boom")))
    result = sh._check_platform("tiktok", _status_frame(), expected_fields=[])
    assert result["status"] == expected_status
    assert "boom" in result["detail"]






def test_check_platform_no_test_item_warns(monkeypatch):
    _patch_scraper(monkeypatch, FakeScraper())
    result = sh._check_platform("youtube", _status_frame(), expected_fields=[])
    assert result["status"] == "warn"
    assert result["item_id"] is None






class _FakeResponse:
    def __init__(self, status_code, chunk):
        self.status_code = status_code
        self._chunk = chunk

    def iter_content(self, chunk_size):
        yield self._chunk

    def close(self):
        pass






def test_media_probe_ok(monkeypatch):
    scraper = FakeScraper(probe_target={"url": "https://cdn/x.mp4", "headers": {}})
    monkeypatch.setattr(sh.requests, "get",
                        lambda url, **kwargs: _FakeResponse(206, b"x" * 4096))
    result = sh._probe_media(scraper, "111")
    assert result["status"] == "ok"
    assert result["bytes_read"] == 4096






def test_media_probe_failures_only_warn(monkeypatch):
    scraper = FakeScraper(probe_target={"url": "https://cdn/x.mp4", "headers": {}})
    monkeypatch.setattr(sh.requests, "get",
                        lambda url, **kwargs: _FakeResponse(403, b""))
    assert sh._probe_media(scraper, "111")["status"] == "warn"

    def _boom(url, **kwargs):
        raise OSError("connection reset")
    monkeypatch.setattr(sh.requests, "get", _boom)
    assert sh._probe_media(scraper, "111")["status"] == "warn"

    assert sh._probe_media(FakeScraper(probe_target=None), "111")["status"] == "skipped"
    assert sh._probe_media(FakeScraper(probe_error=ValueError("no formats")), "111")["status"] == "warn"






def test_media_probe_warn_bubbles_into_platform_status(monkeypatch):
    raw = pd.DataFrame([{"desc": "hello"}])
    scraper = FakeScraper(fetch_result=raw, canonical_columns=["desc"],
                          probe_target={"url": "https://cdn/x.mp4", "headers": {}})
    _patch_scraper(monkeypatch, scraper)
    monkeypatch.setattr(sh.requests, "get",
                        lambda url, **kwargs: _FakeResponse(403, b""))
    result = sh._check_platform("tiktok", _status_frame(), expected_fields=["desc"])
    assert result["status"] == "warn"
    assert result["media"]["status"] == "warn"






def test_load_fill_profiles_thresholds(monkeypatch):
    base_cols = ["desc", "play_count", "storage_link"]
    monkeypatch.setattr(sh.sc, "load_contract", lambda: {})
    monkeypatch.setattr(sh.sc, "base_field_names", lambda contract: list(base_cols))
    monkeypatch.setattr(sh, "get_config", lambda: {"labels": {"SCRAPES_LABEL": "scrapes"}})
    frame = pd.DataFrame({
        "source_platform": ["tiktok"] * 10 + ["instagram"] * 10,
        "desc": ["d"] * 20,
        "play_count": [1] * 10 + [pd.NA] * 10,
    })
    monkeypatch.setattr(sh.data_io, "exists", lambda **kwargs: True)
    monkeypatch.setattr(sh.data_io, "load_parquet_selective", lambda **kwargs: frame)

    profiles = sh._load_fill_profiles()
    assert profiles["tiktok"] == ["desc", "play_count"]
    # Instagram's play_count is historically all-NA — never expected.
    assert profiles["instagram"] == ["desc"]
    # storage_link is orchestrator-stamped and excluded before profiling.
    assert "storage_link" not in profiles["tiktok"]






def test_load_fill_profiles_missing_file(monkeypatch):
    monkeypatch.setattr(sh, "get_config", lambda: {"labels": {"SCRAPES_LABEL": "scrapes"}})
    assert sh._load_fill_profiles() == {}






def test_check_gemini_no_client(monkeypatch):
    monkeypatch.setattr(sh.machine_annotation, "initialize_machine", lambda: None)
    monkeypatch.setattr(sh, "get_config", lambda: {"machine": {"client": None}})
    assert sh._check_gemini()["status"] == "fail"






def test_check_gemini_ok_and_fail(monkeypatch):
    class FakeModels:
        def __init__(self, error=None):
            self.error = error

        def generate_content(self, **kwargs):
            if self.error:
                raise self.error
            return object()

    class FakeClient:
        def __init__(self, error=None):
            self.models = FakeModels(error)

    monkeypatch.setattr(sh.machine_annotation, "initialize_machine", lambda: None)
    monkeypatch.setattr(sh, "get_config",
                        lambda: {"machine": {"client": FakeClient(), "model": "gemini-test"}})
    result = sh._check_gemini()
    assert result["status"] == "ok"
    assert "gemini-test" in result["message"]

    monkeypatch.setattr(sh, "get_config",
                        lambda: {"machine": {"client": FakeClient(RuntimeError("quota")),
                                             "model": "gemini-test"}})
    result = sh._check_gemini()
    assert result["status"] == "fail"
    assert "quota" in result["detail"]






def test_boot_gate_skips_fresh_result(monkeypatch):
    calls = []
    monkeypatch.setattr(sh, "start_health_check", lambda trigger: calls.append(trigger))
    monkeypatch.setattr(sh, "get_config",
                        lambda: {"web": {"health_check_max_age_hours": 6}})

    monkeypatch.setattr(sh, "get_health",
                        lambda: {"finished_at": sh._now_iso(), "checks": {}})
    sh.maybe_start_boot_check()
    assert calls == []

    monkeypatch.setattr(sh, "get_health",
                        lambda: {"finished_at": "2020-01-01T00:00:00+00:00", "checks": {}})
    sh.maybe_start_boot_check()
    assert calls == ["boot"]

    # max_age 0 forces a run even with a fresh result.
    calls.clear()
    monkeypatch.setattr(sh, "get_config",
                        lambda: {"web": {"health_check_max_age_hours": 0}})
    monkeypatch.setattr(sh, "get_health",
                        lambda: {"finished_at": sh._now_iso(), "checks": {}})
    sh.maybe_start_boot_check()
    assert calls == ["boot"]






def test_start_health_check_concurrency_guard(monkeypatch):
    assert sh._run_lock.acquire(blocking=False)
    try:
        assert sh.is_running() is True
        assert sh.start_health_check("manual") is False
    finally:
        sh._run_lock.release()
    assert sh.is_running() is False






def test_get_health_downgrades_interrupted_run(monkeypatch):
    stale_doc = {"schema_version": 1, "overall": "running", "finished_at": None,
                 "checks": {"gemini": {"status": "ok"}}}
    with open(sh._HEALTH_PATH, "w", encoding="utf-8") as f:
        json.dump(stale_doc, f)
    doc = sh.get_health()
    assert doc["overall"] == "ok"
    assert doc["interrupted"] is True






def test_run_all_checks_survives_crashing_checks(monkeypatch):
    monkeypatch.setattr(sh, "_load_status_frame", lambda: None)
    monkeypatch.setattr(sh, "_load_fill_profiles", lambda: {})
    monkeypatch.setattr(sh.sc, "load_contract", lambda: {})
    monkeypatch.setattr(sh.sc, "platforms", lambda contract: ["tiktok"])
    monkeypatch.setattr(sh, "_cached_cookie_health", lambda platform: {"status": "healthy"})

    def _crash(*args, **kwargs):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(sh, "_check_platform", _crash)
    monkeypatch.setattr(sh, "_check_gemini",
                        lambda: {"status": "ok", "message": "", "detail": None,
                                 "duration_s": 0.1, "checked_at": sh._now_iso()})

    sh._run_all_checks("manual")
    doc = sh.get_health()
    assert doc["overall"] == "fail"
    assert doc["checks"]["scrape_tiktok"]["status"] == "fail"
    assert "kaboom" in doc["checks"]["scrape_tiktok"]["detail"]
    assert doc["checks"]["gemini"]["status"] == "ok"
    assert doc["finished_at"] is not None






def test_worst_chip_severity():
    assert sh._worst_chip("ok", "warn") == "warn"
    assert sh._worst_chip("ok", "fail", "warn") == "fail"
    assert sh._worst_chip("unknown", "ok") == "ok"
    assert sh._worst_chip("unknown", "unknown") == "unknown"
    assert sh._worst_chip() == "unknown"






def _card_doc():
    return {"overall": "warn", "checks": {
        "scrape_tiktok": {"status": "ok", "message": "Fetched X",
                          "checked_at": "2026-07-13T00:00:00+00:00",
                          "cookie": {"status": "healthy"}, "media": {"status": "ok"}},
        "scrape_youtube": {"status": "warn", "message": "drift",
                           "checked_at": "2026-07-13T00:00:00+00:00",
                           "media": {"status": "warn", "message": "bot wall"}},
        "scrape_instagram": {"status": "ok", "message": "Fetched Y",
                             "checked_at": "2026-07-13T00:00:00+00:00"},
        "gemini": {"status": "fail", "message": "quota",
                   "checked_at": "2026-07-13T00:00:00+00:00"},
    }}






def test_derive_card_health_combines_scrape_cookie_annotation(monkeypatch):
    monkeypatch.setattr(sh, "get_health", lambda: _card_doc())
    monkeypatch.setattr(sh.sc, "load_contract", lambda: {})
    monkeypatch.setattr(sh.sc, "platforms",
                        lambda contract: ["tiktok", "instagram", "youtube"])
    live = {"tiktok": {"status": "healthy"},
            "youtube": {"status": "healthy"},
            "instagram": {"status": "expired", "message": "Session expired"}}

    result = sh.derive_card_health(live_cookie=live)
    assert result["ran"] is True
    # ok scrape + healthy cookie -> ok
    assert result["platforms"]["tiktok"]["status"] == "ok"
    # ok scrape but expired cookie -> fail (worst wins)
    assert result["platforms"]["instagram"]["status"] == "fail"
    assert "Session expired" in result["platforms"]["instagram"]["summary"]
    # warn scrape + healthy cookie -> warn; media detail surfaced
    assert result["platforms"]["youtube"]["status"] == "warn"
    assert "bot wall" in result["platforms"]["youtube"]["summary"]
    # annotation follows the gemini check
    assert result["annotation"]["status"] == "fail"
    assert result["annotation"]["summary"] == "quota"






def test_derive_card_health_never_run_falls_back_to_cookie(monkeypatch):
    monkeypatch.setattr(sh, "get_health",
                        lambda: {"overall": "never_run", "checks": {}})
    monkeypatch.setattr(sh.sc, "load_contract", lambda: {})
    monkeypatch.setattr(sh.sc, "platforms", lambda contract: ["tiktok"])

    result = sh.derive_card_health(live_cookie={"tiktok": {"status": "healthy"}})
    assert result["ran"] is False
    # No scrape check yet -> chip driven by the live cookie only.
    assert result["platforms"]["tiktok"]["status"] == "ok"
    assert "not yet checked" in result["platforms"]["tiktok"]["summary"]
    assert result["annotation"]["status"] == "unknown"
