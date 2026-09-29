"""Worker gating for the embedding backend: start refusal + cloud-run guard."""

import numpy as np
import pytest

import fyp.analysis.embedding_backends as embedding_backends
from fyp.analysis.embedding_backends.base import BackendAvailability, EmbeddingBackend
from tests._web import web_client

_TEST_ADMIN = "__embed_gate_test_admin__"


class _StubLocalEmbedBackend(EmbeddingBackend):
    """Registered stub standing in for a local embedding backend."""

    name = "stub_embed_local"
    cloud_run_capable = False

    def __init__(self, ok: bool = True):
        self._ok = ok

    def model_id(self) -> str:
        return "stub-embed-model"

    def dim(self) -> int:
        return 8

    def availability(self, deep: bool = False) -> BackendAvailability:
        return BackendAvailability(ok=self._ok, reason="" if self._ok else "stub not ready")

    def embed_texts(self, texts, reporter=None) -> np.ndarray:
        return np.zeros((len(texts), 8), dtype=np.float32)


@pytest.fixture
def stub_embed_backend(monkeypatch):
    stub = _StubLocalEmbedBackend()
    monkeypatch.setitem(embedding_backends._instances, "stub_embed_local", stub)
    monkeypatch.setattr(embedding_backends, "BACKEND_IDS", ("gemini", "stub_embed_local"))
    monkeypatch.setitem(embedding_backends._BACKEND_MODULES, "stub_embed_local", "unused")
    from fyp.analysis.embedding_backends import settings as embed_settings

    monkeypatch.setattr(embed_settings, "get_embedding_backend", lambda: "stub_embed_local")
    return stub


def test_start_process_refuses_cloud_dispatch_for_local_backend(stub_embed_backend, monkeypatch):
    import web_interface.tasks.process_manager as pm

    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    ok, msg = pm.start_process("embeddings_refresh", "unused_script.py")
    assert ok is False
    assert "local machine" in msg
    assert "stub_embed_local" in msg


def test_start_process_local_mode_unaffected(stub_embed_backend, monkeypatch):
    """Locally (no K_SERVICE) the guard must not fire; the subprocess path is
    reached (we stop it by faking an already-running process)."""
    import web_interface.tasks.process_manager as pm

    monkeypatch.delenv("K_SERVICE", raising=False)

    class _FakeProc:
        def poll(self):
            return None

    monkeypatch.setitem(pm.processes["embeddings_refresh"], "proc", _FakeProc())
    ok, msg = pm.start_process("embeddings_refresh", "unused_script.py")
    assert ok is False
    assert msg == "Process already running"


@pytest.fixture
def client(monkeypatch):
    from web_interface.auth.accounts import ROLE_ADMIN

    with web_client(monkeypatch, {_TEST_ADMIN: ROLE_ADMIN}, login_as=_TEST_ADMIN) as test_client:
        yield test_client


def test_api_start_refuses_unavailable_embedding_backend(client, stub_embed_backend, monkeypatch):
    stub_embed_backend._ok = False
    resp = client.post("/api/start/embeddings_refresh", json={})
    assert resp.status_code == 400
    assert "stub not ready" in resp.get_json()["message"]


def test_embedding_backends_endpoint_shape(client):
    resp = client.get("/api/manage/embedding/backends")
    assert resp.status_code == 200
    backends = resp.get_json()["backends"]
    names = [b["name"] for b in backends]
    assert names == ["gemini", "qwen_api", "qwen_local"]
    assert sum(1 for b in backends if b["active"]) == 1
    for b in backends:
        assert {"ok", "reason", "checks"} <= set(b["availability"])
        assert b["model"], f"{b['name']} entry is missing its model id"
