"""Unit tests for the scrape contract loader and the platform-scraper base class.

Cost-free, no network: exercises the declarative contract
(``config/scrape_contract.toml``), the canonical field set / dtypes, the per-K
engagement + plays-per-day derivations, and the registry/factory.
"""

import pandas as pd

from fyp import scrape_contract as sc
from fyp.platform_scraper import BaseScraper, get_scraper




def test_contract_loads_and_validates() -> None:
    """The shipped contract parses and validates with no errors."""
    contract = sc.load_contract()
    assert sc.validate_contract(contract) == []
    assert sc.default_platform(contract) == "tiktok"
    print("test_contract_loads_and_validates PASSED")




def test_base_and_platform_field_split() -> None:
    """field_dtypes returns the 14 base fields, plus the TikTok platform set."""
    contract = sc.load_contract()
    base = sc.field_dtypes(contract)
    assert len(base) == 14, base
    for expected in ("scrape_status", "storage_link", "scrape_ts", "create_time",
                     "play_count", "comments_per_K_play", "plays_per_day", "author_name"):
        assert expected in base, expected
    full = sc.field_dtypes(contract, "tiktok")
    assert set(base).issubset(full)
    assert "stats_diggCount" in full and "video_downloaded" in full
    print("test_base_and_platform_field_split PASSED")




def test_get_scraper_registry() -> None:
    """get_scraper resolves the registered TikTok subclass; unknown raises."""
    scraper = get_scraper()
    assert isinstance(scraper, BaseScraper)
    assert scraper.platform == "tiktok"
    assert len(scraper.base_columns) == 14
    try:
        get_scraper("no_such_platform")
    except ValueError:
        pass
    else:
        raise AssertionError("get_scraper should raise for an unknown platform")
    print("test_get_scraper_registry PASSED")




def test_canonicalize_batch_renames_and_derives() -> None:
    """A raw frame is renamed to canonical names with per-K + plays_per_day filled."""
    scraper = get_scraper()
    raw = pd.DataFrame([{
        "item_id": "1", "stats_playCount": 1000, "stats_diggCount": 50,
        "stats_commentCount": 10, "stats_shareCount": 5, "stats_collectCount": 2,
        "createTime": pd.Timestamp("2025-01-01"), "last_modified": pd.Timestamp("2025-01-11"),
        "video_duration": 30, "author_nickname": "bob", "desc": "hi", "author_id": "a",
    }])
    out = scraper.canonicalize_batch(raw.copy(), status="ok")

    # renames applied, legacy names gone
    for legacy in ("stats_playCount", "createTime", "video_duration", "author_nickname", "last_modified"):
        assert legacy not in out.columns, legacy
    assert out["play_count"].iloc[0] == 1000
    assert out["author_name"].iloc[0] == "bob"
    assert out["scrape_status"].iloc[0] == "ok"
    # per-K = count / plays * 1000
    assert float(out["faves_per_K_play"].iloc[0]) == 50.0
    assert float(out["comments_per_K_play"].iloc[0]) == 10.0
    # plays_per_day = 1000 / 10 days
    assert float(out["plays_per_day"].iloc[0]) == 100.0
    # every base column present
    assert all(c in out.columns for c in scraper.base_columns)
    print("test_canonicalize_batch_renames_and_derives PASSED")




def test_overflow_repair() -> None:
    """A signed-32-bit-wrapped count is recovered by adding 2**32."""
    scraper = get_scraper()
    df = pd.DataFrame([{"item_id": "x", "play_count": -1000000, "stats_diggCount": -5}])
    out = scraper.repair_counts(df.copy())
    assert int(out["play_count"].iloc[0]) == (1 << 32) - 1000000
    # the -1/-5 sentinels below -1 are repaired too; -1 itself would be left alone
    print("test_overflow_repair PASSED")




if __name__ == "__main__":
    test_contract_loads_and_validates()
    test_base_and_platform_field_split()
    test_get_scraper_registry()
    test_canonicalize_batch_renames_and_derives()
    test_overflow_repair()
    print("All tests passed.")
