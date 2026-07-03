"""Verify source_platform stamping through the scrape pipeline.

Covers: BaseScraper.canonicalize_batch stamps the scraper's platform on every
row (string[pyarrow]); the consolidation backfill fills missing/NA values with
the default platform for pre-column history.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from fyp import scrape_contract as sc
from fyp.platform_scraper import get_scraper


def main() -> int:
    scraper = get_scraper("tiktok")

    raw = pd.DataFrame({
        "item_id": ["1", "2"],
        "createTime": pd.Series([pd.Timestamp("2026-01-01")] * 2, dtype="timestamp[ns][pyarrow]"),
        "stats_playCount": pd.Series([1000, 2000], dtype="int64[pyarrow]"),
    })
    out = scraper.canonicalize_batch(raw.copy(), status="ok")

    assert "source_platform" in out.columns, "canonicalize_batch must stamp source_platform"
    assert (out["source_platform"] == "tiktok").all(), out["source_platform"].tolist()
    # Same pyarrow-backed string dtype scrape_status uses (repr shows the storage)
    assert "pyarrow" in repr(out["source_platform"].dtype), repr(out["source_platform"].dtype)
    assert repr(out["source_platform"].dtype) == repr(out["scrape_status"].dtype)

    # The field is a base contract field, so ensure_base_columns knows it
    assert "source_platform" in scraper.base_columns

    # Consolidation backfill semantics: NA / missing → default platform
    default = sc.default_platform(sc.load_contract()) or "tiktok"
    legacy = pd.DataFrame({"item_id": ["9"]})
    if "source_platform" not in legacy.columns:
        legacy["source_platform"] = pd.NA
    legacy["source_platform"] = (
        legacy["source_platform"].fillna(default).astype("string[pyarrow]")
    )
    assert legacy["source_platform"].tolist() == [default]

    mixed = pd.concat([out, legacy], ignore_index=True)
    mixed["source_platform"] = mixed["source_platform"].fillna(default).astype("string[pyarrow]")
    assert mixed["source_platform"].notna().all()

    print("OK — source_platform stamping + backfill verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
