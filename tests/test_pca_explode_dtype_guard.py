"""Regression test for the explode dtype guard in transform_category_column_to_counts_df.

Reproduces the prod AttributeError ('StringDtype' object has no attribute
'pyarrow_dtype') and confirms list-valued and large_list features still count
correctly. Cost-free, no I/O.
"""

import pandas as pd
import pyarrow as pa

from fyp.pca import transform_category_column_to_counts_df


def _check(series: pd.Series, expected_total: float, label: str) -> None:
    df = pd.DataFrame(
        {
            "group": pd.array(["a", "a", "b"], dtype="string[pyarrow]"),
            "cat": series,
        }
    )
    counts = transform_category_column_to_counts_df(
        df, the_column="cat", grouping_factors=["group"]
    )
    total = float(counts.to_numpy().sum())
    assert total == expected_total, f"{label}: expected {expected_total} counts, got {total}"
    print(f"  OK {label}: shape={counts.shape} total_counts={total}")


def main() -> None:
    # 1. Scalar StringDtype column — the exact dtype that crashed in prod.
    _check(
        pd.array(["x", "y", "x"], dtype="string[pyarrow]"),
        expected_total=3.0,
        label="scalar string[pyarrow]",
    )

    # 2. pyarrow large_list column — explode was a silent no-op (under-count).
    _check(
        pd.Series(
            pd.arrays.ArrowExtensionArray(
                pa.array([["x", "y"], ["y"], ["z"]], type=pa.large_list(pa.large_string()))
            )
        ),
        expected_total=4.0,
        label="large_list<large_string>",
    )

    # 3. Plain python-list object column — the already-working path.
    _check(
        pd.Series([["x", "y"], ["y"], ["z"]], dtype=object),
        expected_total=4.0,
        label="object list",
    )

    print("All explode dtype-guard cases passed.")


if __name__ == "__main__":
    main()
