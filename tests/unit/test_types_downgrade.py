#!/usr/bin/env python3
"""Tests for the ``large_*`` arrow-type downgrade in ``fyp.types``.

Polars writes parquet with ``large_string`` / ``large_list`` types.
Pandas 2.2.x has partial kernel coverage for those variants — most
importantly ``DataFrame.explode()`` is a silent no-op on ``large_list``
columns, and ``dictionary_encode`` (used by ``factorize``,
``pd.Categorical``, ``crosstab``, ``value_counts``) has no kernel for
``large_list``. ``fyp/types.convert_dtypes_to_pyarrow`` must normalize
to the non-large variants on every parquet load so downstream code
(PCA, explore, correlations…) keeps working on data written by the
polars round-trip.

These tests lock in the downgrade contract directly, so regressions
show up here rather than as cryptic ``ArrowNotImplementedError`` from
downstream consumers in production.

Run:
    source .venv/bin/activate
    PYTHONPATH=. python tests/unit/test_types_downgrade.py
"""


import os
import sys
import tempfile
import traceback
from pathlib import Path

import pandas as pd
import pyarrow as pa

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from fyp.types import (  # noqa: E402
    convert_dtypes_to_pyarrow,
    downgrade_arrow_type,
    downgrade_large_arrow_columns,
    downgrade_series_if_large,
)





def test_downgrade_arrow_type_leaves_non_large_unchanged() -> None:
    for t in [pa.string(), pa.int64(), pa.float32(), pa.bool_(), pa.binary()]:
        assert downgrade_arrow_type(t) == t





def test_downgrade_arrow_type_scalar_variants() -> None:
    assert downgrade_arrow_type(pa.large_string()) == pa.string()
    assert downgrade_arrow_type(pa.large_binary()) == pa.binary()





def test_downgrade_arrow_type_list_variants() -> None:
    assert downgrade_arrow_type(
        pa.large_list(pa.large_string())
    ) == pa.list_(pa.string())
    # Non-large list with large inner type still needs inner downgrade.
    assert downgrade_arrow_type(
        pa.list_(pa.large_string())
    ) == pa.list_(pa.string())
    # Non-large list with plain inner is a no-op.
    assert downgrade_arrow_type(pa.list_(pa.string())) == pa.list_(pa.string())





def test_downgrade_arrow_type_struct_nested() -> None:
    """Struct fields get recursively downgraded, leaving untouched ones alone."""
    t = pa.struct([
        pa.field("a", pa.large_string()),
        pa.field("b", pa.int64()),
        pa.field("c", pa.large_list(pa.large_string())),
    ])
    got = downgrade_arrow_type(t)
    expected = pa.struct([
        pa.field("a", pa.string()),
        pa.field("b", pa.int64()),
        pa.field("c", pa.list_(pa.string())),
    ])
    assert got == expected





def test_downgrade_series_if_large_preserves_values() -> None:
    """Values must survive unchanged — only the offset width changes."""
    large_t = pd.ArrowDtype(pa.large_list(pa.large_string()))
    values = [["news", "sport"], None, ["music"]]
    series = pd.Series(values, dtype=large_t, name="tags")

    got = downgrade_series_if_large(series)

    assert got.dtype == pd.ArrowDtype(pa.list_(pa.string()))
    # Null must remain null; list entries must match.
    assert got.iloc[0] == ["news", "sport"]
    assert got.iloc[1] is pd.NA or got.iloc[1] is None or (
        hasattr(got.iloc[1], "__len__") and len(got.iloc[1]) == 0
    )
    assert got.iloc[2] == ["music"]
    assert got.name == "tags"





def test_downgrade_series_if_large_is_noop_for_non_arrow() -> None:
    """Non-ArrowDtype series pass through untouched."""
    series = pd.Series(["x", "y"], dtype=object)
    assert downgrade_series_if_large(series) is series





def test_downgrade_large_arrow_columns_only_changes_offending_cols() -> None:
    """Columns that don't need downgrade must be left alone; if nothing
    needs downgrading the input is returned identity (no copy)."""
    large_str = pd.ArrowDtype(pa.large_string())
    regular_str = pd.ArrowDtype(pa.string())

    # All regular types → identity return.
    clean = pd.DataFrame({
        "a": pd.array([1, 2], dtype="int64[pyarrow]"),
        "b": pd.Series(["x", "y"], dtype=regular_str),
    })
    assert downgrade_large_arrow_columns(clean) is clean

    # Mixed → a new frame with offending columns rewritten.
    dirty = pd.DataFrame({
        "a": pd.array([1, 2], dtype="int64[pyarrow]"),
        "b": pd.Series(["x", "y"], dtype=large_str),
    })
    got = downgrade_large_arrow_columns(dirty)
    assert got["a"].dtype == dirty["a"].dtype  # untouched
    assert got["b"].dtype == regular_str       # downgraded





def test_convert_dtypes_fast_path_downgrades_large() -> None:
    """The all-ArrowDtype fast path in `convert_dtypes_to_pyarrow` must
    still apply the downgrade — this is what kicks in after parquet load
    for a recoded cache written by the polars round-trip."""
    large_list = pd.ArrowDtype(pa.large_list(pa.large_string()))
    large_str = pd.ArrowDtype(pa.large_string())

    df = pd.DataFrame({
        "item_id": pd.array(["v1", "v2"], dtype=large_str),
        "tags":    pd.Series([["a", "b"], ["c"]], dtype=large_list),
        "n":       pd.array([1, 2], dtype="int64[pyarrow]"),
    })

    got = convert_dtypes_to_pyarrow(df)

    assert got["item_id"].dtype == pd.ArrowDtype(pa.string())
    assert got["tags"].dtype == pd.ArrowDtype(pa.list_(pa.string()))
    assert got["n"].dtype == pd.ArrowDtype(pa.int64())

    # And crucially, the downstream path (explode) now works.
    exploded = got.explode("tags")
    assert exploded["tags"].iloc[0] == "a"
    assert exploded["tags"].iloc[1] == "b"





def test_parquet_roundtrip_of_polars_written_file() -> None:
    """End-to-end: a parquet written with ``large_list<large_string>``
    loads cleanly, gets downgraded, and supports explode + crosstab.

    This simulates what happens when `pca_refresh` picks up a recoded
    parquet written by the polars-backed shebang merge.
    """
    large_list = pd.ArrowDtype(pa.large_list(pa.large_string()))
    large_str = pd.ArrowDtype(pa.large_string())

    # Write with the "bad" (polars-style) dtypes.
    source = pd.DataFrame({
        "item_id": pd.array(["v1", "v2", "v3"], dtype=large_str),
        "tags":    pd.Series(
            [["news", "politics"], ["music"], ["news"]],
            dtype=large_list,
        ),
    })

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.parquet")
        source.to_parquet(path, engine="pyarrow")
        loaded = pd.read_parquet(
            path, engine="pyarrow", dtype_backend="pyarrow"
        )

    # Raw load has large_* types.
    assert pa.types.is_large_list(loaded["tags"].dtype.pyarrow_dtype)

    # After dtype normalization, they should be downgraded.
    normalized = convert_dtypes_to_pyarrow(loaded)
    assert normalized["tags"].dtype == pd.ArrowDtype(pa.list_(pa.string()))
    assert normalized["item_id"].dtype == pd.ArrowDtype(pa.string())

    # And explode + crosstab finally work.
    exploded = normalized.explode("tags")
    assert len(exploded) == 4
    counts = pd.crosstab(
        index=[exploded["item_id"]], columns=[exploded["tags"]]
    )
    assert counts.loc["v1", "news"] == 1
    assert counts.loc["v1", "politics"] == 1
    assert counts.loc["v3", "news"] == 1





TESTS = [
    test_downgrade_arrow_type_leaves_non_large_unchanged,
    test_downgrade_arrow_type_scalar_variants,
    test_downgrade_arrow_type_list_variants,
    test_downgrade_arrow_type_struct_nested,
    test_downgrade_series_if_large_preserves_values,
    test_downgrade_series_if_large_is_noop_for_non_arrow,
    test_downgrade_large_arrow_columns_only_changes_offending_cols,
    test_convert_dtypes_fast_path_downgrades_large,
    test_parquet_roundtrip_of_polars_written_file,
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
