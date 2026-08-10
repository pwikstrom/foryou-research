"""Unit tests for the session/episode builder (fyp.analysis.session_explorer).

Uses a synthetic 3-cluster embedding space plus hand-built play sequences —
no store or parquet access.
"""

import numpy as np
import pandas as pd
import pytest

from fyp.analysis import entropy_metrics, session_explorer as se


@pytest.fixture(scope="module")
def space():
    """A directional store with 3 well-separated clusters of 10 vectors each."""
    rng = np.random.default_rng(0)
    base = np.eye(3, 32) * 5.0
    vecs, ids = [], []
    for c in range(3):
        for i in range(10):
            vecs.append(base[c] + 0.02 * rng.normal(size=32))
            ids.append(f"c{c}_{i}")
    mat = np.array(vecs, dtype=np.float32)
    U = entropy_metrics.to_directional(mat, mat.mean(axis=0)).astype(np.float32)
    return {iid: i for i, iid in enumerate(ids)}, U, ids


@pytest.fixture(scope="module")
def feat(space):
    _, _, ids = space
    return pd.DataFrame({
        "item_id": pd.Series(ids, dtype="string"),
        "niche_name": [f"niche_{iid[1]}" for iid in ids],
        "category": "x", "story": "story",
        "political_score": 0.1, "sensitivity_score": 0.0,
        "advertising": "none", "author": "author_a",
    }).set_index("item_id")


def _plays(items, session="col1__0", start="2026-01-01 12:00:00", gap_s=40):
    ts0 = pd.Timestamp(start)
    rows = [{"collection_id": "col1", "item_id": iid,
             "_ts": ts0 + pd.Timedelta(seconds=gap_s * i), "play_duration": 20.0,
             "session_id": session, "source_platform": "tiktok"}
            for i, iid in enumerate(items)]
    df = pd.DataFrame(rows)
    df["item_id"] = df["item_id"].astype("string")
    df["session_id"] = df["session_id"].astype("string")
    return df


def _id_sets(ids):
    s = set(ids)
    return {"scraped": s, "annotated": s, "embedded": s, "downloaded": s}






def test_episode_splits_at_cluster_jump(space, feat):
    id2idx, U, ids = space
    seq = [f"c0_{i}" for i in range(6)] + [f"c1_{i}" for i in range(6)]
    srows, erows, wrows = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    assert len(srows) == 1
    assert len(erows) == 2
    assert erows[0]["member_item_ids"] == [f"c0_{i}" for i in range(6)]
    assert erows[1]["member_item_ids"] == [f"c1_{i}" for i in range(6)]
    assert erows[0]["dominant_niche"] == "niche_0"
    assert erows[1]["dominant_niche"] == "niche_1"
    # Rolling distances align with members: first element None, rest small.
    roll = erows[0]["member_rolling_cosdist"]
    assert len(roll) == 6 and roll[0] is None
    assert all(r < 0.05 for r in roll[1:])






def test_rewatches_extend_span_but_are_not_members(space, feat):
    id2idx, U, ids = space
    # 4 distinct videos, each played twice in a row.
    seq = []
    for i in range(4):
        seq += [f"c0_{i}", f"c0_{i}"]
    srows, erows, wrows = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    assert len(erows) == 1
    assert erows[0]["n_distinct"] == 4
    assert erows[0]["n_plays"] == 8
    assert erows[0]["repeat_rate"] == pytest.approx(2.0)






def test_min_videos_gate_drops_short_runs(space, feat):
    id2idx, U, ids = space
    seq = [f"c0_{i}" for i in range(3)]  # below MIN_VIDEOS=4
    srows, erows, wrows = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    assert erows == []
    assert srows[0]["n_episodes"] == 0






def test_session_boundary_hard_breaks_episodes(space, feat):
    id2idx, U, ids = space
    # 8 same-cluster videos, but split across two sessions of 4 — two episodes,
    # never one merged run.
    p1 = _plays([f"c0_{i}" for i in range(4)], session="col1__0", gap_s=70)
    p2 = _plays([f"c0_{i}" for i in range(4, 8)], session="col1__1",
                start="2026-01-01 14:00:00", gap_s=70)
    plays = pd.concat([p1, p2], ignore_index=True)
    srows, erows, wrows = se.build_collection("col1", plays, id2idx, U, feat, _id_sets(ids))
    assert len(srows) == 2
    assert len(erows) == 2
    assert {e["session_id"] for e in erows} == {"col1__0", "col1__1"}






def test_null_session_ids_become_singletons(space, feat):
    id2idx, U, ids = space
    plays = _plays([f"c0_{i}" for i in range(5)])
    plays["session_id"] = pd.array([None] * 5, dtype="string")
    srows, erows, wrows = se.build_collection("col1", plays, id2idx, U, feat, _id_sets(ids))
    # Each null-session play is isolated — five singleton sessions, no episodes.
    assert len(srows) == 5
    assert all(s["session_id"].startswith("na_") for s in srows)
    assert erows == []






def test_session_coverage_fractions(space, feat):
    id2idx, U, ids = space
    seq = [f"c0_{i}" for i in range(4)] + ["unknown_1", "unknown_2"]
    id_sets = _id_sets(ids)
    id_sets["annotated"] = {f"c0_{i}" for i in range(2)}
    srows, _, _w = se.build_collection("col1", _plays(seq), id2idx, U, feat, id_sets)
    s = srows[0]
    assert s["n_distinct"] == 6
    assert s["coverage_embedded"] == pytest.approx(4 / 6, abs=1e-3)
    assert s["coverage_annotated"] == pytest.approx(2 / 6, abs=1e-3)
    assert s["emb_play_coverage"] == pytest.approx(4 / 6, abs=1e-3)






def test_min_window_focus_requires_enough_embedded(space, feat):
    id2idx, U, ids = space
    # Fewer distinct embedded videos than WINDOW_N=6 → no focus metric.
    srows, _, _w = se.build_collection(
        "col1", _plays([f"c0_{i}" for i in range(5)]), id2idx, U, feat, _id_sets(ids))
    assert srows[0]["min_window_cosdist"] is None
    assert srows[0]["min_window_entropy_norm"] is None

    # A focused 6-window inside a mixed session is found and is small.
    seq = [f"c2_{i}" for i in range(3)] + [f"c0_{i}" for i in range(6)] + ["c1_0", "c2_5"]
    srows, _, _w = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    assert srows[0]["min_window_cosdist"] is not None
    assert srows[0]["min_window_cosdist"] < 0.05






def test_low_entropy_windows_nonoverlapping_and_ranked(space, feat):
    id2idx, U, ids = space
    # 18 embedded videos: three tight 6-video cluster runs → up to 3 windows.
    seq = ([f"c0_{i}" for i in range(6)] + [f"c1_{i}" for i in range(6)]
           + [f"c2_{i}" for i in range(6)])
    srows, _, wrows = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    assert 1 <= len(wrows) <= se.MAX_WINDOWS
    # Ranked ascending by distance; window 0's score is the session's min.
    scores = [w["mean_cosdist"] for w in wrows]
    assert scores == sorted(scores)
    assert srows[0]["min_window_cosdist"] == pytest.approx(scores[0])
    assert srows[0]["min_window_entropy_norm"] == pytest.approx(wrows[0]["entropy_norm"])
    # Non-overlapping member sets, each of exactly WINDOW_N distinct videos.
    seen: set[str] = set()
    for w in wrows:
        members = set(w["member_item_ids"])
        assert len(members) == se.WINDOW_N
        assert not (members & seen)
        seen |= members
        assert w["window_idx"] == wrows.index(w)
        assert w["dominant_niche"] is not None
    # Session shorter than the window → no windows at all.
    srows, _, wrows = se.build_collection(
        "col1", _plays([f"c0_{i}" for i in range(5)]), id2idx, U, feat, _id_sets(ids))
    assert wrows == []
    assert srows[0]["min_window_cosdist"] is None






def test_arrow_frames_roundtrip(space, feat):
    id2idx, U, ids = space
    seq = [f"c0_{i}" for i in range(6)] + [f"c1_{i}" for i in range(6)]
    srows, erows, wrows = se.build_collection("col1", _plays(seq), id2idx, U, feat, _id_sets(ids))
    sdf = se._arrow_frame(srows, se._SESSIONS_SCHEMA)
    edf = se._arrow_frame(erows, se._EPISODES_SCHEMA)
    wdf = se._arrow_frame(wrows, se._WINDOWS_SCHEMA)
    assert len(sdf) == 1 and len(edf) == 2 and len(wdf) == len(wrows) >= 1
    assert all(str(t).endswith("[pyarrow]") for t in sdf.dtypes)
    assert all(str(t).endswith("[pyarrow]") for t in wdf.dtypes)
    members = list(edf["member_item_ids"].iloc[0])
    assert members == [f"c0_{i}" for i in range(6)]
    roll = list(edf["member_rolling_cosdist"].iloc[0])
    assert pd.isna(roll[0]) and len(roll) == 6
    assert len(list(wdf["member_item_ids"].iloc[0])) == se.WINDOW_N




def test_session_record_emits_variable_extremes(space, feat):
    """vmin_/vmax_ columns per trend variable (+ dwell), None on all-null."""
    id2idx, U, ids = space
    f = feat.copy()
    f["sensitivity_score"] = [float(i % 7) for i in range(len(f))]
    f["log_plays"] = pd.array([None] * len(f), dtype="float64")  # all-null
    seq = [f"c0_{i}" for i in range(6)]
    plays = _plays(seq)
    plays["play_duration"] = [5.0, 10.0, 40.0, 20.0, 15.0, 25.0]
    srows, _, _w = se.build_collection(
        "col1", plays, id2idx, U, f, _id_sets(ids),
        trend_cols=["sensitivity_score", "log_plays"])
    s = srows[0]
    expected = [float(i % 7) for i, iid in enumerate(f.index) if iid in set(seq)]
    assert s["vmin_sensitivity_score"] == pytest.approx(min(expected))
    assert s["vmax_sensitivity_score"] == pytest.approx(max(expected))
    assert s["vmin_log_plays"] is None and s["vmax_log_plays"] is None
    assert s["vmin_dwell_s"] == pytest.approx(5.0)
    assert s["vmax_dwell_s"] == pytest.approx(40.0)




def test_search_text_collects_and_caps_the_display_fields(space, feat):
    """The blob is lowercased, deduped, and covers every displayed text field."""
    id2idx, U, ids = space
    f = feat.copy()
    f["desc"] = "A Caption #FunnyCats " + "x" * 500
    f["desc_hashtags"] = "#funnycats"
    seq = [f"c0_{i}" for i in range(4)]
    stories = {"c0_0": "A story about SOURDOUGH bread"}
    srows, _, _w = se.build_collection(
        "col1", _plays(seq), id2idx, U, f, _id_sets(ids), stories=stories)
    blob = srows[0]["search_text"]
    assert blob == blob.lower()
    assert "niche_0" in blob and "author_a" in blob
    assert "sourdough" in blob
    assert "a caption #funnycats" in blob
    # Per-fragment cap: the 500-char caption tail must not survive whole.
    assert "x" * 210 not in blob
    # Dedup: four items share one caption — it appears once.
    assert blob.count("a caption #funnycats") == 1
    assert len(blob) <= 8000




def test_sessions_schema_extends_the_base_with_extremes_and_roundtrips(space, feat):
    id2idx, U, ids = space
    trend_cols = ["sensitivity_score"]
    schema = se.sessions_schema(trend_cols)
    for col in ("search_text", "vmin_sensitivity_score", "vmax_sensitivity_score",
                "vmin_dwell_s", "vmax_dwell_s"):
        assert col in schema
    assert set(se._SESSIONS_SCHEMA) <= set(schema)

    seq = [f"c0_{i}" for i in range(6)]
    srows, _, _w = se.build_collection(
        "col1", _plays(seq), id2idx, U, feat, _id_sets(ids), trend_cols=trend_cols)
    sdf = se._arrow_frame(srows, schema)
    assert all(str(t).endswith("[pyarrow]") for t in sdf.dtypes)
    assert sdf["vmax_sensitivity_score"].iloc[0] == pytest.approx(0.0)
    assert isinstance(sdf["search_text"].iloc[0], str)
    tbl = se._arrow_table(srows, schema)
    assert tbl.num_rows == 1
