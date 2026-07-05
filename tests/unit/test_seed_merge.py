#!/usr/bin/env python3
"""Tests for the donated enrichment-seed fallback merge in consolidation.

Covers the anti-join precedence (a real scrape row beats a donated seed row),
the stamped flags on appended seed rows, and the donated-only / empty edge
cases. Pure-function tests — no I/O.

Usage:
    python tests/unit/test_seed_merge.py
    pytest tests/unit/test_seed_merge.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp.scrape import _merge_enrichment_seeds


def _real_rows() -> pd.DataFrame:
    return pd.DataFrame({
        "item_id": pd.Series(["A1", "B2"], dtype="string[pyarrow]"),
        "source_platform": pd.Series(["instagram", "youtube"], dtype="string[pyarrow]"),
        "scrape_status": pd.Series(["ok", "ok"], dtype="string[pyarrow]"),
        "video_downloaded": pd.Series([True, False], dtype="bool[pyarrow]"),
        "scraped_ok": pd.Series([True, True], dtype="bool[pyarrow]"),
        "desc": pd.Series(["real caption A", "real caption B"], dtype="string[pyarrow]"),
    })




def _seed_rows() -> pd.DataFrame:
    return pd.DataFrame({
        "item_id": pd.Series(["A1", "C3", None], dtype="string[pyarrow]"),
        "source_platform": pd.Series(["instagram", "instagram", "instagram"], dtype="string[pyarrow]"),
        "scrape_status": pd.Series(["donated"] * 3, dtype="string[pyarrow]"),
        "desc": pd.Series(["donated caption A", "donated caption C", "x"], dtype="string[pyarrow]"),
    })




def test_real_row_beats_donated():
    out = _merge_enrichment_seeds(_real_rows(), {"seed.parquet": _seed_rows()})
    a1 = out[(out["item_id"] == "A1") & (out["source_platform"] == "instagram")]
    assert len(a1) == 1
    assert a1.iloc[0]["scrape_status"] == "ok"
    assert a1.iloc[0]["desc"] == "real caption A"
    print("PASS: real scrape row beats donated seed")




def test_donated_only_appended_with_stamped_flags():
    out = _merge_enrichment_seeds(_real_rows(), {"seed.parquet": _seed_rows()})
    c3 = out[out["item_id"] == "C3"]
    assert len(c3) == 1
    row = c3.iloc[0]
    assert row["scrape_status"] == "donated"
    assert row["scraped_ok"] == False  # noqa: E712 — stays scrape-eligible
    assert row["video_downloaded"] == False  # noqa: E712
    assert row["storage_link"] == ""
    print("PASS: donated-only rows appended with scraped_ok/video_downloaded False")




def test_null_item_id_seeds_dropped():
    out = _merge_enrichment_seeds(_real_rows(), {"seed.parquet": _seed_rows()})
    assert out["item_id"].notna().all()
    assert len(out) == 3  # 2 real + C3
    print("PASS: null-item_id seed rows dropped")




def test_same_id_other_platform_survives():
    # The anti-join is composite: A1 on youtube is a different item than the
    # real instagram A1.
    seeds = _seed_rows().copy()
    seeds.loc[0, "source_platform"] = "youtube"
    out = _merge_enrichment_seeds(_real_rows(), {"seed.parquet": seeds})
    a1 = out[out["item_id"] == "A1"]
    assert len(a1) == 2
    assert set(a1["source_platform"]) == {"instagram", "youtube"}
    print("PASS: composite (source_platform, item_id) anti-join")




def test_no_seeds_is_identity():
    real = _real_rows()
    out = _merge_enrichment_seeds(real, {})
    assert out is real
    print("PASS: no seed frames → identity")




def test_empty_scrape_df_gets_all_seeds():
    empty = pd.DataFrame({
        "item_id": pd.Series([], dtype="string[pyarrow]"),
        "source_platform": pd.Series([], dtype="string[pyarrow]"),
        "video_downloaded": pd.Series([], dtype="bool[pyarrow]"),
    })
    out = _merge_enrichment_seeds(empty, {"seed.parquet": _seed_rows()})
    assert len(out) == 2  # A1 + C3 (null dropped)
    assert (out["scrape_status"] == "donated").all()
    print("PASS: empty scrape frame gets all non-null seeds")




if __name__ == "__main__":
    test_real_row_beats_donated()
    test_donated_only_appended_with_stamped_flags()
    test_null_item_id_seeds_dropped()
    test_same_id_other_platform_survives()
    test_no_seeds_is_identity()
    test_empty_scrape_df_gets_all_seeds()
    print("All seed-merge tests passed.")
