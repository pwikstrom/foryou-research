"""QwenLocalBackend: raw-row contract, DNF paths, patch version guard.

mlx_vlm is faked (scripted generate result), ffmpeg/ffprobe calls are mocked
and media resolution is stubbed, so the test runs on any host.
"""

import json

import pytest

import fyp.annotation.backends.qwen_local as ql
from fyp.annotation.backends.qwen_local import QwenLocalBackend

# Every key the flatten/refine pipeline reads off a raw row (the interface
# boundary pinned by the golden batch tests).
_RAW_ROW_KEYS = {
    "item_id", "source_platform", "inference_ts", "inference_duration",
    "model", "prompt_fn", "annotation_version", "structured", "usage",
    "error", "finish_reason", "response",
}






@pytest.fixture
def backend(monkeypatch):
    """A QwenLocalBackend with model load, media and ffmpeg all faked."""
    b = QwenLocalBackend()

    class _Result:
        text = json.dumps({"transcript": "hello", "video_story": "a story"})
        prompt_tokens = 4000
        generation_tokens = 500

    monkeypatch.setattr(QwenLocalBackend, "_ensure_model",
                        lambda self: (object(), object()))
    monkeypatch.setattr(ql, "_generate",
                        lambda *a, **k: {"text": _Result.text,
                                         "prompt_tokens": _Result.prompt_tokens,
                                         "generation_tokens": _Result.generation_tokens})
    monkeypatch.setattr(ql, "_fetch_media", lambda item_id, platform: ("/tmp/fake.mp4", None))
    monkeypatch.setattr(ql, "_probe_duration", lambda path: 30.0)
    monkeypatch.setattr(ql, "_sample_frames", lambda *a, **k: ["/tmp/f0.jpg", "/tmp/f1.jpg"])
    monkeypatch.setattr(ql, "_extract_audio", lambda *a, **k: "/tmp/audio.wav")
    return b






def test_raw_row_shape_complete(backend):
    row = backend.annotate_one("123", platform="tiktok")
    assert _RAW_ROW_KEYS <= set(row)
    assert row["structured"] is True
    assert row["error"] == ""
    assert row["finish_reason"] == "STOP"
    assert row["source_platform"] == "tiktok"
    assert row["model"].startswith("mlx-community/")
    assert json.loads(row["response"])["transcript"] == "hello"
    assert row["usage"]["prompt_tokens"] == 4000
    assert row["usage"]["total_tokens"] == 4500
    assert row["annotation_version"].startswith("av_") or row["annotation_version"] == "unknown"






def test_media_not_found_is_dnf(backend, monkeypatch):
    monkeypatch.setattr(ql, "_fetch_media", lambda item_id, platform: (None, None))
    row = backend.annotate_one("123", platform="tiktok")
    assert row["finish_reason"] == "DNF - media not found"
    assert row["error"]
    assert _RAW_ROW_KEYS <= set(row)






def test_unparseable_response_is_dnf(backend, monkeypatch):
    monkeypatch.setattr(ql, "_generate",
                        lambda *a, **k: {"text": '{"transcript": "trunca',
                                         "prompt_tokens": 1, "generation_tokens": 1})
    row = backend.annotate_one("123", platform="tiktok")
    assert row["finish_reason"] == "DNF - unparseable response"
    assert row["error"].startswith("parse:")
    assert row["response"].startswith('{"transcript"')  # raw text preserved for the archive






def test_generation_crash_is_dnf(backend, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("metal exploded")

    monkeypatch.setattr(ql, "_generate", _boom)
    row = backend.annotate_one("123", platform="tiktok")
    assert row["finish_reason"] == "DNF - see error"
    assert "metal exploded" in row["error"]






def test_non_dict_schema_rejected(backend):
    row = backend.annotate_one("123", platform="tiktok", response_schema=object())
    assert row["finish_reason"] == "DNF - bad schema"






def test_backend_class_contract():
    assert QwenLocalBackend.name == "qwen_local"
    assert QwenLocalBackend.max_workers == 1
    assert QwenLocalBackend.supports_batch_mode is False
    assert QwenLocalBackend.cloud_run_capable is False
    b = QwenLocalBackend()
    assert "audio" in b.prompt_suffix()
    extra = b.version_extra_params()
    assert {"n_frames", "frame_scale", "fps", "with_audio", "repetition_penalty"} <= set(extra)
    gen = b.version_gen_params()
    assert gen["use_structured_output"] is True and gen["temperature"] == 0.0






def test_rope_fix_skips_on_newer_mlx_vlm(monkeypatch):
    import sys
    import types

    import fyp.annotation.backends.qwen_rope_fix as fix

    fake = types.ModuleType("mlx_vlm")
    fake.__version__ = "0.7.0"
    monkeypatch.setitem(sys.modules, "mlx_vlm", fake)
    monkeypatch.setattr(fix, "_APPLIED", False)
    assert fix.apply_patches() is False  # assumed fixed upstream — no-op
