# Selective parquet loading — measured findings

Date: 2026-04-14
Scripts: `tests/probe_parquet_schemas.py`, `tests/bench_parquet_loads.py`
Raw output: `tmp/parquet_schema_probe.txt`, `tmp/parquet_load_bench.txt`

## TL;DR

Selective loading **can make the app significantly faster** — but **not via the
obvious path**. The naive approach (`pd.read_parquet(columns=…,
dtype_backend='pyarrow')`) errors with
`TypeError: data type 'list<element: string>[pyarrow]' not understood` on
every parquet that contains a list-typed column, even when no list column is
projected. This is almost certainly what bit the user in the past.

The workaround that **does** work, and unlocks the speedups: read with
`pq.read_table(columns=…, filters=…)`, **strip the `b'pandas'` key from the
schema metadata**, then `to_pandas(types_mapper=pd.ArrowDtype)`. With that
single change, projection is safe and fast on every candidate file.

Row-filter pushdown is **mostly useless** in the current data layout — none
of the *_recoded.parquet files are sorted by the columns we'd filter on
(`collection_id`, `item_id`), so PyArrow still scans every row group. The
wins are essentially all from column projection.

## Headline numbers (median of 3 runs, MacBook Pro, OS file cache warm)

| File | Size | Rows × Cols | Current `data_io` | `strip_meta` projected | Speedup | Time saved |
|---|---:|---:|---:|---:|---:|---:|
| `collections_metadata.parquet` | 53 KB | 131 × 43 | 28 ms | 2 ms | **15.8×** | 26 ms |
| `collections_recoded.parquet` | 92 MB | 4.8M × 16 | 169 ms | 84 ms | **2.0×** | 85 ms |
| `scrapes_recoded.parquet` | 235 MB | 1.1M × 35 | 390 ms | 38 ms | **10.2×** | 352 ms |
| `machine_annotations_recoded.parquet` | 279 MB | 274K × 39 | 289 ms | 12 ms | **24.0×** | 277 ms |
| `enrichment_status.parquet` | 24 MB | 2.8M × 9 | 89 ms | 58 ms | 1.5× | 31 ms |
| `everything_recoded.parquet` | 1.14 GB | 4.7M × 91 | **2284 ms** | 91 ms | **25.1×** | 2193 ms |
| `paper_three_recoded.parquet` | 869 MB | 2.5M × 91 | **1589 ms** | 51 ms | **31.1×** | 1538 ms |
| `chenglong_recoded.parquet` | 26 MB | 33K × 91 | 59 ms | 4 ms | 13.8× | 55 ms |

The biggest absolute wins are on the **cache `*_recoded.parquet` files**:
~2 seconds saved per load on `everything_recoded.parquet`. These are loaded
during study refresh, PCA, and any per-study analysis.

## Where the time actually goes

Decomposing the cost (median seconds, `everything_recoded.parquet`):

```
  full (data_io, current)                   2.284 s
  raw_full (pd.read_parquet, dtype=pa)      1.135 s   ← post-pipeline costs ~1.1 s
  raw_cols_strip_meta (8 of 91 cols)        0.091 s   ← projection costs ~0.05 s
```

Two distinct overheads are stacked in `data_io.load_parquet()`:

1. **Post-load conversion** (`convert_dtypes_to_pyarrow` +
   `_repair_stringified_multiindex`) — costs ~50% of total wall-clock time
   on every load, even when the data is already pyarrow-typed on disk.
2. **Reading columns the caller doesn't use** — costs scale with file size
   and unused-column count.

Projection eliminates both because `strip_meta` reads less data AND skips
the conversion (data comes back already-typed).

## What did NOT work

Tried five alternative approaches; only one passed:

| Approach | Works? | Why / why not |
|---|---|---|
| `pd.read_parquet(columns=…, dtype_backend='pyarrow')` | ❌ on files with list columns | pandas tries to build ArrowDtype for ALL original columns from `pandas_metadata`, fails on `list<element: string>` notation |
| `pd.read_parquet(columns=…)` (no pa backend) | ❌ same error | same code path |
| `pyarrow.dataset.Scanner(columns=…).to_table().to_pandas(types_mapper=pd.ArrowDtype)` | ❌ same error | the table inherits pandas_metadata from the file |
| `pq.read_table(columns=…).to_pandas()` (no types_mapper) | ❌ same error | same |
| **`pq.read_table(columns=…) → strip b'pandas' → to_pandas(types_mapper=pd.ArrowDtype)`** | ✅ | bypasses dtype resolution for unselected columns |

## Files where projection is safe today

All eight tested files work with the `strip_meta` approach. There are no
caveats from the existing post-pipeline (`_repair_stringified_multiindex`
operates on the live column names — it doesn't need `pandas_metadata`).

**Caveat to confirm before rolling out**: stripping `pandas_metadata` may
also drop the implicit *index* column. For files written with
`df.to_parquet(...)` that had an index, the index becomes a regular column
in the result. The metadata file (`collections_metadata.parquet`) has
collection_id as the index — call sites that rely on
`df.index.astype(str) == collection_id` will need to either set the index
manually after read, OR include `__index_level_0__` in the columns list and
set it as the index post-read.

## Row-filter pushdown — measured, mostly useless

Filter on `collection_id == one_value` (~7-8 % of rows in each file):

| File | full read | full + filter | proj + filter | filter benefit vs proj alone |
|---|---:|---:|---:|---|
| `collections_recoded.parquet` | 169 ms | 104 ms | 86 ms | none (86 ms ≈ 84 ms proj-only) |
| `everything_recoded.parquet` | 2284 ms | 1177 ms | 93 ms | none (93 ms ≈ 91 ms proj-only) |
| `paper_three_recoded.parquet` | 1589 ms | 899 ms | 52 ms | none (52 ms ≈ 51 ms proj-only) |

Filtering on `item_id IN [100 values]` against `scrapes_recoded.parquet`:
390 ms full → 41 ms (proj+filter) ≈ 38 ms (proj alone). Same story.

**Why**: the parquet files aren't sorted by `collection_id` or `item_id`,
so pyarrow can't prune row groups via min/max statistics. The scan still
touches every row group; the filter just discards rows after decode.

If we wanted real filter benefit, the parquets would need to be **written
with sorting** (or partitioning) on the filter column. Worth considering
for the *_recoded.parquet files, separately.

## Caveats

- All measurements are **warm-cache**, on a local SSD (MacBook Pro). On
  Cloud Run + GCS, the gap will be **larger** in absolute terms because
  fewer bytes-over-network is a more direct win, but the relative speedup
  (×) will be similar — projection still has to send full row groups
  over the wire because parquet column chunks are stored separately.
- The post-pipeline (`convert_dtypes_to_pyarrow` +
  `_repair_stringified_multiindex`) appears to be redundant overhead on
  data that's already pyarrow-typed on disk; that's a separate cleanup
  worth measuring on its own.
- `data_io.save_parquet()` was not investigated. If it's writing
  `pandas_metadata` with the broken `list<element: string>` notation,
  a one-line fix at write time would let the naive `columns=` work too.
  That's a longer-term option vs. the strip_meta workaround.

## Recommendation for the follow-up implementation plan

1. **Add a `load_parquet_selective(storage_location, filename, columns=…,
   filters=…)` helper** to `fyp/data_io.py` that uses the
   read-table → strip-pandas-metadata → to_pandas pipeline. Keep it
   separate from `load_parquet()` so existing callers and the existing
   post-pipeline aren't disturbed.
2. **Apply at the cache-`*_recoded.parquet` read sites first** (PCA,
   study refresh, organize_datasets) — that's where the ~2-second wins
   live.
3. **Apply at the metadata read sites next** (`data_service.py:745`,
   `routes/data_routes.py:1707`, `run_timelines_refresh.py:98`) — small
   absolute wins but on a request handler.
4. **Don't bother with row filters** until/unless the *_recoded.parquet
   files are written sorted by collection_id (or partitioned by it).
   Filters are essentially free to add but currently buy nothing.
5. **Investigate the `convert_dtypes_to_pyarrow` overhead separately** —
   a 1.1-second tax on every full read of the big files is bigger than
   any individual filtering opportunity.
