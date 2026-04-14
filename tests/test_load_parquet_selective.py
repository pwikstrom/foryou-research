"""Verify load_parquet_selective() works end-to-end on real local data.

Covers the four shapes the production callers will use:
  1. metadata file with MultiIndex columns + index column
  2. recoded file with simple-typed columns + collection_id filter
  3. cache file (huge) with column projection + filter
  4. graceful failure when requested columns don't exist
"""
import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

import pandas as pd

from fyp import fyp_config
fyp_config.initialize()

from fyp import data_io
from fyp.organize_datasets import COLLECTIONS_LABEL


def _expect(cond, msg):
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        sys.exit(1)


def test_metadata_with_multiindex():
    print("\n[1] Metadata file with MultiIndex columns + set_index='collection_id'")
    df = data_io.load_parquet_selective(
        storage_location='recoded',
        filename=f'{COLLECTIONS_LABEL}_metadata.parquet',
        columns=[
            "('personas', 'first_event_ts')",
            "('personas', 'total_events')",
            "('other', 'ts_added_to_dataset')",
        ],
        set_index='collection_id',
        verbose=True,
    )
    _expect(df is not None, "df is not None")
    _expect(len(df) > 0, f"df has rows ({len(df)})")
    _expect(('personas', 'first_event_ts') in df.columns,
            "MultiIndex column ('personas', 'first_event_ts') restored")
    _expect(('personas', 'total_events') in df.columns,
            "MultiIndex column ('personas', 'total_events') restored")
    _expect(df.index.name == 'collection_id',
            f"index name is collection_id (got {df.index.name!r})")
    print(f"      head:\n{df.head(2).to_string()}")


def test_recoded_with_filter():
    print("\n[2] Recoded events with column projection + collection_id filter")
    # First find an actual collection_id in the file
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    path = join(fyp_config.fyp_cf['paths']['recoded'], f'{COLLECTIONS_LABEL}_recoded.parquet')
    one = pq.read_table(path, columns=['collection_id']).column('collection_id')
    sample_id = one.combine_chunks().to_pylist()[0]
    print(f"      using sample collection_id: {sample_id}")

    df = data_io.load_parquet_selective(
        storage_location='recoded',
        filename=f'{COLLECTIONS_LABEL}_recoded.parquet',
        columns=['collection_id', 'item_id', 'local_timestamp', 'activity_type'],
        filters=[('collection_id', '==', sample_id)],
        verbose=True,
    )
    _expect(df is not None, "df is not None")
    _expect(len(df) > 0, f"filtered df has rows ({len(df)})")
    _expect(set(df.columns) == {'collection_id', 'item_id', 'local_timestamp', 'activity_type'},
            f"columns match request (got {list(df.columns)})")
    _expect((df['collection_id'].astype(str) == str(sample_id)).all(),
            "all rows match the filter")
    print(f"      shape={df.shape}, dtypes={dict(df.dtypes)}")


def test_cache_with_list_columns_present():
    print("\n[3] Cache *_recoded.parquet (has list columns we don't want)")
    cache_dir = fyp_config.fyp_cf['paths']['cache']
    import os
    candidates = [f for f in os.listdir(cache_dir)
                  if f.endswith('_recoded.parquet') and not f.startswith('timeline_')]
    if not candidates:
        print("      [SKIP] no *_recoded.parquet files in cache")
        return
    target = sorted(candidates, key=lambda f: os.path.getsize(join(cache_dir, f)))[-1]
    print(f"      using: cache/{target}")

    df = data_io.load_parquet_selective(
        storage_location='cache',
        filename=target,
        columns=['collection_id', 'item_id', 'local_timestamp',
                 'main_activity', 'political_score', 'sensitivity_score'],
        verbose=True,
    )
    _expect(df is not None, "df is not None (no list-dtype error)")
    _expect(len(df) > 0, f"df has rows ({len(df)})")
    expected = {'collection_id', 'item_id', 'local_timestamp',
                'main_activity', 'political_score', 'sensitivity_score'}
    _expect(set(df.columns) == expected,
            f"columns match request (got {set(df.columns)})")


def test_missing_column_handled():
    print("\n[4] Missing column in request is silently dropped (with warn)")
    df = data_io.load_parquet_selective(
        storage_location='recoded',
        filename=f'{COLLECTIONS_LABEL}_recoded.parquet',
        columns=['collection_id', 'this_column_does_not_exist'],
        verbose=True,
    )
    _expect(df is not None, "df is not None")
    _expect(set(df.columns) == {'collection_id'},
            f"only existing column returned (got {set(df.columns)})")


def test_all_columns_missing_returns_none():
    print("\n[5] All requested columns missing -> returns None with warning")
    df = data_io.load_parquet_selective(
        storage_location='recoded',
        filename=f'{COLLECTIONS_LABEL}_recoded.parquet',
        columns=['nope', 'also_nope'],
        verbose=True,
    )
    _expect(df is None, "df is None when no requested cols exist")


if __name__ == '__main__':
    test_metadata_with_multiindex()
    test_recoded_with_filter()
    test_cache_with_list_columns_present()
    test_missing_column_handled()
    test_all_columns_missing_returns_none()
    print("\n[OK] All checks passed.")
