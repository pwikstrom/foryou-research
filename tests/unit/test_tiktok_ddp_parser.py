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
