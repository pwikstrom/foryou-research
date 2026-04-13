#!/usr/bin/env python3
"""
Safe test for yt-dlp backend — writes only to /tmp, never to production storage.

Usage:
    python tests/test_ytdlp_backend.py
"""

import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fyp.tiktok_dl import save_tiktok, _info_to_row, _DEFAULTS


def test_info_to_row_with_mock_data():
    """Test field mapping with synthetic data — no network needed."""
    mock_info = {
        'id': '1234567890',
        'timestamp': 1700000000,
        'description': 'Test video #funny',
        'duration': 30,
        'uploader_id': '999',
        'uploader': 'testuser',
        'channel': 'Test Nickname',
        'track': 'Cool Song',
        'artist': 'Cool Artist',
        'album': 'Cool Album',
        'like_count': 100,
        'comment_count': 10,
        'view_count': 5000,
        'save_count': 50,
        'repost_count': 20,
    }

    df = _info_to_row(mock_info)

    # Check shape
    assert df.shape == (1, len(_DEFAULTS)), f"Expected {len(_DEFAULTS)} columns, got {df.shape[1]}"

    # Check column order matches _DEFAULTS
    assert list(df.columns) == list(_DEFAULTS.keys()), "Column order mismatch"

    # Check mapped values
    assert df.loc[0, 'item_id'] == '1234567890'
    assert df.loc[0, 'desc'] == 'Test video #funny'
    assert df.loc[0, 'video_duration'] == 30
    assert df.loc[0, 'author_id'] == '999'
    assert df.loc[0, 'author_uniqueId'] == 'testuser'
    assert df.loc[0, 'author_nickname'] == 'Test Nickname'
    assert df.loc[0, 'music_title'] == 'Cool Song'
    assert df.loc[0, 'music_authorName'] == 'Cool Artist'
    assert df.loc[0, 'music_album'] == 'Cool Album'
    assert df.loc[0, 'stats_diggCount'] == 100
    assert df.loc[0, 'stats_commentCount'] == 10
    assert df.loc[0, 'stats_playCount'] == 5000
    assert df.loc[0, 'stats_collectCount'] == 50
    assert df.loc[0, 'stats_shareCount'] == 20
    assert df.loc[0, 'video_downloaded'] == False

    # Check defaults for dropped fields
    assert df.loc[0, 'author_signature'] == ''
    assert df.loc[0, 'author_verified'] == False
    assert df.loc[0, 'poi_name'] == ''
    assert df.loc[0, 'IsAigc'] == False
    assert df.loc[0, 'isAd'] == False
    assert df.loc[0, 'anchors'] == ''

    print("PASS: _info_to_row produces correct DataFrame from mock data")


def test_info_to_row_with_missing_fields():
    """Test graceful handling of empty/missing fields."""
    mock_info = {'id': '111'}

    df = _info_to_row(mock_info)

    assert df.shape == (1, len(_DEFAULTS))
    assert df.loc[0, 'item_id'] == '111'
    assert df.loc[0, 'stats_diggCount'] == -1
    assert df.loc[0, 'video_duration'] == -1
    assert df.loc[0, 'desc'] == ''

    print("PASS: _info_to_row handles missing fields gracefully")


def test_save_tiktok_metadata_only():
    """Test metadata-only extraction (no download) against a real TikTok URL.

    Writes nothing to production storage — save_video=False.
    This test may fail if IP is blocked or video is unavailable.
    """
    test_url = "https://www.tiktok.com/@/video/7442671498102537474/"

    print(f"\nAttempting metadata-only extraction: {test_url}")
    print("(This may fail due to IP blocking — that's OK for a unit test)")

    df = save_tiktok(
        video_url=test_url,
        save_video=False,
        verbose=True,
    )

    if df.empty:
        print("SKIP: Could not extract metadata (likely IP blocked). Skipping live test.")
        return

    print(f"Extracted {len(df.columns)} columns for item_id={df.loc[0, 'item_id']}")
    assert df.shape == (1, len(_DEFAULTS)), f"Wrong shape: {df.shape}"
    assert df.loc[0, 'item_id'] != ''
    assert list(df.columns) == list(_DEFAULTS.keys())

    print("PASS: save_tiktok returns correct schema from live TikTok URL")


if __name__ == '__main__':
    test_info_to_row_with_mock_data()
    test_info_to_row_with_missing_fields()
    test_save_tiktok_metadata_only()
    print("\nAll tests done.")
