"""Local MiniCPM-o annotation backend (Apple Silicon / MLX).

Productizes a validated local pilot (2026-07-18,
20/20 valid vs Gemini, ~24 s/video, 8.1 GB peak on an M2 Max): the same
frames+audio recipe as ``qwen_local`` — N evenly-spaced ffmpeg frames as an
ordered multi-image input plus the full audio track as a mono 16 kHz clip
(MiniCPM-o hears it: near-verbatim transcripts in the pilot, including
Spanish) — with llguidance-constrained JSON output. At 9B it runs on Macs
where the 30B Qwen model cannot (16 GB+ vs 32 GB+).

One mlx-vlm 0.6.x bug is patched at load (see ``minicpm_sanitize_fix``: the
published MLX quants fail to load without it). The frame/audio/generate
helpers are shared with ``qwen_local`` — they are model-agnostic. The model is
loaded once per process and kept resident; ``max_workers`` is 1. Config lives
in ``[machine.minicpm_local]``; the prompt addendum is part of the version
identity by design.
"""

import datetime as _dt
import json
import shutil
import tempfile
import threading

from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability
from fyp.annotation.backends.qwen_local import (
    PROMPT_ADDENDUM,
    _default_platform,
    _extract_audio,
    _fetch_media,
    _generate,
    _probe_duration,
    _sample_frames,
)
from fyp.fyp_config import get_config
from fyp.logging_setup import get_logger

try:
    import mlx_vlm
except ImportError:  # not installed outside Apple Silicon dev machines
    mlx_vlm = None

logger = get_logger(__name__)






def _minicpm_cf() -> dict:
    """The ``[machine.minicpm_local]`` config block with pilot-tuned defaults."""
    stored = get_config()["machine"].get("minicpm_local", {}) or {}
    defaults = {
        "model_id": "mlx-community/MiniCPM-o-4_5-4bit",
        "max_frames": 8,
        "frame_scale": 448,
        "fps": 1.0,
        "max_tokens": 4096,
        "repetition_penalty": 1.08,
        "with_audio": True,
    }
    return {**defaults, **stored}






class MiniCPMLocalBackend(AnnotationBackend):
    """MiniCPM-o running locally via mlx-vlm (frames + audio)."""

    name = "minicpm_local"
    max_workers = 1
    supports_batch_mode = False
    cloud_run_capable = False

    _model = None
    _processor = None
    _loaded_model_id = None
    _load_lock = threading.Lock()


    def _effective_cf(self) -> dict:
        """The ``[machine.minicpm_local]`` config with variant overrides applied."""
        return {**_minicpm_cf(), **self.overrides}


    def availability(self, deep: bool = False) -> BackendAvailability:
        """Hardware/dependency readiness (see ``minicpm_support.check_all``).

        Args:
            deep: Accepted for interface parity; the shallow checks already
                cover everything except an actual generation.

        Returns:
            The availability result with per-check detail rows.
        """
        from fyp.annotation.backends import minicpm_support

        return minicpm_support.availability(self._effective_cf()["model_id"])


    def prompt_suffix(self) -> str:
        """The frames+audio addendum (part of the version identity)."""
        return PROMPT_ADDENDUM


    def effective_model_id(self) -> str:
        """The configured local model id."""
        return self._effective_cf()["model_id"]


    def version_gen_params(self) -> dict:
        """The standard generation params as this backend runs them."""
        minicpm_cf = self._effective_cf()
        return {
            "use_structured_output": True,
            "temperature": 0.0,
            "thinking_budget": None,
            "media_resolution": None,
            "max_output_tokens": minicpm_cf["max_tokens"],
        }


    def version_extra_params(self) -> dict:
        """Frame/audio sampling parameters (output-affecting → identity)."""
        minicpm_cf = self._effective_cf()
        return {
            "n_frames": minicpm_cf["max_frames"],
            "frame_scale": minicpm_cf["frame_scale"],
            "fps": minicpm_cf["fps"],
            "with_audio": minicpm_cf["with_audio"],
            "repetition_penalty": minicpm_cf["repetition_penalty"],
        }


    def _ensure_model(self):
        """Load the model once per process (thread-safe); returns (model, processor)."""
        if mlx_vlm is None:
            raise RuntimeError('mlx-vlm is not installed — pip install -e ".[local_minicpm]"')
        cls = MiniCPMLocalBackend
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
                from fyp.annotation.backends.minicpm_sanitize_fix import apply_patches

                apply_patches()
                logger.info(f"Loading local MiniCPM model {model_id} (one-time per process) ...")
                cls._model, cls._processor = mlx_vlm.load(model_id)
                cls._loaded_model_id = model_id
                logger.info("Local MiniCPM model loaded")
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
        # Canonical subpackage paths, NOT the flat alias shims — see the
        # backends package docstring. max_workers is 1 today, but the rule is
        # the pool body's, not this backend's.
        from fyp.annotation import annotation_versioning
        from fyp.annotation.annotation_schema import get_annotation_json_schema

        minicpm_cf = {**self._effective_cf(),
                      **{k: v for k, v in (gen_overrides or {}).items() if v is not None}}
        now = _dt.datetime.now()
        row: dict = {
            "item_id": item_id,
            "source_platform": platform or _default_platform(),
            "inference_ts": int(now.timestamp()),
            "inference_duration": -1,
            "model": minicpm_cf["model_id"],
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
            row["error"] = "minicpm_local needs a portable JSON-schema dict (got a non-dict schema)"
            row["finish_reason"] = "DNF - bad schema"
            return row

        work_dir = tempfile.mkdtemp(prefix="minicpm_local_")
        local_video, cleanup_video = None, None
        try:
            local_video, cleanup_video = _fetch_media(item_id, platform)
            if local_video is None:
                row["error"] = f"media not found for {platform or '?'}/{item_id}"
                row["finish_reason"] = "DNF - media not found"
                return row

            duration = _probe_duration(local_video) or 60.0
            frames = _sample_frames(local_video, duration, work_dir,
                                    minicpm_cf["fps"], minicpm_cf["max_frames"],
                                    minicpm_cf["frame_scale"])
            audio = _extract_audio(local_video, work_dir) if minicpm_cf["with_audio"] else None

            model, processor = self._ensure_model()
            start = _dt.datetime.now()
            result = _generate(model, processor, frames, audio, duration,
                               system_prompt, response_schema, minicpm_cf)
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
