#!/usr/bin/env python3
"""Tests for the per-platform cookie plumbing (no GCS, no network).

Covers per-platform path derivation, env-var precedence, and cookie_health's
session-expiry parsing incl. the file-age degradation when the named session
cookie carries no expiry row (the YouTube __Secure-3PSID case).

Usage:
    python tests/unit/test_scraper_cookies.py
    pytest tests/unit/test_scraper_cookies.py
"""

import os
import sys
import tempfile
import time
from http.cookiejar import Cookie, MozillaCookieJar
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Import config first so it initializes in local mode, before any test
# temporarily sets K_SERVICE.
import fyp.fyp_config  # noqa: F401
from fyp import scraper_cookies


def _netscape_file(rows: list[tuple[str, int]]) -> str:
    """Write a minimal Netscape cookie file; rows are (name, expiry) pairs."""
    fd, path = tempfile.mkstemp(suffix="_cookies.txt")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for name, expiry in rows:
            f.write(f".example.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\tvalue123\n")
    return path




def test_path_derivation():
    # The local cookie cache lives in the OS temp dir (``/tmp`` on Cloud Run,
    # ``%TEMP%`` on Windows) — derived via tempfile.gettempdir() for portability.
    assert scraper_cookies._local_path("instagram") == os.path.join(
        tempfile.gettempdir(), "instagram_cookies.txt"
    )
    assert scraper_cookies._gcs_blob_name("youtube") == "secrets/youtube_cookies.txt"
    print("PASS: per-platform path derivation")




def test_env_var_precedence():
    generic = _netscape_file([("sessionid", int(time.time()) + 86400)])
    specific = _netscape_file([("sessionid", int(time.time()) + 86400)])
    os.environ["YTDLP_COOKIE_FILE"] = generic
    os.environ["YTDLP_COOKIE_FILE_INSTAGRAM"] = specific
    try:
        assert scraper_cookies._env_cookie_file("instagram") == specific
        assert scraper_cookies._env_cookie_file("youtube") == generic
    finally:
        del os.environ["YTDLP_COOKIE_FILE"]
        del os.environ["YTDLP_COOKIE_FILE_INSTAGRAM"]
        os.remove(generic)
        os.remove(specific)
    print("PASS: env-var precedence (platform-specific before legacy)")




def test_cookie_opts_local_dev():
    # On a dev macOS box (no K_SERVICE, /Applications exists) cookies come from
    # the Chrome profile — via the per-TTL export when it succeeds, degrading
    # to per-call ``cookiesfrombrowser`` when it doesn't.
    if os.environ.get("K_SERVICE") or not os.path.exists("/Applications"):
        print("SKIP: not a local-dev environment")
        return

    with patch.object(scraper_cookies, "_export_chrome_cookies", return_value=None):
        assert scraper_cookies.cookie_opts("instagram") == {"cookiesfrombrowser": ("chrome",)}

    exported = _netscape_file([("sessionid", int(time.time()) + 3600)])
    try:
        with patch.object(scraper_cookies, "_export_chrome_cookies", return_value=exported):
            opts = scraper_cookies.cookie_opts("instagram")
        assert list(opts) == ["cookiefile"], opts
        # A private copy, not the export itself (yt-dlp rewrites its cookiefile).
        assert opts["cookiefile"] != exported
        assert scraper_cookies._looks_like_netscape(opts["cookiefile"])
        os.remove(opts["cookiefile"])
    finally:
        os.remove(exported)
    print("PASS: cookie_opts local dev → Chrome export, cookiesfrombrowser fallback")




def _chrome_cookie(name: str, domain: str):
    """A minimal http.cookiejar.Cookie as Chrome extraction would yield."""
    return Cookie(
        version=0, name=name, value="v", port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True, secure=True, expires=int(time.time()) + 3600,
        discard=False, comment=None, comment_url=None, rest={},
    )




def test_chrome_export_filters_domains_and_caches():
    # The export keeps only the platform's domains and is extracted once per
    # TTL — repeat calls within TTL must not touch Chrome again.
    from yt_dlp.cookies import YoutubeDLCookieJar

    jar = YoutubeDLCookieJar()
    for name, domain in [("sessionid", ".tiktok.com"), ("tt_csrf", ".tiktokv.com"),
                         ("SID", ".google.com"), ("other", ".example.com")]:
        jar.set_cookie(_chrome_cookie(name, domain))

    export_path = os.path.join(tempfile.gettempdir(), "tiktok_chrome_cookies.txt")
    if os.path.exists(export_path):
        os.remove(export_path)
    try:
        with patch("yt_dlp.cookies.extract_cookies_from_browser",
                   return_value=jar) as extract:
            first = scraper_cookies._export_chrome_cookies("tiktok")
            second = scraper_cookies._export_chrome_cookies("tiktok")
        assert first == second == export_path
        assert extract.call_count == 1, "second call within TTL must reuse the export"

        loaded = MozillaCookieJar(export_path)
        loaded.load(ignore_discard=True, ignore_expires=True)
        names = {c.name for c in loaded}
        assert names == {"sessionid", "tt_csrf"}, names
    finally:
        if os.path.exists(export_path):
            os.remove(export_path)
    print("PASS: Chrome export filters domains and caches within TTL")




def test_chrome_export_failure_returns_none():
    # A denied Keychain prompt / missing profile must degrade, not raise.
    export_path = os.path.join(tempfile.gettempdir(), "tiktok_chrome_cookies.txt")
    if os.path.exists(export_path):
        os.remove(export_path)
    with patch("yt_dlp.cookies.extract_cookies_from_browser",
               side_effect=RuntimeError("keychain denied")):
        assert scraper_cookies._export_chrome_cookies("tiktok") is None
    assert not os.path.exists(export_path)
    print("PASS: Chrome export failure returns None")




def _health_with_env(platform: str, session_cookie: str, path: str | None) -> dict:
    """Run cookie_health as if on Cloud Run with an env-provided cookie file."""
    env_key = f"YTDLP_COOKIE_FILE_{platform.upper()}"
    os.environ["K_SERVICE"] = "test-service"
    if path:
        os.environ[env_key] = path
    try:
        return scraper_cookies.cookie_health(platform, session_cookie=session_cookie)
    finally:
        del os.environ["K_SERVICE"]
        os.environ.pop(env_key, None)




def test_health_expiring_soon():
    path = _netscape_file([("sessionid", int(time.time()) + 7 * 86400)])
    try:
        health = _health_with_env("instagram", "sessionid", path)
        assert health["present"] is True
        assert health["status"] == "expiring_soon", health
        assert 6 < health["session_days_left"] < 8
    finally:
        os.remove(path)
    print("PASS: cookie_health expiring_soon")




def test_health_expired():
    path = _netscape_file([("sessionid", int(time.time()) - 86400)])
    try:
        health = _health_with_env("instagram", "sessionid", path)
        assert health["status"] == "expired", health
    finally:
        os.remove(path)
    print("PASS: cookie_health expired")




def test_health_degrades_to_file_age_without_session_row():
    # YouTube case: no readable __Secure-3PSID expiry → age-based status.
    path = _netscape_file([("some_other_cookie", int(time.time()) + 86400)])
    try:
        health = _health_with_env("youtube", "__Secure-3PSID", path)
        assert health["present"] is True
        assert health["session_expires_at"] is None
        # File was just created → age-based healthy.
        assert health["status"] == "healthy", health
        assert "__Secure-3PSID" in health["message"]
    finally:
        os.remove(path)
    print("PASS: cookie_health degrades to file age without session row")




def test_health_missing_file():
    # No env cookie file, no GCS bucket locally, /tmp cache path absent for a
    # platform name that never exists.
    local = scraper_cookies._local_path("nosuchplatform")
    assert not os.path.exists(local)
    health = _health_with_env("nosuchplatform", "sessionid", None)
    assert health["status"] == "missing", health
    print("PASS: cookie_health missing file")




if __name__ == "__main__":
    test_path_derivation()
    test_env_var_precedence()
    test_cookie_opts_local_dev()
    test_chrome_export_filters_domains_and_caches()
    test_chrome_export_failure_returns_none()
    test_health_expiring_soon()
    test_health_expired()
    test_health_degrades_to_file_age_without_session_row()
    test_health_missing_file()
    print("All scraper-cookie tests passed.")
