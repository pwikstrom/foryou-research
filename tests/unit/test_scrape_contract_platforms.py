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

    # Explicit [meta].platforms — Instagram owns no platform-scoped fields
    # anymore (all its columns map to generic base names) but must stay
    # registered for its queue/worker/version-descriptor.
    assert set(sc.platforms(contract)) == {"tiktok", "instagram", "youtube"}
    assert sc.default_platform(contract) == "tiktok"

    base = sc.base_field_names(contract)
    assert "video_downloaded" in base, "video_downloaded must be base-scoped now"

    # Generic popularity counts + author handle are base fields.
    for col in ("fave_count", "comment_count", "share_count", "save_count", "author_handle"):
        assert col in base, f"missing generic base field {col}"

    # The flat [perk] table maps every rate to its generic count.
    assert sc.per_k_sources(contract) == {
        "faves_per_K_play": "fave_count",
        "comments_per_K_play": "comment_count",
        "shares_per_K_play": "share_count",
        "saves_per_K_play": "save_count",
    }

    # The retired per-platform names are gone from the contract...
    field_names = {f["name"] for f in contract.get("fields", [])}
    assert not set(sc.RETIRED_TO_GENERIC) & field_names, "retired fields must not be contract fields"
    # ...and every retirement target is a base field.
    assert set(sc.RETIRED_TO_GENERIC.values()) <= set(base)

    yt = sc.field_dtypes(contract, "youtube")
    for col in ("yt_channel_follower_count", "yt_categories"):
        assert col in yt, f"missing youtube field {col}"

    # image_list stays TikTok-scoped (no Instagram carousel support yet).
    ig = sc.field_dtypes(contract, "instagram")
    assert "image_list" not in ig
    print("PASS: scrape contract registers instagram + youtube correctly")




if __name__ == "__main__":
    test_contract_platforms_and_fields()
    print("All scrape-contract platform tests passed.")
