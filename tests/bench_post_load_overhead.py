"""Decompose the post-load overhead in data_io.load_parquet().

For everything_recoded.parquet (1.14 GB / 4.7M x 91), the bench previously
showed ~1.15s of overhead between `pd.read_parquet(dtype_backend='pyarrow')`
finishing and `data_io.load_parquet()` returning. This script attributes
that time to specific steps so we know what to skip.
"""
import sys
import time
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

import pandas as pd
import pyarrow as pa

from fyp import fyp_config
fyp_config.initialize()

from fyp.data_io import _repair_stringified_multiindex
from fyp.types import convert_dtypes_to_pyarrow


CANDIDATES = [
    ('cache', 'everything_recoded.parquet'),
    ('cache', 'paper_three_recoded.parquet'),
    ('recoded', 'collections_metadata.parquet'),
]


def _time(label, fn):
    t0 = time.perf_counter()
    r = fn()
    return label, (time.perf_counter() - t0) * 1000.0, r


def _all_arrow(df):
    return all(isinstance(d, pd.ArrowDtype) for d in df.dtypes)


def bench(storage, fname):
    from fyp import fyp_config as cfg
    base = cfg.fyp_cf['paths'][storage]
    path = join(base, fname)

    print(f"\n{'='*78}\n{storage}/{fname}")

    # --- Read once to a baseline df (this is the input to all post-steps) ---
    t0 = time.perf_counter()
    df0 = pd.read_parquet(path, engine='pyarrow', dtype_backend='pyarrow', use_threads=True)
    raw_ms = (time.perf_counter() - t0) * 1000.0
    print(f"  shape={df0.shape}  size_on_disk_MB~  raw_read={raw_ms:.1f}ms  all_arrow_dtypes={_all_arrow(df0)}")

    # === Decompose the post-load pipeline ===
    # The pipeline (data_io.load_parquet lines 808-810 / 862-864) is:
    #   df = convert_dtypes_to_pyarrow(df)
    #   df = _repair_stringified_multiindex(df)
    #
    # convert_dtypes_to_pyarrow itself is:
    #   1) df = df.copy()
    #   2) df = df.convert_dtypes(dtype_backend='pyarrow')
    #   3) iterate object-typed columns (none if already arrow)
    #   4) describe() over numeric columns to catch overflows

    rows = []
    for run in range(3):
        df = df0.copy()  # fresh starting point each run
        results = []

        # 1. df.copy() (the very first thing convert_dtypes_to_pyarrow does)
        results.append(_time("1.copy", lambda: df.copy()))
        df_after_copy = results[-1][2]

        # 2. df.convert_dtypes(dtype_backend='pyarrow')
        results.append(_time("2.convert_dtypes(pa)", lambda: df_after_copy.convert_dtypes(dtype_backend='pyarrow')))
        df_after_cd = results[-1][2]

        # 3. object-column refinement loop (count only)
        n_object = sum(1 for c in df_after_cd.columns if df_after_cd[c].dtype == 'object')
        results.append(("3.object_cols_count", 0.0, n_object))

        # 4. numeric overflow check via .describe()
        numeric_cols = [c for c in df_after_cd.columns
                        if pd.api.types.is_numeric_dtype(df_after_cd[c])]
        results.append(_time(f"4.describe(numeric={len(numeric_cols)})",
                             lambda: df_after_cd[numeric_cols].describe()))

        # 5. _repair_stringified_multiindex
        results.append(_time("5.repair_multiindex", lambda: _repair_stringified_multiindex(df_after_cd.copy())))

        # 6. Total: full convert_dtypes_to_pyarrow + repair (the actual pipeline)
        results.append(_time("6.full_pipeline", lambda: _repair_stringified_multiindex(
            convert_dtypes_to_pyarrow(df0, verbose=False))))

        rows.append(results)

    # Aggregate medians
    n = len(rows[0])
    print(f"  {'step':<32} {'med ms':>10}  {'min':>8}  {'max':>8}")
    for i in range(n):
        label = rows[0][i][0]
        times = sorted(r[i][1] for r in rows)
        med = times[1] if len(times) >= 2 else times[0]
        print(f"  {label:<32} {med:>10.1f}  {times[0]:>8.1f}  {times[-1]:>8.1f}")


if __name__ == '__main__':
    for s, f in CANDIDATES:
        try:
            bench(s, f)
        except FileNotFoundError as e:
            print(f"\n[SKIP] {s}/{f} — not present locally ({e})")
