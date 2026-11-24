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
    "item_id",
    "source_platform",
    "inference_ts",
    "inference_duration",
    "model",
    "prompt_fn",
    "annotation_version",
    "structured",
    "usage",
    "error",
    "finish_reason",
    "response",
}


@pytest.fixture
def backend(monkeypatch):
    """A QwenLocalBackend with model load, media and ffmpeg all faked."""
    b = QwenLocalBackend()

    class _Result:
        text = json.dumps({"transcript": "hello", "video_story": "a story"})
        prompt_tokens = 4000
        generation_tokens = 500

    monkeypatch.setattr(QwenLocalBackend, "_ensure_model", lambda self: (object(), object()))
    monkeypatch.setattr(
        ql,
        "_generate",
        lambda *a, **k: {
            "text": _Result.text,
            "prompt_tokens": _Result.prompt_tokens,
            "generation_tokens": _Result.generation_tokens,
        },
    )
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
    monkeypatch.setattr(
        ql,
        "_generate",
        lambda *a, **k: {
            "text": '{"transcript": "trunca',
            "prompt_tokens": 1,
            "generation_tokens": 1,
        },
    )
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


def test_audio_cap_is_applied_and_versioned():
    import types

    from fyp.annotation.backends import qwen_local as ql

    extractor = types.SimpleNamespace(
        chunk_length=30, n_samples=480000, nb_max_frames=3000, sampling_rate=16000, hop_length=160
    )
    assert ql._apply_audio_cap(types.SimpleNamespace(feature_extractor=extractor), 120) is True
    assert (extractor.chunk_length, extractor.n_samples, extractor.nb_max_frames) == (
        120,
        1920000,
        12000,
    )
    # A processor without a Whisper-style extractor is left alone.
    assert ql._apply_audio_cap(types.SimpleNamespace(), 120) is False
    # The cap changes what the model hears, so it is part of the version identity.
    assert "max_audio_seconds" in QwenLocalBackend().version_extra_params()


def test_fetch_media_downloads_gcs_blob_directly(monkeypatch, tmp_path):
    """GCS media has no gcs_paths entry — the blob downloads via the bucket
    handle (the local_copy('media', ...) path raised KeyError in production)."""
    import os

    import fyp.core.media_paths as media_paths
    from fyp.annotation.backends import qwen_local
    from fyp.core.fyp_config import get_config

    class _Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, path):
            with open(path, "wb") as f:
                f.write(b"mp4-bytes")

    class _Bucket:
        def blob(self, name):
            return _Blob(name)

    monkeypatch.setattr(
        media_paths,
        "resolve_media",
        lambda item_id, platform=None: {
            "kind": "gcs",
            "bucket_name": "b",
            "blob_name": "media/tiktok/123.mp4",
        },
    )
    monkeypatch.setitem(get_config()["data_io"], "bucket", _Bucket())

    path, cleanup = qwen_local._fetch_media("123", "tiktok")
    assert path is not None and os.path.exists(path)
    with open(path, "rb") as fh:
        assert fh.read() == b"mp4-bytes"
    cleanup()
    assert not os.path.exists(path)

    # No bucket handle (GCS offline) -> clean miss, not an exception.
    monkeypatch.setitem(get_config()["data_io"], "bucket", None)
    assert qwen_local._fetch_media("123", "tiktok") == (None, None)


def test_sample_frames_skips_points_past_the_last_frame(tmp_path):
    """A one-frame-per-second clip has no frame at its late sample points."""
    import os
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    video = str(tmp_path / "slow.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=1",
            "-t",
            "4",
            "-pix_fmt",
            "yuv420p",
            video,
        ],
        check=True,
    )
    frames = ql._sample_frames(video, 4.0, str(tmp_path), fps=1.0, max_frames=8, scale=448)
    assert 1 <= len(frames) <= 4
    assert all(os.path.getsize(f) > 0 for f in frames)

    broken = str(tmp_path / "broken.mp4")
    with open(broken, "w") as fh:
        fh.write("not a video")
    with pytest.raises(RuntimeError):
        ql._sample_frames(broken, 4.0, str(tmp_path), fps=1.0, max_frames=8, scale=448)
