"""Explore filter-change stats: viz-set narrowing + Arrow-native list counts.

/api/explore/filter's cost is ``get_current_stats`` over the filtered frame.
Two changes keep it proportional to what the user sees:

- stats are computed only for the user's effective viz set (composed
  server-side from ``viz_priority`` + ``user.settings.variable_prefs.viz``,
  the same composition Timelines uses), not for every column;
- list columns count values through pyarrow ``list_flatten``/``value_counts``
  instead of pandas ``explode()``, which dominated the remaining cost.
"""

import pandas as pd
import pytest

from web_interface import explorer_backend as explorer
from web_interface.routes.api_explorer_routes import _viz_stats_col_types


_COL_TYPES = {
    "duration": "number",
    "content_category": "category",
    "desc_hashtags": "list",
    "raw_file": "category",
    "tz_offset": "number",
    "User Tags": "list",
    "Has Annotation": "category",
    "Machine Annotations": "category",
}





@pytest.fixture
def schema_meta(monkeypatch):
    """Patch the schema lookup with a fixed catalog."""
    meta = {
        "viz_priority": ["duration", "content_category", "desc_hashtags"],
        "all_variables_order": ["content_category", "desc_hashtags", "duration",
                                "raw_file", "tz_offset"],
    }
    monkeypatch.setattr(
        "web_interface.routes.api_explorer_routes.load_schema_metadata",
        lambda m: {**m, **meta},
    )
    return meta





def test_narrowing_keeps_viz_and_dynamic_columns_only(schema_meta):
    out = _viz_stats_col_types(_COL_TYPES, {})

    assert set(out) == {
        "duration", "content_category", "desc_hashtags",
        "User Tags", "Has Annotation", "Machine Annotations",
    }
    assert "raw_file" not in out and "tz_offset" not in out





def test_narrowing_honours_user_include_and_exclude(schema_meta):
    settings = {"variable_prefs": {"viz": {
        "include": ["tz_offset"], "exclude": ["desc_hashtags"],
    }}}

    out = _viz_stats_col_types(_COL_TYPES, settings)

    assert "tz_offset" in out
    assert "desc_hashtags" not in out
    assert "duration" in out





def test_narrowing_falls_back_to_all_columns_without_a_viz_list(monkeypatch):
    """A fresh install with no var_schema must keep computing everything —
    the frontend then renders every stats key."""
    monkeypatch.setattr(
        "web_interface.routes.api_explorer_routes.load_schema_metadata",
        lambda m: {**m, "viz_priority": [], "all_variables_order": []},
    )

    out = _viz_stats_col_types(_COL_TYPES, {})

    assert out == _COL_TYPES





def _list_frame(values):
    return pd.DataFrame({
        "tags": pd.array(values, dtype=pd.ArrowDtype(
            __import__("pyarrow").list_(__import__("pyarrow").string()))),
    })





def test_arrow_list_counts_match_pandas_explode():
    df = _list_frame([["a", "b"], ["a"], None, [], ["b", "a", "c"], ["c"]])

    got = explorer._list_value_counts_top(df["tags"], n=20)
    expected = df["tags"].explode().dropna().value_counts().to_dict()

    assert got == {str(k): int(v) for k, v in expected.items()}





def test_arrow_list_counts_truncate_to_n_most_frequent():
    df = _list_frame([["x"] * 3, ["y"] * 2, ["z"], ["x", "y"]])

    got = explorer._list_value_counts_top(df["tags"], n=2)

    assert got == {"x": 4, "y": 3}





def test_arrow_list_counts_empty_and_all_null():
    assert explorer._list_value_counts_top(_list_frame([])["tags"]) == {}
    assert explorer._list_value_counts_top(_list_frame([None, None])["tags"]) == {}





def test_stats_use_arrow_path_for_arrow_list_columns():
    """get_current_stats end-to-end: the list branch produces the same top-20
    dict shape the frontend expects."""
    df = _list_frame([["a", "b"], ["a"], ["c", "a"]])

    res = explorer.get_current_stats(df, {"tags": "list"})

    assert res["count"] == 3
    assert res["stats"]["tags"] == {"a": 3, "b": 1, "c": 1}
