"""Row identity in Video Analysis: the same video watched twice is two rows.

The viewer is handed one ``row_idx`` per row with its id chunk and sends it back
to name the exact row behind the video on screen. The item id cannot do that job
— a rewatched video shares its id across every occurrence, and answering with
the first matching row makes the detail panel report one activity timestamp for
all of them.

That is what happened: recoded parquets are written with whatever index the last
upstream join left behind, and several carry a float index that is mostly NaN
with a handful of duplicated labels. ``NaN != NaN``, so the row lookup never
matched and fell through to the item_id path for every occurrence; a NaN label
is also not valid JSON, so it could not survive the trip to the client intact
either.

These tests pin the repair: the cached frame's index is normalised to a unique
RangeIndex at load time (which fixes every already-written parquet without a
re-recode), and the lookup resolves each occurrence to its own row.
"""

import json

import numpy as np
import pandas as pd

from web_interface import explorer_backend as explorer
from web_interface.services import study_data


# The video the bug was reported against: watched twice in one morning, ~85
# minutes apart, and both occurrences reported the earlier timestamp.
_REWATCHED = "7000000000000000004"


def _dirty_frame():
    """A recoded frame carrying the index shape seen in the wild: float64,
    mostly NaN, and what non-NaN labels there are repeat."""
    rows = [
        ("7000000000000000001", "2025-03-11 10:02:00"),
        (_REWATCHED, "2025-03-11 11:49:17"),
        ("7000000000000000002", "2025-03-11 12:30:00"),
        (_REWATCHED, "2025-03-11 13:14:22"),
        ("7000000000000000003", "2025-03-11 13:40:00"),
    ]
    df = pd.DataFrame({
        "item_id": [r[0] for r in rows],
        "utc_timestamp": pd.to_datetime([r[1] for r in rows]),
        "activity_type": ["play"] * len(rows),
        "annotated_ok": [True] * len(rows),
        "scraped_ok": [True] * len(rows),
        "play_duration": [4.0, 9.0, 11.0, 28.0, 6.0],
    })
    df.index = pd.Index([np.nan, np.nan, 884.0, np.nan, 884.0], dtype="float64")
    return df


def _col_types():
    return {
        "item_id": "identifier",
        "utc_timestamp": "datetime",
        "activity_type": "category",
        "annotated_ok": "category",
        "scraped_ok": "category",
        "play_duration": "number",
    }


def _load(monkeypatch, study="row_identity_study", frame=None):
    """Drive the real load path with a dirty frame standing in for the parquet."""
    df = _dirty_frame() if frame is None else frame
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 1.0)
    monkeypatch.setattr(explorer, "load_data", lambda s, verbose=False: (df, _col_types()))
    study_data.study_cache.invalidate(study)
    return study


def test_cached_frame_index_is_normalised(monkeypatch):
    """The cached frame's index is the row_idx contract, so it has to be
    unique and finite regardless of what the parquet carried."""
    study = _load(monkeypatch)

    df, _col, _status = study_data._cached_study_frame(study)

    assert df.index.is_unique
    assert list(df.index) == list(range(len(df)))
    assert not np.isnan(np.asarray(df.index, dtype="float64")).any()


def test_row_idxs_handed_out_are_json_serialisable(monkeypatch):
    """A NaN label serialises to a bare ``NaN`` token, which no JSON parser
    accepts — the client's whole chunk fetch would fail on it."""
    study = _load(monkeypatch)

    df, _ = study_data.get_explorer_data(study, context="viewer")
    row_idxs = df.index.tolist()

    assert json.loads(json.dumps(row_idxs, allow_nan=False)) == row_idxs
    assert all(isinstance(i, int) for i in row_idxs)


def test_each_occurrence_resolves_to_its_own_row(monkeypatch):
    """The bug itself: two plays of one video must report their own
    timestamps, not the first occurrence's twice."""
    study = _load(monkeypatch)

    df, _ = study_data.get_explorer_data(study, context="viewer")
    ordered = df.sort_values("utc_timestamp")
    occurrences = ordered.index[ordered["item_id"] == _REWATCHED].tolist()
    assert len(occurrences) == 2, "fixture must contain the video twice"

    seen = []
    for row_idx in occurrences:
        rows, _ = study_data.get_explorer_rows(
            study, item_id=_REWATCHED, row_index=row_idx,
        )
        # The route renders rows.iloc[0], so that is what must be right.
        record = rows.iloc[0]
        assert record["item_id"] == _REWATCHED
        seen.append(str(record["utc_timestamp"]))

    assert seen == ["2025-03-11 11:49:17", "2025-03-11 13:14:22"]
    assert len(set(seen)) == 2, "both occurrences reported the same timestamp"


def test_play_duration_also_tracks_the_occurrence(monkeypatch):
    """Every per-row column travels with the row, not just the timestamp —
    dwell differs between a skip and a full watch of the same video."""
    study = _load(monkeypatch)

    df, _ = study_data.get_explorer_data(study, context="viewer")
    ordered = df.sort_values("utc_timestamp")
    occurrences = ordered.index[ordered["item_id"] == _REWATCHED].tolist()

    durations = [
        study_data.get_explorer_rows(
            study, item_id=_REWATCHED, row_index=r,
        )[0].iloc[0]["play_duration"]
        for r in occurrences
    ]

    assert durations == [9.0, 28.0]


def test_unusable_row_index_does_not_resolve_to_a_wrong_row(monkeypatch):
    """A client holding a chunk from before the repair can still send a NaN.
    It must not be treated as a valid label — falling through to the item_id
    path is the honest answer, silently answering with row 0 is not."""
    study = _load(monkeypatch)

    rows, _ = study_data.get_explorer_rows(
        study, item_id=_REWATCHED, row_index=float("nan"),
    )

    # Falls back to the item_id match: every occurrence, caller disambiguates.
    assert len(rows) == 2
    assert set(rows["item_id"]) == {_REWATCHED}


def test_stale_row_index_falls_back_instead_of_raising(monkeypatch):
    """A label past the end of a re-filtered frame must not KeyError."""
    study = _load(monkeypatch)

    rows, _ = study_data.get_explorer_rows(
        study, item_id=_REWATCHED, row_index=999_999,
    )

    assert len(rows) == 2
    assert set(rows["item_id"]) == {_REWATCHED}


def test_clean_index_is_left_alone(monkeypatch):
    """A frame that already has a sane index keeps its row order untouched."""
    frame = _dirty_frame()
    frame.index = pd.RangeIndex(len(frame))
    study = _load(monkeypatch, study="clean_index_study", frame=frame)

    df, _col, _status = study_data._cached_study_frame(study)

    assert df["item_id"].tolist() == frame["item_id"].tolist()
    assert list(df.index) == list(range(len(frame)))
