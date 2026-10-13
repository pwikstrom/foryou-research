"""Off-feed signals (fyp.analysis.sessions.offfeed) and their place in the Sessions build."""

import numpy as np
import pandas as pd
import pytest

from fyp.analysis.sessions import inputs, offfeed, publish, segment
from fyp.analysis.viewing import completion

T0 = pd.Timestamp("2026-01-01 12:00:00")


def _plays(rows):
    """rows: (session, seconds after T0, author, posting time offset in days or None)."""
    return pd.DataFrame(
        {
            "collection_id": ["c1"] * len(rows),
            "session_id": [r[0] for r in rows],
            "_ts": [T0 + pd.Timedelta(seconds=r[1]) for r in rows],
            "author": [r[2] for r in rows],
            "created": [
                (pd.Timestamp("2025-01-01") + pd.Timedelta(days=r[3])) if r[3] is not None else None
                for r in rows
            ],
        }
    )


def _events(rows):
    """rows: (activity_type, seconds after T0, handle)."""
    return pd.DataFrame(
        {
            "collection_id": ["c1"] * len(rows),
            "activity_type": [r[0] for r in rows],
            "_ts": [T0 + pd.Timedelta(seconds=r[1]) for r in rows],
            "extra_data": [r[2] for r in rows],
        }
    )


def test_search_and_follow_window_edges():
    plays = _plays([("s", 0, "a", None), ("s", 120, "b", None), ("s", 121, "c", None)])
    sig = offfeed.flag_plays(plays, _events([("search", 0, "term")]))
    # Inclusive at both ends: a search at the play's own second and 120 s before count.
    assert list(sig) == [offfeed.SIG_SEARCH, offfeed.SIG_SEARCH, 0]
    sig = offfeed.flag_plays(plays, _events([("follow", 0, "x")]))
    assert list(sig) == [offfeed.SIG_FOLLOW, offfeed.SIG_FOLLOW, 0]


def test_event_after_play_does_not_count():
    plays = _plays([("s", 0, "a", None)])
    assert list(offfeed.flag_plays(plays, _events([("search", 1, "t")]))) == [0]


def test_following_and_followed_by_are_not_follow():
    plays = _plays([("s", 10, "a", None)])
    ev = _events([("following", 0, "x"), ("followed_by", 0, "y")])
    assert list(offfeed.flag_plays(plays, ev)) == [0]


def test_narrow_follow_needs_the_followed_creator():
    plays = _plays([("s", 10, "a", None), ("s", 20, "b", None)])
    ev = _events([("follow", 0, "A")])  # handles compare case-insensitively
    assert list(offfeed.flag_plays(plays, ev, narrow_follow=True)) == [offfeed.SIG_FOLLOW, 0]
    assert list(offfeed.flag_plays(plays, ev)) == [offfeed.SIG_FOLLOW, offfeed.SIG_FOLLOW]


def test_same_creator_within_session_only_and_known_only():
    plays = _plays(
        [
            ("s1", 0, "a", None),
            ("s1", 10, "a", None),  # same as previous: flagged
            ("s2", 20, "a", None),  # new session: not flagged
            ("s2", 30, None, None),
            ("s2", 40, None, None),  # unknown creators never match
        ]
    )
    sig = offfeed.flag_plays(plays, None)
    assert list(sig) == [0, offfeed.SIG_SAME_CREATOR, 0, 0, 0]


@pytest.mark.parametrize(
    "days, expected_run",
    [
        ([1, 2, 3], True),  # increasing posting times
        ([3, 2, 1], True),  # decreasing
        ([1, 3, 2], False),  # not monotone
        ([1, 1, 2], False),  # tie (a repeat) breaks strict order
        ([1, None, 3], False),  # unknown posting time
    ],
)
def test_posting_order_run(days, expected_run):
    plays = _plays([("s", 10 * i, "a", d) for i, d in enumerate(days)])
    sig = offfeed.flag_plays(plays, None)
    run = [(int(v) & offfeed.SIG_POSTING_RUN) != 0 for v in sig]
    assert run == [expected_run] * 3


def test_run_of_two_is_not_a_posting_run():
    plays = _plays([("s", 0, "a", 1), ("s", 10, "a", 2)])
    sig = offfeed.flag_plays(plays, None)
    assert list(sig) == [0, offfeed.SIG_SAME_CREATOR]


def test_result_is_aligned_to_input_order():
    rows = [("s", 20, "a", None), ("s", 0, "b", None), ("s", 10, "b", None)]
    sig = offfeed.flag_plays(_plays(rows), None)
    # Time order is b(0), b(10), a(20): the second b repeats the creator.
    assert list(sig) == [0, 0, offfeed.SIG_SAME_CREATOR]


def test_signal_counts():
    counts = offfeed.signal_counts([0, 1, 2, 3, 4, 8, 12])
    assert counts == {"search": 2, "follow": 2, "same_creator": 2, "posting_run": 2, "any": 6}


def test_attach_offfeed_signals_uses_scraped_author_and_posting_time():
    plays = pd.DataFrame(
        {
            "collection_id": ["c1"] * 3,
            "session_id": ["s"] * 3,
            "item_id": pd.array(["v1", "v2", "v3"], dtype="string[pyarrow]"),
            "_ts": [T0, T0 + pd.Timedelta(seconds=10), T0 + pd.Timedelta(seconds=20)],
            "play_duration": [5.0, 5.0, 5.0],
            "source_platform": ["tiktok"] * 3,
        }
    )
    feat = pd.DataFrame(
        {
            "author": ["a", "a", "a"],
            "created": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
        },
        index=pd.Index(["v1", "v2", "v3"], name="item_id"),
    )
    out = publish.attach_offfeed_signals(plays, feat, _events([]), {})
    assert list(out["offfeed_signals"]) == [
        offfeed.SIG_POSTING_RUN,
        offfeed.SIG_SAME_CREATOR | offfeed.SIG_POSTING_RUN,
        offfeed.SIG_SAME_CREATOR | offfeed.SIG_POSTING_RUN,
    ]
    tbl = publish.plays_table(out)
    assert tbl.column("offfeed_signals").to_pylist() == list(out["offfeed_signals"])


def test_episode_and_session_rows_carry_offfeed_counts():
    rng = np.random.default_rng(0)
    base = rng.normal(size=8)
    U = np.stack([base + 0.01 * rng.normal(size=8) for _ in range(5)]).astype(np.float32)
    U /= np.linalg.norm(U, axis=1, keepdims=True)
    ids = [f"v{i}" for i in range(5)]
    id2idx = {iid: i for i, iid in enumerate(ids)}
    plays = pd.DataFrame(
        {
            "collection_id": ["c1"] * 5,
            "session_id": ["s"] * 5,
            "item_id": ids,
            "_ts": [T0 + pd.Timedelta(seconds=30 * i) for i in range(5)],
            "play_duration": [20.0] * 5,
            "offfeed_signals": np.array([0, 4, 0, 0, 1], dtype=np.int8),
        }
    )
    feat = pd.DataFrame(
        {
            "niche_name": ["n"] * 5,
            "author": ["a", "b", "c", "d", "e"],
            "advertising": ["no"] * 5,
            "political_score": [0.0] * 5,
            "sensitivity_score": [0.0] * 5,
        },
        index=pd.Index(ids, name="item_id"),
    )
    id_sets = {"scraped": set(ids), "annotated": set(ids), "embedded": set(ids)}
    p = {**inputs.default_params(), "min_minutes": 1.0}
    prepared, play_ts = segment._prepare_collection(plays, id2idx)
    srows, erows, _ = segment.build_session_group(
        "c1", prepared, play_ts, id2idx, U, feat, id_sets, p
    )
    assert len(erows) == 1
    ep = erows[0]
    assert ep["member_offfeed"] == [0, 4, 0, 0, 1]
    assert ep["n_offfeed"] == 2
    assert ep["offfeed_share"] == pytest.approx(0.4)
    assert srows[0]["n_offfeed"] == 2
    assert srows[0]["offfeed_share"] == pytest.approx(0.4)


def test_records_without_signals_leave_fields_empty():
    U = np.eye(4, dtype=np.float32)
    ep = {
        "ids": ["v0"],
        "idx": [0],
        "m_ts": [T0],
        "m_dur": [1.0],
        "start_ts": T0,
        "end_ts": T0,
        "n_plays": 1,
        "n_skipped": 0,
    }
    feat = pd.DataFrame(
        {
            "niche_name": ["n"],
            "author": ["a"],
            "advertising": ["no"],
            "political_score": [0.0],
            "sensitivity_score": [0.0],
        },
        index=pd.Index(["v0"], name="item_id"),
    )
    row = segment.episode_record(ep, "c1", "s", U, feat, np.array([T0.value]))
    assert row["member_offfeed"] is None and row["n_offfeed"] is None


def test_coverage_spec_without_clip_spans_the_whole_history():
    studies = {
        "st": {
            "START_DATE": "2025-03-01",
            "END_DATE": "2025-03-31",
            "SELECTED_COLLECTIONS": ["c1"],
        }
    }
    clipped = inputs.compute_coverage_spec(studies, clip=True)
    assert clipped["c1"] == [["2025-02-26", "2025-04-04"]]
    full = inputs.compute_coverage_spec(studies, clip=False)
    # The same wide window a study with no saved bounds gets.
    assert full == inputs.compute_coverage_spec({"st": {"SELECTED_COLLECTIONS": ["c1"]}}, clip=True)


def test_completion_helper_caps():
    assert completion([10, 60, None], [20, 20, 20], cap=1.0).tolist()[:2] == [0.5, 1.0]
    assert completion([60], [20], cap=3.0).tolist() == [3.0]
    out = completion([5], [0.5], cap=3.0, min_length_s=1)
    assert out.isna().all()
