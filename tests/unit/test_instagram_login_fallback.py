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

The same morning's run showed the other side: 14 gated posts in ~60 s (two
logged-in API calls each) and Instagram logged the session out, answering the
logged-in API with its login page, which yt-dlp surfaces as "Failed to parse
JSON"; the scraper then sent 87 attempts into the dead session. Also pinned:
logged-in requests are paced across threads, the media leg downloads from the
metadata leg's info dict (one logged-in call per post), and a logged-out
session ends every logged-in request for the run and stops it uncharged.

Run: pytest tests/unit/test_instagram_login_fallback.py
"""

import pytest
from yt_dlp.utils import ExtractorError

from fyp.scrape import instagram_dl
from fyp.scrape.instagram_dl import InstagramScraper
from fyp.scrape.platform_scraper import SESSION_EXPIRED

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


@pytest.fixture(autouse=True)
def _fresh_session():
    """Each test starts with a live session and an idle pacing clock."""
    instagram_dl._reset_session_state()
    yield
    instagram_dl._reset_session_state()


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



# --------------------------------------------------------------------------- #
# 2026-09-23: pacing, one logged-in call per post, a logged-out session
# --------------------------------------------------------------------------- #

LOGGED_OUT = ("ERROR: [Instagram] DZxhX1CAB1m: Failed to parse JSON (caused by "
              "JSONDecodeError(\"Expecting value in '': line 1 column 1 (char 0)\"))")


def test_logged_in_requests_are_spaced_across_threads(monkeypatch):
    clock = {"t": 100.0}
    waits = []

    def fake_sleep(s):
        waits.append(round(s, 3))
        clock["t"] += s

    monkeypatch.setattr(instagram_dl, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(instagram_dl, "sleep", fake_sleep)
    monkeypatch.setattr(instagram_dl, "_auth_interval", lambda: 20.0)
    for _ in range(3):
        instagram_dl._pace_authenticated()
    assert waits == [20.0, 20.0], "the first goes at once, each next one 20 s after the last"


def test_only_the_logged_in_pass_is_paced(ig, monkeypatch):
    paced = []
    monkeypatch.setattr(instagram_dl, "_pace_authenticated", lambda: paced.append(1))
    ig(anon=_POST)
    instagram_dl._extract_metadata("u", "public")
    assert paced == [], "public posts go at anonymous speed"
    ig(anon=AUDIENCE_RULING, authed=_POST)
    instagram_dl._extract_metadata("u", "gated")
    assert paced == [1]


def test_the_metadata_leg_selects_the_media_legs_format(ig):
    """A default (DASH) selection in the info dict made the reuse download 403."""
    seen = []
    orig_init = _FakeYDL.__init__

    def recording_init(self, opts):
        seen.append(opts)
        orig_init(self, opts)

    _FakeYDL.__init__ = recording_init
    try:
        ig(anon=_POST)
        instagram_dl._extract_metadata("u", "x")
    finally:
        _FakeYDL.__init__ = orig_init
    assert seen[-1]["format"] == instagram_dl._FORMAT


def test_a_logged_out_session_ends_all_logged_in_requests_for_the_run(ig):
    ig(anon=AUDIENCE_RULING, authed=LOGGED_OUT)
    _, fail = instagram_dl._extract_metadata("u", "first")
    assert fail.attrs["error_type"] == SESSION_EXPIRED
    assert _FakeYDL.calls == ["anonymous", "cookies"], "no retries into a dead session"
    assert InstagramScraper().classify_error(SESSION_EXPIRED) == "transient:session_expired"

    _FakeYDL.calls = []
    _, fail = instagram_dl._extract_metadata("u", "second")
    assert fail.attrs["error_type"] == SESSION_EXPIRED
    assert _FakeYDL.calls == ["anonymous"], "later gated posts never touch the cookies"

    _FakeYDL.calls = []
    ig(anon=_POST)
    info, fail = instagram_dl._extract_metadata("u", "public")
    assert fail is None and _FakeYDL.calls == ["anonymous"], "public posts still scrape"


class _MediaYDL:
    """yt_dlp.YoutubeDL stand-in for the media leg: records how it downloads."""

    events: list[str] = []
    reuse_fails = False

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def process_ie_result(self, info, download=False):
        assert not any(k.startswith("_fyp_") for k in info), "private keys reach yt-dlp"
        _MediaYDL.events.append("reuse")
        if _MediaYDL.reuse_fails:
            raise ExtractorError("HTTP Error 403: Forbidden", expected=True)
        self._write()

    def download(self, urls):
        _MediaYDL.events.append("re-extract")
        self._write()

    def _write(self):
        path = self.opts["outtmpl"].replace("%(ext)s", "mp4")
        with open(path, "wb") as f:
            f.write(b"x")


@pytest.fixture
def media(monkeypatch, tmp_path):
    _MediaYDL.events, _MediaYDL.reuse_fails = [], False
    paced = []
    monkeypatch.setattr(instagram_dl.yt_dlp, "YoutubeDL", _MediaYDL)
    monkeypatch.setattr(instagram_dl, "sleep", lambda s: None)
    monkeypatch.setattr(instagram_dl, "_pace_authenticated", lambda: paced.append(1))
    monkeypatch.setattr(instagram_dl, "_probe_duration", lambda path: 12.0)
    monkeypatch.setattr(instagram_dl, "_cf", lambda: {"paths": {"temp": str(tmp_path)}})
    monkeypatch.setattr(instagram_dl.scraper_cookies, "cookie_opts", lambda platform: dict(COOKIES))

    def run(**kw):
        return instagram_dl._download_media("u", "x", str(tmp_path / "out"), **kw)
    (tmp_path / "out").mkdir()
    return run, paced


def test_the_media_leg_downloads_from_the_extracted_info(media):
    run, paced = media
    ok, *_ = run(authenticated=True, info={**_POST, "_fyp_authenticated": True})
    assert ok and _MediaYDL.events == ["reuse"]
    assert paced == [], "downloading from the info dict makes no logged-in request"


def test_a_failed_reuse_falls_back_to_a_paced_re_extraction(media):
    run, paced = media
    _MediaYDL.reuse_fails = True
    ok, *_ = run(authenticated=True, info=dict(_POST))
    assert ok and _MediaYDL.events == ["reuse", "re-extract"]
    assert paced == [1]


def test_no_re_extraction_once_the_session_is_logged_out(media):
    run, paced = media
    _MediaYDL.reuse_fails = True
    instagram_dl._SESSION_DEAD.set()
    ok, category, _, _ = run(authenticated=True, info=dict(_POST))
    assert not ok and category == SESSION_EXPIRED
    assert _MediaYDL.events == ["reuse"] and paced == []


# --------------------------------------------------------------------------- #
# The orchestrator's side of a logged-out session
# --------------------------------------------------------------------------- #

def test_the_batch_flags_a_logged_out_session_and_raises_its_alert():
    from unittest.mock import patch

    import pandas as pd

    from fyp.scrape import scrape

    ids = [f"v{i}" for i in range(12)]
    alerts = []

    def fake_dl(video_id=None, **kwargs):
        return instagram_dl._empty_fail(SESSION_EXPIRED, "logged out")

    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "_transient_storm_threshold", return_value=5), \
         patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                      lambda: None), \
         patch.object(scrape, "check_existing_media", return_value={}), \
         patch.object(scrape.data_io, "save_json", lambda **kw: None), \
         patch.object(scrape.scraper_alerts, "raise_alert",
                      side_effect=lambda **kw: alerts.append(kw)), \
         patch.object(scrape.scraper_alerts, "clear_alert", side_effect=Exception):
        results, perm, trans = scrape.download_video_threads(
            interesting_videos=ids, max_workers=1, dry_run=False, platform="instagram")

    assert isinstance(results, pd.DataFrame) and results.attrs["session_expired"] is True
    assert results.attrs["transient_storm_tripped"] is False, \
        "a known cause must not masquerade as a transient storm"
    assert perm == [] and set(trans) == set(ids)
    assert [a["kind"] for a in alerts] == ["session_expired"]


def test_the_run_stops_after_a_logged_out_batch_and_charges_nothing():
    from unittest.mock import patch

    import pandas as pd

    from fyp.scrape import scrape

    ids = [f"v{i}" for i in range(4)]
    calls = {"n": 0}

    def fake_threads(interesting_videos=None, **kwargs):
        calls["n"] += 1
        frame = pd.DataFrame()
        for k in ("circuit_breaker_tripped", "permanent_storm_tripped",
                  "transient_storm_tripped", "memory_stop"):
            frame.attrs[k] = False
        frame.attrs["session_expired"] = True
        frame.attrs["media_retry_ids"] = ["v0"]
        return frame, [], list(interesting_videos)

    with patch.object(scrape, "download_video_threads", side_effect=fake_threads), \
         patch.object(scrape.scrape_queues, "prune_scrape_queue",
                      side_effect=lambda p, i: (len(i), 0)), \
         patch.object(scrape.scrape_queues, "charge_zero_progress",
                      side_effect=AssertionError("no zero-progress strike")), \
         patch.object(scrape.scrape_queues, "charge_media_retry",
                      side_effect=lambda p, retry, resolved: (
                          (_ for _ in ()).throw(AssertionError("no media strike"))
                          if retry else [])):
        scrape.scraper_loop_from_list(video_list=ids, batch_size=2, platform="instagram")

    assert calls["n"] == 1, "the run stops after the batch that saw the logout"
