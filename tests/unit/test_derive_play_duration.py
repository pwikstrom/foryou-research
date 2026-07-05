#!/usr/bin/env python3
"""Tests for the shared forward-delta play_duration derivation (no I/O).

Covers the base forward-delta assignment, non-play NA, the same-item run
collapse onto the first play (incl. the extra_data annotation of non-lead
activity types), the cap, the last-row NA, and empty/single-row frames.
The semantics are ported unchanged from the original TikTok DDP block, so
these assertions pin the behaviour for every DDP platform.

Usage:
    python tests/unit/test_derive_play_duration.py
    pytest tests/unit/test_derive_play_duration.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from fyp.ingest import derive_play_duration


def _frame(seconds, activity_types, item_ids, extra_data=None):
    df = pd.DataFrame({
        "utc_timestamp": pd.to_datetime(seconds, unit="s", utc=True),
        "activity_type": pd.array(activity_types, dtype="string[pyarrow]"),
        "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
    })
    if extra_data is not None:
        df["extra_data"] = pd.array(extra_data, dtype="string[pyarrow]")
    return df




def test_forward_delta_on_plays():
    out = derive_play_duration(_frame([0, 12, 40], ["play", "play", "play"], ["a", "b", "c"]))
    assert out.loc[0, "play_duration"] == 12
    assert out.loc[1, "play_duration"] == 28
    # Last activity has no forward delta.
    assert pd.isna(out.loc[2, "play_duration"])
    assert str(out["play_duration"].dtype) == "int64[pyarrow]"
    print("PASS: forward delta assigned to plays; last row NA")




def test_non_play_rows_get_na():
    out = derive_play_duration(_frame([0, 10, 20], ["play", "search", "play"], ["a", None, "b"]))
    assert out.loc[0, "play_duration"] == 10
    assert pd.isna(out.loc[1, "play_duration"])
    print("PASS: non-play rows get NA")




def test_cap_yields_na():
    out = derive_play_duration(_frame([0, 601, 700], ["play", "play", "play"], ["a", "b", "c"]))
    assert pd.isna(out.loc[0, "play_duration"])  # 601 > 600
    assert out.loc[1, "play_duration"] == 99
    out2 = derive_play_duration(
        _frame([0, 50, 100], ["play", "play", "play"], ["a", "b", "c"]), cap_seconds=40
    )
    assert pd.isna(out2.loc[0, "play_duration"])
    print("PASS: durations above the cap become NA")




def test_same_item_run_collapses_onto_first_play():
    # play(x) at t=0, fave(x) at t=15, play(y) at t=25: the lead play gets
    # 15 + 10 = 25 (the fave's forward delta included) and the fave folds
    # into the lead's extra_data.
    out = derive_play_duration(_frame(
        [0, 15, 25, 30],
        ["play", "fave", "play", "play"],
        ["x", "x", "y", "z"],
    ))
    assert out.loc[0, "play_duration"] == 25
    assert pd.isna(out.loc[1, "play_duration"])
    assert out.loc[0, "extra_data"] == "fave"
    assert out.loc[2, "play_duration"] == 5
    print("PASS: same-item run collapses onto the first play with extra_data annotation")




def test_run_annotation_includes_payload():
    out = derive_play_duration(_frame(
        [0, 5, 20],
        ["play", "comment", "play"],
        ["x", "x", "y"],
        extra_data=[None, "nice,  video", None],
    ))
    assert out.loc[0, "play_duration"] == 20
    assert out.loc[0, "extra_data"] == "comment:nice video"
    print("PASS: run annotation carries the cleaned activity payload")




def test_run_without_play_gets_na():
    out = derive_play_duration(_frame(
        [0, 5, 20],
        ["fave", "comment", "play"],
        ["x", "x", "y"],
    ))
    assert pd.isna(out.loc[0, "play_duration"])
    assert pd.isna(out.loc[1, "play_duration"])
    print("PASS: same-item run without a play stays NA")




def test_empty_and_single_row():
    empty = derive_play_duration(_frame([], [], []))
    assert "play_duration" in empty.columns and len(empty) == 0

    single = derive_play_duration(_frame([0], ["play"], ["a"]))
    assert pd.isna(single.loc[0, "play_duration"])
    print("PASS: empty and single-row frames handled")




def test_missing_extra_data_column_is_added():
    out = derive_play_duration(_frame([0, 10], ["play", "play"], ["a", "b"]))
    assert "extra_data" in out.columns
    print("PASS: missing extra_data column added")




if __name__ == "__main__":
    test_forward_delta_on_plays()
    test_non_play_rows_get_na()
    test_cap_yields_na()
    test_same_item_run_collapses_onto_first_play()
    test_run_annotation_includes_payload()
    test_run_without_play_gets_na()
    test_empty_and_single_row()
    test_missing_extra_data_column_is_added()
    print("All derive_play_duration tests passed.")
