"""Verify the three production call-sites still get the columns/index they
expect after switching to load_parquet_selective().

Mirrors the exact arguments used in:
  - web_interface/data_service.py:745
  - web_interface/routes/data_routes.py:1707
  - web_interface/run_timelines_refresh.py:98
"""
import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(join(dirname(__file__), '..')))

from fyp import fyp_config
fyp_config.initialize()

from fyp import data_io
from fyp.organize_datasets import COLLECTIONS_LABEL


def _expect(cond, msg):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        sys.exit(1)


def test_data_service_745():
    print("\n[A] data_service.py:745 — first_event_ts for one collection")
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
        columns=["('personas', 'first_event_ts')", "first_event_ts"],
        set_index='collection_id',
        verbose=False,
    )
    _expect(df is not None, "df not None")
    _expect(df.index.name == 'collection_id', f"index is collection_id (got {df.index.name!r})")
    has_tuple_col = ('personas', 'first_event_ts') in df.columns
    has_flat_col = 'first_event_ts' in df.columns
    _expect(has_tuple_col or has_flat_col,
            f"first_event_ts column present (cols: {list(df.columns)})")
    print(f"      shape={df.shape}, MultiIndex cols? {has_tuple_col}, flat? {has_flat_col}")
    # Real downstream check: pick a collection_id and grab its first_event_ts
    sample_cid = df.index[0]
    row = df[df.index.astype(str) == str(sample_cid)]
    _expect(not row.empty, f"can locate sample collection {sample_cid}")


def test_data_routes_1707():
    print("\n[B] data_routes.py:1707 — accepted mask + collection_id")
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
        columns=["('other', 'accepted')", "accepted"],
        set_index='collection_id',
    )
    _expect(df is not None, "df not None")
    _expect(df.index.name == 'collection_id', f"index is collection_id (got {df.index.name!r})")
    accepted_col = None
    if ('other', 'accepted') in df.columns:
        accepted_col = ('other', 'accepted')
    elif 'accepted' in df.columns:
        accepted_col = 'accepted'
    _expect(accepted_col is not None,
            f"accepted column present (cols: {list(df.columns)})")
    accepted_mask = df[accepted_col] == True
    print(f"      shape={df.shape}, accepted={int(accepted_mask.sum())}/{len(df)}")
    # The handler does df_reset = df.reset_index() — verify that works
    df_reset = df.reset_index()
    _expect('collection_id' in df_reset.columns,
            f"reset_index produced collection_id column")


def test_run_timelines_refresh_98():
    print("\n[C] run_timelines_refresh.py:98 — accepted + first_event_ts")
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
        columns=[
            "('other', 'accepted')", "other_accepted",
            "('personas', 'first_event_ts')", "first_event_ts",
        ],
        set_index='collection_id',
        verbose=False,
    )
    _expect(df is not None, "df not None")
    _expect(df.index.name == 'collection_id', f"index is collection_id (got {df.index.name!r})")
    found_accepted = (('other', 'accepted') in df.columns
                      or 'other_accepted' in df.columns)
    found_fe = (('personas', 'first_event_ts') in df.columns
                or 'first_event_ts' in df.columns)
    _expect(found_accepted, f"accepted column present (cols: {list(df.columns)})")
    _expect(found_fe, f"first_event_ts column present (cols: {list(df.columns)})")
    print(f"      shape={df.shape}, cols={list(df.columns)}")
    # Replicate downstream lookup pattern
    if ('other', 'accepted') in df.columns:
        accepted_mask = df[('other', 'accepted')] == True
        all_cols = set(df[accepted_mask].index.astype(str))
        print(f"      accepted collections: {len(all_cols)}")


if __name__ == '__main__':
    test_data_service_745()
    test_data_routes_1707()
    test_run_timelines_refresh_98()
    print("\n[OK] All metadata selective-load patterns work end-to-end.")
