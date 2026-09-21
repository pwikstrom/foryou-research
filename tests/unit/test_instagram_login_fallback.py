"""Instagram: anonymous first, the session cookies for posts hidden from logged-out viewers.

Covers the 2026-09-21 field log. The scraper had run fully anonymously since
2026-07 (Instagram's authenticated web API 404'd then, and yt-dlp takes that
path whenever cookies are attached). By September 65 of 75 queued posts failed
every run: 50 with Instagram's own ruling "This content isn't available to
everyone: It can't be seen by certain audiences" (fell through to a retryable
``unknown``) and 15 with yt-dlp's "Instagram sent an empty media response"
(``rate_limited``). Measured that day, 13/13 sampled posts failed anonymously
and 13/13 extracted with the operator's Chrome cookies.

Pinned here: the ruling classifies as a login wall; a login-gated anonymous
failure is retried once with the cookies — straight away, without burning the
anonymous retries — and the media leg follows the metadata leg's auth mode;
with the cookies attached an empty media response is throttling again.

Run: pytest tests/unit/test_instagram_login_fallback.py
"""

import pytest
from yt_dlp.utils import ExtractorError

from fyp.scrape import instagram_dl
from fyp.scrape.instagram_dl import InstagramScraper

AUDIENCE_RULING = ("ERROR: [Instagram] DUi1MEGieRX: This content isn't available to "
                   "everyone: It can't be seen by certain audiences.")
EMPTY_MEDIA = ("ERROR: [Instagram] DdXvQ_7HFSh: Instagram sent an empty media response. "
               "Check if this post is accessible in your browser without being logged-in.")
COOKIES = {'cookiefile': '/tmp/instagram_cookies.txt'}
_POST = {'id': '1', 'description': 'caption', 'timestamp': 1750000000,
         'uploader_id': '99', 'channel': 'someuser', 'uploader': 'Some User',
         'view_count': 10, 'like_count': 2, 'comment_count': 1, 'duration': 12.0,
         'formats': [{'format_id': 'dash', 'url': 'https://x'}]}


class _FakeYDL:
    """yt_dlp.YoutubeDL stand-in whose outcome depends on the auth mode.

    ``anon`` / ``authed`` are either an error message (raised as an
    ExtractorError) or an info dict to return.
    """

    anon: object = None
    authed: object = None
    calls: list[str] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        authed = 'cookiefile' in self.opts
        _FakeYDL.calls.append("cookies" if authed else "anonymous")
        outcome = _FakeYDL.authed if authed else _FakeYDL.anon
        if isinstance(outcome, str):
            raise ExtractorError(outcome, expected=True)
        return dict(outcome)


@pytest.fixture
def ig(monkeypatch):
    _FakeYDL.calls = []
    monkeypatch.setattr(instagram_dl.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(instagram_dl, "sleep", lambda s: None)
    monkeypatch.setattr(instagram_dl.scraper_cookies, "cookie_opts", lambda platform: dict(COOKIES))

    def script(anon, authed=None):
        _FakeYDL.anon, _FakeYDL.authed = anon, authed
    return script


def test_the_audience_ruling_is_a_login_wall():
    category, _ = instagram_dl._classify_error(ExtractorError(AUDIENCE_RULING, expected=True))
    assert category == "login_required", "it fell through to 'unknown' until 2026-09-21"
    assert category in instagram_dl._RETRYABLE


@pytest.mark.parametrize("anonymous_failure", [AUDIENCE_RULING, EMPTY_MEDIA])
def test_a_login_gated_post_is_retried_once_with_the_cookies(ig, anonymous_failure):
    ig(anon=anonymous_failure, authed=_POST)
    info, fail = instagram_dl._extract_metadata("u", "x")
    assert fail is None and info['_fyp_authenticated'] is True
    assert _FakeYDL.calls == ["anonymous", "cookies"], \
        "one anonymous attempt — repeating it cannot get past the wall"


def test_a_public_post_never_touches_the_cookies(ig):
    ig(anon=_POST, authed="must not be called")
    info, fail = instagram_dl._extract_metadata("u", "x")
    assert fail is None and "_fyp_authenticated" not in info
    assert _FakeYDL.calls == ["anonymous"]


def test_other_failures_do_not_try_the_cookies(ig):
    ig(anon="ERROR: [Instagram] x: This post is unavailable", authed=_POST)
    _, fail = instagram_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == "removed"
    assert _FakeYDL.calls == ["anonymous"]


def test_without_cookies_the_anonymous_verdict_stands(ig, monkeypatch):
    monkeypatch.setattr(instagram_dl.scraper_cookies, "cookie_opts", lambda platform: {})
    ig(anon=AUDIENCE_RULING)
    _, fail = instagram_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == "login_required"
    assert _FakeYDL.calls == ["anonymous"]


def test_with_the_cookies_an_empty_media_response_is_throttling(ig):
    ig(anon=EMPTY_MEDIA, authed=EMPTY_MEDIA)
    _, fail = instagram_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == "rate_limited"
    assert _FakeYDL.calls == ["anonymous"] + ["cookies"] * instagram_dl._META_MAX_RETRIES, \
        "authenticated, it retries with backoff like any rate limit"


def test_hidden_even_from_the_session_ends_after_one_authenticated_attempt(ig):
    ig(anon=AUDIENCE_RULING, authed=AUDIENCE_RULING)
    _, fail = instagram_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == "login_required"
    assert _FakeYDL.calls == ["anonymous", "cookies"]


@pytest.mark.parametrize("anon,expect_authenticated", [(AUDIENCE_RULING, True), (_POST, False)])
def test_the_media_leg_follows_the_metadata_legs_auth_mode(ig, monkeypatch, anon,
                                                           expect_authenticated):
    ig(anon=anon, authed=_POST)
    seen = {}
    monkeypatch.setattr(instagram_dl, "_download_media",
                        lambda *a, **k: seen.update(k) or (True, None, "", 12.0))
    row = InstagramScraper().fetch("x", save_media=True, save_path="/tmp")
    assert row.loc[0, "video_downloaded"] == True  # noqa: E712
    assert seen["authenticated"] is expect_authenticated


def test_download_media_attaches_the_cookies_only_when_authenticated(monkeypatch, tmp_path):
    opts_seen = []

    class _RecordingYDL(_FakeYDL):
        def __init__(self, opts):
            opts_seen.append(opts)
            super().__init__(opts)

        def download(self, urls):
            raise ExtractorError("stop here", expected=True)

    monkeypatch.setattr(instagram_dl.yt_dlp, "YoutubeDL", _RecordingYDL)
    monkeypatch.setattr(instagram_dl.scraper_cookies, "cookie_opts", lambda platform: dict(COOKIES))
    monkeypatch.setattr(instagram_dl, "sleep", lambda s: None)
    monkeypatch.setattr(instagram_dl, "_cf", lambda: {"paths": {"temp": str(tmp_path)}})
    for authenticated in (False, True):
        opts_seen.clear()
        instagram_dl._download_media("u", "x", str(tmp_path), authenticated=authenticated)
        assert all(("cookiefile" in o) is authenticated for o in opts_seen), opts_seen
