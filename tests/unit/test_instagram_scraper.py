#!/usr/bin/env python3
"""Tests for the Instagram scraper (no network).

Covers raw-row construction from a canned yt-dlp info dict (requested-shortcode
item_id stamping, >10-column shape gate), the canonicalization pipeline
(canonical names, per-K rates, NA shares/saves, sentinel handling), the error
classification truth table, and the generic media-duration cap.

Usage:
    python tests/unit/test_instagram_scraper.py
    pytest tests/unit/test_instagram_scraper.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp.fyp_config import fyp_cf
from fyp.instagram_dl import _classify_error, _info_to_row, InstagramScraper


# A trimmed yt-dlp Instagram info dict. 'id' is the numeric media pk — the
# raw row must carry the requested shortcode instead.
_INFO = {
    'id': '3521098765432109876',
    'description': 'A reel caption #tag',
    'timestamp': 1750000000,
    'duration': 17.4,
    'channel_id': 'someuser',
    'uploader_id': 'someuser',
    'uploader': 'Some User',
    'channel': 'someuser',
    'view_count': 250000,
    'like_count': 12500,
    'comment_count': 250,
}




def test_info_to_row_stamps_requested_id():
    row = _info_to_row(_INFO, "DY1zHU_xQM2")
    assert row.loc[0, 'item_id'] == "DY1zHU_xQM2"
    assert row.shape[0] == 1
    # The orchestrator gates success on > 10 columns.
    assert row.shape[1] > 10
    assert row.loc[0, 'desc'] == 'A reel caption #tag'
    assert row.loc[0, 'ig_author_handle'] == 'someuser'
    assert row.loc[0, 'author_name_raw'] == 'Some User'
    assert row.loc[0, 'video_downloaded'] is False or row.loc[0, 'video_downloaded'] == False  # noqa: E712
    print("PASS: Instagram _info_to_row stamps the requested shortcode")




def test_missing_counts_use_sentinel():
    info = dict(_INFO)
    del info['like_count']
    info['comment_count'] = None
    info['view_count'] = None
    row = _info_to_row(info, "DY1zHU_xQM2")
    assert row.loc[0, 'ig_like_count'] == -1
    assert row.loc[0, 'ig_comment_count'] == -1
    assert row.loc[0, 'play_count_raw'] == -1
    print("PASS: Instagram missing counts fall back to the -1 sentinel")




def test_canonicalize_batch():
    scraper = InstagramScraper()
    df = _info_to_row(_INFO, "DY1zHU_xQM2")
    df = scraper.prepare_raw_batch(df)
    out = scraper.canonicalize_batch(df.copy(), status="ok")

    for col in ('create_time', 'duration', 'play_count', 'author_name', 'scrape_ts'):
        assert col in out.columns, f"missing canonical column {col}"

    assert out.loc[0, 'source_platform'] == 'instagram'
    assert out.loc[0, 'scrape_status'] == 'ok'
    assert str(out.loc[0, 'scrape_contract_version']).startswith('sv_')

    # Per-K rates from the [perk.instagram] map.
    expected_faves = 12500 / 250000 * 1000
    assert abs(out.loc[0, 'faves_per_K_play'] - expected_faves) < 1e-9
    expected_comments = 250 / 250000 * 1000
    assert abs(out.loc[0, 'comments_per_K_play'] - expected_comments) < 1e-9

    # Instagram exposes no share/save counts — those base rates stay NA.
    assert pd.isna(out.loc[0, 'shares_per_K_play'])
    assert pd.isna(out.loc[0, 'saves_per_K_play'])
    print("PASS: Instagram canonicalize_batch derives per-K and stamps provenance")




def test_sentinel_counts_yield_na_rates():
    scraper = InstagramScraper()
    info = dict(_INFO)
    info['like_count'] = None
    df = _info_to_row(info, "DY1zHU_xQM2")
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(df), status="ok")
    assert pd.isna(out.loc[0, 'faves_per_K_play'])
    print("PASS: Instagram -1 sentinel count yields NA rate")




def test_duration_sentinel_becomes_na():
    scraper = InstagramScraper()
    info = dict(_INFO)
    info['duration'] = None
    df = _info_to_row(info, "DY1zHU_xQM2")
    out = scraper.prepare_raw_batch(df)
    assert pd.isna(out.loc[0, 'duration_raw'])
    print("PASS: Instagram unknown duration becomes NA")




def test_classify_error_truth_table():
    scraper = InstagramScraper()
    cases = {
        "There is no video in this post": ("no_video", "permanent"),
        "Requested content is not available, rate-limit reached or login required":
            ("rate_limited", "transient"),
        "Instagram sent an empty media response": ("rate_limited", "transient"),
        "This account is private": ("private", "permanent"),
        "Login required to access this content": ("login_required", "transient"),
        "The page you requested was not found": ("removed", "permanent"),
        "Connection reset by peer": ("network", "transient"),
        "something entirely new": ("unknown", "transient"),
    }
    for msg, (category, bucket) in cases.items():
        got_cat, _ = _classify_error(Exception(msg))
        assert got_cat == category, f"{msg!r}: expected {category}, got {got_cat}"
        status = scraper.classify_error(got_cat)
        assert status == f"{bucket}:{category}", f"{msg!r}: got {status}"
    assert scraper.classify_error(None) == "ok"
    print("PASS: Instagram classify_error truth table")




def test_duration_cap_and_override():
    scraper = InstagramScraper()
    default_cap = int(fyp_cf['misc']['max_duration_for_download'])
    assert scraper.media_duration_cap() == default_cap
    assert scraper.should_download_media(default_cap) is True
    assert scraper.should_download_media(default_cap + 1) is False
    assert scraper.should_download_media(None) is True
    assert scraper.should_download_media(pd.NA) is True
    assert scraper.should_download_media(-1) is True  # sentinel: unknown

    fyp_cf['misc']['max_duration_for_download_instagram'] = 90
    try:
        assert scraper.media_duration_cap() == 90
        assert scraper.should_download_media(91) is False
    finally:
        del fyp_cf['misc']['max_duration_for_download_instagram']
    print("PASS: Instagram duration cap + per-platform override")




def test_throttle_limits_capped():
    scraper = InstagramScraper()
    assert scraper.throttle_limits(8) == (2, 1, 3)
    assert scraper.throttle_limits(1) == (1, 1, 3)
    print("PASS: Instagram throttle limits capped")




if __name__ == "__main__":
    test_info_to_row_stamps_requested_id()
    test_missing_counts_use_sentinel()
    test_canonicalize_batch()
    test_sentinel_counts_yield_na_rates()
    test_duration_sentinel_becomes_na()
    test_classify_error_truth_table()
    test_duration_cap_and_override()
    test_throttle_limits_capped()
    print("All Instagram scraper tests passed.")
