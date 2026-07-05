#!/usr/bin/env python3
"""Tests for the multi-platform scrape contract (instagram + youtube blocks).

Usage:
    python tests/unit/test_scrape_contract_platforms.py
    pytest tests/unit/test_scrape_contract_platforms.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fyp import scrape_contract as sc


def test_contract_platforms_and_fields():
    contract = sc.load_contract()

    assert set(sc.platforms(contract)) == {"tiktok", "instagram", "youtube"}
    assert sc.default_platform(contract) == "tiktok"

    base = sc.base_field_names(contract)
    assert "video_downloaded" in base, "video_downloaded must be base-scoped now"

    assert sc.per_k_sources(contract, "instagram") == {
        "faves_per_K_play": "ig_like_count",
        "comments_per_K_play": "ig_comment_count",
    }
    assert sc.per_k_sources(contract, "youtube") == {
        "faves_per_K_play": "yt_like_count",
        "comments_per_K_play": "yt_comment_count",
    }

    ig = sc.field_dtypes(contract, "instagram")
    for col in ("ig_like_count", "ig_comment_count", "ig_author_handle"):
        assert col in ig, f"missing instagram field {col}"

    yt = sc.field_dtypes(contract, "youtube")
    for col in ("yt_like_count", "yt_comment_count", "yt_channel_follower_count",
                "yt_categories", "yt_author_handle"):
        assert col in yt, f"missing youtube field {col}"

    # image_list stays TikTok-scoped (no Instagram carousel support yet).
    assert "image_list" not in ig
    print("PASS: scrape contract registers instagram + youtube correctly")




if __name__ == "__main__":
    test_contract_platforms_and_fields()
    print("All scrape-contract platform tests passed.")
