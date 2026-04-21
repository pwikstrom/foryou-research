#!/usr/bin/env python3
"""Functional-identity tests for the pandas -> polars hot-path migration.

Covers:

1. The `fyp.polars_ops.fast_vertical_concat` and `fast_join` helpers,
   verifying they produce DataFrames that are element-wise equal to the
   corresponding pandas `pd.concat(..., ignore_index=True)` /
   `pd.merge(..., on=..., how='left')` output, once normalized through
   `convert_dtypes_to_pyarrow` (the dtype-normalization convention used
   throughout the codebase).

2. The recode-loop deferred-concat fix in `fyp/recode_variables.py`: we
   reconstruct the original in-loop-concat behavior as a reference impl
   and verify the new deferred implementation produces an identical
   DataFrame across a handful of representative schemas, including the
   edge case where a dict-unpacked column name would be iterated by a
   later pass of the outer loop.

Run:
    source .fypenv314/bin/activate
    python tests/unit/test_polars_ops_migration.py
"""


import sys
import traceback
import warnings
from copy import copy
from pathlib import Path

import pandas as pd
import pyarrow as pa

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from fyp.polars_ops import fast_join, fast_vertical_concat
from fyp.types import convert_dtypes_to_pyarrow





def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Run the codebase's dtype normalization so comparisons are dtype-stable."""
    return convert_dtypes_to_pyarrow(df).reset_index(drop=True)





# ---------------------------------------------------------------------------
# Section 1 — fast_vertical_concat
# ---------------------------------------------------------------------------





def test_concat_identical_schemas() -> None:
    a = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "n":       pd.array([1, 2, 3], dtype="int64[pyarrow]"),
    })
    b = pd.DataFrame({
        "item_id": pd.array(["d", "e"], dtype="string[pyarrow]"),
        "n":       pd.array([4, 5], dtype="int64[pyarrow]"),
    })

    got = fast_vertical_concat([a, b])
    want = _normalize(pd.concat([a, b], ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_concat_heterogeneous_schemas() -> None:
    a = pd.DataFrame({
        "x": pd.array([1, 2], dtype="int64[pyarrow]"),
        "y": pd.array(["p", "q"], dtype="string[pyarrow]"),
    })
    b = pd.DataFrame({
        "x": pd.array([3, 4], dtype="int64[pyarrow]"),
        "z": pd.array([0.5, 1.5], dtype="double[pyarrow]"),
    })

    got = fast_vertical_concat([a, b])
    want = _normalize(pd.concat([a, b], ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_concat_empty_and_nonempty() -> None:
    empty = pd.DataFrame({
        "x": pd.array([], dtype="int64[pyarrow]"),
        "y": pd.array([], dtype="string[pyarrow]"),
    })
    full = pd.DataFrame({
        "x": pd.array([10, 20], dtype="int64[pyarrow]"),
        "y": pd.array(["u", "v"], dtype="string[pyarrow]"),
    })

    got = fast_vertical_concat([empty, full])
    want = _normalize(pd.concat([empty, full], ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_concat_single_frame_with_empty_siblings() -> None:
    empty = pd.DataFrame({
        "x": pd.array([], dtype="int64[pyarrow]"),
        "y": pd.array([], dtype="string[pyarrow]"),
    })
    full = pd.DataFrame({
        "x": pd.array([1], dtype="int64[pyarrow]"),
        "y": pd.array(["only"], dtype="string[pyarrow]"),
    })

    got = fast_vertical_concat([empty, full, empty])
    want = _normalize(pd.concat([empty, full, empty], ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_concat_list_vs_allnull_string_prealigns() -> None:
    """Regression for the 'tags' column schema mismatch seen in real data.

    A collection with populated annotations has `tags` as
    `large_list<large_string>`; a collection with still-pending annotations
    can arrive with the same column as `string[pyarrow]` all-null. Polars
    rejects this mismatch directly. Our helper must pre-align the all-null
    scalar side to the list type and succeed without falling back.
    """
    tags_type = pd.ArrowDtype(pa.large_list(pa.large_string()))
    populated = pd.DataFrame({
        "item_id": pd.array(["v1", "v2"], dtype="string[pyarrow]"),
        "tags":    pd.array([["news"], ["music", "pop"]], dtype=tags_type),
    })
    all_null_string = pd.DataFrame({
        "item_id": pd.array(["v3", "v4"], dtype="string[pyarrow]"),
        "tags":    pd.array([None, None], dtype="string[pyarrow]"),
    })

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = fast_vertical_concat([populated, all_null_string])

    # Pre-alignment should make this a clean polars concat — no fallback.
    fallback_warnings = [
        w for w in caught
        if "fast_vertical_concat falling back" in str(w.message)
    ]
    assert not fallback_warnings, (
        f"Expected pre-alignment fast path, but fallback was triggered: "
        f"{[str(w.message) for w in fallback_warnings]}"
    )
    assert len(got) == 4
    assert got["tags"].iloc[0] == ["news"]
    assert got["tags"].iloc[1] == ["music", "pop"]
    assert got["tags"].iloc[2] is pd.NA or got["tags"].iloc[2] is None or (
        hasattr(got["tags"].iloc[2], "__len__") and len(got["tags"].iloc[2]) == 0
    )





def test_concat_list_vs_object_null_falls_back() -> None:
    """When schemas can't be pre-aligned, we must fall back to pandas.

    Object-dtype all-null columns have no pyarrow type to promote from, so
    pre-alignment can't touch them. The helper catches the polars error and
    falls back to `pd.concat`, emitting a RuntimeWarning.
    """
    tags_type = pd.ArrowDtype(pa.large_list(pa.large_string()))
    populated = pd.DataFrame({
        "item_id": pd.array(["v1", "v2"], dtype="string[pyarrow]"),
        "tags":    pd.array([["a", "b"], ["c"]], dtype=tags_type),
    })
    object_null = pd.DataFrame({
        "item_id": pd.array(["v3", "v4"], dtype="string[pyarrow]"),
        "tags":    pd.Series([None, None], dtype=object),
    })

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = fast_vertical_concat([populated, object_null])

    fallback_warnings = [
        w for w in caught
        if "fast_vertical_concat falling back" in str(w.message)
    ]
    assert fallback_warnings, "Expected fallback warning; got none"
    assert len(got) == 4
    # First two rows keep their list data regardless of fallback path.
    assert got["tags"].iloc[0] == ["a", "b"]
    assert got["tags"].iloc[1] == ["c"]





def test_join_output_with_all_null_list_column_does_not_raise() -> None:
    """Regression for the user-reported `ArrowNotImplementedError`.

    The shebang merge in `organize_datasets.py:1201` joins activity ×
    enriched on `item_id`. A newly-ingested collection can yield an
    `enriched` frame where a nested column (e.g. an annotation list) is
    typed as `large_list<large_string>` but entirely null — either
    because annotations are still pending or because all rows happen to
    have no matches.

    The resulting joined frame then carries an all-null list column,
    which pandas' whole-frame `convert_dtypes(dtype_backend='pyarrow')`
    cannot normalize: it internally tries to cast the column to the
    pyarrow `null` type, and pyarrow raises
    `ArrowNotImplementedError: Unsupported cast from
    large_list<item: large_string> to null`. Our helper must be
    resilient to this (via per-column dtype normalization) and return a
    usable DataFrame rather than propagating the pyarrow error.
    """
    activity = pd.DataFrame({
        "item_id": pd.array(["v1", "v2", "v3"], dtype="string[pyarrow]"),
        "event":   pd.array([1, 2, 3], dtype="int64[pyarrow]"),
    })
    # `enriched` has a list-typed column but no data matches the join key,
    # so every row in the joined output will have `tags` = null.
    tags_array = pd.arrays.ArrowExtensionArray(
        pa.nulls(1, type=pa.large_list(pa.large_string()))
    )
    enriched = pd.DataFrame({
        "item_id": pd.array(["never_matches"], dtype="string[pyarrow]"),
        "tags":    pd.Series(tags_array),
    })

    got = fast_join(activity, enriched, on="item_id", how="left")
    assert len(got) == 3
    assert "tags" in got.columns
    assert got["tags"].isna().all()





def test_concat_output_with_all_null_list_column_does_not_raise() -> None:
    """Same class of bug as the join version, exercised via concat.

    Two frames both carry `tags` as `large_list<large_string>` all-null;
    the concat output inherits that all-null nested column. Must not
    raise `ArrowNotImplementedError` during dtype normalization.
    """
    def _null_list_df(item_ids: list[str]) -> pd.DataFrame:
        return pd.DataFrame({
            "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
            "tags": pd.Series(
                pd.arrays.ArrowExtensionArray(
                    pa.nulls(len(item_ids), type=pa.large_list(pa.large_string()))
                )
            ),
        })

    got = fast_vertical_concat([_null_list_df(["v1", "v2"]), _null_list_df(["v3", "v4"])])
    assert len(got) == 4
    assert got["tags"].isna().all()





def test_concat_real_conflict_falls_back_cleanly() -> None:
    """When both sides have populated but genuinely-conflicting dtypes,
    we accept the (semantically correct but unergonomic) pandas behavior:
    the output column becomes object-typed with mixed contents. The helper
    must not crash — fallback covers this edge case."""
    tags_type = pd.ArrowDtype(pa.large_list(pa.large_string()))
    left = pd.DataFrame({
        "item_id": pd.array(["v1"], dtype="string[pyarrow]"),
        "tags":    pd.array([["actual", "list"]], dtype=tags_type),
    })
    right = pd.DataFrame({
        "item_id": pd.array(["v2"], dtype="string[pyarrow]"),
        "tags":    pd.array(["scalar string"], dtype="string[pyarrow]"),
    })

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = fast_vertical_concat([left, right])

    fallback_warnings = [
        w for w in caught
        if "fast_vertical_concat falling back" in str(w.message)
    ]
    assert fallback_warnings, "Expected fallback warning; got none"
    assert len(got) == 2
    assert list(got["item_id"]) == ["v1", "v2"]





def test_fast_join_downgrades_large_list_to_list() -> None:
    """Regression for the ``dictionary_encode``/``explode`` failure chain.

    Polars' native pandas bridge produces ``large_list<large_string>``,
    but pandas 2.2.x has partial kernel coverage for the large variants:
    notably ``DataFrame.explode()`` silently no-ops on ``large_list``
    columns, which then makes the PCA counts crosstab call
    ``dictionary_encode`` on a list-typed column and crash. We must
    downgrade to the regular variants so the whole downstream pipeline
    (explode → crosstab → PCA) keeps working."""
    tags_type = pd.ArrowDtype(pa.large_list(pa.large_string()))
    activity = pd.DataFrame({
        "item_id": pd.array(["v1", "v2", "v3"], dtype="string[pyarrow]"),
        "event":   pd.array([1, 2, 3], dtype="int64[pyarrow]"),
    })
    enriched = pd.DataFrame({
        "item_id": pd.array(["v1", "v2"], dtype="string[pyarrow]"),
        "tags":    pd.Series([["news", "politics"], ["music"]], dtype=tags_type),
    })

    got = fast_join(activity, enriched, on="item_id", how="left")

    # After fast_join, the list column must be `list<string>`, not
    # `large_list<large_string>`.
    tags_pa = got["tags"].dtype.pyarrow_dtype
    assert pa.types.is_list(tags_pa), (
        f"Expected list<..>, got {tags_pa!r}"
    )
    assert not pa.types.is_large_list(tags_pa)
    assert pa.types.is_string(tags_pa.value_type), (
        f"Expected inner string, got {tags_pa.value_type!r}"
    )

    # Full downstream path that was crashing in prod: explode + crosstab.
    exploded = got.explode("tags")
    # explode on a *list* (not large_list) must actually flatten.
    assert exploded["tags"].dtype == pd.ArrowDtype(pa.string())
    assert exploded["tags"].iloc[0] == "news"

    counts = pd.crosstab(
        index=[exploded["item_id"]], columns=[exploded["tags"]]
    )
    assert counts.loc["v1", "news"] == 1
    assert counts.loc["v1", "politics"] == 1
    assert counts.loc["v2", "music"] == 1





def test_fast_join_downgrades_large_string_scalar_columns() -> None:
    """Same downgrade principle applied to scalar string columns."""
    large_str_t = pd.ArrowDtype(pa.large_string())
    left = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "title":   pd.Series(["First", "Second"], dtype=large_str_t),
    })
    right = pd.DataFrame({
        "item_id": pd.array(["a"], dtype="string[pyarrow]"),
        "extra":   pd.Series(["X"], dtype=large_str_t),
    })

    got = fast_join(left, right, on="item_id", how="left")

    # Both string columns come back as regular `string`, not `large_string`.
    for col in ("title", "extra"):
        assert isinstance(got[col].dtype, pd.ArrowDtype)
        pa_t = got[col].dtype.pyarrow_dtype
        assert pa.types.is_string(pa_t) and not pa.types.is_large_string(pa_t), (
            f"Column {col!r} came back as {pa_t!r}, expected string"
        )





def test_fast_vertical_concat_downgrades_large_types() -> None:
    """Downgrade also applies on the concat path, matching join."""
    tags_type = pd.ArrowDtype(pa.large_list(pa.large_string()))
    a = pd.DataFrame({
        "item_id": pd.array(["v1", "v2"], dtype="string[pyarrow]"),
        "tags":    pd.Series([["a", "b"], ["c"]], dtype=tags_type),
    })
    b = pd.DataFrame({
        "item_id": pd.array(["v3", "v4"], dtype="string[pyarrow]"),
        "tags":    pd.Series([["d"], ["e", "f"]], dtype=tags_type),
    })

    got = fast_vertical_concat([a, b])

    tags_pa = got["tags"].dtype.pyarrow_dtype
    assert pa.types.is_list(tags_pa)
    assert not pa.types.is_large_list(tags_pa)
    assert pa.types.is_string(tags_pa.value_type)
    # explode must flatten
    exploded = got.explode("tags")
    assert exploded["tags"].iloc[0] == "a"





def test_concat_preserves_null_semantics() -> None:
    a = pd.DataFrame({
        "k": pd.array(["a", None, "c"], dtype="string[pyarrow]"),
        "v": pd.array([1, None, 3], dtype="int64[pyarrow]"),
    })
    b = pd.DataFrame({
        "k": pd.array([None, "e"], dtype="string[pyarrow]"),
        "v": pd.array([None, 5], dtype="int64[pyarrow]"),
    })

    got = fast_vertical_concat([a, b])
    want = _normalize(pd.concat([a, b], ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





# ---------------------------------------------------------------------------
# Section 2 — fast_join
# ---------------------------------------------------------------------------





def test_left_join_basic() -> None:
    # Mimics organize_datasets.py:1198 — activity × enriched on item_id.
    activity = pd.DataFrame({
        "item_id":  pd.array(["v1", "v2", "v3", "v4"], dtype="string[pyarrow]"),
        "event":    pd.array([10, 20, 30, 40], dtype="int64[pyarrow]"),
    })
    enriched = pd.DataFrame({
        "item_id":  pd.array(["v1", "v3"], dtype="string[pyarrow]"),
        "category": pd.array(["news", "music"], dtype="string[pyarrow]"),
    })

    got = fast_join(activity, enriched, on="item_id", how="left")
    want = _normalize(pd.merge(activity, enriched, on="item_id", how="left"))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_left_join_preserves_left_row_order() -> None:
    left = pd.DataFrame({
        "item_id": pd.array(["b", "a", "c", "a", "b"], dtype="string[pyarrow]"),
        "row":     pd.array([1, 2, 3, 4, 5], dtype="int64[pyarrow]"),
    })
    right = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "val":     pd.array([100, 200], dtype="int64[pyarrow]"),
    })

    got = fast_join(left, right, on="item_id", how="left")
    want = _normalize(pd.merge(left, right, on="item_id", how="left"))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_left_join_unmatched_rows_become_null() -> None:
    left = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "x":       pd.array([1, 2, 3], dtype="int64[pyarrow]"),
    })
    right = pd.DataFrame({
        "item_id": pd.array(["a"], dtype="string[pyarrow]"),
        "y":       pd.array([10], dtype="int64[pyarrow]"),
    })

    got = fast_join(left, right, on="item_id", how="left")
    want = _normalize(pd.merge(left, right, on="item_id", how="left"))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_left_join_many_to_many_duplicates_on_right() -> None:
    left = pd.DataFrame({
        "k": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "x": pd.array([1, 2], dtype="int64[pyarrow]"),
    })
    right = pd.DataFrame({
        "k": pd.array(["a", "a"], dtype="string[pyarrow]"),
        "y": pd.array([10, 20], dtype="int64[pyarrow]"),
    })

    got = fast_join(left, right, on="k", how="left")
    want = _normalize(pd.merge(left, right, on="k", how="left"))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





# ---------------------------------------------------------------------------
# Section 3 — recode_variables deferred-concat equivalence
# ---------------------------------------------------------------------------
#
# The goal is to show that the inner loop in `recode_variables.execute_recode`
# still produces the same result after replacing N in-loop concats with a
# single flush at the end. To keep the test self-contained and avoid loading
# the full fyp_cf config, we isolate the dict-unpacking subroutine here:
# a reference (old) implementation and the new (deferred) implementation,
# both operating on a `(cool_events, var_schema)` pair without any of the
# surrounding recode-policy logic.





def _old_dict_unpack(
    cool_events: pd.DataFrame, var_schema: pd.DataFrame
) -> pd.DataFrame:
    """Reference: in-loop concat (the original pattern pre-fix)."""
    cool_events = cool_events.copy()
    cool_columns = copy(cool_events.columns)
    for c in cool_columns:
        if c not in var_schema.index:
            continue
        valid_c = cool_events[c].dropna()
        if valid_c.empty:
            continue
        first_val = valid_c.iloc[0]
        if not isinstance(first_val, dict):
            continue

        new_thing = pd.json_normalize(cool_events[c])
        new_thing = new_thing.add_prefix(f"{c}_")
        new_thing.index = cool_events.index

        for new_thing_c in copy(new_thing.columns):
            if (
                new_thing_c not in var_schema.index
                or var_schema.loc[new_thing_c, "role"] == "skip"
            ):
                new_thing = new_thing.drop(columns=new_thing_c)

        if var_schema.loc[c, "role"] == "raw":
            cool_events = pd.concat(
                [cool_events.drop(columns=[c]), new_thing], axis=1
            )
        else:
            cool_events = pd.concat([cool_events, new_thing], axis=1)
    return cool_events





def _new_dict_unpack(
    cool_events: pd.DataFrame, var_schema: pd.DataFrame
) -> pd.DataFrame:
    """Candidate: deferred-concat (matches the fix in recode_variables.py)."""
    cool_events = cool_events.copy()
    cool_columns = copy(cool_events.columns)
    remaining_columns_by_index = [
        set(cool_columns[j + 1 :]) for j in range(len(cool_columns))
    ]
    deferred_unpacked_frames: list[pd.DataFrame] = []

    for i, c in enumerate(cool_columns):
        if c not in var_schema.index:
            continue
        valid_c = cool_events[c].dropna()
        if valid_c.empty:
            continue
        first_val = valid_c.iloc[0]
        if not isinstance(first_val, dict):
            continue

        new_thing = pd.json_normalize(cool_events[c])
        new_thing = new_thing.add_prefix(f"{c}_")
        new_thing.index = cool_events.index

        for new_thing_c in copy(new_thing.columns):
            if (
                new_thing_c not in var_schema.index
                or var_schema.loc[new_thing_c, "role"] == "skip"
            ):
                new_thing = new_thing.drop(columns=new_thing_c)

        if var_schema.loc[c, "role"] == "raw":
            cool_events = cool_events.drop(columns=[c])

        if set(new_thing.columns).isdisjoint(remaining_columns_by_index[i]):
            deferred_unpacked_frames.append(new_thing)
        else:
            cool_events = pd.concat([cool_events, new_thing], axis=1)

    if deferred_unpacked_frames:
        cool_events = pd.concat(
            [cool_events, *deferred_unpacked_frames], axis=1
        )
    return cool_events





def _build_schema(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """Build a minimal var_schema indexed by variable name with a 'role' col."""
    return pd.DataFrame(
        {"role": [r for _, r in rows]},
        index=pd.Index([n for n, _ in rows], name="variable_name"),
    )





def test_recode_defer_role_raw() -> None:
    """Dict column with role=raw: original dropped, children kept."""
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "meta":    [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}],
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("meta",    "raw"),
        ("meta_x",  "measure"),
        ("meta_y",  "measure"),
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





def test_recode_defer_role_nonraw_keeps_original() -> None:
    """Dict column with role!=raw: original kept, children added."""
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "meta":    [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("meta",    "measure"),   # NOT raw
        ("meta_x",  "measure"),
        ("meta_y",  "measure"),
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





def test_recode_defer_multiple_dict_columns() -> None:
    """The common case that benefits the most: many dict columns in one df."""
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "m1":      [{"a": 1}, {"a": 2}, {"a": 3}],
        "m2":      [{"b": 10, "c": 100}, {"b": 20, "c": 200}, {"b": 30, "c": 300}],
        "m3":      [{"d": 0.5}, {"d": 1.5}, {"d": 2.5}],
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("m1",      "raw"),
        ("m1_a",    "measure"),
        ("m2",      "raw"),
        ("m2_b",    "measure"),
        ("m2_c",    "measure"),
        ("m3",      "measure"),
        ("m3_d",    "measure"),
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





def test_recode_defer_skipped_children() -> None:
    """Children whose role is 'skip' are dropped from new_thing in both impls."""
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "meta":    [{"x": 1, "y": 2, "z": 3}, {"x": 4, "y": 5, "z": 6}],
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("meta",    "raw"),
        ("meta_x",  "measure"),
        ("meta_y",  "skip"),      # should be dropped from new_thing
        # meta_z not in schema → also dropped
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





def test_recode_defer_collision_with_future_iteration() -> None:
    """Edge case: an unpacked column name equals a future iteration's `c`.

    If dict column 'meta' unpacks to 'meta_x' and the outer loop is also
    scheduled to iterate the column named 'meta_x' (present in the input
    DataFrame from the start), the original code materialized the concat
    immediately so that subsequent access to `cool_events['meta_x']` would
    see the unpacked value. The deferred implementation must detect this
    collision and fall back to in-loop concat for that iteration.
    """
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "meta":    [{"x": 1}, {"x": 2}],
        "meta_x":  pd.array([99, 99], dtype="int64[pyarrow]"),   # collision!
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("meta",    "raw"),
        ("meta_x",  "measure"),
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





def test_recode_defer_no_dict_columns_noop() -> None:
    """A DataFrame with no dict columns should round-trip unchanged."""
    cool_events = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "n":       pd.array([1, 2, 3], dtype="int64[pyarrow]"),
    })
    var_schema = _build_schema([
        ("item_id", "factor"),
        ("n",       "measure"),
    ])

    old = _old_dict_unpack(cool_events, var_schema)
    new = _new_dict_unpack(cool_events, var_schema)
    pd.testing.assert_frame_equal(
        old.reset_index(drop=True), new.reset_index(drop=True)
    )





# ---------------------------------------------------------------------------
# Section 4 — smoke tests mirroring the real call sites
# ---------------------------------------------------------------------------





def test_ingest_accumulating_load_processed() -> None:
    """Mirror fyp/ingest.py:121 — accumulating concat pattern.

    Simulates multiple calls to `load_processed` that keep extending
    `self.data`. Compares the final `self.data` against the pandas baseline.
    """
    chunks = [
        pd.DataFrame({
            "raw_file": pd.array([f"f{i}_{k}" for k in range(3)], dtype="string[pyarrow]"),
            "n":        pd.array([i * 3 + k for k in range(3)], dtype="int64[pyarrow]"),
        })
        for i in range(5)
    ]
    # New impl: accumulate via fast_vertical_concat one chunk at a time.
    self_data = pd.DataFrame()
    for chunk in chunks:
        if len(self_data) > 0:
            self_data = fast_vertical_concat([self_data, chunk])
        else:
            self_data = chunk.copy()
    # Baseline: a single pandas concat of all chunks.
    want = _normalize(pd.concat(chunks, ignore_index=True))
    pd.testing.assert_frame_equal(
        self_data.reset_index(drop=True), want.reset_index(drop=True), check_dtype=True,
    )





def test_ingest_many_dfs_bulk_concat() -> None:
    """Mirror fyp/ingest.py:245 — bulk concat of per-file DataFrames."""
    many_dfs = [
        pd.DataFrame({
            "raw_file": pd.array([f"fn_{i}"] * 4, dtype="string[pyarrow]"),
            "event":    pd.array([i, i + 1, i + 2, i + 3], dtype="int64[pyarrow]"),
        })
        for i in range(7)
    ]
    got = fast_vertical_concat(many_dfs)
    want = _normalize(pd.concat(many_dfs, ignore_index=True))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





def test_organize_shebang_merge_shape() -> None:
    """Mirror fyp/organize_datasets.py:1198 — activity × enriched."""
    # 40 events spread across 5 items; 3 of 5 items have enrichment.
    activity = pd.DataFrame({
        "item_id":  pd.array([f"item{i % 5}" for i in range(40)], dtype="string[pyarrow]"),
        "event_n":  pd.array(list(range(40)), dtype="int64[pyarrow]"),
    })
    enriched = pd.DataFrame({
        "item_id":  pd.array(["item0", "item2", "item4"], dtype="string[pyarrow]"),
        "category": pd.array(["news", "music", "sport"], dtype="string[pyarrow]"),
        "score":    pd.array([0.1, 0.5, 0.9], dtype="double[pyarrow]"),
    })

    got = fast_join(activity, enriched, on="item_id", how="left")
    want = _normalize(pd.merge(activity, enriched, on="item_id", how="left"))
    pd.testing.assert_frame_equal(got, want, check_dtype=True)





# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------





TESTS = [
    # helpers
    test_concat_identical_schemas,
    test_concat_heterogeneous_schemas,
    test_concat_empty_and_nonempty,
    test_concat_single_frame_with_empty_siblings,
    test_concat_list_vs_allnull_string_prealigns,
    test_concat_list_vs_object_null_falls_back,
    test_join_output_with_all_null_list_column_does_not_raise,
    test_concat_output_with_all_null_list_column_does_not_raise,
    test_fast_join_downgrades_large_list_to_list,
    test_fast_join_downgrades_large_string_scalar_columns,
    test_fast_vertical_concat_downgrades_large_types,
    test_concat_real_conflict_falls_back_cleanly,
    test_concat_preserves_null_semantics,
    test_left_join_basic,
    test_left_join_preserves_left_row_order,
    test_left_join_unmatched_rows_become_null,
    test_left_join_many_to_many_duplicates_on_right,
    # recode deferred concat equivalence
    test_recode_defer_role_raw,
    test_recode_defer_role_nonraw_keeps_original,
    test_recode_defer_multiple_dict_columns,
    test_recode_defer_skipped_children,
    test_recode_defer_collision_with_future_iteration,
    test_recode_defer_no_dict_columns_noop,
    # call-site smoke tests
    test_ingest_accumulating_load_processed,
    test_ingest_many_dfs_bulk_concat,
    test_organize_shebang_merge_shape,
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
