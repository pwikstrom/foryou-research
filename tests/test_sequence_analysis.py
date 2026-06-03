"""Tests for fyp.sequence_analysis.

Synthetic tests verify the windowing / session / lift logic against hand-computed
expectations (no data dependency). The real-data smoke test runs the full
pipeline on a cached study if one is available, and is skipped otherwise.

Run standalone:  python tests/test_sequence_analysis.py
Run via pytest:  pytest tests/test_sequence_analysis.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyp import sequence_analysis as sa


REAL_STUDY = "ABC Verify 2026"


def _make_events(collection_id, base_ts, dwells, categories, gaps=None, activity="play"):
    """Build a small per-collection event frame with evenly/customly spaced plays."""
    n = len(dwells)
    gaps = gaps if gaps is not None else [10] * (n - 1)
    ts = [base_ts]
    for g in gaps:
        ts.append(ts[-1] + g)
    return pd.DataFrame(
        {
            "collection_id": [collection_id] * n,
            "utc_timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert(None),
            "activity_type": [activity] * n,
            "play_duration": dwells,
            "completion_rate": [0.5] * n,
            "political_score": [50.0] * n,
            "sensitivity_score": [10.0] * n,
            "content_category": categories,
        }
    )




def test_sequence_index_and_session_split():
    """feed_position is monotonic per participant; a >SESSION_GAP_S gap splits sessions."""
    # Two events close together, then a 5-minute gap, then two more.
    df = _make_events(
        "A", 1000, [5, 5, 5, 5],
        [["comedy"]] * 4,
        gaps=[10, sa.SESSION_GAP_S + 60, 10],
    )
    indexed = sa.add_sequence_index(df)
    assert list(indexed["feed_position"]) == [0, 1, 2, 3]
    # The big gap starts a new session.
    assert list(indexed["session_id"]) == [0, 0, 1, 1]
    assert list(indexed["session_position"]) == [0, 1, 0, 1]




def test_windows_drop_partial_and_aggregate():
    """A 3-event session with window_n=2 yields exactly one full window."""
    df = _make_events("A", 0, [4, 8, 99], [["news"], ["news"], ["comedy"]])
    indexed = sa.add_sequence_index(df)
    specs = sa.classify_targets(df, requested=["content_category"])
    windows, _ = sa.build_windows(indexed, specs, window_n=2)
    assert len(windows) == 1  # trailing partial (1 event) dropped
    row = windows.iloc[0]
    assert row["n_videos"] == 2
    assert row["dwell_mean"] == 6.0  # mean(4, 8)
    news_col = sa._share_col("content_category", "news")
    assert news_col in windows.columns
    assert row[news_col] == 1.0  # both windowed videos are news
    # 'comedy' was only on the dropped partial-window row, so it isn't in vocab.
    assert sa._share_col("content_category", "comedy") not in windows.columns




def test_multilabel_category_share():
    """A video tagged with two categories contributes to both shares."""
    df = _make_events("A", 0, [5, 5], [["comedy", "news"], ["comedy"]])
    indexed = sa.add_sequence_index(df)
    specs = sa.classify_targets(df, requested=["content_category"])
    windows, _ = sa.build_windows(indexed, specs, window_n=2)
    row = windows.iloc[0]
    assert row[sa._share_col("content_category", "comedy")] == 1.0  # both videos comedy
    assert row[sa._share_col("content_category", "news")] == 0.5     # one of two videos news




def test_dichotomous_target_proportion():
    """A Y/N target reduces to the window's proportion of 'yes'."""
    df = _make_events("A", 0, [5, 5, 5, 5], [["news"]] * 4)
    df["aigc"] = ["yes", "no", "yes", "yes"]
    indexed = sa.add_sequence_index(df)
    specs = sa.classify_targets(df, requested=["aigc"])
    assert specs and specs[0]["kind"] == "share"
    windows, _ = sa.build_windows(indexed, specs, window_n=2)
    # window 0 = [yes, no] → 0.5 yes; window 1 = [yes, yes] → 1.0 yes
    yes_col = sa._share_col("aigc", "yes")
    assert sorted(windows[yes_col].tolist()) == [0.5, 1.0]




def test_scalar_target_window_mean():
    """A numeric target reduces to the window mean, and is classified scalar."""
    df = _make_events("A", 0, [5, 5, 5, 5], [["news"]] * 4)
    df["faces_age_estimate"] = [20.0, 30.0, 40.0, 60.0]
    indexed = sa.add_sequence_index(df)
    specs = sa.classify_targets(df, requested=["faces_age_estimate"])
    assert specs and specs[0]["kind"] == "scalar"
    windows, tidx = sa.build_windows(indexed, specs, window_n=2)
    mcol = tidx["faces_age_estimate"]["mean_column"]
    assert sorted(windows[mcol].tolist()) == [25.0, 50.0]  # mean(20,30), mean(40,60)




def test_dwell_predictor_columns_barred_as_targets():
    """play_duration / completion_rate can never be selected as targets."""
    df = _make_events("A", 0, [5, 5], [["news"], ["news"]])
    specs = sa.classify_targets(df, requested=["play_duration", "completion_rate", "content_category"])
    names = {s["name"] for s in specs}
    assert "play_duration" not in names
    assert "completion_rate" not in names
    assert "content_category" in names




def test_transition_lift_discriminates():
    """Short-dwell→news and Long-dwell→comedy should yield lift>1 / lift<1.

    Each participant has 4 windows (n=2), so they clear the MIN_WINDOWS gate.
    Per-participant dwell-mean sequence [5,100,5,100] ranks to bins
    [Short, Long, Medium, Long]. Categories are arranged so every Short/Medium
    current window is followed by news and every Long by comedy. With three
    identical participants the min-participants gate is met, so lift is reported.
    """
    cats = [
        ["daily life"], ["daily life"],  # w0 (Short)  → followed by w1=news
        ["news"], ["news"],              # w1 (Long)   → followed by w2=comedy
        ["comedy"], ["comedy"],          # w2 (Medium) → followed by w3=news
        ["news"], ["news"],              # w3 (Long)
    ]
    dwells = [5, 5, 100, 100, 5, 5, 100, 100]
    frames = [_make_events(cid, 0, dwells, cats) for cid in ("A", "B", "C")]
    df = pd.concat(frames, ignore_index=True)

    windows, tidx, elig = sa.prepare_window_table(df, window_n=2, requested_targets=["content_category"])
    assert (elig["n_windows"] == 4).all()
    assert bool(elig["eligible"].all())

    spec = tidx["content_category"]
    result = sa.compute_share_transition(windows, spec["value_columns"], spec["values"], horizon=1)
    lift = result["lift"]
    # Short-dwell windows are followed by news; Long-dwell by comedy.
    assert lift["Short"]["news"] is not None and lift["Short"]["news"] > 1.0
    assert lift["Long"]["news"] == 0.0
    assert lift["Long"]["comedy"] is not None and lift["Long"]["comedy"] > 1.0
    # Probabilities are populated regardless of the participant gate.
    assert result["prob"]["Short"]["news"] == 1.0




def test_eligibility_gate_on_dwell_coverage():
    """A participant with mostly-null dwell is marked ineligible."""
    df = _make_events("A", 0, [5, 5, 5, 5, 5, 5, 5, 5], [["news"]] * 8)
    # Null out most dwell values (simulating Zeeschuimer observe rows).
    df.loc[1:, "play_duration"] = pd.NA
    indexed = sa.add_sequence_index(df)
    specs = sa.classify_targets(df, requested=["content_category"])
    windows, _ = sa.build_windows(indexed, specs, window_n=2)
    elig = sa.compute_participant_eligibility(indexed, windows)
    assert not bool(elig.loc[elig["collection_id"] == "A", "eligible"].iloc[0])




def test_real_study_smoke():
    """Run the full pipeline on a cached study if available; else skip."""
    try:
        from fyp import data_io, fyp_config
        fyp_config.initialize()
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] config init failed: {exc}")
        return

    filename = f"{REAL_STUDY}_recoded.parquet"
    if not data_io.exists(storage_location="cache", filename=filename):
        print(f"  [skip] {filename} not in cache")
        return

    df = data_io.load_parquet(storage_location="cache", filename=filename)
    windows, tidx, elig = sa.prepare_window_table(df, window_n=sa.DEFAULT_WINDOW_N)
    summary = sa.compute_summary(windows, tidx, elig)

    meta = summary["metadata"]
    print(f"  participants total={meta['n_participants_total']} "
          f"eligible={meta['n_participants_eligible']}")
    print(f"  windows={meta['n_windows']} targets={len(tidx)}")

    # Invariants.
    assert meta["n_participants_total"] > 0
    if meta["n_windows"] > 0:
        share_prefix = f"{sa.SHARE_PREFIX}{sa.SHARE_SEP}"
        share_cols = [c for c in windows.columns if c.startswith(share_prefix)]
        for col in share_cols:
            vals = windows[col].dropna()
            assert ((vals >= 0) & (vals <= 1)).all(), f"{col} out of [0,1]"
        assert windows["dwell_bin"].notna().any()
        # Show a sample of the horizon-1 lift for content_category.
        cc = summary["horizons"]["1"].get("content_category")
        if cc and cc["values"]:
            top = cc["values"][0]
            print(f"  h=1 content_category lift[bin][{top!r}]: "
                  + ", ".join(f"{b}={cc['lift'].get(b, {}).get(top)}" for b in cc["bins"]))


def _run_all():
    tests = [
        test_sequence_index_and_session_split,
        test_windows_drop_partial_and_aggregate,
        test_multilabel_category_share,
        test_dichotomous_target_proportion,
        test_scalar_target_window_mean,
        test_dwell_predictor_columns_barred_as_targets,
        test_transition_lift_discriminates,
        test_eligibility_gate_on_dwell_coverage,
        test_real_study_smoke,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
