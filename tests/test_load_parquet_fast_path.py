"""Confirm the convert_dtypes_to_pyarrow fast path:
  - returns identical output (shape, dtypes, values) for already-arrow input
  - meaningfully reduces wall-clock on full load_parquet() of a big file
"""
import sys
import time
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

import pandas as pd

from fyp import fyp_config
fyp_config.initialize()

from fyp import data_io
from fyp.types import convert_dtypes_to_pyarrow


def _expect(cond, msg):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        sys.exit(1)


def test_equivalence_on_arrow_df():
    print("\n[1] Fast-path returns equivalent DataFrame for already-arrow input")
    df0 = pd.read_parquet(
        join(fyp_config.fyp_cf['paths']['cache'], 'chenglong_recoded.parquet'),
        engine='pyarrow', dtype_backend='pyarrow',
    )
    out = convert_dtypes_to_pyarrow(df0, verbose=True)
    _expect(out.shape == df0.shape, f"shape preserved: {out.shape} == {df0.shape}")
    _expect(list(out.columns) == list(df0.columns), "column order preserved")
    _expect(all(out.dtypes == df0.dtypes), "dtypes preserved")
    # spot-check values on a few cols
    for c in df0.columns[:5]:
        _expect(out[c].equals(df0[c]), f"values preserved for column '{c}'")
    _expect(out is not df0, "returns a copy, not the same object (caller-mutation safety)")


def test_speedup_on_big_file():
    print("\n[2] Speedup on cache/everything_recoded.parquet")
    fname = 'everything_recoded.parquet'
    if not data_io.exists('cache', fname):
        print(f"      [SKIP] {fname} not present")
        return
    times = []
    for run in range(3):
        t0 = time.perf_counter()
        df = data_io.load_parquet(storage_location='cache', filename=fname, verbose=False)
        times.append(time.perf_counter() - t0)
    med = sorted(times)[len(times) // 2]
    print(f"      load_parquet medians (3 runs): {[f'{t:.3f}s' for t in sorted(times)]}")
    print(f"      median = {med:.3f}s   shape = {df.shape}")
    # Previous bench median: ~2.28s. Fast path should drop to ~1.15s or less.
    _expect(med < 2.0, f"median load time below 2.0s (was {med:.3f}s; target <1.5s)")


def test_metadata_load_still_works():
    print("\n[3] load_parquet of metadata file (MultiIndex repair) still works")
    from fyp.organize_datasets import COLLECTIONS_LABEL
    df = data_io.load_parquet(
        storage_location='recoded',
        filename=f'{COLLECTIONS_LABEL}_metadata.parquet',
        verbose=False,
    )
    _expect(df is not None and not df.empty, f"loaded ok ({df.shape if df is not None else None})")
    # Should have at least one tuple-form column repaired
    has_tuple = any(isinstance(c, tuple) for c in df.columns)
    _expect(has_tuple, f"MultiIndex columns repaired (cols sample: {list(df.columns)[:3]})")


def test_non_arrow_df_still_takes_slow_path():
    print("\n[4] Non-arrow input still goes through full conversion (sanity)")
    # Build a df that's *not* arrow-typed
    import numpy as np
    df = pd.DataFrame({
        'a': np.array([1, 2, 3], dtype='int64'),
        'b': np.array([1.0, 2.0, 3.0], dtype='float64'),
        'c': ['x', 'y', 'z'],
    })
    _expect(not all(isinstance(d, pd.ArrowDtype) for d in df.dtypes),
            "input df is NOT all-arrow (sanity)")
    out = convert_dtypes_to_pyarrow(df, verbose=False)
    _expect(all(isinstance(d, pd.ArrowDtype) for d in out.dtypes),
            f"slow-path output is all-arrow (got {list(out.dtypes)})")


if __name__ == '__main__':
    test_equivalence_on_arrow_df()
    test_speedup_on_big_file()
    test_metadata_load_still_works()
    test_non_arrow_df_still_takes_slow_path()
    print("\n[OK] Fast-path test suite passed.")
