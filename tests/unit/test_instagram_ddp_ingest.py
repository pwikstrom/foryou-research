"""Fixture test for InstagramDDPCollection: a hand-built export zip through
``load_single_raw`` and ``process_single``.

Covers both record schemas Instagram has shipped (``label_values`` for the
watched-videos stream, classic ``string_list_data`` for likes), the shortcode
extraction, the donor-timezone tail, and the engagement fold onto the play
row with its ``link_method``.
"""

import io
import json
import zipfile

import pandas as pd
import pytest

import fyp.ingest.instagram as instagram_mod

_T0 = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def _export_zip(path) -> str:
    """Twelve watched videos a minute apart plus one like of the first, 50 s
    after it was watched."""
    watched = [
        {
            "label_values": [
                {"label": "URL", "value": f"https://www.instagram.com/reel/SHORT{i:02d}/"},
                {"label": "Caption", "value": f"caption {i}"},
                {"title": "Owner", "dict": [{"dict": [
                    {"label": "Name", "value": "Some Creator"},
                    {"label": "Username", "value": "creator"},
                ]}]},
            ],
            "timestamp": _T0 + 60 * i,
        }
        for i in range(12)
    ]
    likes = {"likes_media_likes": [{
        "title": "creator",
        "string_list_data": [{"href": "https://www.instagram.com/p/SHORT00/", "timestamp": _T0 + 50}],
    }]}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ads_information/ads_and_topics/videos_watched.json", json.dumps(watched))
        zf.writestr("your_instagram_activity/likes/liked_posts.json", json.dumps(likes))
    return str(path)


@pytest.fixture
def collection(monkeypatch, tmp_path):
    zip_path = _export_zip(tmp_path / "export.zip")
    monkeypatch.setattr(instagram_mod.data_io, "local_copy",
                        lambda storage_location=None, filename=None: zip_path)
    monkeypatch.setattr(instagram_mod.data_io, "release_local_copy", lambda p: None)
    return instagram_mod.InstagramDDPCollection(verbose=False)


def test_export_zip_parses_into_activity_rows(collection):
    df = collection.load_single_raw("export.zip")
    df["raw_file"] = "export.zip"

    out = collection.process_single(df)

    assert (out["activity_type"] == "play").sum() == 12
    assert (out["activity_type"] == "fave").sum() == 1
    assert set(out.loc[out["activity_type"] == "play", "item_id"]) == {f"SHORT{i:02d}" for i in range(12)}
    assert out["utc_timestamp"].notna().all()
    assert (out["seed_author_id"] == "creator").all()


def test_like_folds_onto_its_play_and_names_the_method(collection):
    df = collection.load_single_raw("export.zip")
    df["raw_file"] = "export.zip"

    out = collection.process_single(df).sort_values("utc_timestamp").reset_index(drop=True)

    first_play = out[(out["activity_type"] == "play") & (out["item_id"] == "SHORT00")].iloc[0]
    assert first_play["extra_data"] == "fave"
    assert first_play["link_method"] == "adjacent"
    assert first_play["play_duration"] == 60  # 50 s to the like plus 10 s to the next play


def test_supplied_donor_timezone_drives_the_offset(collection):
    df = collection.load_single_raw("export.zip")
    df["raw_file"] = "export.zip"
    df["manifest_tz"] = "Europe/Amsterdam"

    out = collection.process_single(df)

    assert (out["tz_offset"] == 1).all()  # November: CET, no daylight saving


def test_zip_without_expected_members_raises(monkeypatch, tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("something_else.json", "[]")
    monkeypatch.setattr(instagram_mod.data_io, "local_copy",
                        lambda storage_location=None, filename=None: str(path))
    monkeypatch.setattr(instagram_mod.data_io, "release_local_copy", lambda p: None)
    collection = instagram_mod.InstagramDDPCollection(verbose=False)

    with pytest.raises(ValueError):
        collection.load_single_raw("empty.zip")


def _likes_heavy_zip(path, n_watched: int, n_likes: int) -> str:
    """An export whose row count clears the floor only because of likes."""
    watched = [
        {
            "label_values": [
                {"label": "URL", "value": f"https://www.instagram.com/reel/SHORT{i:02d}/"},
            ],
            "timestamp": _T0 + 60 * i,
        }
        for i in range(n_watched)
    ]
    likes = {"likes_media_likes": [{
        "title": "creator",
        "string_list_data": [{"href": f"https://www.instagram.com/p/LIKED{i:02d}/",
                              "timestamp": _T0 + 100 + i}],
    } for i in range(n_likes)]}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ads_information/ads_and_topics/videos_watched.json", json.dumps(watched))
        zf.writestr("your_instagram_activity/likes/liked_posts.json", json.dumps(likes))
    return str(path)


def test_likes_alone_do_not_satisfy_the_viability_floor(monkeypatch, tmp_path):
    """23 rows, but only 3 of them are viewing — the donation is not usable."""
    zip_path = _likes_heavy_zip(tmp_path / "likes.zip", n_watched=3, n_likes=20)
    monkeypatch.setattr(instagram_mod.data_io, "local_copy",
                        lambda storage_location=None, filename=None: zip_path)
    monkeypatch.setattr(instagram_mod.data_io, "release_local_copy", lambda p: None)
    col = instagram_mod.InstagramDDPCollection(verbose=False)

    assert col.load_single_raw("likes.zip").empty


def test_enough_views_still_load_when_likes_are_present(monkeypatch, tmp_path):
    """The floor counts views only — it must not reject a healthy export."""
    zip_path = _likes_heavy_zip(tmp_path / "ok.zip", n_watched=10, n_likes=1)
    monkeypatch.setattr(instagram_mod.data_io, "local_copy",
                        lambda storage_location=None, filename=None: zip_path)
    monkeypatch.setattr(instagram_mod.data_io, "release_local_copy", lambda p: None)
    col = instagram_mod.InstagramDDPCollection(verbose=False)

    df = col.load_single_raw("ok.zip")
    assert (df["activity_type"] == "play").sum() == 10
    assert (df["activity_type"] == "fave").sum() == 1
