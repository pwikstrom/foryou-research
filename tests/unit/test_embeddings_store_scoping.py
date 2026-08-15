"""Model-scoped embedding store: mixed-model shards never mix in one matrix."""

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import fyp.analysis.embeddings as embeddings

_GEMINI = "gemini-embedding-001"
_QWEN = "Qwen/Qwen3-Embedding-0.6B"






def _shard_frame(item_ids: list[str], dim: int, model: str) -> pd.DataFrame:
    rng = np.random.RandomState(len(item_ids) + dim)
    matrix = rng.rand(len(item_ids), dim).astype(np.float32)
    return pd.DataFrame({
        "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
        "embedding": pd.array(
            [row.astype(np.float16).tobytes() for row in matrix],
            dtype=pd.ArrowDtype(pa.large_binary())),
        "model": pd.array([model] * len(item_ids), dtype="string[pyarrow]"),
        "dim": pd.array([dim] * len(item_ids), dtype="int32[pyarrow]"),
    })






@pytest.fixture
def mixed_store(monkeypatch):
    """A fake two-shard store: one gemini@1536 shard, one qwen@1024 shard."""
    shards = {
        "video_embeddings__aaa.parquet": _shard_frame(["g1", "g2", "g3"], 1536, _GEMINI),
        "video_embeddings__bbb.parquet": _shard_frame(["q1", "q2"], 1024, _QWEN),
    }

    monkeypatch.setattr(embeddings.data_io, "listdir",
                        lambda **kw: list(shards) + ["unrelated.parquet"])

    def _load(storage_location, filename, columns=None, **kw):
        df = shards[filename]
        return df[columns] if columns else df

    monkeypatch.setattr(embeddings.data_io, "load_parquet_selective", _load)
    return shards






def test_embedded_item_ids_scopes_to_model(mixed_store):
    assert embeddings.embedded_item_ids(model=_GEMINI) == {"g1", "g2", "g3"}
    assert embeddings.embedded_item_ids(model=_QWEN) == {"q1", "q2"}
    assert embeddings.embedded_item_ids(model="unknown-model") == set()






def test_load_embeddings_scopes_and_never_vstack_crashes(mixed_store):
    ids, matrix = embeddings.load_embeddings(model=_GEMINI)
    assert sorted(ids) == ["g1", "g2", "g3"]
    assert matrix.shape == (3, 1536)
    assert matrix.dtype == np.float32

    ids, matrix = embeddings.load_embeddings(model=_QWEN)
    assert sorted(ids) == ["q1", "q2"]
    assert matrix.shape == (2, 1024)






def test_load_embeddings_empty_for_unseen_model(mixed_store, monkeypatch):
    """A backend switch starts from an empty store for the new model."""
    ids, matrix = embeddings.load_embeddings(model="brand-new-model")
    assert ids == []
    assert matrix.shape[0] == 0






def test_load_embeddings_dedupes_twin_shards(monkeypatch):
    """Duplicate item_ids across shards collapse to the last occurrence.

    Regression for 2026-08-14: two concurrent embeddings_refresh runs wrote
    the same backlog slice as two uuid-named shards, and the duplicated ids
    flowed through video_map into a sessions_refresh crash. The last
    occurrence must win — the same winner the dense sidecar's index picks.
    """
    first = _shard_frame(["g1", "g2", "g3"], 8, _GEMINI)
    twin_a = _shard_frame(["g4", "g5"], 8, _GEMINI)
    twin_b = _shard_frame(["g4", "g5"], 8, _GEMINI)
    # The real twins held independent embedding calls' outputs — make the
    # copies distinguishable so keep="last" is actually asserted.
    twin_b["embedding"] = pd.array(
        [np.full(8, i + 1, dtype=np.float16).tobytes() for i in range(2)],
        dtype=pd.ArrowDtype(pa.large_binary()))
    shards = {
        "video_embeddings__aaa.parquet": first,
        "video_embeddings__bbb.parquet": twin_a,
        "video_embeddings__ccc.parquet": twin_b,
    }
    monkeypatch.setattr(embeddings.data_io, "listdir", lambda **kw: list(shards))
    monkeypatch.setattr(
        embeddings.data_io, "load_parquet_selective",
        lambda storage_location, filename, columns=None, **kw:
            shards[filename][columns] if columns else shards[filename])

    ids, matrix = embeddings.load_embeddings(model=_GEMINI)
    assert ids == ["g1", "g2", "g3", "g4", "g5"]
    assert matrix.shape == (5, 8)
    # The later twin's vectors win (keep="last" in shard-listing order).
    expected = np.stack([
        np.frombuffer(twin_b["embedding"][i], dtype=np.float16).astype(np.float32)
        for i in range(2)
    ])
    np.testing.assert_array_equal(matrix[3:], expected)
    # The unaffected leading shard decodes into the same rows as before.
    lead = np.stack([
        np.frombuffer(first["embedding"][i], dtype=np.float16).astype(np.float32)
        for i in range(3)
    ])
    np.testing.assert_array_equal(matrix[:3], lead)






def test_model_mask_attributes_legacy_shards_to_gemini():
    """A hypothetical pre-provenance shard counts as the original model."""
    df = pd.DataFrame({"item_id": ["a", "b"]})
    assert embeddings._model_mask(df, _GEMINI).all()
    assert not embeddings._model_mask(df, _QWEN).any()






def test_default_model_is_active_backend(mixed_store, monkeypatch):
    class _FakeBackend:
        name = "fake"

        def model_id(self):
            return _QWEN

        def dim(self):
            return 1024

    monkeypatch.setattr(embeddings, "active_embedding_backend", lambda: _FakeBackend())
    assert embeddings.embedded_item_ids() == {"q1", "q2"}
    ids, matrix = embeddings.load_embeddings()
    assert matrix.shape == (2, 1024)
