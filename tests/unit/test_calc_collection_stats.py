"""Per-collection stats: most_active_weekday is the weekday, not its count.

The pre-fix code took ``value_counts().iloc[0]`` — the COUNT of the most
frequent weekday — so the stored value was an integer like 3.
"""

import pandas as pd

from fyp.analysis.calc_collection_stats import generate_personas, process_single_collection


def _events(weekdays, collection_id="c1"):
    """One play event per weekday entry, one hour apart."""
    ts = pd.date_range("2026-09-07 08:00", periods=len(weekdays), freq="h")
    return pd.DataFrame(
        {
            "collection_id": collection_id,
            "activity_type": "play",
            "local_timestamp": ts,
            "local_weekday": pd.array(weekdays, dtype="string[pyarrow]"),
            "tz_offset": 0.0,
        }
    )


def test_most_active_weekday_is_the_weekday_name():
    df = _events(["monday", "tuesday", "tuesday", "tuesday", "friday", "friday"])
    stats = process_single_collection(df)
    assert stats["most_active_weekday"] == "tuesday"
    assert isinstance(stats["most_active_weekday"], str)


def test_most_active_weekday_none_when_no_weekday():
    df = _events([None, None])
    stats = process_single_collection(df)
    assert stats["most_active_weekday"] is None
    assert stats["total_events"] == 2


def test_empty_frame_returns_no_stats():
    empty = _events([]).iloc[0:0]
    assert process_single_collection(empty) == {}
    assert generate_personas(empty).empty


def test_generate_personas_per_collection_weekday():
    df = pd.concat(
        [
            _events(["sunday", "sunday", "monday"], collection_id="a"),
            _events(["wednesday"], collection_id="b"),
        ],
        ignore_index=True,
    )
    personas = generate_personas(df).set_index("collection_id")
    assert personas.loc["a", "most_active_weekday"] == "sunday"
    assert personas.loc["b", "most_active_weekday"] == "wednesday"
