"""Hosted Qwen embedding backend + the explicit [embedding.gemini] config."""

import numpy as np

import fyp.analysis.embedding_backends as embedding_backends
from fyp.analysis.embedding_backends import qwen_api as eq
from fyp.analysis.embedding_backends.gemini import _gemini_cf
from fyp.fyp_config import get_config






def test_qwen_api_backend_registers():
    b = embedding_backends.get_backend("qwen_api")
    assert b.name == "qwen_api"
    assert b.cloud_run_capable is True
    assert b.model_id() == "text-embedding-v4"
    assert b.dim() == 1024






def test_qwen_api_availability_requires_key(monkeypatch):
    monkeypatch.delenv(eq.API_KEY_ENV, raising=False)
    result = embedding_backends.get_backend("qwen_api").availability()
    assert result.ok is False
    assert eq.API_KEY_ENV in result.reason

    monkeypatch.setenv(eq.API_KEY_ENV, "sk-test")
    assert embedding_backends.get_backend("qwen_api").availability().ok is True






def test_qwen_api_config_overrides(monkeypatch):
    monkeypatch.setitem(get_config().setdefault("embedding", {}), "qwen_api",
                        {"model_id": "text-embedding-v5", "dim": 512})
    b = embedding_backends.get_backend("qwen_api")
    assert b.model_id() == "text-embedding-v5"
    assert b.dim() == 512






def test_qwen_api_embed_texts_batches_and_zero_fills(monkeypatch):
    """Vectors come back in input order; a failed batch yields zero rows."""
    calls: list[list[str]] = []

    def fake_embed_batch(key, cf, chunk):
        calls.append(chunk)
        if chunk[0] == "fail":
            return None
        return [[1.0] * int(cf["dim"])] * len(chunk)

    monkeypatch.setenv(eq.API_KEY_ENV, "sk-test")
    monkeypatch.setitem(get_config().setdefault("embedding", {}), "qwen_api",
                        {"dim": 4, "batch_size": 2})
    monkeypatch.setattr(eq, "_embed_batch", fake_embed_batch)

    b = embedding_backends.get_backend("qwen_api")
    matrix = b.embed_texts(["a", "b", "fail", "d", "e"])
    assert matrix.shape == (5, 4)
    assert matrix.dtype == np.float32
    assert matrix[0].sum() == 4.0 and matrix[1].sum() == 4.0   # first batch ok
    assert matrix[2].sum() == 0.0 and matrix[3].sum() == 0.0   # failed batch zeroed
    assert matrix[4].sum() == 4.0                              # last batch ok
    assert [len(c) for c in sorted(calls, key=len, reverse=True)] == [2, 2, 1]






def test_gemini_embedding_config_defaults_and_overrides(monkeypatch):
    cf = _gemini_cf()
    assert cf["model_id"] == "gemini-embedding-001"
    assert cf["dim"] == 1536
    assert cf["location"] == "us-central1"
    assert cf["task_type"] == "CLUSTERING"

    monkeypatch.setitem(get_config().setdefault("embedding", {}), "gemini",
                        {"model_id": "gemini-embedding-002", "dim": 3072})
    b = embedding_backends.get_backend("gemini")
    assert b.model_id() == "gemini-embedding-002"
    assert b.dim() == 3072






def test_legacy_shard_attribution_is_config_independent(monkeypatch):
    """Rows without a model column always belong to the original literal."""
    import pandas as pd

    from fyp.analysis.embeddings import _model_mask

    monkeypatch.setitem(get_config().setdefault("embedding", {}), "gemini",
                        {"model_id": "gemini-embedding-002"})
    df = pd.DataFrame({"item_id": ["1", "2"]})
    assert _model_mask(df, "gemini-embedding-001").all()
    assert not _model_mask(df, "gemini-embedding-002").any()
