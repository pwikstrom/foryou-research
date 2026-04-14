"""Benchmark parquet load strategies for selective-loading investigation.

For each candidate parquet, time:
  (1) full       — current data_io.load_parquet (full read, full post-pipeline)
  (2) raw_full   — direct pq.read_table with no projection/filter, then to_pandas
  (3) raw_cols   — direct pq.read_table with column projection only
  (4) raw_full_filt   — direct pq.read_table with row filter only
  (5) raw_cols_filt   — direct pq.read_table with both projection and row filter
  (6) di_cols    — data_io.load_parquet with columns= (monkey-patched to expose it)
  (7) di_cols_filt   — data_io.load_parquet with columns= and filters=

Each variant runs once for cold-cache warmup (discarded) and then 3 timed runs
(median reported). Wall-clock + peak RSS delta. Output is a per-file table to
stdout AND tmp/parquet_load_bench.txt.

NOTE: This script monkey-patches data_io.load_parquet to expose `columns=`
inside the bench process only. It does NOT modify fyp/data_io.py.
"""
import gc
import os
import resource
import statistics
import sys
import time
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from fyp import fyp_config
fyp_config.initialize()
fyp_cf = fyp_config.fyp_cf

import fyp.data_io as data_io
from fyp.types import convert_dtypes_to_pyarrow

# Keep a handle on the original
_orig_load_parquet = data_io.load_parquet


def _patched_load_parquet(storage_location='cache', filename='', columns=None,
                           filters=None, verbose=False):
    """Re-implement load_parquet with `columns` actually exposed.

    Mirrors the local-file branch of fyp/data_io.py:load_parquet, including the
    post-load convert_dtypes + multiindex repair, so we measure apples-to-apples.
    """
    from fyp.data_io import _resolve_paths, _repair_stringified_multiindex, exists
    if filename == '' or storage_location == '':
        raise ValueError('storage_location and filename are required')
    if not exists(storage_location, filename):
        raise FileNotFoundError(filename)
    primary, _, mode, _ = _resolve_paths(storage_location, filename)
    # Local only for the bench
    if columns is not None:
        try:
            existing = pq.read_schema(primary).names
            columns = [c for c in columns if c in existing]
        except Exception:
            pass
    df = pd.read_parquet(
        primary,
        engine='pyarrow',
        dtype_backend='pyarrow',
        use_threads=True,
        columns=columns,
        filters=filters,
    )
    df = convert_dtypes_to_pyarrow(df, verbose=False)
    df = _repair_stringified_multiindex(df)
    return df


LOCAL_DATA = fyp_cf['paths']['local_data']
COLLECTIONS_LABEL = fyp_cf['labels']['COLLECTIONS_LABEL']


def _resolve_path(storage_location, filename):
    return join(LOCAL_DATA, storage_location, filename)


# Per-file projection / filter recipes that mirror the actual hot-path usage
# documented in the plan. `columns` are on-disk names. `filter_factory` builds
# a filter expression from the parquet itself (so we don't hardcode IDs that
# may not exist in any given dev dataset).
RECIPES = [
    {
        'name': 'collections_metadata.parquet',
        'storage_location': 'recoded',
        'cols': [
            "('personas', 'first_event_ts')",
            "('personas', 'total_events')",
            "('other', 'ts_added_to_dataset')",
            "('counts', 'total')",
        ],
        # Metadata uses the index for collection_id; on-disk this is __index_level_0__
        # so filtering needs to use that name. Skip filter for this file.
        'filter_factory': None,
    },
    {
        'name': 'collections_recoded.parquet',
        'storage_location': 'recoded',
        'cols': ['collection_id', 'item_id', 'local_timestamp', 'activity_type'],
        # Filter on collection_id — pick the most common one in the file.
        'filter_factory': lambda path: _filter_top_string(path, 'collection_id'),
    },
    {
        'name': 'scrapes_recoded.parquet',
        'storage_location': 'recoded',
        'cols': ['item_id', 'video_duration', 'stats_playCount', 'createTime'],
        # Filter on a small set of item_ids
        'filter_factory': lambda path: _filter_sample_strings(path, 'item_id', n=100),
    },
    {
        'name': 'machine_annotations_recoded.parquet',
        'storage_location': 'recoded',
        'cols': ['item_id', 'main_activity', 'political_score', 'sensitivity_score',
                 'aigc', 'tiktok_native', 'advertising'],
        'filter_factory': lambda path: _filter_sample_strings(path, 'item_id', n=100),
    },
    {
        'name': 'enrichment_status.parquet',
        'storage_location': 'recoded',
        'cols': ['item_id', 'scraped_ok', 'annotated_ok'],
        'filter_factory': lambda path: ('scraped_ok', '==', True),
    },
    {
        'name': 'everything_recoded.parquet',
        'storage_location': 'cache',
        'cols': ['collection_id', 'item_id', 'local_timestamp', 'activity_type',
                 'main_activity', 'political_score', 'sensitivity_score',
                 'video_duration'],
        'filter_factory': lambda path: _filter_top_string(path, 'collection_id'),
    },
    {
        'name': 'paper_three_recoded.parquet',
        'storage_location': 'cache',
        'cols': ['collection_id', 'item_id', 'local_timestamp', 'activity_type',
                 'main_activity', 'political_score', 'sensitivity_score',
                 'video_duration'],
        'filter_factory': lambda path: _filter_top_string(path, 'collection_id'),
    },
    {
        'name': 'chenglong_recoded.parquet',
        'storage_location': 'cache',
        'cols': ['collection_id', 'item_id', 'local_timestamp', 'activity_type',
                 'main_activity', 'political_score', 'sensitivity_score',
                 'video_duration'],
        'filter_factory': lambda path: _filter_top_string(path, 'collection_id'),
    },
]


def _filter_top_string(path, column):
    """Build a row filter that selects the most-common value of a string column."""
    table = pq.read_table(path, columns=[column])
    arr = table.column(column).combine_chunks()
    counts = pc.value_counts(arr)
    # value_counts returns a struct array with 'values' and 'counts' fields
    values = counts.field('values').to_pylist()
    counts_list = counts.field('counts').to_pylist()
    pairs = [(v, c) for v, c in zip(values, counts_list) if v is not None]
    if not pairs:
        return None
    pairs.sort(key=lambda p: -p[1])
    top_value = pairs[0][0]
    return (column, '==', top_value)


def _filter_sample_strings(path, column, n=100):
    """Build a row filter selecting `n` sample values from a string column."""
    table = pq.read_table(path, columns=[column])
    arr = table.column(column).combine_chunks().to_pylist()
    seen = []
    for v in arr:
        if v is not None and v not in seen:
            seen.append(v)
            if len(seen) >= n:
                break
    if not seen:
        return None
    return (column, 'in', seen)


def _peak_rss_mb():
    # On macOS, ru_maxrss is in bytes; on Linux, in KB. We're on darwin.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _time_runs(label, fn, n_runs=3):
    """Warmup once (discarded), then n_runs timed. Return median seconds and
    the (rows, cols) shape of the last result. On exception, returns an error
    record so the bench can continue past a failing variant."""
    try:
        _ = fn()
    except Exception as e:
        return {'label': label, 'error': f"{type(e).__name__}: {e}",
                'median_s': float('nan'), 'min_s': float('nan'), 'max_s': float('nan'),
                'rows': 0, 'cols': 0, 'rss_delta_mb': 0.0}
    gc.collect()
    times = []
    shape = (None, None)
    rss_before = _peak_rss_mb()
    for _ in range(n_runs):
        gc.collect()
        t0 = time.perf_counter()
        try:
            df = fn()
        except Exception as e:
            return {'label': label, 'error': f"{type(e).__name__}: {e}",
                    'median_s': float('nan'), 'min_s': float('nan'), 'max_s': float('nan'),
                    'rows': 0, 'cols': 0, 'rss_delta_mb': 0.0}
        dt = time.perf_counter() - t0
        times.append(dt)
        shape = (len(df), len(df.columns))
        del df
        gc.collect()
    rss_after = _peak_rss_mb()
    return {
        'label': label,
        'median_s': statistics.median(times),
        'min_s': min(times),
        'max_s': max(times),
        'rows': shape[0],
        'cols': shape[1],
        'rss_delta_mb': rss_after - rss_before,
    }


def _read_parquet_raw(path, columns=None, filters=None):
    """Match the engine/dtype settings used by fyp/data_io.load_parquet, so the
    raw_* variants measure pd.read_parquet directly without the post-pipeline.
    """
    return pd.read_parquet(
        path,
        engine='pyarrow',
        dtype_backend='pyarrow',
        use_threads=True,
        columns=columns,
        filters=filters,
    )


def bench_recipe(recipe, out):
    name = recipe['name']
    storage_location = recipe['storage_location']
    path = _resolve_path(storage_location, name)

    header = f"\n{'=' * 78}\n{storage_location}/{name}\n{'=' * 78}"
    print(header)
    out.append(header)

    if not os.path.exists(path):
        msg = f"  [SKIP] not found at {path}"
        print(msg)
        out.append(msg)
        return

    size_mb = os.path.getsize(path) / (1024 * 1024)
    n_rows = pq.ParquetFile(path).metadata.num_rows
    n_cols = len(pq.read_schema(path).names)
    info = f"  size={size_mb:.1f} MB  rows={n_rows:,}  cols={n_cols}"
    print(info)
    out.append(info)

    cols = recipe['cols']
    cols_present = [c for c in cols if c in pq.read_schema(path).names]
    print(f"  projected cols ({len(cols_present)}/{len(cols)} present): {cols_present}")
    out.append(f"  projected cols ({len(cols_present)}/{len(cols)} present): {cols_present}")

    filt = None
    if recipe['filter_factory'] is not None:
        try:
            filt = recipe['filter_factory'](path)
            if filt:
                # Compact filter for display (truncate long IN-lists)
                col, op, val = filt
                if isinstance(val, list) and len(val) > 5:
                    disp = f"({col!r}, {op!r}, [<{len(val)} values>])"
                else:
                    disp = f"({col!r}, {op!r}, {val!r})"
                print(f"  row filter: {disp}")
                out.append(f"  row filter: {disp}")
        except Exception as e:
            print(f"  row filter: <failed to build: {e}>")
            out.append(f"  row filter: <failed to build: {e}>")
            filt = None

    filters_arg = [filt] if filt else None

    # Build benchmark functions
    def f_full():
        return _orig_load_parquet(storage_location=storage_location, filename=name)

    def f_raw_full():
        return _read_parquet_raw(path)

    def f_raw_cols():
        return _read_parquet_raw(path, columns=cols_present)

    def f_raw_cols_numpy():
        # Workaround for the dtype-resolution bug: drop dtype_backend='pyarrow'.
        # Returns numpy-backed dataframe; post-pipeline would need to upcast.
        return pd.read_parquet(
            path, engine='pyarrow', use_threads=True, columns=cols_present)

    def f_raw_cols_dataset():
        # Alternate path: pyarrow.dataset (avoids pandas_metadata interpretation).
        import pyarrow.dataset as pads
        ds = pads.dataset(path, format='parquet')
        scanner = ds.scanner(columns=cols_present)
        tbl = scanner.to_table()
        return tbl.to_pandas(types_mapper=pd.ArrowDtype)

    def f_raw_cols_strip_meta():
        # Workaround for the list<element/item:string>[pyarrow] dtype bug:
        # strip the pandas_metadata embedded in the parquet schema before
        # converting to pandas. This is the only variant that should work for
        # files containing list-typed columns (even when those aren't selected).
        tbl = pq.read_table(path, columns=cols_present)
        # Drop the b'pandas' key from schema metadata
        meta = tbl.schema.metadata or {}
        new_meta = {k: v for k, v in meta.items() if k != b'pandas'}
        tbl = tbl.replace_schema_metadata(new_meta or None)
        return tbl.to_pandas(types_mapper=pd.ArrowDtype)

    def f_raw_cols_filt_strip_meta():
        tbl = pq.read_table(path, columns=cols_present, filters=filters_arg)
        meta = tbl.schema.metadata or {}
        new_meta = {k: v for k, v in meta.items() if k != b'pandas'}
        tbl = tbl.replace_schema_metadata(new_meta or None)
        return tbl.to_pandas(types_mapper=pd.ArrowDtype)

    def f_raw_full_filt():
        return _read_parquet_raw(path, filters=filters_arg)

    def f_raw_cols_filt():
        return _read_parquet_raw(path, columns=cols_present, filters=filters_arg)

    def f_di_cols():
        return _patched_load_parquet(storage_location=storage_location, filename=name,
                                      columns=cols_present)

    def f_di_cols_filt():
        return _patched_load_parquet(storage_location=storage_location, filename=name,
                                      columns=cols_present, filters=filters_arg)

    runs = []
    runs.append(_time_runs('full (data_io, current)', f_full))
    runs.append(_time_runs('raw_full (pd, dtype=pa)', f_raw_full))
    runs.append(_time_runs('raw_cols (pd, dtype=pa, projected)', f_raw_cols))
    runs.append(_time_runs('raw_cols_numpy (pd, projected, no pa backend)', f_raw_cols_numpy))
    runs.append(_time_runs('raw_cols_dataset (pyarrow.dataset, projected)', f_raw_cols_dataset))
    runs.append(_time_runs('raw_cols_strip_meta (pq, projected, no pd-meta)', f_raw_cols_strip_meta))
    if filters_arg:
        runs.append(_time_runs('raw_full_filt (pd, dtype=pa, filter)', f_raw_full_filt))
        runs.append(_time_runs('raw_cols_filt (pd, dtype=pa, proj+filter)', f_raw_cols_filt))
        runs.append(_time_runs('raw_cols_filt_strip_meta (pq, proj+filter, no pd-meta)', f_raw_cols_filt_strip_meta))
    runs.append(_time_runs('di_cols (data_io patched, projected)', f_di_cols))
    if filters_arg:
        runs.append(_time_runs('di_cols_filt (data_io patched, proj+filter)', f_di_cols_filt))

    base = runs[0]['median_s']
    line = f"  {'variant':48s} {'median':>9s}  {'min':>9s}  {'max':>9s}  {'shape':>14s}  speedup"
    print(line)
    out.append(line)
    import math
    for r in runs:
        if 'error' in r:
            line = f"  {r['label']:48s}   ERROR  {r['error']}"
        else:
            speedup = base / r['median_s'] if r['median_s'] > 0 and not math.isnan(base) else float('nan')
            line = (f"  {r['label']:48s} {r['median_s']:>8.3f}s  "
                    f"{r['min_s']:>8.3f}s  {r['max_s']:>8.3f}s  "
                    f"{r['rows']:>6d}x{r['cols']:<5d}    {speedup:5.1f}x")
        print(line)
        out.append(line)


def main():
    out = []
    for recipe in RECIPES:
        bench_recipe(recipe, out)
        # Free between files
        gc.collect()

    out_path = abspath(join(dirname(__file__), '..', 'tmp', 'parquet_load_bench.txt'))
    os.makedirs(dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        f.write('\n'.join(out) + '\n')
    print(f"\n[OK] Wrote {out_path}")


if __name__ == '__main__':
    main()
