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

import fyp.instagram_dl as instagram_dl
from fyp.fyp_config import fyp_cf
from fyp.instagram_dl import _classify_error, _info_to_row, _parse_page_counts, InstagramScraper


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




def test_plays_per_day_sentinel_masked():
    scraper = InstagramScraper()

    # Missing view count (-1 sentinel) must yield NA, never a negative rate.
    info = dict(_INFO)
    info['view_count'] = None
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(_info_to_row(info, "DY1zHU_xQM2")), status="ok")
    assert pd.isna(out.loc[0, 'plays_per_day']), f"expected NA, got {out.loc[0, 'plays_per_day']}"

    # A genuine zero-play item is a real value: 0 plays/day.
    info = dict(_INFO)
    info['view_count'] = 0
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(_info_to_row(info, "DY1zHU_xQM2")), status="ok")
    assert out.loc[0, 'plays_per_day'] == 0

    # And a real count still yields a positive rate.
    out = scraper.canonicalize_batch(scraper.prepare_raw_batch(_info_to_row(_INFO, "DY1zHU_xQM2")), status="ok")
    assert out.loc[0, 'plays_per_day'] > 0
    print("PASS: plays_per_day masks the -1 sentinel (NA), keeps 0 and positive counts")




_RELAY_HTML = (
    '<html><head></head><body>'
    '<script type="application/json">{"require": [{"data": {'
    '"xdt_api__v1__media__shortcode__web_info": {"items": [{'
    '"code": "DY1zHU_xQM2", "play_count": 98765, "ig_play_count": 98765, '
    '"like_count": 432, "comment_count": 21}]}}}]}</script>'
    '</body></html>'
)




def test_parse_page_counts_relay_json():
    counts = _parse_page_counts(_RELAY_HTML, "DY1zHU_xQM2")
    assert counts == {'play_count': 98765, 'like_count': 432, 'comment_count': 21}
    # A different shortcode in the same payload finds nothing structured and no
    # window-scoped fallback match either... the regex fallback is page-global,
    # so it still returns the play count.
    counts_other = _parse_page_counts(_RELAY_HTML, "ZZZZZZZZZZZ")
    assert counts_other == {'play_count': 98765, 'like_count': None, 'comment_count': None}
    print("PASS: page relay JSON counts extracted")




def test_parse_page_counts_regex_fallback():
    html = '<html><script>window.__data = {"media": {"ig_play_count": 5555}};</script></html>'
    counts = _parse_page_counts(html, "DY1zHU_xQM2")
    assert counts == {'play_count': 5555, 'like_count': None, 'comment_count': None}
    print("PASS: page regex fallback extracts play count")




def test_parse_page_counts_garbage():
    assert _parse_page_counts("<html><body>login wall</body></html>", "DY1zHU_xQM2") is None
    assert _parse_page_counts("", "DY1zHU_xQM2") is None
    print("PASS: garbage page yields None (never raises)")




def test_fetch_supplements_sentinels(monkeypatch=None):
    """A -1 sentinel row is supplemented from the page counts; failures keep -1."""
    scraper = InstagramScraper()
    info = dict(_INFO)
    info['view_count'] = None

    orig_extract = instagram_dl._extract_metadata
    orig_fetch_counts = instagram_dl._fetch_page_counts
    instagram_dl._extract_metadata = lambda url, item_id, verbose=False: (info, None)
    try:
        instagram_dl._fetch_page_counts = lambda url, item_id: {
            'play_count': 98765, 'like_count': None, 'comment_count': None}
        row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
        assert row.loc[0, 'play_count_raw'] == 98765
        # like/comment came from yt-dlp and must not be overwritten.
        assert row.loc[0, 'ig_like_count'] == _INFO['like_count']

        instagram_dl._fetch_page_counts = lambda url, item_id: None
        row = scraper.fetch("DY1zHU_xQM2", save_media=False, save_path="")
        assert row.loc[0, 'play_count_raw'] == -1
    finally:
        instagram_dl._extract_metadata = orig_extract
        instagram_dl._fetch_page_counts = orig_fetch_counts
    print("PASS: fetch supplements -1 sentinels from page counts and degrades to -1")




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
    test_plays_per_day_sentinel_masked()
    test_parse_page_counts_relay_json()
    test_parse_page_counts_regex_fallback()
    test_parse_page_counts_garbage()
    test_fetch_supplements_sentinels()
    test_duration_sentinel_becomes_na()
    test_classify_error_truth_table()
    test_duration_cap_and_override()
    test_throttle_limits_capped()
    print("All Instagram scraper tests passed.")
