#!/usr/bin/env python3
"""Thin helpers that use polars for expensive pandas operations at scale.

The project is pandas-first (see CLAUDE.md), but a handful of hot paths —
vertical concats of activity-level DataFrames and the activity × item-metadata
left join in `organize_datasets.py` — are slow and memory-hungry on pandas
because pandas is single-threaded and copies aggressively. This module wraps
those operations with polars while keeping pandas DataFrames at the boundary,
so callers don't have to care which engine is used.

Because the codebase already stores everything as pyarrow-backed dtypes
(`fyp/types.py`), the pandas<->polars round-trip goes through Arrow and is
effectively zero-copy for most column types.

Robustness notes
----------------
Polars' schema unification for `diagonal_relaxed` concat (and for joins that
need column-type alignment) does NOT always promote scalar-typed all-null
columns to the "richer" type present in a sibling frame. Real pipelines
routinely carry the same logical column with different pandas dtypes — e.g.
`tags` arrives as `large_list<large_string>` in a collection that has
annotations but as `string[pyarrow]` in a collection whose annotations are
still pending (all-null). These helpers mitigate that with:

1. A pyarrow-level schema pre-alignment pass that casts all-null columns to
   match the richest sibling dtype before polars ever sees the frames.
2. A graceful fallback to `pd.concat` / `pd.merge` if polars still raises a
   schema-unification error; correctness always wins over speed.
"""


import warnings
from typing import Iterable, Literal

import pandas as pd
import polars as pl
import pyarrow as pa

from fyp.types import downgrade_series_if_large
from fyp.logging_setup import get_logger

logger = get_logger(__name__)





# Catch the base PolarsError so every schema/unification/cast failure falls
# through to the pandas safe path. Narrow catches have proven too narrow —
# deeply nested types (list<string>, struct) surface failures from multiple
# polars internals, and the public exception labels for them shift between
# polars versions.
_POLARS_ERRORS: tuple[type[Exception], ...] = (pl.exceptions.PolarsError,)





def _pandas_to_polars(df: pd.DataFrame) -> pl.DataFrame:
    """Convert a pandas DataFrame to a polars DataFrame via pyarrow."""
    return pl.from_pandas(df)





def _safe_convert_dtypes_pyarrow(df: pd.DataFrame) -> pd.DataFrame:
    """Like `df.convert_dtypes(dtype_backend='pyarrow')`, but column-wise,
    tolerant of a pandas + pyarrow bug that kills the whole-frame version,
    and also downgrades pyarrow ``large_*`` types to their regular variants.

    Two failure modes this works around:

    1. When a column is entirely null with a nested arrow type (e.g.
       ``large_list<large_string>``, ``struct<...>``), pandas internally
       tries to cast the column to the pyarrow ``null`` type as part of
       dtype normalization. pyarrow does not implement that cast and
       raises::

           ArrowNotImplementedError: Unsupported cast from
           large_list<item: large_string> to null using function cast_null

       Because the whole-frame ``convert_dtypes`` is all-or-nothing, a
       single bad column takes out the entire call — including downstream
       consumers like the pandas fallback in ``fast_vertical_concat`` /
       ``fast_join``. Converting per column isolates the failure.

    2. Polars produces ``large_string`` / ``large_list`` types when
       converting back to pandas. Pandas 2.2.x has partial kernel coverage
       for those: ``df.explode()`` is a silent no-op on ``large_list``
       columns, and ``dictionary_encode`` (called by ``factorize``,
       ``Categorical``, ``crosstab``, etc.) has no kernel for
       ``large_list``. Downgrading each column to the non-large variants
       restores all those paths while preserving the actual values.
    """
    out: dict[str, pd.Series] = {}
    for col in df.columns:
        series = df[col]
        try:
            converted = series.convert_dtypes(dtype_backend="pyarrow")
        except Exception:
            # Known-failure path: all-null nested-type columns. The
            # original series is already pyarrow-backed and functionally
            # correct.
            converted = series
        out[col] = downgrade_series_if_large(converted)
    result = pd.DataFrame(out, index=df.index)
    # Ensure column order matches input
    return result[list(df.columns)]





def _polars_to_pandas(df: pl.DataFrame) -> pd.DataFrame:
    """Convert a polars DataFrame back to pandas with pyarrow-backed columns.

    `use_pyarrow_extension_array=True` ensures columns come back as
    `pd.ArrowDtype`-backed series. The follow-up per-column
    `_safe_convert_dtypes_pyarrow` normalizes quirks that arise from polars'
    preference for `pa.large_string`, `pa.large_list`, etc. — mapping them
    to the canonical `string[pyarrow]` / `list[pyarrow]` dtypes that
    `fyp/types.convert_dtypes_to_pyarrow` produces elsewhere in the
    codebase — while tolerating the all-null-nested-column pandas bug.
    """
    out = df.to_pandas(use_pyarrow_extension_array=True)
    return _safe_convert_dtypes_pyarrow(out)





def _normalize_via_pandas(df: pd.DataFrame) -> pd.DataFrame:
    """Reset index and normalize to the canonical ArrowDtype flavor.

    Uses `_safe_convert_dtypes_pyarrow` so the fallback path (which runs
    when polars can't unify schemas and we retreat to `pd.concat` /
    `pd.merge`) isn't killed by the same all-null-nested-column bug.
    """
    return _safe_convert_dtypes_pyarrow(df.reset_index(drop=True))





def _log_polars_fallback(
    helper_name: str,
    exc: BaseException,
) -> None:
    """Emit a RuntimeWarning and print a stderr-visible diagnostic when a
    polars helper falls back to pandas. Flask / Cloud Run pipe stderr to
    logs, so this surfaces real trigger cases in production without making
    the caller crash."""
    message = (
        f"{helper_name} falling back to pandas because polars raised "
        f"{type(exc).__name__}: {exc}"
    )
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    # Also log so the trigger is visible in server logs even if the caller
    # doesn't configure the warnings filter.
    logger.warning(f"[polars_ops] {message}")





def _richer_arrow_type(a: pa.DataType, b: pa.DataType) -> pa.DataType:
    """Return the "richer" of two pyarrow types for schema alignment.

    Rules (checked in order):
      1. `null` loses to everything.
      2. Nested types (`list`, `large_list`, `struct`) beat scalar types.
      3. Larger string variant beats smaller (`large_string` beats `string`).
      4. Otherwise, fall back to `a` — polars can usually resolve scalar
         supertypes on its own; the special cases above are where it fails.
    """
    if pa.types.is_null(a):
        return b
    if pa.types.is_null(b):
        return a

    a_nested = pa.types.is_list(a) or pa.types.is_large_list(a) or pa.types.is_struct(a)
    b_nested = pa.types.is_list(b) or pa.types.is_large_list(b) or pa.types.is_struct(b)
    if a_nested and not b_nested:
        return a
    if b_nested and not a_nested:
        return b

    if pa.types.is_large_string(a) and pa.types.is_string(b):
        return a
    if pa.types.is_large_string(b) and pa.types.is_string(a):
        return b

    return a





def _arrow_type_of(series: pd.Series) -> pa.DataType | None:
    """Best-effort extraction of a column's underlying pyarrow type.

    Returns None when the column is not pyarrow-backed (we'll let polars
    handle those via its normal pandas ingestion path).
    """
    dtype = series.dtype
    if isinstance(dtype, pd.ArrowDtype):
        return dtype.pyarrow_dtype
    # pandas' StringDtype(storage='pyarrow') — treat it as arrow string.
    if isinstance(dtype, pd.StringDtype) and getattr(dtype, "storage", None) == "pyarrow":
        return pa.string()
    return None





def _align_schemas_for_concat(
    dfs: list[pd.DataFrame],
) -> list[pd.DataFrame]:
    """Pre-align DataFrame schemas so polars can unify them safely.

    For every column that appears in more than one frame with a "scalar vs
    nested" type mismatch (the case polars cannot auto-promote), cast the
    all-null scalar version to the nested type in pandas before handing off.
    Frames are only modified where needed; inputs are not mutated.

    This only rewrites columns that are entirely null in the frame being
    coerced, to avoid losing real data by forcing a cast of populated rows.
    Non-null scalar-vs-nested mismatches are left alone and will still
    trigger polars' native error (which we then catch in the caller).
    """
    # Discover the richest type we've seen for each column name.
    best: dict[str, pa.DataType] = {}
    for df in dfs:
        for col in df.columns:
            t = _arrow_type_of(df[col])
            if t is None:
                continue
            if col in best:
                best[col] = _richer_arrow_type(best[col], t)
            else:
                best[col] = t

    aligned: list[pd.DataFrame] = []
    for df in dfs:
        recasts: dict[str, pd.Series] = {}
        for col in df.columns:
            target = best.get(col)
            if target is None:
                continue
            current = _arrow_type_of(df[col])
            if current is None or current == target:
                continue
            # Only coerce when the column is entirely null. Non-null columns
            # with real-but-different scalar data should be left alone;
            # caller's fallback will handle genuine conflicts.
            if not df[col].isna().all():
                continue
            null_array = pa.nulls(len(df), type=target)
            recasts[col] = pd.Series(
                pd.arrays.ArrowExtensionArray(null_array),
                index=df.index,
                name=col,
            )
        if recasts:
            aligned.append(df.assign(**recasts))
        else:
            aligned.append(df)

    return aligned





def fast_vertical_concat(
    dfs: Iterable[pd.DataFrame],
    ignore_index: bool = True,
) -> pd.DataFrame:
    """Vertically concatenate pandas DataFrames using polars.

    Semantically equivalent to `pd.concat(dfs, ignore_index=ignore_index)` for
    the row-stacking use case: the union of columns is used, and missing
    columns in any input are filled with nulls. Column order follows the first
    appearance in the input sequence, matching pandas' behavior.

    Empty DataFrames (no rows) are dropped from the concat to avoid dtype
    coercion surprises when the empty input has object-typed columns.

    Schema pre-alignment is performed automatically for the common case of a
    column arriving as a nested type (`list`, `struct`) in one frame and as a
    scalar-null column in another. If polars still rejects the concat after
    alignment, this helper falls back to `pd.concat` so correctness is
    guaranteed (with a warning so the scenario can be investigated).

    Args:
        dfs: Iterable of pandas DataFrames to stack vertically.
        ignore_index: If True (default), the returned DataFrame has a fresh
            RangeIndex. `ignore_index=False` is not supported — polars has
            no index concept.

    Returns:
        A pandas DataFrame with pyarrow-backed columns containing the stacked
        rows.

    Raises:
        NotImplementedError: If ``ignore_index=False`` is requested.
        ValueError: If the input iterable is empty.
    """
    if not ignore_index:
        raise NotImplementedError(
            "fast_vertical_concat only supports ignore_index=True; polars has no "
            "index concept."
        )

    dfs_list = list(dfs)
    if len(dfs_list) == 0:
        raise ValueError("No objects to concatenate")

    non_empty = [df for df in dfs_list if len(df) > 0]

    if len(non_empty) == 0:
        return pd.concat(dfs_list, ignore_index=True)

    if len(non_empty) == 1:
        return _normalize_via_pandas(non_empty[0])

    # Alignment + polars concat wrapped in a single try/except so that any
    # failure (even from pyarrow during the alignment pre-pass) falls back
    # to pandas cleanly rather than escaping to the caller.
    try:
        aligned = _align_schemas_for_concat(non_empty)
        pl_frames = [_pandas_to_polars(df) for df in aligned]
        combined = pl.concat(pl_frames, how="diagonal_relaxed")
        return _polars_to_pandas(combined)
    except _POLARS_ERRORS as exc:
        _log_polars_fallback("fast_vertical_concat", exc)
        return _normalize_via_pandas(
            pd.concat(non_empty, ignore_index=True)
        )
    except Exception as exc:
        # Defensive: anything else from pyarrow / pandas round-trips is
        # also non-fatal — correctness via pandas fallback.
        _log_polars_fallback("fast_vertical_concat", exc)
        return _normalize_via_pandas(
            pd.concat(non_empty, ignore_index=True)
        )





def fast_join(
    left: pd.DataFrame,
    right: pd.DataFrame,
    on: str | list[str],
    how: Literal["left", "inner", "outer", "right"] = "left",
) -> pd.DataFrame:
    """Join two pandas DataFrames using polars' parallel hash join.

    Semantically equivalent to
    `pd.merge(left, right, on=on, how=how)` for the common column-equality
    join. Left-row ordering is preserved for ``how="left"`` (polars >= 0.19),
    matching pandas' default behavior.

    On schema-unification errors (e.g. incompatible dtypes on the join key
    or on overlapping non-key columns), this helper falls back to
    `pd.merge` and emits a RuntimeWarning so correctness is guaranteed.

    Args:
        left: The left pandas DataFrame.
        right: The right pandas DataFrame.
        on: Column name or list of column names to join on. Must exist in
            both frames with compatible dtypes.
        how: The join strategy. Supported: ``"left"``, ``"inner"``,
            ``"outer"``, ``"right"``.

    Returns:
        A pandas DataFrame with pyarrow-backed columns, row ordering
        matching polars' join semantics for the given ``how``.
    """
    try:
        left_pl = _pandas_to_polars(left)
        right_pl = _pandas_to_polars(right)
        pl_how = "full" if how == "outer" else how
        joined = left_pl.join(right_pl, on=on, how=pl_how, maintain_order="left")
        return _polars_to_pandas(joined)
    except _POLARS_ERRORS as exc:
        _log_polars_fallback("fast_join", exc)
        return _normalize_via_pandas(
            pd.merge(left, right, on=on, how=how)
        )
    except Exception as exc:
        _log_polars_fallback("fast_join", exc)
        return _normalize_via_pandas(
            pd.merge(left, right, on=on, how=how)
        )
