"""Global search matches in place, with the semantics the cast-based path had.

The cast the search used to run (``astype("string[pyarrow]")`` per column, all
of them retained at once) copied every byte of the searchable corpus and
exhausted a 16 GiB instance on a 2.4M-row study. These tests pin the
replacement to the old results — the reference implementation below is the
previous code path — and assert that the new one materialises nothing.
"""
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from web_interface import explorer_backend as explorer


def _arrow(values, pa_type):
    return pd.Series(pd.arrays.ArrowExtensionArray(pa.array(values, type=pa_type)))


@pytest.fixture
def frame():
    """A frame with every column shape the search has to handle."""
    return pd.DataFrame({
        "desc": _arrow(["Eriksson at the beach", None, "nothing here",
                        "ERIKSSON again", "eriksson"], pa.string()),
        "video_story": _arrow(["a long story", "mentions Eriksson", None,
                               "no match", ""], pa.large_string()),
        "desc_hashtags": _arrow([["eriksson", "beach"], None, [],
                                 ["sunset"], ["Eriksson"]],
                                pa.list_(pa.string())),
        "play_count": _arrow([1, 42, 7, 42, 0], pa.int64()),
        "scraped_ok": _arrow([True, False, True, True, False], pa.bool_()),
        "utc_timestamp": _arrow(
            pd.to_datetime(["2026-08-17", "2026-08-16", "2026-08-15",
                            "2026-08-14", "2026-08-13"]).tolist(),
            pa.timestamp("ns")),
    })


@pytest.fixture
def col_types():
    return {
        "desc": "long_text",
        "video_story": "long_text",
        "desc_hashtags": "list",
        "play_count": "number",
        "scraped_ok": "category",
        "utc_timestamp": "category",
    }


def _reference_search(df, column_types, terms):
    """The pre-2026-08-17 cast-based path, kept as the behavioural oracle."""
    mask = pd.Series(True, index=df.index)
    searchable = [c for c in df.columns
                  if column_types.get(c) in ("category", "long_text", "list")]
    for term in terms:
        term_mask = pd.Series(False, index=df.index)
        cols = list(searchable)
        if term.replace(".", "", 1).isdigit():
            cols += [c for c in df.columns if column_types.get(c) == "number"]
        for col in cols:
            try:
                if column_types.get(col) == "list":
                    import pyarrow.compute as pc
                    joined = pd.Series(pd.arrays.ArrowExtensionArray(
                        pc.binary_join(df[col].array._pa_array, " ")),
                        index=df.index)
                    series = joined
                else:
                    series = df[col].astype("string[pyarrow]")
                term_mask |= series.str.contains(term, case=False, regex=False,
                                                 na=False)
            except Exception:
                continue
        mask &= term_mask
    return df[mask]


@pytest.mark.parametrize("query", [
    "eriksson",              # hits long_text and a list column, mixed case
    "ERIKSSON",              # the term arrives lowercased either way
    "beach",                 # list element only
    "42",                    # numeric term also sweeps number columns
    "eriksson,beach",        # two terms: AND across terms, OR across columns
    "nothing here",          # term containing a space
    "no-such-value",         # empty result
])
def test_matches_reference_implementation(frame, col_types, query):
    terms = [t.strip().lower() for t in query.split(",") if t.strip()]
    expected = _reference_search(frame, col_types, terms)
    got = explorer.filter_dataframe(frame, col_types, {}, query)
    assert list(got.index) == list(expected.index), f"query={query!r}"


def test_search_composes_with_a_column_filter(frame, col_types):
    got = explorer.filter_dataframe(
        frame, col_types, {"scraped_ok": {"value": ["True"]}}, "eriksson")
    assert list(got.index) == [0, 3]


def test_nulls_and_empty_lists_never_match(frame, col_types):
    got = explorer.filter_dataframe(frame, col_types, {}, "eriksson")
    assert 2 not in got.index          # null hashtags, no textual match


def test_list_term_spanning_two_elements_still_matches(frame, col_types):
    # "eriksson beach" exists only across two separate list elements; the
    # joined form is what the old path matched, so it must keep matching.
    got = explorer.filter_dataframe(frame, col_types, {}, "eriksson beach")
    assert list(got.index) == [0]


def test_text_columns_are_never_cast_to_a_pandas_string(frame):
    """The regression guard: text columns carry the corpus, so a copy of one
    is what exhausted the instance. They must be matched on Arrow buffers."""
    calls = []
    original = pd.Series.astype

    def spy(self, dtype, *args, **kwargs):
        calls.append(str(dtype))
        return original(self, dtype, *args, **kwargs)

    pd.Series.astype = spy
    try:
        for col in ("desc", "video_story", "desc_hashtags"):
            explorer._column_search_mask(frame[col], "eriksson")
            explorer._column_search_mask(frame[col], "two words")
    finally:
        pd.Series.astype = original

    assert calls == [], f"text column was cast to {calls}"


def test_non_text_columns_still_take_the_transient_cast(frame):
    """Arrow cannot substring-match a timestamp, so that column documents the
    boundary: the cast still happens there, for one small column at a time."""
    mask = explorer._column_search_mask(frame["utc_timestamp"], "2026-08-15")
    assert list(mask) == [False, False, True, False, False]


def test_mask_helper_is_positional_not_index_aligned(frame, col_types):
    """A frame whose index is not 0..n-1 still selects the right rows."""
    shifted = frame.copy(deep=False)
    shifted.index = pd.RangeIndex(100, 100 + len(frame))
    got = explorer.filter_dataframe(shifted, col_types, {}, "eriksson")
    assert list(got.index) == [100, 101, 103, 104]


def test_column_search_mask_shapes(frame):
    for col in ("desc", "video_story", "desc_hashtags"):
        mask = explorer._column_search_mask(frame[col], "eriksson")
        assert isinstance(mask, np.ndarray) and mask.dtype == bool
        assert len(mask) == len(frame)


def test_multi_chunk_list_column_maps_rows_correctly():
    """list_parent_indices is chunk-local; the running offset must fix it."""
    chunked = pa.chunked_array([
        pa.array([["eriksson"], ["x"]], type=pa.list_(pa.string())),
        pa.array([["y"], ["z", "Eriksson"]], type=pa.list_(pa.string())),
    ])
    series = pd.Series(pd.arrays.ArrowExtensionArray(chunked))
    mask = explorer._column_search_mask(series, "eriksson")
    assert list(mask) == [True, False, False, True]
