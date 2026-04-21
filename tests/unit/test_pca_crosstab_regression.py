#!/usr/bin/env python3
"""Regression for `transform_category_column_to_counts_df` on ArrowDtype.

Real trigger (2026-04-21 on fyp-task-runner):

    ArrowInvalid: Could not convert 0    023e7afc-...
    Name: collection_id, Length: 771915, dtype: string[pyarrow]
    ...
    File ".../pandas/core/reshape/pivot.py", line 696, in crosstab
        pass_objs = [x for x in index + columns if ...]

Root cause: pandas 2.2.x `pd.crosstab` doesn't normalise a bare Series
passed as the ``columns=`` kwarg; it expects a list-like. The fyp code
at ``fyp/pca.py`` was passing ``columns=df_exploded[the_column]``
unwrapped. Inside crosstab, ``list + Series`` triggers Series
arithmetic; for pyarrow-backed string columns the arithmetic path
fails inside pyarrow's ``_box_pa``. For the older
``StringDtype(storage="pyarrow")`` flavour, the arithmetic silently
returned garbage that crosstab happened to filter out — so the bug was
latent until my polars migration started normalising string columns to
``ArrowDtype(pa.string())`` after the shebang merge. Fix is to wrap
``columns`` in a list.

These tests pin the contract regardless of whether the inputs are
``string[pyarrow]`` (StringDtype) or ``ArrowDtype(pa.string())``, and
regardless of whether the values are UUIDs or simple category labels.

Run:
    source .fypenv314/bin/activate
    PYTHONPATH=. python tests/unit/test_pca_crosstab_regression.py
"""


import sys
import traceback
from pathlib import Path

import pandas as pd
import pyarrow as pa

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from fyp.pca import transform_category_column_to_counts_df  # noqa: E402





def _build_events(dtype_label: str) -> pd.DataFrame:
    """Build a small events DataFrame where the target column uses the
    requested pandas string dtype flavour.

    ``dtype_label``:
        - ``"string[pyarrow]"`` — pandas' StringDtype with pyarrow storage.
        - ``"arrow_string"``    — pandas' ArrowDtype(pa.string()).
        - ``"arrow_large"``     — pandas' ArrowDtype(pa.large_string()).
    """
    collection_ids = [
        "023e7afc-fa82-4d8e-8712-984baba7e833",
        "023e7afc-fa82-4d8e-8712-984baba7e833",
        "fd13a5a7-35e8-44d1-b8b8-07092e20ca1d",
        "fd13a5a7-35e8-44d1-b8b8-07092e20ca1d",
        "fd13a5a7-35e8-44d1-b8b8-07092e20ca1d",
    ]
    categories = ["a", "b", "a", "c", "b"]

    if dtype_label == "string[pyarrow]":
        target_dtype = pd.StringDtype(storage="pyarrow")
    elif dtype_label == "arrow_string":
        target_dtype = pd.ArrowDtype(pa.string())
    elif dtype_label == "arrow_large":
        target_dtype = pd.ArrowDtype(pa.large_string())
    else:
        raise ValueError(dtype_label)

    return pd.DataFrame({
        "collection_id": pd.array(collection_ids, dtype=target_dtype),
        "category":      pd.array(categories,     dtype=target_dtype),
    })





def test_crosstab_with_string_pyarrow_dtype() -> None:
    """Historical baseline: StringDtype(storage='pyarrow'). Did not raise
    before the regression either — locking it in."""
    events = _build_events("string[pyarrow]")
    counts = transform_category_column_to_counts_df(
        events, the_column="category", grouping_factors=["collection_id"]
    )
    assert counts.shape == (2, 3)
    # Spot-check one cell: first collection_id has one 'a' and one 'b'
    assert counts.loc["023e7afc-fa82-4d8e-8712-984baba7e833", "a"] == 1.0
    assert counts.loc["023e7afc-fa82-4d8e-8712-984baba7e833", "b"] == 1.0





def test_crosstab_with_arrow_dtype_string() -> None:
    """The dtype my polars round-trip produces: ArrowDtype(pa.string()).
    Before the fix, this raised ArrowInvalid inside pd.crosstab."""
    events = _build_events("arrow_string")
    counts = transform_category_column_to_counts_df(
        events, the_column="category", grouping_factors=["collection_id"]
    )
    assert counts.shape == (2, 3)
    assert counts.loc["fd13a5a7-35e8-44d1-b8b8-07092e20ca1d", "b"] == 1.0
    assert counts.loc["fd13a5a7-35e8-44d1-b8b8-07092e20ca1d", "c"] == 1.0





def test_crosstab_with_arrow_large_string_dtype() -> None:
    """Polars' native preference: ArrowDtype(pa.large_string()).
    Same failure mode as arrow_string before the fix."""
    events = _build_events("arrow_large")
    counts = transform_category_column_to_counts_df(
        events, the_column="category", grouping_factors=["collection_id"]
    )
    assert counts.shape == (2, 3)





def test_crosstab_with_multiple_grouping_factors() -> None:
    """Two grouping factors: the crosstab index becomes a MultiIndex.
    The list-of-Series `index=` path must continue to work."""
    events = pd.DataFrame({
        "collection_id": pd.array(
            ["c1", "c1", "c2", "c2"], dtype=pd.ArrowDtype(pa.string())
        ),
        "cohort": pd.array(
            ["A", "B", "A", "B"], dtype=pd.ArrowDtype(pa.string())
        ),
        "category": pd.array(
            ["x", "y", "x", "x"], dtype=pd.ArrowDtype(pa.string())
        ),
    })
    counts = transform_category_column_to_counts_df(
        events,
        the_column="category",
        grouping_factors=["collection_id", "cohort"],
    )
    # 4 unique (collection_id, cohort) pairs × 2 category values
    assert counts.shape == (4, 2)





def test_crosstab_with_list_column_explodes_before_counting() -> None:
    """The target column can be a list-valued arrow column — elements
    are exploded to individual rows before crosstabbing."""
    list_type = pd.ArrowDtype(pa.list_(pa.string()))
    events = pd.DataFrame({
        "collection_id": pd.array(
            ["c1", "c1", "c2"], dtype=pd.ArrowDtype(pa.string())
        ),
        "tags": pd.Series(
            [["sport", "news"], ["news"], ["music", "sport"]],
            dtype=list_type,
        ),
    })
    counts = transform_category_column_to_counts_df(
        events, the_column="tags", grouping_factors=["collection_id"]
    )
    # c1: sport=1, news=2.  c2: music=1, sport=1.
    assert counts.shape == (2, 3)
    assert counts.loc["c1", "news"] == 2.0
    assert counts.loc["c1", "sport"] == 1.0
    assert counts.loc["c2", "music"] == 1.0





TESTS = [
    test_crosstab_with_string_pyarrow_dtype,
    test_crosstab_with_arrow_dtype_string,
    test_crosstab_with_arrow_large_string_dtype,
    test_crosstab_with_multiple_grouping_factors,
    test_crosstab_with_list_column_explodes_before_counting,
]





def main() -> int:
    fails = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            fails += 1
    total = len(TESTS)
    print(f"\n{total - fails}/{total} passed")
    return 0 if fails == 0 else 1





if __name__ == "__main__":
    sys.exit(main())
