#!/usr/bin/env python3
"""TikTok verdicts read from the post's own page.

Two kinds of post kept a retry queue from ever draining (2026-10-14, about
2,000 campaign retries). Most were posts TikTok's page says it cannot show
(status 10204 "item doesn't exist", "status_deleted", "status_self_see", ...):
yt-dlp words them all "Your IP address is blocked", an uncorroborated
permanent verdict, so a queue of them trips the permanent-storm guard on
every run. The rest were live posts whose page shows them as videos but
offers no file: yt-dlp finds only the soundtrack, the scraper took them for
photo posts that failed ("carousel", retryable), and 25 in a row tripped the
transient-storm guard. An aborted run charges no retry budget, and the queue
starts with the same items every time.

Usage:
    pytest tests/unit/test_tiktok_page_verdicts.py
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from fyp.scrape import scrape, tiktok_dl
from fyp.scrape.tiktok_dl import TikTokScraper

URL = "https://www.tiktok.com/@/video/7000000000000000001/"
STORM_THRESHOLD = 5

STRUCT_NO_FILE = {
    "id": "7000000000000000001",
    "desc": "x",
    "video": {"id": "7000000000000000001", "duration": 22, "playAddr": "", "downloadAddr": ""},
    "author": {"uniqueId": "a"},
}
AUDIO_ONLY_INFO = {
    "id": "7000000000000000001",
    "timestamp": 1717000000,
    "duration": 22,
    "formats": [{"format_id": "audio", "vcodec": "none", "acodec": "mp3"}],
}


# --------------------------------------------------------------------------- #
# Reading the page
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "msg", "expected"),
    [
        (0, "", None),
        (None, "", None),
        (10204, "item doesn't exist", "removed"),
        (10204, "status_deleted", "removed"),
        (10204, "status_audit_not_pass", "removed"),
        (10204, "author_status,status_reviewing", "removed"),
        (10240, "item is dark post", "removed"),
        (203009, "short drama unsupported type", "removed"),
        (10204, "status_self_see", "private"),
        (10204, "content_classification,status_self_see", "private"),
        (10204, "status_friend_see", "private"),
        (10216, "", "private"),
        (10222, "", "private"),
    ],
)
def test_page_status_category(code, msg, expected):
    assert tiktok_dl._page_status_category(code, msg) == expected


def test_page_status_reads_the_video_detail_scope():
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": 10204, "statusMsg": "x"}}}
    assert tiktok_dl._page_status(data) == (10204, "x")
    assert tiktok_dl._page_status({"__DEFAULT_SCOPE__": {}}) == (None, "")


@pytest.mark.parametrize(
    ("struct", "expected"),
    [
        (STRUCT_NO_FILE, True),
        (None, False),
        ({**STRUCT_NO_FILE, "imagePost": {"images": []}}, False),
        ({**STRUCT_NO_FILE, "video": {"duration": 22, "playAddr": "https://v"}}, False),
        ({**STRUCT_NO_FILE, "video": {"duration": 22, "bitrateInfo": [{}]}}, False),
        ({**STRUCT_NO_FILE, "video": {"playAddr": ""}}, False),
    ],
)
def test_offers_no_video_file(struct, expected):
    assert tiktok_dl._offers_no_video_file(struct) is expected


def test_the_age_gate_is_private():
    category, _ = tiktok_dl._classify_error(
        Exception(
            "ERROR: [TikTok] 7404885645589515552: This post may not be comfortable for some "
            "audiences. Log in for access. Use --cookies-from-browser or --cookies for the "
            "authentication."
        )
    )
    assert category == "private"


# --------------------------------------------------------------------------- #
# save_tiktok
# --------------------------------------------------------------------------- #


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: returns ``info`` or raises ``error``."""

    info: dict = {}
    error: Exception | None = None

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if _FakeYDL.error is not None:
            raise _FakeYDL.error
        return dict(_FakeYDL.info)


@pytest.fixture
def fake_tiktok(monkeypatch):
    """Script yt-dlp and the page; returns a setter and the list of page fetches."""
    pages: list[str] = []
    page = {"value": (None, None, "")}
    monkeypatch.setattr(tiktok_dl.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(tiktok_dl, "_cookie_opts", dict)
    monkeypatch.setattr(tiktok_dl, "sleep", lambda s: None)

    def fetch_page(url):
        pages.append(url)
        return page["value"]

    monkeypatch.setattr(tiktok_dl, "_fetch_page", fetch_page)

    def play(*, info=None, error=None, page_result=(None, None, "")):
        _FakeYDL.info = info or {}
        _FakeYDL.error = error
        page["value"] = page_result

    return play, pages


def _download_error(message: str):
    return tiktok_dl.yt_dlp.utils.DownloadError(f"ERROR: [TikTok] 7000000000000000001: {message}")


def test_a_post_its_page_names_as_gone_is_a_corroborated_failure(fake_tiktok):
    play, _ = fake_tiktok
    play(
        error=_download_error("Your IP address is blocked from accessing this post"),
        page_result=(None, 10204, "status_deleted"),
    )
    res = tiktok_dl.save_tiktok(URL)
    assert res.empty
    assert res.attrs["error_type"] == "removed"
    assert res.attrs["verdict_corroborated"] is True
    assert TikTokScraper().classify_error("removed") == "permanent:removed"


def test_a_stub_page_corroborates_nothing(fake_tiktok):
    """A bot-challenge stub has no status: the storm guards must still see the session."""
    play, _ = fake_tiktok
    play(error=_download_error("Your IP address is blocked from accessing this post"))
    res = tiktok_dl.save_tiktok(URL)
    assert res.attrs["error_type"] == "ip_blocked"
    assert not res.attrs.get("verdict_corroborated")


def test_a_throttle_error_never_reads_the_page(fake_tiktok):
    play, pages = fake_tiktok
    play(error=_download_error("HTTP Error 429: Too Many Requests"), page_result=(None, 10204, "x"))
    res = tiktok_dl.save_tiktok(URL)
    assert res.attrs["error_type"] == "rate_limited"
    assert pages == []


def test_a_post_with_no_video_file_keeps_its_metadata(fake_tiktok):
    play, _ = fake_tiktok
    play(info=AUDIO_ONLY_INFO, page_result=(STRUCT_NO_FILE, 0, ""))
    row = tiktok_dl.save_tiktok(URL)
    assert len(row) == 1, "the metadata row is returned, not an empty failure"
    assert not bool(row.loc[0, "video_downloaded"])
    assert row.attrs["media_error_type"] == "no_stream"
    assert row.attrs["verdict_corroborated"] is True
    assert row.attrs["verdict_needs_witness"] is True
    assert TikTokScraper().classify_error("no_stream") == "permanent:no_stream"


def test_a_failed_page_fetch_is_still_a_retryable_carousel(fake_tiktok):
    play, _ = fake_tiktok
    play(info=AUDIO_ONLY_INFO)
    res = tiktok_dl.save_tiktok(URL)
    assert res.empty
    assert res.attrs["error_type"] == "carousel"
    assert TikTokScraper().classify_error("carousel") == "transient:carousel"


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #


def _row(item_id: str, no_file: bool = False) -> pd.DataFrame:
    row = pd.DataFrame(
        [
            {
                "item_id": item_id,
                "desc": "x",
                "create_time_raw": pd.Timestamp("2026-01-01"),
                "duration_raw": 30,
                "author_id": "a",
                "author_handle": "@a",
                "author_name_raw": "A",
                "play_count_raw": 1,
                "fave_count_raw": 0,
                "comment_count_raw": 0,
                "share_count_raw": 0,
                "video_downloaded": not no_file,
            }
        ]
    )
    if no_file:
        row.attrs.update(
            media_error_type="no_stream",
            media_error_detail="simulated",
            verdict_corroborated=True,
            verdict_needs_witness=True,
        )
    return row


def _run_batch(ids, fake_dl, max_workers=1):
    with (
        patch.object(scrape, "download_single_video", side_effect=fake_dl),
        patch.object(scrape, "_permanent_storm_threshold", return_value=STORM_THRESHOLD),
        patch.object(scrape, "_transient_storm_threshold", return_value=STORM_THRESHOLD),
        patch.object(scrape.scrape_versioning, "ensure_active_version_registered", lambda: None),
    ):
        return scrape.download_video_threads(
            interesting_videos=ids, max_workers=max_workers, dry_run=True, platform="tiktok"
        )


def test_posts_with_no_file_leave_the_queue_when_the_batch_downloaded_media():
    no_file = [f"n{i}" for i in range(STORM_THRESHOLD * 3)]
    ids = no_file + ["ok"]

    results, perm, trans = _run_batch(
        ids, lambda video_id=None, **k: _row(video_id, video_id != "ok")
    )

    assert set(results["item_id"]) == set(ids), "every metadata row is saved"
    assert results.attrs["permanent_storm_tripped"] is False
    assert results.attrs["transient_storm_tripped"] is False
    assert results.attrs["media_retry_ids"] == []
    assert trans == [] and perm == []


def test_without_a_witness_they_take_the_media_retry_budget():
    ids = [f"n{i}" for i in range(STORM_THRESHOLD * 3)]

    results, perm, trans = _run_batch(ids, lambda video_id=None, **k: _row(video_id, True))

    assert results.attrs["permanent_storm_tripped"] is False
    assert results.attrs["transient_storm_tripped"] is False
    assert sorted(results.attrs["media_retry_ids"]) == sorted(ids)
    assert sorted(trans) == sorted(ids), "kept queued, not pruned"
    assert perm == []


def test_media_that_already_existed_is_no_witness():
    ok = _row("had")
    assert scrape._batch_downloaded_media({0: ok}, ["had"], {}) is True
    assert scrape._batch_downloaded_media({0: ok}, ["had"], {"had": "gs://x"}) is False
    assert scrape._batch_downloaded_media({0: _row("n", True)}, ["n"], {}) is False
