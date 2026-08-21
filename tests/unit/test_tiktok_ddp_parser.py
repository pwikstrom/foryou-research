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
