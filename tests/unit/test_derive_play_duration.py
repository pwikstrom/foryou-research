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




def test_fallback_fold_links_distant_engagement():
    # A fave a week after the (only logged) play of the same item still folds
    # into that play's extra_data — the IG videos_watched / liked_posts case.
    out = derive_play_duration(_frame(
        [0, 20, 7 * 86400], ["play", "play", "fave"], ["a", "b", "a"],
    ))
    assert out.loc[0, "extra_data"] == "fave"
    assert pd.isna(out.loc[1, "extra_data"])
    # play_duration stays adjacency-based: the distant fave adds no dwell.
    assert out.loc[0, "play_duration"] == 20
    print("PASS: fallback fold links distant same-item engagement")




def test_fallback_fold_picks_nearest_play():
    # The item was played twice; a later non-adjacent fave folds onto the
    # nearer of the two plays.
    out = derive_play_duration(_frame(
        [0, 100000, 100200, 100500],
        ["play", "play", "play", "fave"],
        ["a", "a", "b", "a"],
    ))
    # Rows 0-1 are an adjacency run (rewatch): the second play's type is
    # annotated on the lead play, as before.
    assert out.loc[0, "extra_data"] == "play"
    # The fave folds onto row 1 — the play of "a" nearest in time.
    assert out.loc[1, "extra_data"] == "fave"
    print("PASS: fallback fold picks the nearest play of the item")




def test_fallback_fold_carries_comment_payload():
    out = derive_play_duration(_frame(
        [0, 100, 5000], ["play", "play", "comment"], ["a", "b", "a"],
        extra_data=[None, None, "nice, one"],
    ))
    assert out.loc[0, "extra_data"] == "comment:nice one"
    print("PASS: fallback fold carries the cleaned comment payload")




def test_fallback_fold_without_matching_play_is_noop():
    out = derive_play_duration(_frame(
        [0, 3600], ["play", "fave"], ["a", "z"],
    ))
    assert pd.isna(out.loc[0, "extra_data"])
    assert pd.isna(out.loc[1, "extra_data"])
    print("PASS: engagement without a matching play stays standalone")




if __name__ == "__main__":
    test_forward_delta_on_plays()
    test_non_play_rows_get_na()
    test_cap_yields_na()
    test_same_item_run_collapses_onto_first_play()
    test_run_annotation_includes_payload()
    test_run_without_play_gets_na()
    test_empty_and_single_row()
    test_missing_extra_data_column_is_added()
    test_fallback_fold_links_distant_engagement()
    test_fallback_fold_picks_nearest_play()
    test_fallback_fold_without_matching_play_is_noop()
    test_fallback_fold_carries_comment_payload()
    print("All derive_play_duration tests passed.")
