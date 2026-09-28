"""convert_dtypes_to_pyarrow must keep Arrow date columns as dates.

pandas' ``convert_dtypes(dtype_backend="pyarrow")`` turns a ``date32`` column
into ``timestamp[ms]``; ``data_io.save_parquet`` runs every frame through the
conversion, so the activity table's ``local_date`` (a ``date32`` in the
activity contract) was stored as a timestamp.
"""

import pandas as pd

from fyp.core.types import convert_dtypes_to_pyarrow


def test_date32_survives_a_mixed_frame():
    ts = pd.Series(pd.to_datetime(["2026-09-01 10:00", "2026-09-02 23:30"]))
    df = pd.DataFrame(
        {
            "local_date": ts.dt.date.astype("date32[pyarrow]"),
            "n": [1, 2],  # a numpy column forces the batch conversion path
        }
    )

    out = convert_dtypes_to_pyarrow(df)

    assert str(out["local_date"].dtype) == "date32[day][pyarrow]", out["local_date"].dtype
    assert out["local_date"].tolist() == df["local_date"].tolist()
    assert isinstance(out["n"].dtype, pd.ArrowDtype)


def test_fast_join_keeps_date32():
    """new_merge joins through polars; the round trip must not widen dates."""
    from fyp.core.polars_ops import fast_join

    ts = pd.Series(pd.to_datetime(["2026-09-01 10:00", "2026-09-02 23:30"]))
    left = pd.DataFrame(
        {
            "item_id": pd.Series(["a", "b"], dtype="string[pyarrow]"),
            "local_date": ts.dt.date.astype("date32[pyarrow]"),
        }
    )
    right = pd.DataFrame(
        {
            "item_id": pd.Series(["a", "b"], dtype="string[pyarrow]"),
            "n": pd.Series([1, 2], dtype="int64[pyarrow]"),
        }
    )

    out = fast_join(left, right, on="item_id", how="left")

    assert str(out["local_date"].dtype) == "date32[day][pyarrow]", out["local_date"].dtype
