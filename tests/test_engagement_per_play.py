"""Unit test for BaseScraper per-K engagement-rate derivation + the contract map.

The retired ``fyp.scrape._engagement_per_play`` (per-play) is replaced by
``BaseScraper.derive_engagement_rates`` (per-1K-play), with the rate → raw-count
mapping sourced from ``[perk.<platform>]`` in the scrape contract.
"""

import math

import pandas as pd

from fyp import scrape_contract as sc
from fyp.platform_scraper import get_scraper




def test_derive_engagement_rates() -> None:
    """Verify per-K derivation handles the -1 sentinel, zero/negative plays, dtype."""
    scraper = get_scraper()
    df = pd.DataFrame({
        "play_count": pd.Series([1000, 0, -1, 500, 200], dtype="int64[pyarrow]"),
        "stats_commentCount": pd.Series([10, 5, 5, -1, 50], dtype="int64[pyarrow]"),
    })

    result = scraper.derive_engagement_rates(df.copy())["comments_per_K_play"]

    assert str(result.dtype) == "double[pyarrow]", f"unexpected dtype {result.dtype}"
    # Row 0: 10 / 1000 * 1000 = 10.0 (normal)
    assert math.isclose(float(result.iloc[0]), 10.0), result.iloc[0]
    # Row 1: plays == 0 -> NA
    assert pd.isna(result.iloc[1]), "zero plays should be NA"
    # Row 2: plays == -1 (sentinel) -> NA
    assert pd.isna(result.iloc[2]), "sentinel plays should be NA"
    # Row 3: numerator == -1 (sentinel) -> NA even though plays valid
    assert pd.isna(result.iloc[3]), "sentinel numerator should be NA"
    # Row 4: 50 / 200 * 1000 = 250.0 (normal)
    assert math.isclose(float(result.iloc[4]), 250.0), result.iloc[4]

    print("test_derive_engagement_rates PASSED")




def test_perk_mapping() -> None:
    """The per-K rate fields map to the expected raw TikTok counts."""
    contract = sc.load_contract()
    assert sc.per_k_sources(contract, "tiktok") == {
        "faves_per_K_play": "stats_diggCount",
        "comments_per_K_play": "stats_commentCount",
        "shares_per_K_play": "stats_shareCount",
        "saves_per_K_play": "stats_collectCount",
    }
    print("test_perk_mapping PASSED")




if __name__ == "__main__":
    test_derive_engagement_rates()
    test_perk_mapping()
    print("All tests passed.")
