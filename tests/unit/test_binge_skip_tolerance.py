"""``segment_session``'s tolerance for off-theme videos inside a binge.

Before 2026-08-10 a single off-theme video both ENDED the run and became the
first member of the next one. That second effect was the damaging one: the
theme then had to re-accumulate from an anchor that was not the theme, so long
on-theme stretches fragmented into runs too small to keep — 99.4% of candidate
runs on the production corpus ended under ``min_videos``.
"""

import numpy as np
import pandas as pd
import pytest

from fyp.analysis import session_explorer as se

CUT, MEM, MIN_VIDEOS, MIN_MINUTES = 0.5, 6, 4, 1.0


def _vectors():
    """Two well-separated directions: 0-7 on-theme, 8-11 off-theme."""
    theme = np.zeros((12, 4), dtype=np.float32)
    theme[:8, 0] = 1.0
    theme[8:, 1] = 1.0
    return theme


def _seq(rows, minutes_apart=1.0):
    t0 = pd.Timestamp("2026-01-01T10:00:00")
    return [(f"v{r}", r, t0 + pd.Timedelta(minutes=minutes_apart * i), 10.0)
            for i, r in enumerate(rows)]




def test_a_single_off_theme_video_no_longer_ends_the_binge():
    U = _vectors()
    # four on-theme, ONE ad, four on-theme
    seq = _seq([0, 1, 2, 3, 8, 4, 5, 6, 7])

    old = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=0)
    new = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=1)

    assert len(old) == 2, "pre-fix: the ad splits the run in two"
    assert len(new) == 1, "post-fix: one binge spanning the ad"
    assert len(new[0]["idx"]) == 8          # all eight on-theme videos
    assert new[0]["n_skipped"] == 1         # ...and the ad is reported, not hidden




def test_a_tolerated_video_is_not_a_member_and_never_enters_the_centroid():
    U = _vectors()
    seq = _seq([0, 1, 2, 3, 8, 4, 5, 6, 7])
    ep = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=1)[0]

    assert "v8" not in ep["ids"] and "v8" not in ep["seen"]
    assert 8 not in ep["idx"]
    # The span ends on the last MEMBER, so an interruption cannot stretch it.
    assert ep["end_ts"] == seq[-1][2]
    assert len(ep["m_ts"]) == len(ep["idx"]) == 8




def test_tolerance_is_for_CONSECUTIVE_outliers_only():
    U = _vectors()
    # Three ads in a row exceeds max_skip=2 — that is a real break.
    seq = _seq([0, 1, 2, 3, 8, 9, 10, 4, 5, 6, 7])
    eps = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=2)
    assert len(eps) == 2
    assert [len(e["idx"]) for e in eps] == [4, 4]
    # The run that ended on interruptions does not count them.
    assert eps[0]["n_skipped"] == 0




def test_a_real_break_rewinds_so_the_outliers_can_open_the_next_binge():
    """The videos that ended one binge must not be silently dropped."""
    U = _vectors()
    # four on-theme, then four off-theme that are themselves a coherent run.
    seq = _seq([0, 1, 2, 3, 8, 9, 10, 11])
    eps = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=2)
    assert len(eps) == 2
    assert eps[0]["ids"] == ["v0", "v1", "v2", "v3"]
    # v8 and v9 were tolerated by the first run, then rewound into the second.
    assert eps[1]["ids"] == ["v8", "v9", "v10", "v11"]




def test_max_skip_zero_reproduces_the_shipped_behaviour():
    """The old semantics must remain reachable, including outlier-as-seed."""
    U = _vectors()
    seq = _seq([0, 1, 2, 3, 8, 4, 5, 6, 7])
    eps = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=0)
    assert [e["ids"] for e in eps] == [
        ["v0", "v1", "v2", "v3"], ["v4", "v5", "v6", "v7"]]
    # Pre-fix, the ad seeded the second run before being dropped from it...
    assert all(e["n_skipped"] == 0 for e in eps)




def test_rewind_terminates_on_an_all_outlier_sequence():
    """Every restart must advance, or the scan loops forever."""
    U = _vectors()
    # Alternating themes: each video breaks the previous run.
    seq = _seq([0, 8, 1, 9, 2, 10, 3, 11] * 3)
    eps = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=2)
    assert isinstance(eps, list)          # returned at all == terminated




def test_rewatched_videos_still_extend_without_becoming_members():
    U = _vectors()
    seq = _seq([0, 1, 2, 3, 0, 8, 4])
    ep = se.segment_session(seq, U, CUT, MEM, MIN_VIDEOS, MIN_MINUTES, max_skip=1)[0]
    assert ep["ids"] == ["v0", "v1", "v2", "v3", "v4"]
    assert ep["n_plays"] == 6             # 5 members + 1 rewatch, ad excluded
    assert ep["n_skipped"] == 1




def _seq_dwell(rows_with_dwell, seconds_apart=30.0):
    t0 = pd.Timestamp("2026-01-01T10:00:00")
    return [(f"v{r}", r, t0 + pd.Timedelta(seconds=seconds_apart * i), d)
            for i, (r, d) in enumerate(rows_with_dwell)]




def test_flicked_off_theme_videos_do_not_spend_the_skip_budget():
    """Three 1-s off-theme flicks must not end the run (the AIO-00060 case)."""
    U = _vectors()
    rows = [(0, 10.0), (1, 10.0), (2, 10.0), (3, 10.0),
            (8, 1.0), (9, 1.0), (10, 1.0),          # 3 flicks > max_skip=2
            (4, 10.0), (5, 10.0), (6, 10.0), (7, 10.0)]
    counted = se.segment_session(_seq_dwell(rows), U, CUT, MEM, MIN_VIDEOS,
                                 MIN_MINUTES, max_skip=2, flick_seconds=0)
    flicked = se.segment_session(_seq_dwell(rows), U, CUT, MEM, MIN_VIDEOS,
                                 MIN_MINUTES, max_skip=2, flick_seconds=3.0)
    assert len(counted) == 2, "count-only rule: the 3 flicks break the run"
    assert len(flicked) == 1, "flick rule: one binge spanning the flicks"
    assert len(flicked[0]["idx"]) == 8
    assert flicked[0]["n_skipped"] == 3     # tolerated, and reported honestly




def test_watched_off_theme_videos_still_spend_the_budget():
    """Dwell at/above the flick threshold counts exactly as before."""
    U = _vectors()
    rows = [(0, 10.0), (1, 10.0), (2, 10.0), (3, 10.0),
            (8, 3.0), (9, 5.0), (10, 30.0),         # all watched (>= 3.0 s)
            (4, 10.0), (5, 10.0), (6, 10.0), (7, 10.0)]
    eps = se.segment_session(_seq_dwell(rows), U, CUT, MEM, MIN_VIDEOS,
                             MIN_MINUTES, max_skip=2, flick_seconds=3.0)
    assert len(eps) == 2
    assert [len(e["idx"]) for e in eps] == [4, 4]




def test_unknown_dwell_counts_toward_the_budget():
    """A missing play_duration cannot prove a flick, so it spends the budget."""
    U = _vectors()
    rows = [(0, 10.0), (1, 10.0), (2, 10.0), (3, 10.0),
            (8, None), (9, float("nan")), (10, None),
            (4, 10.0), (5, 10.0), (6, 10.0), (7, 10.0)]
    eps = se.segment_session(_seq_dwell(rows), U, CUT, MEM, MIN_VIDEOS,
                             MIN_MINUTES, max_skip=2, flick_seconds=3.0)
    assert len(eps) == 2, "unknown dwell must behave like watched, not flicked"




def test_flicked_videos_are_never_members():
    U = _vectors()
    rows = [(0, 10.0), (1, 10.0), (2, 10.0), (3, 10.0),
            (8, 1.0), (9, 1.0), (10, 1.0),
            (4, 10.0), (5, 10.0), (6, 10.0), (7, 10.0)]
    ep = se.segment_session(_seq_dwell(rows), U, CUT, MEM, MIN_VIDEOS,
                            MIN_MINUTES, max_skip=2, flick_seconds=3.0)[0]
    assert not {"v8", "v9", "v10"} & set(ep["ids"])
    assert not {8, 9, 10} & set(ep["idx"])




def test_default_params_carries_flick_seconds_from_config():
    p = se.default_params()
    assert p["flick_seconds"] == pytest.approx(se.FLICK_SECONDS)
    from fyp.fyp_config import fyp_cf
    cfg = fyp_cf.get("sessions", {})
    assert "binge_flick_seconds" in cfg, \
        "config.toml [sessions] must carry binge_flick_seconds"
    assert float(cfg["binge_flick_seconds"]) == p["flick_seconds"]




def test_default_params_carries_max_skip_from_config():
    p = se.default_params()
    assert p["max_skip"] == pytest.approx(se.MAX_SKIP)
    assert p["max_skip"] >= 0
    from fyp.fyp_config import fyp_cf
    cfg = fyp_cf.get("sessions", {})
    assert "binge_max_skip" in cfg, "config.toml [sessions] must carry binge_max_skip"
    assert int(cfg["binge_max_skip"]) == p["max_skip"]
    assert float(cfg["binge_min_minutes"]) == p["min_minutes"]
