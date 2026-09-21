"""Corroborated permanent verdicts, YouTube's refusal reasons, and the residential-IP guard.

Covers the 2026-09-21 YouTube deadlock. Every id in the queue had already
failed, so retries had distilled it down to dead and blocked videos. The
metadata leg runs with ``ignore_no_formats_error``, which swallowed each
refusal and saved an empty placeholder row (no author, -1 plays, created
2000-01-01); the media leg then answered a bare "Video unavailable" →
permanent:removed; fifteen in a row tripped the permanent-storm guard; and an
aborted batch charges no retry budget — so 190 ids stayed queued run after run
with zero drained. A queue-wide sweep with the tv player client found every
one of them genuinely gone or blocked (137 with no record left at all, 23 kept
but region- or rights-blocked), so the guard was right about the session but
wrong about the items.

Pinned here:
  * the metadata leg captures the swallowed reason; a video with no record is a
    failure (never a placeholder row), corroborated when its reason is itself
    permanent, and never when the reason reads as throttling;
  * a video YouTube keeps but will not play here (region, rights claim) is
    scraped metadata-only without a pointless media attempt, and leaves the
    queue;
  * a corroborated verdict neither extends nor resets a storm run, and is
    pruned even when the guard trips; uncorroborated verdicts keep the old
    protection;
  * Instagram and YouTube refuse to run on Cloud Run, and the enrichment
    supervisor leaves their queues to the local install.

Run: pytest tests/unit/test_scrape_verdict_corroboration.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import pytest

from fyp.scrape import scrape, youtube_dl
from fyp.scrape.youtube_dl import YouTubeScraper

STORM_THRESHOLD = 5

# Real reasons and shapes, from the 2026-09-21 sweep of the live queue.
_GONE = {'id': 'x', 'title': 'youtube video #x', 'formats': []}
_KEPT = {'id': 'x', 'title': 'ICE-COLD FROM HAALAND', 'formats': [],
         'channel_id': 'UC1', 'uploader_id': '@c', 'channel': 'C',
         'view_count': 1265231, 'duration': 11, 'description': ''}
_PLAYABLE = {**_KEPT, 'formats': [{'format_id': '18', 'url': 'https://x'}]}


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: replays one scripted extraction.

    ``script`` is ``(warnings, info)``: each warning goes to the logger the
    metadata leg installs, exactly as yt-dlp reports a swallowed refusal.
    """

    script: tuple[list[str], dict] = ([], {})
    opts_seen: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        _FakeYDL.opts_seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        warnings, info = _FakeYDL.script
        for w in warnings:
            self.opts['logger'].warning(w)
        return dict(info)


@pytest.fixture
def fake_ydl(monkeypatch):
    _FakeYDL.opts_seen = []
    monkeypatch.setattr(youtube_dl.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(youtube_dl.scraper_cookies, "cookie_opts", lambda platform: {})

    def play(warnings, info):
        _FakeYDL.script = (warnings, info)
    return play


# --------------------------------------------------------------------------- #
# The metadata leg
# --------------------------------------------------------------------------- #

def test_no_record_with_a_removal_reason_is_a_corroborated_failure(fake_ydl):
    fake_ydl(["[youtube] This video has been removed by the uploader"], _GONE)
    info, fail = youtube_dl._extract_metadata("u", "x")
    assert info is None
    assert fail.empty, "a video with no record must not become a row"
    assert fail.attrs["error_type"] == "removed"
    assert fail.attrs["verdict_corroborated"] is True


@pytest.mark.parametrize("reason,category", [
    ("[youtube] Sign in to confirm you're not a bot. Use --cookies", "bot_check"),
    ("[youtube] Video unavailable. This content isn't available, try again later. "
     "The current session has been rate-limited by YouTube for up to an hour.", "rate_limited"),
])
def test_no_record_with_a_throttle_reason_is_never_corroborated(fake_ydl, reason, category):
    """A walled session may return no record for a live video — that must stay transient."""
    fake_ydl([reason], _GONE)
    _, fail = youtube_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == category
    assert "verdict_corroborated" not in fail.attrs
    assert not YouTubeScraper().classify_error(category).startswith("permanent")


def test_no_record_and_no_reason_is_unknown(fake_ydl):
    fake_ydl([], _GONE)
    _, fail = youtube_dl._extract_metadata("u", "x")
    assert fail.attrs["error_type"] == "unknown"
    assert "verdict_corroborated" not in fail.attrs


def test_a_playable_video_ignores_stray_warnings(fake_ydl):
    fake_ydl(["[youtube] Video unavailable"], _PLAYABLE)
    info, fail = youtube_dl._extract_metadata("u", "x")
    assert fail is None
    assert "_fyp_unplayable" not in info


def test_the_metadata_leg_asks_the_tv_client_and_keeps_the_po_token_wiring(fake_ydl, monkeypatch):
    pot = {'youtubepot-bgutilscript': {'server_home': ['/srv/pot']}}
    monkeypatch.setattr(youtube_dl, "_pot_extractor_args", lambda: {'extractor_args': dict(pot)})
    fake_ydl([], _PLAYABLE)
    youtube_dl._extract_metadata("u", "x")
    args = _FakeYDL.opts_seen[-1]['extractor_args']
    assert args['youtube'] == {'player_client': ['default', 'tv']}
    assert args['youtubepot-bgutilscript'] == pot['youtubepot-bgutilscript']
    assert _FakeYDL.opts_seen[-1]['ignore_no_formats_error'] is True


def test_reason_log_keeps_only_playability_reasons():
    log = youtube_dl._ReasonLog()
    for msg in ("[youtube] [pot:bgutil:http] Error reaching GET http://127.0.0.1:4416/ping",
                "No video formats found!",
                "[youtube] No video formats found!",
                "[youtube] Requested format is not available",
                "[youtube] x: n challenge solving failed: Some formats may be missing",
                "[generic] something else",
                "[youtube] The uploader has not made this video available in your country"):
        log.warning(msg)
    assert log.reasons == ["The uploader has not made this video available in your country"]


def test_playability_verdict_lets_a_throttle_signal_win():
    """A permanent reason must never mask a session problem."""
    verdict = youtube_dl._playability_verdict(
        ["This video has been removed by the uploader", "Sign in to confirm you're not a bot"])
    assert verdict[0] == "bot_check"
    assert youtube_dl._playability_verdict([]) is None
    assert youtube_dl._playability_verdict(["weird", "This video is private"])[0] == "private"


# --------------------------------------------------------------------------- #
# fetch(): kept but unplayable here
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason,category", [
    ("[youtube] The uploader has not made this video available in your country", "geo_blocked"),
    ("[youtube] It was blocked due to the claimed content by UFC.", "blocked"),
])
def test_kept_but_blocked_here_is_metadata_only_and_skips_the_media_leg(
        fake_ydl, monkeypatch, reason, category):
    fake_ydl([reason], _KEPT)
    monkeypatch.setattr(youtube_dl, "_download_media",
                        lambda *a, **k: pytest.fail("the media leg could only repeat the refusal"))
    row = YouTubeScraper().fetch("x", save_media=True, save_path="/tmp")
    assert not row.empty and row.loc[0, "author_name_raw"] == "C"
    assert row.loc[0, "video_downloaded"] == False  # noqa: E712
    assert row.attrs["media_error_type"] == category
    assert row.attrs["verdict_corroborated"] is True


def test_kept_with_a_bare_unavailable_still_takes_the_distrusted_media_leg(fake_ydl, monkeypatch):
    """The 2026-09-18 signature: record intact, bare reason — never corroborated."""
    fake_ydl(["[youtube] This video is not available"], _KEPT)
    calls = []
    monkeypatch.setattr(youtube_dl, "_download_media",
                        lambda *a, **k: calls.append(1) or (False, "removed", "Video unavailable"))
    row = YouTubeScraper().fetch("x", save_media=True, save_path="/tmp")
    assert calls, "the media leg must still be tried"
    assert row.attrs["media_error_type"] == "removed"
    assert "verdict_corroborated" not in row.attrs


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #

def _failure(category: str, corroborated: bool = False) -> pd.DataFrame:
    return youtube_dl._empty_fail(category, "simulated", corroborated=corroborated)


def _metadata_row(item_id: str, media_error: str | None = None,
                  corroborated: bool = False) -> pd.DataFrame:
    row = pd.DataFrame([{
        "item_id": item_id, "desc": "x", "create_time_raw": pd.Timestamp("2026-01-01"),
        "duration_raw": 30, "author_id": "a", "yt_author_handle": "@a",
        "author_name_raw": "A", "play_count_raw": 1, "yt_like_count": 0,
        "yt_comment_count": 0, "yt_channel_follower_count": 0,
        "yt_categories": "", "video_downloaded": media_error is None,
    }])
    if media_error is not None:
        row.attrs["media_error_type"] = media_error
        row.attrs["media_error_detail"] = "simulated"
    if corroborated:
        row.attrs["verdict_corroborated"] = True
    return row


def _run_batch(ids, fake_dl, max_workers=1):
    with patch.object(scrape, "download_single_video", side_effect=fake_dl), \
         patch.object(scrape, "_permanent_storm_threshold", return_value=STORM_THRESHOLD), \
         patch.object(scrape.scrape_versioning, "ensure_active_version_registered",
                      lambda: None), \
         patch.object(YouTubeScraper, "inter_request_delay", return_value=0.0):
        return scrape.download_video_threads(
            interesting_videos=ids, max_workers=max_workers,
            dry_run=True, platform="youtube")


def test_a_queue_of_dead_videos_drains_instead_of_storming():
    """The 2026-09-21 queue: nothing but corroborated removals, far past the threshold."""
    ids = [f"v{i}" for i in range(STORM_THRESHOLD * 4)]

    results, perm, trans = _run_batch(ids, lambda video_id=None, **k: _failure("removed", True))

    assert results.attrs["permanent_storm_tripped"] is False
    assert set(perm) == set(ids), "corroborated removals are pruned as permanent"
    assert trans == []


def test_corroborated_verdicts_neither_extend_nor_reset_a_storm_run():
    """3 bare removals, 5 corroborated, 2 bare: the bare run reaches 5 on the last call."""
    # By call order, not id: the pool's threads need not take the single
    # throttle slot in submission order (same trick as the storm-guard tests).
    plan = iter([False] * 3 + [True] * 5 + [False] * 2)
    ids = [f"v{i}" for i in range(10)]
    proven = set()

    def fake_dl(video_id=None, **kwargs):
        corroborated = next(plan)
        if corroborated:
            proven.add(video_id)
        return _failure("removed", corroborated=corroborated)

    results, perm, trans = _run_batch(ids, fake_dl)

    assert results.attrs["permanent_storm_tripped"] is True
    assert len(proven) == 5
    assert set(perm) == proven, \
        "corroborated ids are pruned even though the guard tripped on their category"
    assert set(trans) == set(ids) - proven, \
        "the uncorroborated storm ids are demoted and stay queued, as before"


def test_a_corroborated_media_verdict_prunes_with_its_row():
    ids = ["kept_blocked", "media_flaky"]

    def fake_dl(video_id=None, **kwargs):
        if video_id == "kept_blocked":
            return _metadata_row(video_id, media_error="geo_blocked", corroborated=True)
        return _metadata_row(video_id, media_error="removed")

    results, perm, trans = _run_batch(ids, fake_dl)

    assert set(results["item_id"]) == set(ids), "both metadata rows are saved"
    assert results.attrs["media_retry_ids"] == ["media_flaky"]
    assert trans == ["media_flaky"], "only the distrusted media failure stays queued"
    assert perm == []


# --------------------------------------------------------------------------- #
# Residential IP only
# --------------------------------------------------------------------------- #

def test_instagram_and_youtube_refuse_cloud_run_and_tiktok_does_not(monkeypatch):
    from fyp.scrape.platform_scraper import get_scraper

    monkeypatch.delenv("K_SERVICE", raising=False)
    assert all(get_scraper(p).unavailable_here() is None
               for p in ("tiktok", "instagram", "youtube"))

    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    assert get_scraper("tiktok").unavailable_here() is None
    for platform in ("instagram", "youtube"):
        why = get_scraper(platform).unavailable_here()
        assert why and "residential IP" in why and platform in why


class _Reporter:
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(str(msg))

    def update_progress(self, *a, **k):
        pass

    def emit_data(self, payload):
        pass

    def check_cancelled(self):
        return False


def test_the_cloud_run_worker_leaves_a_residential_queue_untouched(monkeypatch):
    import fyp.scrape as fyp_scrape
    from fyp.scrape import scrape_queues
    from web_interface.run_queue_scraper import run_queue_scraper

    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    monkeypatch.setattr(scrape_queues, "load_scrape_queue",
                        lambda platform: pytest.fail("the queue must not be read"))
    monkeypatch.setattr(fyp_scrape, "download_video_threads",
                        lambda **k: pytest.fail("nothing may be scraped"))
    reporter = _Reporter()

    assert run_queue_scraper(reporter, {"platform": "youtube"}) is None
    assert any("residential IP" in line for line in reporter.lines), reporter.lines


def test_the_cloud_run_supervisor_leaves_a_residential_queue_to_the_local_install(monkeypatch):
    from fyp.scrape import scrape_queues
    from web_interface import run_enrichment_supervisor as sup

    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    monkeypatch.setattr(scrape_queues, "queue_lengths", lambda: {"youtube": 190})
    monkeypatch.setattr(sup, "_scrape_lane_busy", lambda platform: False)
    monkeypatch.setattr(sup, "_scraper_blocked", lambda platform: None)
    monkeypatch.setattr(sup, "_queue_stalled",
                        lambda *a, **k: pytest.fail("no stall may be charged for a skipped queue"))
    monkeypatch.setattr(sup, "_start", lambda *a, **k: pytest.fail("no worker may be started"))
    reporter = _Reporter()

    assert sup._drain(reporter, {"c1": {"platform": "youtube"}}) is None
    assert any("residential IP" in line for line in reporter.lines), reporter.lines
