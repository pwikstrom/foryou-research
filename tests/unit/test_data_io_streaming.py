"""Streaming data_io primitives (local mode): iter/write/concat/bytes/ranges.

GCS branches mirror the exact plumbing load_parquet_selective / save_text
already use in production; these tests pin the local semantics and the
shared contracts (projection skip, filter shapes, schema handling).
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import fyp.data_io as data_io

LOC = "cache"






@pytest.fixture
def local_cache(tmp_path, monkeypatch):
    """Point the 'cache' location at a temp dir (local mode)."""
    from fyp.fyp_config import fyp_cf

    monkeypatch.setitem(fyp_cf["paths"], LOC, str(tmp_path))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    return tmp_path






def _frame(n: int = 1000) -> pd.DataFrame:
    return pd.DataFrame({
        "collection_id": pd.array([f"c{i % 7}" for i in range(n)], dtype="string[pyarrow]"),
        "value": pd.array(np.arange(n), dtype="int64[pyarrow]"),
    })






def test_iter_parquet_batches_streams_all_rows(local_cache):
    df = _frame(1000)
    data_io.save_parquet(df=df, storage_location=LOC, filename="s.parquet")

    batches = list(data_io.iter_parquet_batches(
        storage_location=LOC, filename="s.parquet", batch_size=128))
    assert len(batches) >= 8  # 1000 rows / 128
    got = pa.Table.from_batches(batches).to_pandas()
    assert len(got) == 1000
    assert sorted(got.columns) == ["collection_id", "value"]






def test_iter_parquet_batches_projection_and_filter(local_cache):
    data_io.save_parquet(df=_frame(500), storage_location=LOC, filename="s.parquet")

    batches = list(data_io.iter_parquet_batches(
        storage_location=LOC, filename="s.parquet",
        columns=["value", "not_a_column"],
        filters=[("collection_id", "in", ["c0", "c1"])]))
    got = pa.Table.from_batches(batches)
    assert got.column_names == ["value"]  # missing column skipped
    # c0: ids 0,7,14... c1: 1,8,15... -> 2/7 of 500 rows (72 + 72 = 143 or 144)
    assert 140 <= got.num_rows <= 146






def test_iter_parquet_batches_no_valid_columns_yields_nothing(local_cache):
    data_io.save_parquet(df=_frame(10), storage_location=LOC, filename="s.parquet")
    out = list(data_io.iter_parquet_batches(
        storage_location=LOC, filename="s.parquet", columns=["nope"]))
    assert out == []






def test_write_parquet_stream_roundtrip(local_cache):
    schema = pa.schema([("a", pa.string()), ("b", pa.float32())])
    batches = [
        pa.record_batch([pa.array(["x", "y"]), pa.array([1.0, 2.0], type=pa.float32())], schema=schema),
        pa.record_batch([pa.array(["z"]), pa.array([3.0], type=pa.float32())], schema=schema),
    ]
    n = data_io.write_parquet_stream(
        storage_location=LOC, filename="out.parquet", batches=batches, schema=schema)
    assert n == 3
    back = data_io.load_parquet_selective(storage_location=LOC, filename="out.parquet")
    assert back["a"].tolist() == ["x", "y", "z"]






def test_write_parquet_stream_empty_iterable_writes_valid_file(local_cache):
    schema = pa.schema([("a", pa.string())])
    n = data_io.write_parquet_stream(
        storage_location=LOC, filename="empty.parquet", batches=[], schema=schema)
    assert n == 0
    back = data_io.load_parquet_selective(storage_location=LOC, filename="empty.parquet")
    assert list(back.columns) == ["a"] and len(back) == 0






def test_concat_parquet_files_preserves_rows_and_order(local_cache):
    df1, df2 = _frame(300), _frame(200)
    data_io.save_parquet(df=df1, storage_location=LOC, filename="p1.parquet")
    data_io.save_parquet(df=df2, storage_location=LOC, filename="p2.parquet")

    n = data_io.concat_parquet_files(
        src_storage_location=LOC, src_filenames=["p1.parquet", "p2.parquet"],
        dst_storage_location=LOC, dst_filename="all.parquet", batch_size=64)
    assert n == 500
    back = data_io.load_parquet_selective(storage_location=LOC, filename="all.parquet")
    assert back["value"].tolist() == df1["value"].tolist() + df2["value"].tolist()






def test_save_load_bytes_roundtrip_and_range(local_cache):
    payload = bytes(range(256)) * 4
    assert data_io.save_bytes(data=payload, storage_location=LOC, filename="blob.bin") == len(payload)
    assert data_io.load_bytes(storage_location=LOC, filename="blob.bin") == payload
    assert data_io.load_bytes(storage_location=LOC, filename="blob.bin",
                              start=10, length=5) == payload[10:15]
    assert data_io.load_bytes(storage_location=LOC, filename="missing.bin") is None






def test_read_byte_ranges_positional(local_cache):
    payload = bytes(range(256)) * 16
    data_io.save_bytes(data=payload, storage_location=LOC, filename="blob.bin")
    ranges = [(0, 4), (1000, 8), (4095, 1), (256, 256)]
    out = data_io.read_byte_ranges(storage_location=LOC, filename="blob.bin", ranges=ranges)
    assert out == [payload[o:o + l] for o, l in ranges]
    assert data_io.read_byte_ranges(storage_location=LOC, filename="blob.bin", ranges=[]) == []
