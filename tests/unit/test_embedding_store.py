"""Dense embedding sidecar (fyp.analysis.embedding_store) invariants.

Local-mode tests over a synthetic shard store: build/append round-trip, the
append-only row-stability invariant, manifest invalidation, last-occurrence
id dedup, the ranged-read path vs the memmap path, and corpus-mean parity
with the full-matrix mean.
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings

MODEL = "test-embed-model"
DIM = 24






@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the 'recoded' location at a temp dir (local mode)."""
    from fyp.fyp_config import fyp_cf

    monkeypatch.setitem(fyp_cf["paths"], "recoded", str(tmp_path))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    return tmp_path






def _write_shard(item_ids, matrix, model=MODEL):
    df = pd.DataFrame({
        "item_id": pd.array([str(i) for i in item_ids], dtype="string[pyarrow]"),
        "embedding": pd.array(
            pa.array([row.astype(np.float16).tobytes() for row in matrix],
                     type=pa.large_binary()),
            dtype=pd.ArrowDtype(pa.large_binary())),
        "model": pd.array([model] * len(item_ids), dtype="string[pyarrow]"),
        "dim": pd.array([matrix.shape[1]] * len(item_ids), dtype="int32[pyarrow]"),
    })
    name = f"{embeddings.SHARD_PREFIX}{len(item_ids)}_{abs(hash(tuple(item_ids))) % 10**8}{embeddings.SHARD_SUFFIX}"
    data_io.save_parquet(df=df, storage_location="recoded", filename=name)
    return name






def _rand(n, seed):
    return np.random.default_rng(seed).standard_normal((n, DIM)).astype(np.float16)






def test_build_and_roundtrip(store):
    ids = [f"vid{i:04d}" for i in range(50)]
    mat = _rand(50, 0)
    _write_shard(ids, mat)

    manifest = embedding_store.ensure_dense_store(MODEL)
    assert manifest["n_rows"] == 50 and manifest["dim"] == DIM

    index = embedding_store.load_index(MODEL)
    rows, found = index.lookup(ids)
    assert found.all()
    got = embedding_store.read_vectors(MODEL, rows, index)
    np.testing.assert_array_equal(got, mat.astype(np.float32))

    # Unknown ids report not-found without erroring.
    rows2, found2 = index.lookup(["nope", ids[3]])
    assert list(found2) == [False, True]
    np.testing.assert_array_equal(
        embedding_store.read_vectors(MODEL, rows2, index)[0],
        mat[3].astype(np.float32))






def test_append_only_row_stability(store):
    """Adding a shard must never move an existing item's row (O(new) append)."""
    ids1 = [f"a{i}" for i in range(30)]
    _write_shard(ids1, _rand(30, 1))
    embedding_store.ensure_dense_store(MODEL)
    idx1 = embedding_store.load_index(MODEL)
    rows1, _ = idx1.lookup(ids1)

    ids2 = [f"b{i}" for i in range(20)]
    _write_shard(ids2, _rand(20, 2))
    manifest = embedding_store.ensure_dense_store(MODEL)
    assert manifest["n_rows"] == 50
    assert len(manifest["parts"]) == 2  # one part per shard, no rewrite

    idx2 = embedding_store.load_index(MODEL)
    rows1_after, _ = idx2.lookup(ids1)
    np.testing.assert_array_equal(rows1, rows1_after)






def test_idempotent_when_fresh(store):
    _write_shard([f"x{i}" for i in range(10)], _rand(10, 3))
    m1 = embedding_store.ensure_dense_store(MODEL)
    m2 = embedding_store.ensure_dense_store(MODEL)
    assert m1["built_at"] == m2["built_at"]  # second call was a no-op






def test_last_occurrence_wins_for_duplicate_ids(store):
    """Parity with load_directional_store's {iid: i} dict (last wins)."""
    mat1, mat2 = _rand(3, 4), _rand(3, 5)
    _write_shard(["dup", "u1", "u2"], mat1)
    embedding_store.ensure_dense_store(MODEL)
    _write_shard(["dup", "u3", "u4"], mat2)
    embedding_store.ensure_dense_store(MODEL)

    index = embedding_store.load_index(MODEL)
    rows, found = index.lookup(["dup"])
    assert found.all()
    got = embedding_store.read_vectors(MODEL, rows, index)
    np.testing.assert_array_equal(got[0], mat2[0].astype(np.float32))






def test_mutated_shard_triggers_full_rebuild(store):
    ids = [f"m{i}" for i in range(10)]
    name = _write_shard(ids, _rand(10, 6))
    embedding_store.ensure_dense_store(MODEL)

    # Mutate the compacted shard (append-only invariant broken).
    df = data_io.load_parquet_selective(storage_location="recoded", filename=name)
    data_io.save_parquet(df=df.iloc[:5], storage_location="recoded", filename=name)

    manifest = embedding_store.ensure_dense_store(MODEL)
    assert manifest["n_rows"] == 5
    index = embedding_store.load_index(MODEL)
    _, found = index.lookup(ids[:5])
    assert found.all()






def test_corpus_mean_matches_full_matrix_and_fingerprint_gates(store):
    ids = [f"c{i}" for i in range(40)]
    mat = _rand(40, 7)
    _write_shard(ids[:25], mat[:25])
    _write_shard(ids[25:], mat[25:])
    embedding_store.ensure_dense_store(MODEL)

    fp = embedding_store.store_fingerprint()
    mean, count, got_fp = embedding_store.get_corpus_mean(MODEL, expected_fp=fp)
    assert count == 40 and got_fp == fp
    np.testing.assert_allclose(
        mean, mat.astype(np.float32).mean(axis=0, dtype=np.float64), atol=1e-6)

    # A stale mean (fingerprint mismatch) must be refused.
    assert embedding_store.load_corpus_mean(MODEL, expected_fp="different") is None

    # A consumer pinned to the old fingerprint must see drift after a change.
    _write_shard(["late1"], _rand(1, 8))
    with pytest.raises(embedding_store.CorpusMeanDrift):
        embedding_store.get_corpus_mean(MODEL, expected_fp=fp)






def test_ranged_read_path_matches_memmap(store, monkeypatch):
    """Force the GCS branch onto local files: byte ranges == memmap."""
    ids = [f"r{i:03d}" for i in range(64)]
    mat = _rand(64, 9)
    _write_shard(ids, mat)
    embedding_store.ensure_dense_store(MODEL)
    index = embedding_store.load_index(MODEL)

    picks = ["r003", "r001", "r050", "r002", "r063", "r030"]
    rows, found = index.lookup(picks)
    assert found.all()
    via_memmap = embedding_store.read_vectors(MODEL, rows, index)

    # Route the part read through the ranged branch: force mode='gcs' for the
    # part file and serve the byte ranges from the local file, so the
    # coalescing + slicing logic is exercised end to end.
    real_resolve = data_io._resolve_paths
    real_ranges = data_io.read_byte_ranges

    def _fake_resolve(loc, fn):
        primary, secondary, mode, blob = real_resolve(loc, fn)
        if fn.startswith(embedding_store.DENSE_BLOB_PREFIX):
            return primary, secondary, 'gcs', blob
        return primary, secondary, mode, blob

    def _local_ranges(storage_location="cache", filename="", ranges=None, **kw):
        path = real_resolve(storage_location, filename)[0]
        out = []
        with open(path, 'rb') as f:
            for off, length in ranges:
                f.seek(off)
                out.append(f.read(length))
        return out

    monkeypatch.setattr(data_io, "_resolve_paths", _fake_resolve)
    monkeypatch.setattr(data_io, "read_byte_ranges", _local_ranges)
    via_ranges = embedding_store.read_vectors(MODEL, rows, index,
                                              coalesce_bytes=DIM * 2 * 4)
    np.testing.assert_array_equal(via_memmap, via_ranges)
    expected = mat[[int(p[1:]) for p in picks]].astype(np.float32)
    np.testing.assert_array_equal(via_memmap, expected)






def test_local_part_cache_matches_memmap_and_downloads_once(store, monkeypatch, tmp_path):
    """GCS mode + local_cache: parts are fetched whole once, then memmapped."""
    ids = [f"c{i:03d}" for i in range(48)]
    mat = _rand(48, 11)
    _write_shard(ids, mat)
    embedding_store.ensure_dense_store(MODEL)
    index = embedding_store.load_index(MODEL)
    rows, found = index.lookup(["c007", "c001", "c040", "c020"])
    assert found.all()
    want = embedding_store.read_vectors(MODEL, rows, index)

    real_resolve = data_io._resolve_paths

    def _fake_resolve(loc, fn):
        primary, secondary, mode, blob = real_resolve(loc, fn)
        if fn.startswith(embedding_store.DENSE_BLOB_PREFIX):
            return primary, secondary, 'gcs', blob
        return primary, secondary, mode, blob

    fetches: list[str] = []

    def _fetch(filename):
        fetches.append(filename)
        with open(real_resolve(embedding_store.STORE_LOCATION, filename)[0], "rb") as f:
            return f.read()

    monkeypatch.setattr(data_io, "_resolve_paths", _fake_resolve)
    monkeypatch.setattr(embedding_store, "_fetch_part_bytes", _fetch)
    monkeypatch.setenv("FYP_DENSE_CACHE_DIR", str(tmp_path / "dense_cache"))
    # A leftover directory from an older store fingerprint must be evicted.
    stale = tmp_path / "dense_cache" / embedding_store._safe_model(MODEL) / "0000stalefp"
    stale.mkdir(parents=True)
    (stale / "old.f16").write_bytes(b"x")

    got = embedding_store.read_vectors(MODEL, rows, index, local_cache=True)
    np.testing.assert_array_equal(want, got)
    assert len(fetches) == len(index.parts)
    assert not stale.exists()

    again = embedding_store.read_vectors(MODEL, rows, index, local_cache=True)
    np.testing.assert_array_equal(want, again)
    assert len(fetches) == len(index.parts)  # served from the cache

    # A truncated cached file is not trusted: it is re-fetched.
    cached = tmp_path / "dense_cache" / embedding_store._safe_model(MODEL)
    part_file = next(cached.rglob("*.f16"))
    part_file.write_bytes(part_file.read_bytes()[:-2])
    embedding_store.read_vectors(MODEL, rows, index, local_cache=True)
    assert len(fetches) == len(index.parts) + 1




def test_sparse_request_skips_the_whole_part_cache(store, monkeypatch, tmp_path):
    """A request wanting few rows of a cold part reads by range, not by caching.

    2026-09-03 prod: a one-collection sessions refresh wanted ~0.5% of each of
    34 parts and the cache pulled 1.4 GB in 21 s to serve ~10 MB. Below
    CACHE_MIN_PART_DENSITY a cold part is served by ranged read; a part that is
    already cached is used regardless of density.
    """
    ids = [f"s{i:03d}" for i in range(48)]
    mat = _rand(48, 11)
    _write_shard(ids, mat)
    embedding_store.ensure_dense_store(MODEL)
    index = embedding_store.load_index(MODEL)
    assert len(index.parts) >= 1

    real_resolve = data_io._resolve_paths

    def _fake_resolve(loc, fn):
        primary, secondary, mode, blob = real_resolve(loc, fn)
        if fn.startswith(embedding_store.DENSE_BLOB_PREFIX):
            return primary, secondary, 'gcs', blob
        return primary, secondary, mode, blob

    fetches: list[str] = []
    ranged: list[str] = []

    def _fetch(filename):
        fetches.append(filename)
        with open(real_resolve(embedding_store.STORE_LOCATION, filename)[0], "rb") as f:
            return f.read()

    def _local_ranges(storage_location="cache", filename="", ranges=None, **kw):
        ranged.append(filename)
        path = real_resolve(storage_location, filename)[0]
        out = []
        with open(path, 'rb') as f:
            for off, length in ranges:
                f.seek(off)
                out.append(f.read(length))
        return out

    monkeypatch.setattr(data_io, "_resolve_paths", _fake_resolve)
    monkeypatch.setattr(data_io, "read_byte_ranges", _local_ranges)
    monkeypatch.setattr(embedding_store, "_fetch_part_bytes", _fetch)
    monkeypatch.setenv("FYP_DENSE_CACHE_DIR", str(tmp_path / "dense_cache"))

    one_row, found = index.lookup(["s005"])
    assert found.all()
    expected = mat[[5]].astype(np.float32)

    # Sparse (one row of a part, threshold set above it): ranged, no download.
    monkeypatch.setattr(embedding_store, "CACHE_MIN_PART_DENSITY", 0.5)
    got = embedding_store.read_vectors(MODEL, one_row, index, local_cache=True)
    np.testing.assert_array_equal(got, expected)
    assert fetches == [], "a sparse request must not download the part whole"
    assert len(ranged) == 1

    # Dense enough (threshold at zero): the part is cached whole.
    monkeypatch.setattr(embedding_store, "CACHE_MIN_PART_DENSITY", 0.0)
    got = embedding_store.read_vectors(MODEL, one_row, index, local_cache=True)
    np.testing.assert_array_equal(got, expected)
    assert len(fetches) == 1
    assert len(ranged) == 1  # unchanged

    # Warm part beats any density rule: same sparse request, served from cache.
    monkeypatch.setattr(embedding_store, "CACHE_MIN_PART_DENSITY", 1.0)
    got = embedding_store.read_vectors(MODEL, one_row, index, local_cache=True)
    np.testing.assert_array_equal(got, expected)
    assert len(fetches) == 1 and len(ranged) == 1, (
        "an already-cached part must be memmapped, not re-fetched or ranged")




def test_other_models_rows_are_excluded(store):
    _write_shard(["mine1", "mine2"], _rand(2, 10), model=MODEL)
    _write_shard(["other1"], _rand(1, 11), model="other-model")
    manifest = embedding_store.ensure_dense_store(MODEL)
    assert manifest["n_rows"] == 2
    index = embedding_store.load_index(MODEL)
    _, found = index.lookup(["other1"])
    assert not found.any()
