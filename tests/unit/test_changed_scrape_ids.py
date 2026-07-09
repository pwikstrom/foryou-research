"""Regression test: consolidation must flag re-scraped *value* changes, not
only brand-new item_ids.

Pinned 2026-07-06: ``consolidate_and_save_scrape_data`` computed the "changed"
item set as ``set(new["item_id"]) - existing_ids`` — a pure set-difference on
item_id. A re-scrape that backfilled the values of an item already consolidated
(e.g. an Instagram ``play_count`` going from the -1 sentinel to a real count)
kept the same item_id, so it fell out of that difference. The consolidation
impact analysis then never listed the studies that item belonged to, the
auto-refresh reported "No cached files needed refreshing", and Explore kept the
stale count-less values until a manual forced study recode. Fixed by diffing the
actual row values (``_compute_changed_scrape_ids``), excluding only the
backstage/provenance columns that change on every scrape.

Usage:
    python tests/unit/test_changed_scrape_ids.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from fyp.scrape import _compute_changed_scrape_ids


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["item_id"] = df["item_id"].astype("string[pyarrow]")
    if "source_platform" in df:
        df["source_platform"] = df["source_platform"].astype("string[pyarrow]")
    if "play_count" in df:
        df["play_count"] = df["play_count"].astype("int64[pyarrow]")
    if "video_downloaded" in df:
        df["video_downloaded"] = df["video_downloaded"].astype("bool[pyarrow]")
    return df


def test_rescrape_value_backfill_is_flagged() -> None:
    existing = _frame([
        {"item_id": "A", "source_platform": "instagram", "play_count": -1,
         "video_downloaded": False, "scrape_ts": "2026-07-01", "desc": "cats"},
        {"item_id": "B", "source_platform": "instagram", "play_count": 500,
         "video_downloaded": True, "scrape_ts": "2026-07-01", "desc": "dogs"},
    ])
    new = _frame([
        # A re-scraped: real play_count now, newer scrape_ts.
        {"item_id": "A", "source_platform": "instagram", "play_count": 12345,
         "video_downloaded": False, "scrape_ts": "2026-07-06", "desc": "cats"},
        # B unchanged.
        {"item_id": "B", "source_platform": "instagram", "play_count": 500,
         "video_downloaded": True, "scrape_ts": "2026-07-01", "desc": "dogs"},
        # C brand new.
        {"item_id": "C", "source_platform": "instagram", "play_count": 9,
         "video_downloaded": True, "scrape_ts": "2026-07-06", "desc": "birds"},
    ])
    assert _compute_changed_scrape_ids(existing, new) == {"A", "C"}


def test_provenance_only_change_is_ignored() -> None:
    # scrape_ts / scrape_contract_version / storage_link change on every scrape
    # without touching an analysis variable — they must not flag the item.
    existing = _frame([
        {"item_id": "A", "source_platform": "tiktok", "play_count": 100,
         "video_downloaded": True, "scrape_ts": "2026-07-01",
         "scrape_contract_version": "sv_old", "storage_link": "a.mp4", "desc": "x"},
    ])
    new = _frame([
        {"item_id": "A", "source_platform": "tiktok", "play_count": 100,
         "video_downloaded": True, "scrape_ts": "2026-07-06",
         "scrape_contract_version": "sv_new", "storage_link": "tiktok/a.mp4", "desc": "x"},
    ])
    assert _compute_changed_scrape_ids(existing, new) == set()


def test_first_consolidation_flags_all() -> None:
    new = _frame([
        {"item_id": "A", "source_platform": "tiktok", "play_count": 1},
        {"item_id": "B", "source_platform": "tiktok", "play_count": 2},
    ])
    assert _compute_changed_scrape_ids(None, new) == {"A", "B"}


def test_na_to_value_and_value_to_na_are_flagged() -> None:
    existing = pd.DataFrame({
        "item_id": pd.array(["A", "B"], dtype="string[pyarrow]"),
        "play_count": pd.array([pd.NA, 5], dtype="int64[pyarrow]"),
    })
    new = pd.DataFrame({
        "item_id": pd.array(["A", "B"], dtype="string[pyarrow]"),
        "play_count": pd.array([777, pd.NA], dtype="int64[pyarrow]"),
    })
    assert _compute_changed_scrape_ids(existing, new) == {"A", "B"}


def test_identical_frame_flags_nothing() -> None:
    df = _frame([
        {"item_id": "A", "source_platform": "tiktok", "play_count": 100,
         "video_downloaded": True, "scrape_ts": "2026-07-01", "desc": "x"},
        {"item_id": "B", "source_platform": "youtube", "play_count": 9,
         "video_downloaded": False, "scrape_ts": "2026-07-02", "desc": "y"},
    ])
    assert _compute_changed_scrape_ids(df, df.copy()) == set()


def test_column_set_change_flags_all():
    """A contract migration (renamed/coalesced columns) marks every item changed.

    Signatures cover only the column intersection, so a pure schema change would
    otherwise diff as 'nothing changed' and studies would never gain the new
    columns.
    """
    import pandas as pd
    old = pd.DataFrame([
        {"item_id": "A", "play_count": 5, "stats_diggCount": 2, "desc": "x"},
        {"item_id": "B", "play_count": 9, "stats_diggCount": 4, "desc": "y"},
    ])
    new = pd.DataFrame([
        {"item_id": "A", "play_count": 5, "fave_count": 2, "desc": "x"},
        {"item_id": "B", "play_count": 9, "fave_count": 4, "desc": "y"},
    ])
    assert _compute_changed_scrape_ids(old, new) == {"A", "B"}
    # A provenance-only column difference is NOT a schema change.
    with_prov = new.copy()
    with_prov["scrape_contract_version"] = "sv_x"
    assert _compute_changed_scrape_ids(new, with_prov) == set()


_TESTS = [
    test_rescrape_value_backfill_is_flagged,
    test_provenance_only_change_is_ignored,
    test_first_consolidation_flags_all,
    test_na_to_value_and_value_to_na_are_flagged,
    test_identical_frame_flags_nothing,
    test_column_set_change_flags_all,
]


def _main() -> int:
    passed = 0
    for test in _TESTS:
        try:
            test()
            print(f"PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{passed}/{len(_TESTS)} passed")
    return 0 if passed == len(_TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
