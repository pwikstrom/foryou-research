"""The stored-data migration onto the 2026-09 engagement vocabulary.

Drives ``fyp.ingest.migrations.engagement_vocabulary.migrate`` with a
synthetic parquet-shaped frame and an injected raw-export loader: TikTok
bookmarks stored as ``fave`` become ``save`` (and their fold tokens follow),
``following`` becomes ``follow``, shares can be appended from a raw export
that still carries them, other platforms are untouched, and a second run is
a no-op.
"""

import pandas as pd
import pytest

from fyp.ingest.migrations import engagement_vocabulary as mig

_T0 = pd.Timestamp("2026-05-01 10:00:00", tz="UTC")


def _ts(seconds: int) -> pd.Timestamp:
    return _T0 + pd.Timedelta(seconds=seconds)


def _stored_frame() -> pd.DataFrame:
    """Two TikTok files and one Instagram file, as the parquet held them."""
    rows = [
        # raw_file, platform, source, cid, activity, item, ts, extra
        ("a.json", "tiktok", "ddp", "c1", "play",      "1001", _ts(0),    None),
        ("a.json", "tiktok", "ddp", "c1", "fave",      "1001", _ts(30),   None),   # a real like
        ("a.json", "tiktok", "ddp", "c1", "play",      "1002", _ts(60),   None),
        ("a.json", "tiktok", "ddp", "c1", "fave",      "1002", _ts(90),   None),   # a bookmark stored as fave
        ("a.json", "tiktok", "ddp", "c1", "play",      "1003", _ts(120),  None),
        ("a.json", "tiktok", "ddp", "c1", "following", None,   _ts(150),  "creator"),
        ("a.json", "tiktok", "ddp", "c1", "comment",   "1003", _ts(160),  "nice"),
        ("b.json", "tiktok", "aio", "c2", "play",      "2001", _ts(0),    None),
        ("b.json", "tiktok", "aio", "c2", "fave",      "2001", _ts(20),   None),   # raw missing → left alone
        ("ig.zip", "instagram", "ddp", "c3", "play",   "S1",   _ts(0),    None),
        ("ig.zip", "instagram", "ddp", "c3", "fave",   "S1",   _ts(40),   None),
    ]
    df = pd.DataFrame(rows, columns=[
        "raw_file", "source_platform", "data_source", "collection_id",
        "activity_type", "item_id", "utc_timestamp", "extra_data"])
    for col in ("raw_file", "source_platform", "data_source", "collection_id", "activity_type", "item_id", "extra_data"):
        df[col] = df[col].astype("string[pyarrow]")
    df["utc_timestamp"] = df["utc_timestamp"].astype("timestamp[ns, tz=UTC][pyarrow]")
    df["tz_offset"] = pd.Series([10] * len(df), dtype="int64[pyarrow]")
    # The pre-migration fold: the bookmark folded as a like token.
    df["link_method"] = pd.array([pd.NA] * len(df), dtype="string[pyarrow]")
    df.loc[[0, 2], "extra_data"] = pd.array(["fave", "fave"], dtype="string[pyarrow]")
    df.loc[[0, 2], "link_method"] = pd.array(["adjacent", "adjacent"], dtype="string[pyarrow]")
    df.loc[6, "link_method"] = "ffill_180s"
    df["play_duration"] = pd.Series([60, pd.NA, 60, pd.NA, 40, pd.NA, pd.NA, 20, pd.NA, 40, pd.NA], dtype="int64[pyarrow]")
    return df


def _raw_a() -> dict:
    """a.json as TikTok exported it: the like and the bookmark in their sections."""
    link = "https://www.tiktokv.com/share/video/{}/"
    return {
        "Your Activity": {
            "Watch History": {"VideoList": [
                {"Date": "2026-05-01 10:00:00", "Link": link.format(1001)},
                {"Date": "2026-05-01 10:01:00", "Link": link.format(1002)},
                {"Date": "2026-05-01 10:02:00", "Link": link.format(1003)},
            ]},
            "Share History": {"ShareHistoryList": [
                {"Date": "2026-05-01 10:00:45", "SharedContent": "share_video",
                 "Link": link.format(1001), "Method": "copy_link"},
            ]},
            "Reposts": {"RepostList": [
                {"Date": "2026-05-01 10:02:10", "Link": link.format(1003)},
            ]},
        },
        "Likes and Favorites": {
            "Like List": {"ItemFavoriteList": [{"date": "2026-05-01 10:00:30", "link": link.format(1001)}]},
            "Favorite Videos": {"FavoriteVideoList": [{"Date": "2026-05-01 10:01:30", "Link": link.format(1002)}]},
        },
    }


def _loader(calls=None):
    def load(data_source, raw_file):
        if calls is not None:
            calls.append((data_source, raw_file))
        return _raw_a() if raw_file == "a.json" else None
    return load


def test_bookmarks_become_saves_and_the_fold_follows():
    out, report = mig.migrate(_stored_frame(), _loader(), log=lambda *_: None)

    a = out[out["raw_file"] == "a.json"].sort_values("utc_timestamp").reset_index(drop=True)
    assert a.loc[a["item_id"] == "1002", "activity_type"].tolist() == ["play", "save"]
    assert a.loc[a["item_id"] == "1001", "activity_type"].tolist() == ["play", "fave"], "a real like stays a like"
    plays = a[a["activity_type"] == "play"].set_index("item_id")
    assert plays.loc["1002", "extra_data"] == "save"
    assert plays.loc["1001", "extra_data"] == "fave"
    assert plays.loc["1003", "extra_data"] == "comment:nice"
    assert report["retag"]["retagged"] == 1
    assert report["renamed_following"] == 1
    assert "following" not in set(out["activity_type"])
    assert (a["activity_type"] == "follow").sum() == 1


def test_parser_written_link_method_survives_the_refold():
    out, _ = mig.migrate(_stored_frame(), _loader(), log=lambda *_: None)
    comment = out[(out["activity_type"] == "comment")].iloc[0]
    assert comment["link_method"] == "ffill_180s"
    play = out[(out["activity_type"] == "play") & (out["item_id"] == "1003")].iloc[0]
    assert play["link_method"] == "nearest_play", "the follow row sits between the play and its comment"


def test_missing_raw_and_other_platforms_are_left_alone():
    calls = []
    out, report = mig.migrate(_stored_frame(), _loader(calls), log=lambda *_: None)
    assert "b.json" in report["retag"]["missing_raw"]
    assert out.loc[out["raw_file"] == "b.json", "activity_type"].tolist() == ["play", "fave"]
    ig = out[out["raw_file"] == "ig.zip"].sort_values("utc_timestamp")
    assert ig["activity_type"].tolist() == ["play", "fave"]
    assert ig.iloc[0]["extra_data"] == "fave"
    # Only TikTok files are read back; the Instagram zip never is.
    assert all(f != "ig.zip" for _, f in calls)


def test_version_stamp_and_idempotency():
    first, r1 = mig.migrate(_stored_frame(), _loader(), log=lambda *_: None)
    assert first["activity_contract_version"].notna().all()
    second, r2 = mig.migrate(first, _loader(), log=lambda *_: None)
    assert r2["renamed_following"] == 0
    assert r2["retag"]["retagged"] == 0
    pd.testing.assert_frame_equal(
        first.sort_values(["raw_file", "utc_timestamp"]).reset_index(drop=True),
        second.sort_values(["raw_file", "utc_timestamp"]).reset_index(drop=True),
    )


def test_append_new_sections_adds_shares_once():
    out, report = mig.migrate(_stored_frame(), _loader(), append_new_sections=True, log=lambda *_: None)
    shares = out[out["activity_type"] == "share"].sort_values("utc_timestamp")
    assert len(shares) == 2 and report["append"]["appended"] == 2
    assert shares["extra_data"].tolist() == ["copy_link", "repost"]
    assert shares["collection_id"].tolist() == ["c1", "c1"]
    assert shares["tz_offset"].tolist() == [10, 10]
    assert shares["local_date"].notna().all(), "local-time features derived for the new rows"
    assert out["session_id"].notna().all(), "session ids reassigned across the enlarged frame"
    plays = out[(out["raw_file"] == "a.json") & (out["activity_type"] == "play")].set_index("item_id")
    assert plays.loc["1001", "extra_data"] == "fave,share:copy_link"
    assert plays.loc["1003", "extra_data"] == "share:repost,comment:nice", "the repost is adjacent, the comment folds after"

    again, r2 = mig.migrate(out, _loader(), append_new_sections=True, log=lambda *_: None)
    assert r2["append"]["appended"] == 0
    assert len(again) == len(out)


def test_snapshot_name_is_filesystem_safe():
    name = mig.snapshot_name(pd.Timestamp("2026-09-22 03:04:05", tz="UTC"))
    assert name == "collections_recoded.pre_engagement_vocabulary_20260922T030405.parquet"


def test_default_loader_falls_back_to_every_tiktok_raw_location(monkeypatch):
    """AIO-fetched exports are stored with data_source='ddp' but live in aio_raw."""
    import fyp.data_io as data_io

    seen = []
    monkeypatch.setattr(data_io, "exists",
                        lambda storage_location=None, filename=None, **kw: seen.append(storage_location) or storage_location == "aio_raw")
    monkeypatch.setattr(data_io, "load_json",
                        lambda storage_location=None, filename=None, **kw: {"from": storage_location})
    assert mig.default_raw_loader("ddp", "uuid-file") == {"from": "aio_raw"}
    assert seen == ["ddp_raw", "aio_raw"], "the named source is tried first, then the rest"
    seen.clear()
    monkeypatch.setattr(data_io, "exists", lambda storage_location=None, filename=None, **kw: False)
    assert mig.default_raw_loader("ddp", "gone.json") is None
