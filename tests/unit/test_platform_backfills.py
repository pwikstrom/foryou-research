#!/usr/bin/env python3
"""Tests for the source_platform / play_duration self-heal backfills (no I/O).

Covers the merge-time source_platform guard in fyp.organize_datasets (pre-column
NA rows get the default platform so the composite activity↔enrichment join
matches), and the ingest-side self-heals on ForYouCollection (persisted-parquet
healing of source_platform and the IG/YT play_duration recompute).

Usage:
    python tests/unit/test_platform_backfills.py
    pytest tests/unit/test_platform_backfills.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp import ingest
from fyp.organize_datasets import _backfill_source_platform


def test_merge_guard_fills_default_platform():
    s = pd.Series(["youtube", None, "instagram", None], dtype="string[pyarrow]")
    out = _backfill_source_platform(s)
    assert out.isna().sum() == 0
    assert out.tolist() == ["youtube", "tiktok", "instagram", "tiktok"]
    # No-NA input passes through unchanged (same object, no copy).
    clean = pd.Series(["tiktok"], dtype="string[pyarrow]")
    assert _backfill_source_platform(clean) is clean
    print("PASS: merge-time guard fills NA source_platform with the default platform")




def _bare_collection(df: pd.DataFrame) -> ingest.ForYouCollection:
    """A ForYouCollection with only .data set — the backfills touch nothing else."""
    col = object.__new__(ingest.ForYouCollection)
    col.data = df
    col.verbose = False
    return col




def test_ingest_source_platform_self_heal():
    col = _bare_collection(pd.DataFrame({
        "item_id": pd.array(["1", "2"], dtype="string[pyarrow]"),
        "source_platform": pd.array([None, "youtube"], dtype="string[pyarrow]"),
    }))
    col._backfill_source_platform()
    assert col.data["source_platform"].tolist() == ["tiktok", "youtube"]
    assert "string" in str(col.data["source_platform"].dtype)
    print("PASS: ingest self-heal fills NA source_platform")




def test_ingest_play_duration_self_heal():
    ts = pd.to_datetime([0, 20, 50, 0, 30], unit="s", utc=True)
    df = pd.DataFrame({
        "utc_timestamp": ts,
        "activity_type": pd.array(["play", "play", "play", "play", "play"], dtype="string[pyarrow]"),
        "item_id": pd.array(["a", "b", "c", "d", "e"], dtype="string[pyarrow]"),
        "source_platform": pd.array(["youtube"] * 3 + ["tiktok"] * 2, dtype="string[pyarrow]"),
        "raw_file": pd.array(["yt.zip"] * 3 + ["tt.json"] * 2, dtype="string[pyarrow]"),
        "extra_data": pd.array([None] * 5, dtype="string[pyarrow]"),
        # TikTok rows already carry values — they must be left untouched;
        # YouTube rows are all-NA → recomputed.
        "play_duration": pd.array([None, None, None, 123, None], dtype="int64[pyarrow]"),
    })
    col = _bare_collection(df.copy())
    col._backfill_play_duration()

    yt = col.data[col.data["source_platform"] == "youtube"]
    assert yt["play_duration"].tolist()[:2] == [20, 30]
    assert pd.isna(yt["play_duration"].iloc[2])  # last row, no forward delta

    tt = col.data[col.data["source_platform"] == "tiktok"]
    assert tt["play_duration"].tolist()[0] == 123  # untouched

    # Idempotent: a second run changes nothing (youtube now has non-NA plays).
    before = col.data["play_duration"].tolist()
    col._backfill_play_duration()
    assert col.data["play_duration"].tolist() == before
    print("PASS: ingest self-heal recomputes play_duration for all-NA platforms only")




def test_ingest_play_duration_self_heal_missing_column():
    df = pd.DataFrame({
        "utc_timestamp": pd.to_datetime([0, 15], unit="s", utc=True),
        "activity_type": pd.array(["play", "play"], dtype="string[pyarrow]"),
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "source_platform": pd.array(["instagram", "instagram"], dtype="string[pyarrow]"),
        "raw_file": pd.array(["ig.zip", "ig.zip"], dtype="string[pyarrow]"),
    })
    col = _bare_collection(df)
    col._backfill_play_duration()
    assert col.data["play_duration"].tolist()[0] == 15
    assert str(col.data["play_duration"].dtype) == "int64[pyarrow]"
    print("PASS: ingest self-heal creates the play_duration column when absent")




if __name__ == "__main__":
    test_merge_guard_fills_default_platform()
    test_ingest_source_platform_self_heal()
    test_ingest_play_duration_self_heal()
    test_ingest_play_duration_self_heal_missing_column()
    print("All platform-backfill tests passed.")
