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
    assert scraper_cookies._local_path("instagram") == "/tmp/instagram_cookies.txt"
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
    # On a dev macOS box (no K_SERVICE, /Applications exists) cookies come
    # from the Chrome profile.
    if os.environ.get("K_SERVICE") or not os.path.exists("/Applications"):
        print("SKIP: not a local-dev environment")
        return
    assert scraper_cookies.cookie_opts("instagram") == {"cookiesfrombrowser": ("chrome",)}
    print("PASS: cookie_opts local dev → Chrome")




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
    test_health_expiring_soon()
    test_health_expired()
    test_health_degrades_to_file_age_without_session_row()
    test_health_missing_file()
    print("All scraper-cookie tests passed.")
