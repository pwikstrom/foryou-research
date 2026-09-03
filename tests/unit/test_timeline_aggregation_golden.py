"""aggregate_timeline_frame must equal a plain-Python reference, cell for cell.

2026-09-03: the list-variable block of the timeline aggregation was rewritten
from "explode twice, unstack to a days × unique-tags matrix, json.dumps over
every cell" to "one explode carrying the weight, long-format groupby, JSON from
the cells that exist" — 5× faster on a 150k-row synthetic collection with 40k
distinct tags, and no longer growing with tag cardinality. The batch worker was
also moved onto a forked process pool, which needs the aggregation to be a pure
function of its inputs. This file pins both: the per-day counts, weighted
counts, valid counts and weighted-valid totals for numeric, categorical and
list variables are compared against loops anyone can read.

Edge cases covered: numpy-array lists, empty lists, None inside a list, NaN
rows, rows the universe filter drops (scrape/annotation failed, missing
play_duration), 'observe' rows weighted by video duration, and a list
variable with no tags at all (the '{}' fallback).

Usage:
    python -m pytest tests/unit/test_timeline_aggregation_golden.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd
import pytest

from web_interface.services import timeline_service as ts


def _frame() -> pd.DataFrame:
    rows = [
        # local_date, activity, play_dur, duration, scraped_ok, scraped_fail, annotated_ok, score, cat, tags, extra
        ("2025-01-01", "play",     10.0, 30.0, True,  False, True,  1.0, "a", ["x", "y"],          "fave"),
        ("2025-01-01", "play",     40.0, 30.0, True,  False, True,  3.0, "b", np.array(["y"]),     None),
        ("2025-01-01", "play",      5.0, 30.0, True,  False, True,  np.nan, "a", [],               "fave,comment:hi"),
        ("2025-01-01", "play",      5.0, 30.0, True,  False, False, 9.0, "z", ["dropped"],         None),   # MA failed → out
        ("2025-01-02", "observe",  np.nan, 20.0, True, False, True, 2.0, "b", ["x", None, "z"],    None),   # weight = duration
        ("2025-01-02", "play",     np.nan, 30.0, True, False, True, 2.0, "a", ["x"],               None),   # no play_dur → out
        ("2025-01-02", "play",      0.0, 30.0, True,  False, True,  4.0, None, None,               "follow:acct"),
        ("2025-01-03", "play",     12.0, 10.0, True,  False, True,  6.0, "c", ["y", "y"],          None),   # capped at duration
        ("2025-01-03", "like",     12.0, 10.0, True,  False, True,  6.0, "c", ["nope"],            None),   # not a play → out
        ("2025-01-03", "play",      7.0, 10.0, False, True,  np.nan, 1.0, "c", ["nope"],           None),   # scrape failed → out
        # A scroll-past: kept by the universe filter with weight 0. Its tag
        # "ghost" is counted but must be ABSENT from the weighted dict — the
        # 2026-09-03 real-data check found the long-format rewrite emitting it
        # as 0.0 where the matrix form had dropped it.
        ("2025-01-04", "play",      0.0, 30.0, True,  False, True,  2.0, "d", ["ghost"],           None),
    ]
    df = pd.DataFrame(rows, columns=[
        "local_date", "activity_type", "play_duration", "duration", "scraped_ok",
        "scraped_fail", "annotated_ok", "score", "cat", "tags", "extra_data"])
    df["notags"] = [[] for _ in range(len(df))]
    return df


VIZ = ["score", "cat", "tags", "notags"]


def _reference(df: pd.DataFrame) -> dict:
    """The aggregation in loops: universe filter, weights, per-day tallies."""
    kept = []
    for r in df.itertuples(index=False):
        is_play = r.activity_type in ("play", "observe")
        dur_ok = (not pd.isna(r.play_duration)) or (
            r.activity_type == "observe" and not pd.isna(r.duration) and r.duration > 0)
        if not (is_play and dur_ok and r.scraped_ok is True and r.annotated_ok is True):
            continue
        if r.activity_type == "observe":
            w = float(r.duration)
        else:
            w = float(min(r.play_duration, r.duration)) if not pd.isna(r.duration) else float(r.play_duration)
        kept.append((r.local_date, w, r))

    out: dict = defaultdict(dict)
    for day, w, r in kept:
        d = out[day]
        d["video_count"] = d.get("video_count", 0) + 1
        d["weighted_video_total"] = d.get("weighted_video_total", 0.0) + w
        if not pd.isna(r.score):
            d["score_num"] = d.get("score_num", 0.0) + r.score * w
            d["score_den"] = d.get("score_den", 0.0) + w
            d["score_valid"] = d.get("score_valid", 0) + 1
        if r.cat is not None:
            d["cat_valid"] = d.get("cat_valid", 0) + 1
            d.setdefault("cat_counts", defaultdict(int))[r.cat] += 1
            d.setdefault("cat_w", defaultdict(float))[r.cat] += w
        tags = list(r.tags) if isinstance(r.tags, (list, np.ndarray)) else []
        if tags:
            d["tags_valid"] = d.get("tags_valid", 0) + 1
            d["tags_wvalid"] = d.get("tags_wvalid", 0.0) + w
            for t in tags:
                if t is None:
                    continue
                d.setdefault("tags_counts", defaultdict(int))[t] += 1
                d.setdefault("tags_w", defaultdict(float))[t] += w
    # Weighted dicts list only cells with positive weight (the `if v > 0` of
    # the original matrix form), for categorical and list variables alike.
    for d in out.values():
        for key in ("cat_w", "tags_w"):
            if key in d:
                d[key] = {k: v for k, v in d[key].items() if v > 0}
    return out


def test_aggregation_matches_the_reference():
    df = _frame()
    ref = _reference(df)
    agg = ts.aggregate_timeline_frame(df, VIZ, collection_id="t")
    assert agg is not None
    assert list(agg["period"]) == sorted(ref), "one row per day with kept plays, sorted"

    for _, row in agg.iterrows():
        d = ref[row["period"]]
        assert row["video_count"] == d["video_count"]
        assert row["weighted_video_total"] == pytest.approx(d["weighted_video_total"])
        # numeric: weighted mean and counts
        if "score_den" in d:
            if d["score_den"] > 0:
                assert row["score_val"] == pytest.approx(d["score_num"] / d["score_den"])
            else:
                # only weightless plays carried a value: no weighted mean
                assert pd.isna(row["score_val"])
            assert row["score_valid"] == d["score_valid"]
            assert row["score_weighted_valid"] == pytest.approx(d["score_den"])
        # categorical
        assert row["cat_valid"] == d.get("cat_valid", 0)
        assert json.loads(row["cat_counts"]) == dict(d.get("cat_counts", {}))
        assert json.loads(row["cat_weighted_counts"]) == {
            k: round(v, 2) for k, v in d.get("cat_w", {}).items()}
        # list
        assert row["tags_valid"] == d.get("tags_valid", 0)
        if d.get("tags_valid"):
            assert row["tags_weighted_valid"] == pytest.approx(d["tags_wvalid"])
            assert json.loads(row["tags_counts"]) == dict(d["tags_counts"])
            assert json.loads(row["tags_weighted_counts"]) == {
                k: round(v, 2) for k, v in d["tags_w"].items()}
        else:
            assert pd.isna(row["tags_counts"])
        if row["period"] == "2025-01-04":
            # the scroll-past day: counted, weightless
            assert json.loads(row["tags_counts"]) == {"ghost": 1}
            assert json.loads(row["tags_weighted_counts"]) == {}
            assert row["tags_weighted_valid"] == 0
        # a list var with no tags anywhere falls back to '{}'
        assert row["notags_counts"] == "{}"
        assert row["notags_weighted_counts"] == "{}"
        assert row["notags_valid"] == 0
    assert (agg["timeline_universe"] == "annotated_plays").all()


def test_aggregation_is_pure():
    """The caller's frame is left untouched; the same input gives the same output."""
    df = _frame()
    before = df.copy()
    a = ts.aggregate_timeline_frame(df, VIZ)
    b = ts.aggregate_timeline_frame(df, VIZ)
    pd.testing.assert_frame_equal(df, before)
    pd.testing.assert_frame_equal(a, b)


def test_nothing_survives_the_universe_filter_returns_none():
    df = _frame()
    df["annotated_ok"] = False
    assert ts.aggregate_timeline_frame(df, VIZ) is None


def test_missing_date_column_returns_none():
    assert ts.aggregate_timeline_frame(_frame().drop(columns=["local_date"]), VIZ) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
