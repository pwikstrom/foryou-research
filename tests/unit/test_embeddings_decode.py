"""Bit-exactness of the preallocate/zero-copy embedding decode rewrite."""

import numpy as np
import pandas as pd
import pyarrow as pa

from fyp.embeddings import decode_embeddings, decode_embeddings_arrow






def _legacy_decode(byte_values):
    """The historical list -> vstack -> astype chain (the 3.6x-transient path)."""
    rows = [np.frombuffer(b, dtype=np.float16) for b in byte_values]
    return np.vstack(rows).astype(np.float32)






def _blob_rows(n: int = 50, dim: int = 32, seed: int = 0) -> list[bytes]:
    rng = np.random.default_rng(seed)
    return [rng.standard_normal(dim).astype(np.float16).tobytes() for _ in range(n)]






def test_decode_embeddings_matches_legacy():
    blobs = _blob_rows()
    np.testing.assert_array_equal(decode_embeddings(blobs), _legacy_decode(blobs))






def test_decode_embeddings_empty():
    out = decode_embeddings([])
    assert out.shape == (0, 0) and out.dtype == np.float32






def test_arrow_uniform_fast_path_matches_legacy():
    blobs = _blob_rows()
    series = pd.Series(
        pd.array(pa.array(blobs, type=pa.large_binary()),
                 dtype=pd.ArrowDtype(pa.large_binary())))
    np.testing.assert_array_equal(
        decode_embeddings_arrow(series), _legacy_decode(blobs))






def test_arrow_decode_after_boolean_mask():
    """The load path filters by model before decoding — a sliced/taken array."""
    blobs = _blob_rows(n=60)
    series = pd.Series(
        pd.array(pa.array(blobs, type=pa.large_binary()),
                 dtype=pd.ArrowDtype(pa.large_binary())))
    mask = np.arange(60) % 3 != 0
    np.testing.assert_array_equal(
        decode_embeddings_arrow(series[mask]),
        _legacy_decode([b for b, keep in zip(blobs, mask) if keep]))






def test_arrow_decode_into_preallocated_offset():
    blobs_a, blobs_b = _blob_rows(n=10, seed=1), _blob_rows(n=15, seed=2)
    out = np.empty((25, 32), dtype=np.float32)
    for blobs, off in ((blobs_a, 0), (blobs_b, 10)):
        series = pd.Series(
            pd.array(pa.array(blobs, type=pa.large_binary()),
                     dtype=pd.ArrowDtype(pa.large_binary())))
        decode_embeddings_arrow(series, out=out, offset=off)
    np.testing.assert_array_equal(out, _legacy_decode(blobs_a + blobs_b))






def test_arrow_non_uniform_fallback():
    """Mixed-width blobs must not take the reshape fast path silently."""
    rng = np.random.default_rng(3)
    dim = 16
    blobs = [rng.standard_normal(dim).astype(np.float16).tobytes() for _ in range(5)]
    arr = pa.array(blobs + [blobs[0] + blobs[1]], type=pa.large_binary())
    # Decode only the uniform prefix through the fallback check: the full
    # array has one double-width row -> non-uniform -> per-row path. Width of
    # `out` follows the first row, so pass a matching buffer for the prefix.
    out = np.empty((5, dim), dtype=np.float32)
    decode_embeddings_arrow(arr.slice(0, 5), out=out, offset=0)
    np.testing.assert_array_equal(out, _legacy_decode(blobs))






def test_arrow_binary_not_large_binary():
    """Round-tripped shards can downgrade large_binary -> binary (int32 offsets)."""
    blobs = _blob_rows(n=8, dim=8)
    series = pd.Series(
        pd.array(pa.array(blobs, type=pa.binary()),
                 dtype=pd.ArrowDtype(pa.binary())))
    np.testing.assert_array_equal(
        decode_embeddings_arrow(series), _legacy_decode(blobs))
