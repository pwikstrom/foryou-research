#!/usr/bin/env python3
"""Tests for the YouTube scraper (no network).

Covers raw-row construction from a canned yt-dlp info dict, the upload_date
fallback for create_time, the canonicalization pipeline, the error
classification truth table (incl. the distinct throttling bot_check category),
and the generic media-duration cap.

Usage:
    python tests/unit/test_youtube_scraper.py
    pytest tests/unit/test_youtube_scraper.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime

import pandas as pd

from fyp.platform_scraper import _THROTTLE_CATEGORIES
from fyp.youtube_dl import _classify_error, _info_to_row, _parse_create_time, YouTubeScraper


_INFO = {
    'id': 'pKOOk7f6FHk',
    'description': 'A video description',
    'timestamp': 1750000000,
    'duration': 245,
    'channel_id': 'UCabcdefghijklmnopqrstuv',
    'uploader_id': '@somechannel',
    'channel': 'Some Channel',
    'uploader': 'Some Channel',
    'view_count': 1000000,
    'like_count': 50000,
    'comment_count': 2000,
    'channel_follower_count': 750000,
    'categories': ['Entertainment', 'Comedy'],
}




def test_info_to_row():
    row = _info_to_row(_INFO, "pKOOk7f6FHk")
    assert row.loc[0, 'item_id'] == "pKOOk7f6FHk"
    assert row.shape[1] > 10
    assert row.loc[0, 'author_id'] == 'UCabcdefghijklmnopqrstuv'
    assert row.loc[0, 'yt_author_handle'] == '@somechannel'
    assert row.loc[0, 'yt_categories'] == 'Entertainment | Comedy'
    assert row.loc[0, 'yt_channel_follower_count'] == 750000
    print("PASS: YouTube _info_to_row")




def test_create_time_upload_date_fallback():
    info = dict(_INFO)
    del info['timestamp']
    info['upload_date'] = '20260615'
    assert _parse_create_time(info) == datetime(2026, 6, 15)
    info_none = {k: v for k, v in _INFO.items() if k not in ('timestamp', 'upload_date')}
    assert _parse_create_time(info_none) == datetime(2000, 1, 1)
    print("PASS: YouTube create_time upload_date fallback")




def test_canonicalize_batch():
    scraper = YouTubeScraper()
    df = _info_to_row(_INFO, "pKOOk7f6FHk")
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(df), status="ok")

    for col in ('create_time', 'duration', 'play_count', 'author_name', 'scrape_ts'):
        assert col in out.columns, f"missing canonical column {col}"

    assert out.loc[0, 'source_platform'] == 'youtube'
    assert str(out.loc[0, 'scrape_contract_version']).startswith('sv_')

    expected_faves = 50000 / 1000000 * 1000
    assert abs(out.loc[0, 'faves_per_K_play'] - expected_faves) < 1e-9
    expected_comments = 2000 / 1000000 * 1000
    assert abs(out.loc[0, 'comments_per_K_play'] - expected_comments) < 1e-9
    assert pd.isna(out.loc[0, 'shares_per_K_play'])
    assert pd.isna(out.loc[0, 'saves_per_K_play'])
    assert out.loc[0, 'plays_per_day'] > 0
    print("PASS: YouTube canonicalize_batch")




def test_plays_per_day_sentinel_masked():
    scraper = YouTubeScraper()
    info = dict(_INFO)
    info['view_count'] = None
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(_info_to_row(info, "pKOOk7f6FHk")), status="ok")
    assert pd.isna(out.loc[0, 'plays_per_day']), f"expected NA, got {out.loc[0, 'plays_per_day']}"
    print("PASS: YouTube plays_per_day masks the -1 sentinel")




def test_classify_error_truth_table():
    scraper = YouTubeScraper()
    cases = {
        "Sign in to confirm you're not a bot. This helps protect our community.":
            ("bot_check", "transient"),
        "Video unavailable": ("removed", "permanent"),
        "This video has been removed by the uploader": ("removed", "permanent"),
        "The uploader has not made this video available in your country":
            ("geo_blocked", "permanent"),
        "Private video. Sign in if you've been granted access to this video":
            ("private", "permanent"),
        "Sign in to confirm your age. This video may be inappropriate for some users.":
            ("age_restricted", "permanent"),
        "Join this channel to get access to members-only content":
            ("members_only", "permanent"),
        "HTTP Error 403: Forbidden": ("rate_limited", "transient"),
        "HTTP Error 429: Too Many Requests": ("rate_limited", "transient"),
        "Connection timed out": ("network", "transient"),
        "brand new failure mode": ("unknown", "transient"),
    }
    for msg, (category, bucket) in cases.items():
        got_cat, _ = _classify_error(Exception(msg))
        assert got_cat == category, f"{msg!r}: expected {category}, got {got_cat}"
        status = scraper.classify_error(got_cat)
        assert status == f"{bucket}:{category}", f"{msg!r}: got {status}"
    assert scraper.classify_error(None) == "ok"
    print("PASS: YouTube classify_error truth table")




def test_bot_check_is_throttle_signal():
    assert "bot_check" in _THROTTLE_CATEGORIES
    assert "rate_limited" in _THROTTLE_CATEGORIES
    print("PASS: bot_check shrinks concurrency via the throttle controller")




def test_duration_cap_gates_longform():
    scraper = YouTubeScraper()
    # A 45-minute watch-history item stays metadata-only at the default cap.
    assert scraper.should_download_media(45 * 60) is False
    # A Short gets media.
    assert scraper.should_download_media(42) is True
    print("PASS: YouTube long-form stays metadata-only")




def test_throttle_limits_capped():
    scraper = YouTubeScraper()
    assert scraper.throttle_limits(8) == (2, 1, 4)
    print("PASS: YouTube throttle limits capped")




if __name__ == "__main__":
    test_info_to_row()
    test_create_time_upload_date_fallback()
    test_canonicalize_batch()
    test_plays_per_day_sentinel_masked()
    test_classify_error_truth_table()
    test_bot_check_is_throttle_signal()
    test_duration_cap_gates_longform()
    test_throttle_limits_capped()
    print("All YouTube scraper tests passed.")
