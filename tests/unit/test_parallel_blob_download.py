"""The ranged parallel blob download behind load_parquet's GCS path.

A single download_as_bytes() stream was the dominant cost of a cold study
open (1.64 GiB blob). Large blobs now fan out over ranged reads; these tests
pin reassembly correctness at awkward sizes and the small-blob passthrough.
"""

import pytest

from fyp.core import data_io


class _FakeBlob:
    def __init__(self, payload, record):
        self._payload = payload
        self._record = record
        self.size = len(payload)

    def download_as_bytes(self, start=None, end=None):
        if start is None:
            self._record.append(("full", None))
            return self._payload
        self._record.append((start, end))
        return self._payload[start:end + 1]  # end is inclusive


class _FakeBucket:
    name = "test-bucket"

    def __init__(self, payload):
        self.payload = payload
        self.calls: list = []

    def get_blob(self, blob_name):
        if self.payload is None:
            return None
        return _FakeBlob(self.payload, self.calls)

    def blob(self, blob_name):
        return _FakeBlob(self.payload, self.calls)


def test_small_blob_uses_one_full_download(monkeypatch):
    monkeypatch.setattr(data_io, "_PARALLEL_DL_MIN_BYTES", 1024)
    bucket = _FakeBucket(b"x" * 100)
    out = data_io._download_blob_bytes(bucket, "f")
    assert bytes(out) == b"x" * 100
    assert bucket.calls == [("full", None)]


@pytest.mark.parametrize("size", [1024, 1025, 2048, 3000])
def test_large_blob_reassembles_exactly(monkeypatch, size):
    """Chunk boundaries (exact multiple, off-by-one, ragged tail) must all
    reassemble byte-identically."""
    monkeypatch.setattr(data_io, "_PARALLEL_DL_MIN_BYTES", 512)
    monkeypatch.setattr(data_io, "_PARALLEL_DL_CHUNK_BYTES", 1024)
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    bucket = _FakeBucket(payload)
    out = data_io._download_blob_bytes(bucket, "f")
    assert bytes(out) == payload
    # every call was ranged, covering the blob without overlap
    ranged = sorted(c for c in bucket.calls if c[0] != "full")
    assert ranged[0][0] == 0
    assert ranged[-1][1] == size - 1
    for (s1, e1), (s2, _) in zip(ranged, ranged[1:]):
        assert s2 == e1 + 1


def test_missing_blob_raises(monkeypatch):
    bucket = _FakeBucket(None)
    with pytest.raises(FileNotFoundError):
        data_io._download_blob_bytes(bucket, "missing")
