"""``assign_session_ids``: sittings are built from the donor's own activity."""

import pandas as pd

from fyp.ingest.base import assign_session_ids


def _frame(rows):
    df = pd.DataFrame(rows, columns=["collection_id", "sec", "activity_type"])
    df["utc_timestamp"] = pd.Timestamp("2025-03-01 10:00", tz="UTC") + pd.to_timedelta(df.pop("sec"), unit="s")
    return df


def test_a_follower_does_not_join_two_sittings():
    df = _frame([("c1", 0, "play"), ("c1", 60, "play"), ("c1", 900, "followed_by"), ("c1", 1700, "play")])
    out = assign_session_ids(df, gap_threshold_s=900)
    plays = out[out["activity_type"] == "play"]["session_id"]
    assert plays.nunique() == 2
    assert out.loc[out["activity_type"] == "followed_by", "session_id"].isna().all()


def test_other_non_viewing_rows_still_belong_to_sittings():
    df = _frame([("c1", 0, "play"), ("c1", 800, "fave"), ("c1", 1500, "play"), ("c2", 0, "login")])
    out = assign_session_ids(df, gap_threshold_s=900)
    assert out.loc[out["collection_id"] == "c1", "session_id"].nunique() == 1
    assert out["session_id"].notna().all()


def test_frames_without_an_activity_type_are_sessioned_whole():
    df = _frame([("c1", 0, "play"), ("c1", 2000, "play")]).drop(columns="activity_type")
    assert assign_session_ids(df, gap_threshold_s=900)["session_id"].nunique() == 2


def test_my_collections_counts_viewing_sessions_only():
    from web_interface.services.my_collections_service import _session_stats

    df = _frame([("c1", 0, "play"), ("c1", 1500, "play"), ("c1", 90_000, "fave"), ("c1", 200_000, "login")])
    df = assign_session_ids(df, gap_threshold_s=900)
    df["local_timestamp"] = df["utc_timestamp"].dt.tz_localize(None)
    assert _session_stats(df)["n_sessions"] == 2
