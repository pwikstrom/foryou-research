"""Production-queue backend dispatch: call_machine_threads + config gates."""

import json

import pytest

import fyp.machine_annotation as ma
from fyp.annotation.backends import settings as backend_settings
from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability






class _StubLocalBackend(AnnotationBackend):
    """Registered stub standing in for a local backend."""

    name = "stub_local"
    max_workers = 1
    supports_batch_mode = False
    cloud_run_capable = False

    def __init__(self):
        self.calls = []

    def availability(self, deep: bool = False) -> BackendAvailability:
        return BackendAvailability(ok=True)

    def effective_model_id(self) -> str:
        return "stub-local-model"

    def annotate_one(self, item_id, platform=None, gen_overrides=None,
                     prompt_text=None, response_schema=None):
        self.calls.append(item_id)
        return {"item_id": item_id, "source_platform": platform or "tiktok",
                "inference_ts": 0, "inference_duration": 0.01,
                "model": "stub-local-model", "prompt_fn": "annotation_contract.toml",
                "annotation_version": "av_stub", "structured": True,
                "usage": {"prompt_tokens": 1, "candidates_tokens": 1,
                          "thoughts_tokens": 0, "total_tokens": 2},
                "error": "", "finish_reason": "STOP",
                "response": json.dumps({"transcript": f"t{item_id}"})}






@pytest.fixture
def stub_backend(monkeypatch):
    import fyp.annotation.backends as backends

    stub = _StubLocalBackend()
    monkeypatch.setitem(backends._instances, "stub_local", stub)
    monkeypatch.setattr(backends, "BACKEND_IDS", ("gemini", "stub_local"))
    monkeypatch.setitem(backends._BACKEND_MODULES, "stub_local", "unused")
    monkeypatch.setattr(backend_settings, "_load_settings",
                        lambda: {"annotation_backend": "stub_local"})
    return stub






def test_call_machine_threads_dispatches_to_backend(stub_backend, monkeypatch):
    saved = {}

    def _fake_save_json(data=None, storage_location=None, filename=None, verbose=False):
        saved.update({"data": data, "loc": storage_location, "filename": filename})

    import fyp.data_io as data_io

    monkeypatch.setattr(data_io, "save_json", _fake_save_json)
    monkeypatch.setattr(ma.annotation_versioning, "ensure_current_version_registered",
                        lambda: None)

    results, filename = ma.call_machine_threads(
        interesting_videos=["11", "22"], verbose=False,
        platform_by_id={"11": "tiktok", "22": "instagram"})

    assert stub_backend.calls == ["11", "22"] or sorted(stub_backend.calls) == ["11", "22"]
    assert len(results) == 2
    rows = list(results.values())
    assert all(r["model"] == "stub-local-model" for r in rows)
    assert all(r["finish_reason"] == "STOP" for r in rows)
    assert saved["loc"] == "machine_annotations_raw"  # raw archive still written






def test_annotation_configured_uses_backend_availability(stub_backend):
    ok, reason = ma.annotation_configured()
    assert ok is True and reason == ""






def test_annotation_configured_reports_backend_failure(stub_backend, monkeypatch):
    monkeypatch.setattr(_StubLocalBackend, "availability",
                        lambda self, deep=False: BackendAvailability(
                            ok=False, reason="stub is broken"))
    ok, reason = ma.annotation_configured()
    assert ok is False and reason == "stub is broken"






def test_dry_run_makes_no_backend_calls(stub_backend, monkeypatch):
    import fyp.data_io as data_io

    monkeypatch.setattr(data_io, "save_json", lambda **k: None)
    monkeypatch.setattr(ma.annotation_versioning, "ensure_current_version_registered",
                        lambda: None)
    results, filename = ma.call_machine_threads(
        interesting_videos=["11"], dry_run=True)
    assert stub_backend.calls == []
    assert filename is None
