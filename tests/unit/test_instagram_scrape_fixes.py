"""Regression tests for the Instagram scrape/health fixes (2026-07-16 field log).

Covers:
  * "No video formats found!" (image posts) → permanent ``no_video``, not a
    retried ``unknown`` that churns the queue forever.
  * The live progress "OK" counter counts only non-empty result rows, so failed
    scrapes (empty fail-frames) no longer inflate it.
  * The local-dev cookie-health check actually probes Chrome for a live session
    cookie instead of unconditionally reporting "healthy".
"""

import pandas as pd






def test_no_video_formats_is_permanent_no_video():
    from fyp.scrape import instagram_dl

    exc = instagram_dl.ExtractorError("No video formats found!; please report this issue")
    category, _ = instagram_dl._classify_error(exc)
    assert category == "no_video"
    assert category in instagram_dl._PERMANENT
    assert category not in instagram_dl._RETRYABLE






def test_carousel_category_is_retryable():
    """A partial image-download failure stays queued for a whole-post retry."""
    from fyp.scrape import instagram_dl

    assert "carousel" in instagram_dl._RETRYABLE
    assert "carousel" not in instagram_dl._PERMANENT
    assert instagram_dl.InstagramScraper().classify_error("carousel") == "transient:carousel"






def test_there_is_no_video_still_classified_no_video():
    from fyp.scrape import instagram_dl

    exc = instagram_dl.ExtractorError("There is no video in this post")
    category, _ = instagram_dl._classify_error(exc)
    assert category == "no_video"






def test_empty_media_response_stays_rate_limited():
    """The auth/throttle catch-all must not be swept up by the no_video change."""
    from fyp.scrape import instagram_dl

    exc = instagram_dl.ExtractorError(
        "Instagram sent an empty media response. Check if this post is accessible..."
    )
    category, _ = instagram_dl._classify_error(exc)
    assert category == "rate_limited"






class _FakeFuture:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return self._result


def test_scrape_future_success_predicate():
    from fyp.scrape import scrape
    from fyp.scrape.platform_scraper import empty_fail

    # Non-empty row => success.
    ok_df = pd.DataFrame([{"item_id": "x", "scrape_status": "ok"}])
    assert scrape._scrape_future_succeeded(_FakeFuture(result=(0, ok_df))) is True

    # Empty fail-frame (what a failed fetch returns) => NOT success.
    assert scrape._scrape_future_succeeded(_FakeFuture(result=(1, empty_fail("rate_limited")))) is False

    # A future that raised => NOT success (and must not propagate).
    assert scrape._scrape_future_succeeded(_FakeFuture(exc=RuntimeError("boom"))) is False






def test_requests_cookiejar_local_dev_falls_back_to_chrome(monkeypatch):
    """With no cookie file, local dev sources plain-requests cookies from Chrome.

    The per-TTL Chrome export is tried first; direct per-call profile reading
    is the fallback when the export fails.
    """
    from fyp.scrape import scraper_cookies as sc

    sentinel = object()
    monkeypatch.setattr(sc, "ensure_cookie_file", lambda platform: None)
    monkeypatch.setattr(sc, "_env_cookie_file", lambda platform: "")
    monkeypatch.setattr(sc, "_export_chrome_cookies", lambda platform: None)
    monkeypatch.setattr(sc, "_chrome_requests_cookies", lambda platform: sentinel)

    monkeypatch.setattr(sc, "_is_local_dev", lambda: True)
    assert sc.requests_cookiejar("instagram") is sentinel

    # Headless environments (Cloud Run/Docker) keep the old behavior: None.
    monkeypatch.setattr(sc, "_is_local_dev", lambda: False)
    assert sc.requests_cookiejar("instagram") is None






def test_cookie_health_local_dev_probes_chrome(monkeypatch):
    from fyp.scrape import scraper_cookies as sc

    monkeypatch.setattr(sc, "_is_local_dev", lambda: True)

    # Not logged in (no session cookie) → not healthy.
    monkeypatch.setattr(sc, "_chrome_session_status",
                        lambda platform, session_cookie: ("absent", None, "no cookie"))
    h = sc.cookie_health("instagram", session_cookie="sessionid")
    assert h["status"] == "missing"
    assert h["present"] is False

    # Logged in, comfortable expiry → healthy.
    future = sc.time.time() + 300 * 86400
    monkeypatch.setattr(sc, "_chrome_session_status",
                        lambda platform, session_cookie: ("present", future, None))
    h = sc.cookie_health("instagram", session_cookie="sessionid")
    assert h["status"] == "healthy"
    assert h["present"] is True

    # Chrome unreadable (e.g. app-bound encryption) → unknown, not a false green.
    monkeypatch.setattr(sc, "_chrome_session_status",
                        lambda platform, session_cookie: ("unreadable", None, "could not read"))
    h = sc.cookie_health("instagram", session_cookie="sessionid")
    assert h["status"] == "unknown"
    assert h["present"] is False
