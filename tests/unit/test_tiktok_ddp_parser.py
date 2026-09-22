"""Regression tests for TikTokDDPCollection.process_single's list unpacking.

A donation whose every exported value is a string lets polars unify the
per-file schemas inside ``fast_vertical_concat``, so ``variable_list`` /
``value_list`` come back as pyarrow *list* columns rather than object columns
of Python lists. ``Series.map`` then hands the callback a numpy array, which
the parser's ``isinstance(x, list)`` gate rejected — dropping every row, and
then raising ``KeyError: 'value_list'`` because ``.map()`` on the resulting
empty column returns a non-boolean Series that pandas reads as a list of
column *labels*.

Single-file parsing never reaches that path, which is why it went unnoticed:
these tests drive the multi-file concat explicitly. The fixtures here are
hand-built flat-string exports, deliberately independent of any generator.
"""

import pandas as pd
import pytest

import fyp.ingest.tiktok as tiktok_mod
from fyp.core.polars_ops import fast_vertical_concat


def _flat_ddp_document(seed: int, n_plays: int = 20) -> dict:
    """A minimal TikTok DDP export in which every value is a string.

    Only Date/Link pairs, so nothing forces the object-dtype pandas fallback
    in fast_vertical_concat — which is exactly the shape that broke.
    """
    return {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {
                        "Date": f"2026-05-{(i % 28) + 1:02d} 10:{i % 60:02d}:00",
                        "Link": f"https://www.tiktokv.com/share/video/{seed}{i:015d}/",
                    }
                    for i in range(n_plays)
                ]
            },
            "Login History": {
                "LoginHistoryList": [
                    {"Date": "2026-05-02 09:00:00", "IP": "203.0.113.1"}
                ]
            },
        }
    }


@pytest.fixture
def collection():
    return tiktok_mod.TikTokDDPCollection(verbose=False)


def _load(collection, monkeypatch, filename, doc):
    monkeypatch.setattr(
        tiktok_mod.data_io, "load_json",
        lambda storage_location=None, filename=None, _doc=doc, **kw: _doc,
    )
    df = collection.load_single_raw(filename)
    df["raw_file"] = filename
    return df


def test_parses_after_multi_file_concat(collection, monkeypatch):
    """Every row survives when the frames arrive as Arrow list columns."""
    frames = [
        _load(collection, monkeypatch, f"donor_{i}.json", _flat_ddp_document(seed=7 + i))
        for i in range(3)
    ]

    stacked = fast_vertical_concat(frames)
    # Guard the premise: if this stops being an Arrow list column the test no
    # longer covers the regression it was written for.
    assert "list" in str(stacked["value_list"].dtype)

    processed = stacked.groupby("raw_file", group_keys=False)[stacked.columns].apply(
        collection.process_single)

    assert len(processed) == len(stacked)
    assert set(processed["raw_file"].unique()) == {f"donor_{i}.json" for i in range(3)}
    plays = processed[processed["activity_type"] == "play"]
    assert len(plays) == 60
    assert plays["item_id"].notna().all()
    assert plays["utc_timestamp"].notna().all()


def _ddp_with_comments() -> dict:
    """Twelve plays a minute apart from 10:00 (the loader discards an export
    with ten or fewer), a comment 30 s after the first play (inside the 180 s
    grouping) and one 19 min after the last play (outside). Comments carry no
    video link in a TikTok export."""
    return {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {"Date": f"2026-05-01 10:{i:02d}:00",
                     "Link": f"https://www.tiktokv.com/share/video/70000000000000000{i:02d}/"}
                    for i in range(12)
                ]
            },
        },
        "Comment": {
            "Comments": {
                "CommentsList": [
                    {"Date": "2026-05-01 10:00:30", "Comment": "nice one"},
                    {"Date": "2026-05-01 10:30:00", "Comment": "late reply"},
                ]
            }
        },
    }


def test_supplied_donor_timezone_is_honoured(collection, monkeypatch):
    """A manifest timezone drives tz_offset; the parser used to bypass it
    and always infer from the activity rhythm (found 2026-09 while writing
    the pipeline up)."""
    df = _load(collection, monkeypatch, "donor_0.json", _flat_ddp_document(seed=7))
    df["manifest_tz"] = "Asia/Tokyo"

    out = collection.process_single(df)

    assert len(out) > 0
    assert (out["tz_offset"] == 9).all(), out["tz_offset"].unique()


def test_without_donor_timezone_offset_is_inferred(collection, monkeypatch):
    """No manifest timezone -> the inference still runs and yields one offset."""
    df = _load(collection, monkeypatch, "donor_0.json", _flat_ddp_document(seed=7))

    out = collection.process_single(df)

    assert out["tz_offset"].notna().all()
    assert out["tz_offset"].nunique() == 1


def test_comment_backfill_is_marked_on_the_row(collection, monkeypatch):
    """A comment whose video id came from the 180 s forward fill says so in
    link_method; one with no play in its group stays unlinked and unmarked;
    the play that received the folded comment says how it got it."""
    df = _load(collection, monkeypatch, "donor_0.json", _ddp_with_comments())

    out = collection.process_single(df).sort_values("utc_timestamp").reset_index(drop=True)

    comments = out[out["activity_type"] == "comment"].reset_index(drop=True)
    assert len(comments) == 2
    assert comments.loc[0, "item_id"] == "7000000000000000000"
    assert comments.loc[0, "link_method"] == "ffill_180s"
    assert pd.isna(comments.loc[1, "item_id"])
    assert pd.isna(comments.loc[1, "link_method"])

    plays = out[out["activity_type"] == "play"].reset_index(drop=True)
    assert plays.loc[0, "extra_data"] == "comment:nice one"
    assert plays.loc[0, "link_method"] == "adjacent"
    assert pd.isna(plays.loc[1, "link_method"])


def test_empty_group_keeps_its_columns(collection, monkeypatch):
    """A zero-row group returns intact rather than stripped of every column.

    `.map()` on an empty column returns a non-boolean Series, which pandas
    reads as column-label indexing — an unguarded `df[mask]` produced a frame
    with no columns at all, and the next lookup raised a baffling KeyError.
    """
    df = _load(collection, monkeypatch, "donor_0.json", _flat_ddp_document(seed=7))

    out = collection.process_single(df.iloc[0:0])

    assert len(out) == 0
    assert "value_list" in out.columns
    assert "variable_list" in out.columns


def _ddp_with_off_tiktok_activity(n_off: int = 50) -> dict:
    """Twelve plays plus a large Off TikTok Activity section, whose records
    carry no Date and are never ingested: they must be counted as
    ``outside_whitelist``, not as rows the parser failed to read."""
    doc = _flat_ddp_document(seed=3, n_plays=12)
    doc["Ads and data"] = {
        "Off TikTok Activity": {
            "OffTikTokActivityDataList": [
                {"TimeStamp": f"2026-05-01 10:{i % 60:02d}:00", "Source": "pixel", "Event": "PageView"}
                for i in range(n_off)
            ]
        }
    }
    # One play record with an unreadable date: the only genuine parse failure.
    doc["Activity"]["Video Browsing History"]["VideoList"].append(
        {"Date": "not a date", "Link": "https://www.tiktokv.com/share/video/7000000000000000099/"})
    return doc


def test_sections_outside_the_whitelist_are_counted_by_design_not_as_parse_failures(collection, monkeypatch):
    df = _load(collection, monkeypatch, "off.json", _ddp_with_off_tiktok_activity(n_off=50))
    collection.data = df
    collection.state = "raw"
    collection.file_stats_this_run = {"off.json": {"raw_rows": int(len(df)), "dropped": {}}}

    collection.process()

    dropped = collection.file_stats_this_run["off.json"]["dropped"]
    assert dropped["outside_whitelist"] == 50
    assert dropped["not_parseable"] == 1, "only the unreadable date is a parse failure"
    # 12 plays + 1 login survive; raw = 12 + 1 + 1 bad date + 50 off-platform
    assert len(collection.data) == 13
    assert len(df) == 13 + 1 + 50


def _posted_videos(n: int) -> dict:
    """The donor's own uploads: Post -> Posts -> VideoList, same key as watch history."""
    return {
        "Post": {
            "Posts": {
                "VideoList": [
                    {
                        "Date": f"2026-04-{(i % 28) + 1:02d} 08:{i % 60:02d}:00",
                        "Link": f"https://video-my.tiktokv.com/storage/v1/tos-alisg-pve-0037c001/o{i:022d}?a=1233",
                    }
                    for i in range(n)
                ]
            }
        }
    }


def test_posted_videos_do_not_satisfy_the_viability_floor(collection, monkeypatch):
    """A donation with no watch history is discarded however much the donor posted.

    TikTok keys the donor's own uploads with the same 'VideoList' name as watch
    history, so before the parent-section check these 80 counted as plays and
    carried the file over the floor.
    """
    doc = _posted_videos(80)
    monkeypatch.setattr(
        tiktok_mod.data_io, "load_json",
        lambda storage_location=None, filename=None, _doc=doc, **kw: _doc,
    )

    assert collection.load_single_raw("posted_only.json").empty


def test_posted_videos_are_excluded_by_design_not_counted_as_plays(collection, monkeypatch):
    """Alongside real watch history they neither inflate plays nor look like failures."""
    doc = _flat_ddp_document(seed=7, n_plays=20)
    doc.update(_posted_videos(15))

    df = _load(collection, monkeypatch, "both.json", doc)
    assert (df["activity_type"] == "videolist").sum() == 20, "only watch history is a play"
    assert (df["activity_type"] == "posted_videolist").sum() == 15

    collection.data = df
    collection.state = "raw"
    collection.file_stats_this_run = {"both.json": {"raw_rows": int(len(df)), "dropped": {}}}
    collection.process()

    assert collection.file_stats_this_run["both.json"]["dropped"]["outside_whitelist"] == 15
    assert (collection.data["activity_type"] == "play").sum() == 20


# ---------------------------------------------------------------------------
# Engagement vocabulary: shares, reposts, saves, follows and observed comment
# links. Record shapes follow the sentinel baselines learned from real exports
# (2026-09): ShareHistoryList puts the Link at index 2 after SharedContent,
# newer CommentsList records name their video in `originalPostUrl`.


def _ddp_with_engagement() -> dict:
    plays = [
        {"Date": f"2026-05-01 10:{i:02d}:00",
         "Link": f"https://www.tiktokv.com/share/video/70000000000000000{i:02d}/"}
        for i in range(12)
    ]
    return {
        "Your Activity": {
            "Watch History": {"VideoList": plays},
            "Share History": {"ShareHistoryList": [
                {"Date": "2026-05-01 10:01:20", "SharedContent": "share_video",
                 "Link": "https://www.tiktokv.com/share/video/7000000000000000001/",
                 "Method": "copy_link"},
                # A LIVE share names no video: kept as a share row without an item.
                {"Date": "2026-05-01 10:02:10", "SharedContent": "share_live",
                 "Link": "https://www.tiktok.com/@someone/live", "Method": "whatsapp"},
            ]},
            "Reposts": {"RepostList": [
                {"Date": "2026-05-01 10:03:30",
                 "Link": "https://www.tiktokv.com/share/video/7000000000000000003/"},
            ]},
            "Following": {"Following": [
                {"Date": "2026-05-01 10:04:30", "UserName": "creator_a"},
            ]},
        },
        "Likes and Favorites": {
            "Like List": {"ItemFavoriteList": [
                {"date": "2026-05-01 10:05:30",
                 "link": "https://www.tiktokv.com/share/video/7000000000000000005/"},
            ]},
            "Favorite Videos": {"FavoriteVideoList": [
                {"Date": "2026-05-01 10:06:30",
                 "Link": "https://www.tiktokv.com/share/video/7000000000000000006/"},
            ]},
            # Not video items: stripped as outside the whitelist.
            "Favorite Sounds": {"FavoriteSoundList": [
                {"Date": "2026-05-01 10:07:00", "Link": "https://www.tiktok.com/music/x-1"},
            ]},
        },
        "Comment": {"Comments": {"CommentsList": [
            # Newer vintage: the video is named, so no forward fill is needed
            # and no link_method is set.
            {"date": "2026-05-01 10:08:20", "comment": "seen it", "photo": "N/A",
             "video": "N/A", "url": "", "originalPostUrl":
             "https://www.tiktokv.com/share/video/7000000000000000008/", "original post link": ""},
            # Older vintage: no video anywhere → forward fill.
            {"date": "2026-05-01 10:09:40", "comment": "old style", "photo": "N/A"},
        ]}},
    }


def _process(collection, monkeypatch, doc):
    df = _load(collection, monkeypatch, "donor_e.json", doc)
    return collection.process_single(df).sort_values("utc_timestamp").reset_index(drop=True)


def test_favorite_video_list_is_a_save_and_like_list_a_fave(collection, monkeypatch):
    out = _process(collection, monkeypatch, _ddp_with_engagement())
    saves = out[out["activity_type"] == "save"]
    faves = out[out["activity_type"] == "fave"]
    assert list(saves["item_id"]) == ["7000000000000000006"]
    assert list(faves["item_id"]) == ["7000000000000000005"]
    plays = out[out["activity_type"] == "play"].set_index("item_id")
    assert plays.loc["7000000000000000006", "extra_data"] == "save"
    assert plays.loc["7000000000000000005", "extra_data"] == "fave"


def test_share_history_link_is_found_by_name_and_keeps_the_method(collection, monkeypatch):
    out = _process(collection, monkeypatch, _ddp_with_engagement())
    shares = out[out["activity_type"] == "share"].reset_index(drop=True)
    assert len(shares) == 3
    video_share = shares[shares["extra_data"] == "copy_link"].iloc[0]
    assert video_share["item_id"] == "7000000000000000001"
    live_share = shares[shares["extra_data"] == "whatsapp"].iloc[0]
    assert pd.isna(live_share["item_id"]), "a LIVE share names no video"
    repost = shares[shares["extra_data"] == "repost"].iloc[0]
    assert repost["item_id"] == "7000000000000000003"
    plays = out[out["activity_type"] == "play"].set_index("item_id")
    assert plays.loc["7000000000000000001", "extra_data"] == "share:copy_link"
    assert plays.loc["7000000000000000003", "extra_data"] == "share:repost"


def test_following_becomes_follow_and_never_folds(collection, monkeypatch):
    out = _process(collection, monkeypatch, _ddp_with_engagement())
    follows = out[out["activity_type"] == "follow"]
    assert len(follows) == 1
    assert follows["item_id"].isna().all()
    assert follows["extra_data"].iloc[0] == "creator_a"
    assert "following" not in set(out["activity_type"].dropna())
    plays = out[out["activity_type"] == "play"]
    assert not plays["extra_data"].astype("string").str.contains("follow", na=False).any()


def test_comment_with_original_post_url_is_observed_not_inferred(collection, monkeypatch):
    out = _process(collection, monkeypatch, _ddp_with_engagement())
    comments = out[out["activity_type"] == "comment"].reset_index(drop=True)
    assert len(comments) == 2
    observed = comments[comments["extra_data"] == "seen it"].iloc[0]
    assert observed["item_id"] == "7000000000000000008"
    assert pd.isna(observed["link_method"])
    inferred = comments[comments["extra_data"] == "old style"].iloc[0]
    assert inferred["item_id"] == "7000000000000000009", "the last activity within 180 s"
    assert inferred["link_method"] == "ffill_180s"


def test_non_item_favorites_are_outside_the_whitelist(collection, monkeypatch):
    df = _load(collection, monkeypatch, "donor_e.json", _ddp_with_engagement())
    assert "favoritesoundlist" in set(df["activity_type"])
    assert "favoritesoundlist" not in tiktok_mod.TikTokDDPCollection._ACTIVITY_TYPE_MAP
    assert "post" not in tiktok_mod.TikTokDDPCollection._ACTIVITY_TYPE_MAP, \
        "posted videos are relabelled posted_videolist; a 'post' key is unreachable"
