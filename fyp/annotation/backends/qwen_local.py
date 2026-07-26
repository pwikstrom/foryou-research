"""Local Qwen3-Omni annotation backend (Apple Silicon / MLX).

Productizes the validated pilot from ``scripts/adhoc/qwen_eval/`` (2026-07-17,
20/20 valid vs Gemini, ~30 s/video, 23 GB peak on an M2 Max):

* mlx-vlm's native video path is broken for this model family, so each video
  is represented as N evenly-spaced ffmpeg frames (mid-segment) passed as an
  ordered multi-image input, plus the full audio track as a separate mono
  16 kHz clip — Qwen3-Omni hears it (near-verbatim transcripts in the pilot).
* Output is JSON-schema constrained via llguidance, so the ``response`` string
  flows through the production ``structured`` flatten path verbatim.
* Two mlx-vlm 0.6.x bugs are patched at load (see ``qwen_rope_fix``).

The model is loaded once per process and kept resident; ``max_workers`` is 1
(sequential loop). Config lives in ``[machine.qwen_local]``; the prompt
addendum below is part of the version identity by design (it changes the
prompt hash, correctly forking a new ``av_`` version).
"""

import datetime as _dt
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading

from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability
from fyp.fyp_config import get_config
from fyp.logging_setup import get_logger

try:
    import mlx_vlm
except ImportError:  # not installed outside Apple Silicon dev machines
    mlx_vlm = None

logger = get_logger(__name__)

# The backend receives sampled frames + the audio track as separate inputs.
# Without the repetition guard the constrained transcript field can fall into
# an unbounded loop that exhausts the token budget (pilot: 1/20 items).
PROMPT_ADDENDUM = (
    "\n\nIMPORTANT (this backend receives sampled video frames plus the "
    "video's full audio track as a separate audio clip): transcribe the "
    "speech you hear for 'transcript'. Never repeat the same sentence more "
    "than twice."
)






def _qwen_cf() -> dict:
    """The ``[machine.qwen_local]`` config block with pilot-tuned defaults."""
    stored = get_config()["machine"].get("qwen_local", {}) or {}
    defaults = {
        "model_id": "mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit",
        "max_frames": 8,
        "frame_scale": 448,
        "fps": 1.0,
        "max_tokens": 4096,
        "repetition_penalty": 1.08,
        "with_audio": True,
    }
    return {**defaults, **stored}






class QwenLocalBackend(AnnotationBackend):
    """Qwen3-Omni running locally via mlx-vlm (frames + audio)."""

    name = "qwen_local"
    max_workers = 1
    supports_batch_mode = False
    cloud_run_capable = False

    _model = None
    _processor = None
    _loaded_model_id = None
    _load_lock = threading.Lock()


    def _effective_cf(self) -> dict:
        """The ``[machine.qwen_local]`` config with variant overrides applied."""
        return {**_qwen_cf(), **self.overrides}


    def availability(self, deep: bool = False) -> BackendAvailability:
        """Hardware/dependency readiness (see ``qwen_support.check_all``).

        Args:
            deep: Accepted for interface parity; the shallow checks already
                cover everything except an actual generation, which is too
                heavy for a health ping (model load ≈ 1 min).

        Returns:
            The availability result with per-check detail rows.
        """
        from fyp.annotation.backends import qwen_support

        return qwen_support.availability(self._effective_cf()["model_id"])


    def prompt_suffix(self) -> str:
        """The frames+audio addendum (part of the version identity)."""
        return PROMPT_ADDENDUM


    def effective_model_id(self) -> str:
        """The configured local model id."""
        return self._effective_cf()["model_id"]


    def version_gen_params(self) -> dict:
        """The standard generation params as this backend runs them."""
        qwen_cf = self._effective_cf()
        return {
            "use_structured_output": True,
            "temperature": 0.0,
            "thinking_budget": None,
            "media_resolution": None,
            "max_output_tokens": qwen_cf["max_tokens"],
        }


    def version_extra_params(self) -> dict:
        """Frame/audio sampling parameters (output-affecting → identity)."""
        qwen_cf = self._effective_cf()
        return {
            "n_frames": qwen_cf["max_frames"],
            "frame_scale": qwen_cf["frame_scale"],
            "fps": qwen_cf["fps"],
            "with_audio": qwen_cf["with_audio"],
            "repetition_penalty": qwen_cf["repetition_penalty"],
        }


    def _ensure_model(self):
        """Load the model once per process (thread-safe); returns (model, processor)."""
        if mlx_vlm is None:
            raise RuntimeError('mlx-vlm is not installed — pip install -e ".[local_qwen]"')
        cls = QwenLocalBackend
        model_id = self._effective_cf()["model_id"]
        with cls._load_lock:
            if cls._model is not None and cls._loaded_model_id != model_id:
                # One resident model per process: a second variant of this
                # backend cannot hot-swap it (no reliable MLX unload).
                raise RuntimeError(
                    f"local model {cls._loaded_model_id!r} is already resident; "
                    f"cannot load {model_id!r} in the same process — restart the "
                    f"worker to switch local-model variants")
            if cls._model is None:
                from fyp.annotation.backends.qwen_rope_fix import apply_patches

                apply_patches()
                logger.info(f"Loading local Qwen model {model_id} (one-time per process) ...")
                cls._model, cls._processor = mlx_vlm.load(model_id)
                cls._loaded_model_id = model_id
                logger.info("Local Qwen model loaded")
        return cls._model, cls._processor


    def annotate_one(self, item_id: str, platform: str | None = None,
                     gen_overrides: dict | None = None,
                     prompt_text: str | None = None,
                     response_schema=None) -> dict:
        """Annotate one item; returns the production raw-row dict.

        Args:
            item_id: The item to annotate.
            platform: The item's source platform (media resolution).
            gen_overrides: Optional overrides (``temperature`` /
                ``max_tokens`` / ``repetition_penalty``); the model itself
                cannot be overridden per call (one resident model).
            prompt_text: Optional explicit prompt (A/B arm); the backend
                addendum is appended either way. None = active prompt.
            response_schema: Optional portable JSON schema dict matching
                ``prompt_text``. None = the active contract's schema.

        Returns:
            The raw-row dict (failures in-band, DNF finish_reasons).
        """
        import fyp.annotation_versioning as annotation_versioning
        from fyp.annotation_schema import get_annotation_json_schema

        qwen_cf = {**self._effective_cf(),
                   **{k: v for k, v in (gen_overrides or {}).items() if v is not None}}
        now = _dt.datetime.now()
        row: dict = {
            "item_id": item_id,
            "source_platform": platform or _default_platform(),
            "inference_ts": int(now.timestamp()),
            "inference_duration": -1,
            "model": qwen_cf["model_id"],
            "prompt_fn": annotation_versioning.active_prompt_label(),
            "annotation_version": annotation_versioning.active_annotation_version(),
            "structured": True,
            "usage": {},
            "error": "unknown error",
            "finish_reason": "did not even start",
            "response": "",
        }

        if prompt_text is None:
            prompt_text = annotation_versioning.active_prompt_text()
        system_prompt = prompt_text + self.prompt_suffix()
        if response_schema is None:
            response_schema = get_annotation_json_schema()
        if not isinstance(response_schema, dict):
            row["error"] = "qwen_local needs a portable JSON-schema dict (got a non-dict schema)"
            row["finish_reason"] = "DNF - bad schema"
            return row

        work_dir = tempfile.mkdtemp(prefix="qwen_local_")
        local_video, cleanup_video = None, None
        try:
            local_video, cleanup_video = _fetch_media(item_id, platform)
            if local_video is None:
                row["error"] = f"media not found for {platform or '?'}/{item_id}"
                row["finish_reason"] = "DNF - media not found"
                return row

            duration = _probe_duration(local_video) or 60.0
            frames = _sample_frames(local_video, duration, work_dir,
                                    qwen_cf["fps"], qwen_cf["max_frames"], qwen_cf["frame_scale"])
            audio = _extract_audio(local_video, work_dir) if qwen_cf["with_audio"] else None

            model, processor = self._ensure_model()
            start = _dt.datetime.now()
            result = _generate(model, processor, frames, audio, duration,
                               system_prompt, response_schema, qwen_cf)
            row["inference_duration"] = (_dt.datetime.now() - start).total_seconds()

            row["response"] = result["text"]
            row["usage"] = {
                "prompt_tokens": result.get("prompt_tokens"),
                "candidates_tokens": result.get("generation_tokens"),
                "thoughts_tokens": 0,
                "total_tokens": (result.get("prompt_tokens") or 0)
                                + (result.get("generation_tokens") or 0),
            }
            parsed = json.loads(row["response"] or "null")
            if not isinstance(parsed, dict):
                raise ValueError("response is not a JSON object")
            row["error"] = ""
            row["finish_reason"] = "STOP"
        except json.JSONDecodeError as exc:
            row["error"] = f"parse: {exc}"
            row["finish_reason"] = "DNF - unparseable response"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["finish_reason"] = "DNF - see error"
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if cleanup_video:
                cleanup_video()
        return row






def _default_platform() -> str:
    """The default source platform (mirrors ``call_machine``'s fallback)."""
    import fyp.scrape_queues as scrape_queues

    return scrape_queues.default_platform()






def _fetch_media(item_id: str, platform: str | None):
    """Resolve the item's media to a local file path.

    GCS media lives under ``data_io.gcs_media_prefix`` and has NO
    ``gcs_paths`` location entry (unlike data/cache locations), so the blob
    is downloaded directly via the bucket handle — the same access pattern
    as the viewer's streaming reader.

    Returns:
        ``(path, cleanup)`` — ``cleanup`` removes the temp copy when the
        object was fetched from GCS (``None`` for genuinely local files);
        ``(None, None)`` when the media does not exist anywhere.
    """
    import fyp.media_paths as media_paths

    resolved = media_paths.resolve_media(item_id, platform=platform)
    if resolved is None:
        return None, None
    if resolved["kind"] == "local":
        return resolved["path"], None

    bucket = get_config()["data_io"].get("bucket")
    if bucket is None:
        return None, None
    fd, tmp = tempfile.mkstemp(prefix="fyp_media_", suffix=".mp4")
    os.close(fd)
    try:
        bucket.blob(resolved["blob_name"]).download_to_filename(tmp)
    except Exception as e:
        logger.warning(f"media download failed for {resolved['blob_name']}: {e!r}")
        if os.path.exists(tmp):
            os.unlink(tmp)
        return None, None

    def _cleanup():
        if os.path.exists(tmp):
            os.unlink(tmp)

    return tmp, _cleanup






def _probe_duration(path: str) -> float | None:
    """Media duration in seconds via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None






def _sample_frames(video_file: str, duration: float, out_dir: str,
                   fps: float, max_frames: int, scale: int) -> list[str]:
    """Extract evenly-spaced mid-segment frames with ffmpeg, in order."""
    n = max(2, min(int(max_frames), math.ceil(duration * fps)))
    paths = []
    for i in range(n):
        t = (i + 0.5) * duration / n
        path = os.path.join(out_dir, f"f{i:02}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
             "-i", video_file, "-vframes", "1",
             "-vf", f"scale='min({scale},iw)':-2", path],
            check=True, timeout=60,
        )
        paths.append(path)
    return paths






def _extract_audio(video_file: str, out_dir: str) -> str | None:
    """Extract the mono 16 kHz audio track; None when absent/failed."""
    path = os.path.join(out_dir, "audio.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_file,
             "-vn", "-ac", "1", "-ar", "16000", path],
            check=True, timeout=120,
        )
    except subprocess.SubprocessError:
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 1000 else None






def _generate(model, processor, frames: list[str], audio: str | None,
              duration: float, system_prompt: str, schema: dict, qwen_cf: dict) -> dict:
    """Run one constrained generation; returns text + token counts."""
    from mlx_vlm.generate import generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.structured import build_json_schema_logits_processor

    user_text = (f"These are {len(frames)} frames sampled evenly, in order, "
                 f"from one short video (duration {duration:.0f} seconds)"
                 + (" together with the video's audio track" if audio else "")
                 + ". Analyze this video")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    prompt = apply_chat_template(
        processor, model.config, messages,
        num_images=len(frames), num_audios=1 if audio else 0,
        enable_thinking=False,
    )
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    logits_processor = build_json_schema_logits_processor(tokenizer, schema)

    result = generate(
        model, processor, prompt,
        image=frames,
        audio=[audio] if audio else None,
        temperature=qwen_cf.get("temperature", 0.0),
        max_tokens=qwen_cf["max_tokens"],
        repetition_penalty=qwen_cf["repetition_penalty"],
        repetition_context_size=60,
        logits_processors=[logits_processor],
        verbose=False,
    )
    return {
        "text": result.text if hasattr(result, "text") else str(result),
        "prompt_tokens": getattr(result, "prompt_tokens", None),
        "generation_tokens": getattr(result, "generation_tokens", None),
    }
