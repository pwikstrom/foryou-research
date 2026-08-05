"""Global-search projection + search-loop equivalence.

A free-text search used to disable column projection entirely (the request
fell back to the full ~95-column frame). Projection now adds exactly the
columns the search scans (``explorer.search_columns``), and the search loop
casts each column once per request with ``case=False`` matching instead of
materialising a lowercased copy per term. These tests pin the searchable-set
derivation and that search results are unchanged.
"""

import pandas as pd
import pyarrow as pa
import pytest

from web_interface import explorer_backend as explorer
from web_interface.routes.api_explorer_routes import _filter_request_columns
from web_interface.routes.api_viewer_routes import _IDS_BASE_COLUMNS, _ids_columns

_COL_TYPES = {
    "desc": "long_text",
    "content_category": "category",
    "desc_hashtags": "list",
    "item_id": "identifier",
    "play_count": "number",
    "duration": "number",
}






def test_search_columns_excludes_identifiers_and_numbers():
    out = explorer.search_columns(_COL_TYPES, "hello world")
    assert out == {"desc", "content_category", "desc_hashtags"}






def test_search_columns_adds_numbers_for_numeric_terms():
    out = explorer.search_columns(_COL_TYPES, "cats, 42")
    assert out == {"desc", "content_category", "desc_hashtags",
                   "play_count", "duration"}
    # A decimal term counts as numeric too.
    assert "play_count" in explorer.search_columns(_COL_TYPES, "3.5")






@pytest.fixture
def schema_meta(monkeypatch):
    meta = {
        "viz_priority": ["duration", "content_category"],
        "all_variables_order": ["content_category", "duration", "desc"],
    }
    monkeypatch.setattr(
        "web_interface.routes.api_explorer_routes.load_schema_metadata",
        lambda m: {**m, **meta},
    )
    return meta






def test_filter_request_columns_projects_under_search(schema_meta):
    data = {"search_query": "hello", "filters": {"raw_file": {"value": ["x"]}}}

    out = _filter_request_columns(data, {}, full_col_types=_COL_TYPES)

    assert out is not None
    got = set(out)
    # Viz set + enrichment sources + filter keys + searchable columns.
    assert {"duration", "content_category", "item_id",
            "annotated_ok", "annotation_version", "raw_file",
            "desc", "desc_hashtags"} <= got
    # No search term is numeric, and identifiers are never searched.
    assert "play_count" not in got






def test_filter_request_columns_full_width_without_col_types(schema_meta):
    assert _filter_request_columns({"search_query": "hello"}, {}) is None






def test_filter_request_columns_no_search_unchanged(schema_meta):
    out = _filter_request_columns({"filters": {"desc": {"value": ["x"]}}}, {})
    assert set(out) == {"duration", "content_category", "item_id",
                        "annotated_ok", "annotation_version", "desc"}






def test_ids_columns_projects_under_search():
    out = _ids_columns({"content_category": {"value": ["x"]}}, "hello",
                       full_col_types=_COL_TYPES)
    assert out is not None
    got = set(out)
    assert set(_IDS_BASE_COLUMNS) <= got
    assert {"content_category", "desc", "desc_hashtags"} <= got
    assert "play_count" not in got

    # Search active but no col types available: fall back to full width.
    assert _ids_columns({}, "hello") is None






# --- search-loop equivalence -------------------------------------------------


def _fixture_frame():
    return pd.DataFrame({
        "desc": pd.array(["Hello World", "FOO bar", None, "value 42"],
                         dtype="string[pyarrow]"),
        "content_category": pd.array(["News", "Comedy", "comedy", "News"],
                                     dtype="string[pyarrow]"),
        "desc_hashtags": pd.array([["fyp", "Cats"], None, ["cats"], []],
                                  dtype=pd.ArrowDtype(pa.list_(pa.string()))),
        "play_count": pd.array([10, 42, 7, 9], dtype="int64[pyarrow]"),
    })


_FIXTURE_TYPES = {
    "desc": "long_text",
    "content_category": "category",
    "desc_hashtags": "list",
    "play_count": "number",
}






@pytest.mark.parametrize("query,expected_rows", [
    ("hello", [0]),                    # case-insensitive on long_text
    ("COMEDY", [1, 2]),                # case-insensitive on category
    ("cats", [0, 2]),                  # matches inside list elements
    ("42", [1, 3]),                    # numeric term also scans numbers
    ("foo, bar", [1]),                 # multi-term AND, both in one row
    ("hello, comedy", []),             # multi-term AND across rows never matches
    ("zzz", []),
])
def test_filter_dataframe_search_results(query, expected_rows):
    df = _fixture_frame()
    out = explorer.filter_dataframe(df, _FIXTURE_TYPES, {}, search_query=query)
    assert list(out.index) == expected_rows






def test_filter_dataframe_search_after_categorical_filter():
    df = _fixture_frame()
    out = explorer.filter_dataframe(
        df, _FIXTURE_TYPES,
        {"content_category": {"value": ["News"]}},
        search_query="42",
    )
    assert list(out.index) == [3]
