"""Local Qwen3-Omni annotation backend (Apple Silicon / MLX).

Productizes a validated local pilot (20/20 valid vs Gemini; ~8 s/video and
23 GB peak for the 4-bit model on an M5 Max, ~30 s on an M2 Max):

* Each video is represented as N evenly-spaced ffmpeg frames (mid-segment)
  passed as an ordered multi-image input, plus the audio track as a separate
  mono 16 kHz clip — Qwen3-Omni hears it (near-verbatim transcripts in the
  pilot).
* The published MLX conversions cap audio at 30 seconds (their
  ``processor_config.json`` sets the feature extractor to a 30 s window; the
  official Qwen config allows 300 s). ``max_audio_seconds`` lifts that cap at
  load, so the model hears more than the first half-minute of a longer video.
* Output is JSON-schema constrained via llguidance, so the ``response`` string
  flows through the production ``structured`` flatten path verbatim.
* Requires mlx-vlm 0.8.1 or newer. The 0.6.x line needed two local patches and
  still hit GPU faults on inputs above ~5,200 prompt tokens (long audio or
  more frames); 0.8.1 runs the same inputs cleanly without patches.

* ``video_input = "video"`` hands the model the video itself, sampled at
  ``fps`` frames per second, instead of the still frames.
* ``speech_model`` names a separate speech-recognition model (mlx-audio). When
  set, it transcribes the audio track first and the transcript is given to
  Qwen next to the audio, which keeps on-screen text out of the transcript
  field and steadies Qwen on music clips.
* ``copies`` runs that many copies of the model side by side, each in its own
  child process (see ``qwen_copies``).

The model is loaded once per process and kept resident; ``max_workers`` equals
``copies`` (1 = a sequential loop in this process). Config lives in ``[machine.qwen_local]``; the prompt
addendum below is part of the version identity by design (it changes the
prompt hash, correctly forking a new ``av_`` version).
"""

import datetime as _dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading

from fyp.annotation.backends import qwen_copies
from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability
from fyp.core.fyp_config import get_config
from fyp.core.logging_setup import get_logger

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


# Given to Qwen in the user message together with the speech model's text.
SPEECH_NOTE = (
    "\n\nA separate speech-recognition model listened to the full audio track and "
    "produced the transcript below. Treat it as the record of what is SPOKEN or SUNG. "
    "Use it for the transcript field (you may correct obvious mishearings using what "
    "you hear) and to understand the story. Text that is only visible on screen "
    "belongs in text_overlays, never in the transcript. If the transcript is empty, "
    "no speech was detected.\n<speech_transcript>\n{transcript}\n</speech_transcript>"
)


def _speech_token_cap(audio_seconds: float) -> int:
    """The most tokens the speech model may write for a clip of this length.

    Speech rarely exceeds four tokens a second; the library default of 8,192
    lets a transcript that has fallen into a loop run for most of a minute.
    """
    return int(60 + 8 * max(0.0, audio_seconds))


# Answer length follows speech length: over 980 valid answers (2026-11) the
# tokens written tracked the transcript's characters at r = 0.91, and this
# line sits above all of them, including the longest (3,737 tokens).
ANSWER_CAP_BASE = 1300
ANSWER_CAP_PER_TRANSCRIPT_CHAR = 0.45


def _answer_token_cap(max_tokens: int, transcript: str | None) -> int:
    """The most tokens the model may write for one video.

    With a separate transcript the cap follows its length, so a long talking
    video gets room for its answer while a runaway answer on a music clip is
    cut after about 1,300 tokens. Without one it is ``max_tokens``.
    """
    if transcript is None:
        return int(max_tokens)
    return int(min(max_tokens, ANSWER_CAP_BASE + ANSWER_CAP_PER_TRANSCRIPT_CHAR * len(transcript)))


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
        # 30 = what the published MLX conversions do on their own.
        "max_audio_seconds": 30,
        # "frames" = still frames; "video" = the video itself at `fps`.
        "video_input": "frames",
        # Hugging Face id of an mlx-audio speech model; "" = no separate transcript.
        "speech_model": "",
        # Copies of the model running side by side (each needs its own memory).
        "copies": 1,
    }
    return {**defaults, **stored}


def _apply_audio_cap(processor, seconds) -> bool:
    """Set how many seconds of audio the processor keeps per clip.

    The model's Whisper-style feature extractor truncates audio to
    ``chunk_length`` seconds; the MLX conversions ship 30 where the official
    config has 300. Audio beyond the cap is silently dropped, so a transcript
    stops there.

    Args:
        processor: The loaded mlx-vlm processor.
        seconds: The cap to apply.

    Returns:
        True when the cap was set; False when this processor has no such
        feature extractor (nothing to do).
    """
    extractor = getattr(processor, "feature_extractor", None)
    if extractor is None or not hasattr(extractor, "chunk_length"):
        return False
    seconds = int(seconds)
    extractor.chunk_length = seconds
    extractor.n_samples = seconds * extractor.sampling_rate
    extractor.nb_max_frames = extractor.n_samples // extractor.hop_length
    return True


class QwenLocalBackend(AnnotationBackend):
    """Qwen3-Omni running locally via mlx-vlm (frames + audio)."""

    name = "qwen_local"
    max_workers = 1
    supports_batch_mode = False
    cloud_run_capable = False

    _model = None
    _processor = None
    _loaded_model_id = None
    _speech = None
    _loaded_speech_id = None
    _load_lock = threading.Lock()

    def __init__(self, overrides: dict | None = None, selection: str | None = None):
        super().__init__(overrides=overrides, selection=selection)
        self.max_workers = self._copies()

    def _copies(self) -> int:
        """How many model copies this process should spread items over."""
        if qwen_copies.is_child():
            return 1
        return max(1, int(self._effective_cf()["copies"]))

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

    def prompt_suffix(self, schema_json: dict | None = None) -> str:
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
        params = {
            "n_frames": qwen_cf["max_frames"],
            "frame_scale": qwen_cf["frame_scale"],
            "fps": qwen_cf["fps"],
            "with_audio": qwen_cf["with_audio"],
            "repetition_penalty": qwen_cf["repetition_penalty"],
            "max_audio_seconds": int(qwen_cf["max_audio_seconds"]),
        }
        # Added only when switched on, so a version made before these options
        # existed keeps its identity.
        if qwen_cf["video_input"] != "frames":
            params["video_input"] = qwen_cf["video_input"]
        if qwen_cf["speech_model"]:
            params["speech_model"] = qwen_cf["speech_model"]
            params["speech_note"] = hashlib.sha256(SPEECH_NOTE.encode()).hexdigest()[:12]
            params["answer_cap"] = f"{ANSWER_CAP_BASE}+{ANSWER_CAP_PER_TRANSCRIPT_CHAR}c"
        return params

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
                    f"worker to switch local-model variants"
                )
            if cls._model is None:
                logger.info(f"Loading local Qwen model {model_id} (one-time per process) ...")
                cls._model, cls._processor = mlx_vlm.load(model_id)
                _apply_audio_cap(cls._processor, self._effective_cf()["max_audio_seconds"])
                cls._loaded_model_id = model_id
                logger.info("Local Qwen model loaded")
        return cls._model, cls._processor

    def _ensure_speech_model(self, speech_id: str):
        """Load the speech-recognition model once per process."""
        cls = QwenLocalBackend
        with cls._load_lock:
            if cls._speech is None or cls._loaded_speech_id != speech_id:
                try:
                    from mlx_audio.stt.utils import load_model
                except ImportError as exc:
                    raise RuntimeError(
                        'mlx-audio is not installed — pip install -e ".[local_qwen_speech]"'
                    ) from exc
                logger.info(f"Loading speech model {speech_id} (one-time per process) ...")
                cls._speech = load_model(speech_id)
                cls._loaded_speech_id = speech_id
        return cls._speech

    def _transcribe(self, audio: str | None, speech_id: str) -> str:
        """The speech model's text for one audio file ("" without audio)."""
        if not audio:
            return ""
        seconds = (os.path.getsize(audio) - 44) / 32000  # mono 16 kHz, 16-bit
        out = self._ensure_speech_model(speech_id).generate(
            audio, max_tokens=_speech_token_cap(seconds)
        )
        return (getattr(out, "text", None) or "").strip()

    def annotate_one(
        self,
        item_id: str,
        platform: str | None = None,
        gen_overrides: dict | None = None,
        prompt_text: str | None = None,
        response_schema=None,
    ) -> dict:
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
        if self._copies() > 1:
            row = qwen_copies.get_pool(self._copies(), self.overrides).run(
                {
                    "item_id": item_id,
                    "platform": platform,
                    "gen_overrides": gen_overrides,
                    "prompt_text": prompt_text,
                    "response_schema": response_schema,
                }
            )
            if row is not None:
                return row
            # The copy crashed or hung and has been restarted; fall through to
            # build the standard failure row without running the model here.
            copy_failed = True
        else:
            copy_failed = False

        # Canonical subpackage paths, NOT the flat alias shims — see the
        # backends package docstring: this body runs on pool threads.
        from fyp.annotation import annotation_versioning
        from fyp.annotation.annotation_schema import get_annotation_json_schema

        qwen_cf = {
            **self._effective_cf(),
            **{k: v for k, v in (gen_overrides or {}).items() if v is not None},
        }
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

        if copy_failed:
            row["error"] = "the local model copy crashed or timed out on this item"
            row["finish_reason"] = "DNF - see error"
            return row

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
            native_video = qwen_cf["video_input"] == "video"
            frames = (
                []
                if native_video
                else _sample_frames(
                    local_video,
                    duration,
                    work_dir,
                    qwen_cf["fps"],
                    qwen_cf["max_frames"],
                    qwen_cf["frame_scale"],
                )
            )
            audio = _extract_audio(local_video, work_dir) if qwen_cf["with_audio"] else None

            model, processor = self._ensure_model()
            start = _dt.datetime.now()
            transcript = None
            if qwen_cf["speech_model"]:
                # Its own audio extraction, so the transcript does not depend
                # on whether Qwen itself is given the audio.
                speech_audio = audio or _extract_audio(local_video, work_dir)
                transcript = self._transcribe(speech_audio, qwen_cf["speech_model"])
            result = _generate(
                model,
                processor,
                frames,
                audio,
                duration,
                system_prompt,
                response_schema,
                qwen_cf,
                video=os.path.abspath(local_video) if native_video else None,
                transcript=transcript,
            )
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
    from fyp.scrape import scrape_queues

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
    from fyp.core import media_paths

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
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


def _sample_frames(
    video_file: str, duration: float, out_dir: str, fps: float, max_frames: int, scale: int
) -> list[str]:
    """Extract evenly-spaced mid-segment frames with ffmpeg, in order.

    A sample point can fall after the video's last frame (a clip stored at one
    frame per second has nothing to show at 9.5 s of 10). Such a point is
    skipped rather than failing the whole item; if no point yields a frame the
    first frame of the video is used, and only a video with no readable frame
    at all raises.
    """
    n = max(2, min(int(max_frames), math.ceil(duration * fps)))

    def cut(seek: float | None, path: str) -> bool:
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if seek is not None:
            cmd += ["-ss", f"{seek:.2f}"]
        cmd += ["-i", video_file, "-vframes", "1", "-vf", f"scale='min({scale},iw)':-2", path]
        done = subprocess.run(cmd, check=False, timeout=60, capture_output=True)
        return done.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0

    paths = []
    for i in range(n):
        path = os.path.join(out_dir, f"f{i:02}.jpg")
        if cut((i + 0.5) * duration / n, path):
            paths.append(path)
    if not paths:
        path = os.path.join(out_dir, "f00.jpg")
        if not cut(None, path):
            raise RuntimeError(f"ffmpeg could not read any frame from {video_file}")
        paths.append(path)
    return paths


def _extract_audio(video_file: str, out_dir: str) -> str | None:
    """Extract the mono 16 kHz audio track; None when absent/failed."""
    path = os.path.join(out_dir, "audio.wav")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                video_file,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                path,
            ],
            check=True,
            timeout=120,
        )
    except subprocess.SubprocessError:
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 1000 else None


def _generate(
    model,
    processor,
    frames: list[str],
    audio: str | None,
    duration: float,
    system_prompt: str,
    schema: dict,
    qwen_cf: dict,
    video: str | None = None,
    transcript: str | None = None,
) -> dict:
    """Run one constrained generation; returns text + token counts.

    Args:
        model: The loaded model.
        processor: Its processor.
        frames: Still frames, in order (empty when ``video`` is given).
        audio: The audio clip, or None.
        duration: The video's length in seconds.
        system_prompt: The annotation prompt with the backend addendum.
        schema: The JSON schema the answer must follow.
        qwen_cf: The effective backend config.
        video: Path of the video itself; when given, the model samples it at
            ``fps`` frames per second and ``frames`` is ignored.
        transcript: A separate speech model's text; None = no such step
            ("" is a real result meaning no speech was found).

    Returns:
        ``text``, ``prompt_tokens`` and ``generation_tokens``.
    """
    from mlx_vlm.generate import generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.structured import build_json_schema_logits_processor

    speech = SPEECH_NOTE.format(transcript=transcript) if transcript is not None else ""
    if video:
        content = [{"type": "video", "video": video}]
        if audio:
            content.append({"type": "audio", "audio": audio})
        content.append(
            {
                "type": "text",
                "text": f"This is one short video (duration {duration:.0f} seconds)"
                + (" with its audio track" if audio else "")
                + ". Analyze this video"
                + speech,
            }
        )
        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": content},
        ]
        prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        media = {"video": [video], "fps": qwen_cf["fps"]}
    else:
        user_text = (
            f"These are {len(frames)} frames sampled evenly, in order, "
            f"from one short video (duration {duration:.0f} seconds)"
            + (" together with the video's audio track" if audio else "")
            + ". Analyze this video"
            + speech
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        prompt = apply_chat_template(
            processor,
            model.config,
            messages,
            num_images=len(frames),
            num_audios=1 if audio else 0,
            enable_thinking=False,
        )
        media = {"image": frames}
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    logits_processor = build_json_schema_logits_processor(tokenizer, schema)

    result = generate(
        model,
        processor,
        prompt,
        **media,
        audio=[audio] if audio else None,
        temperature=qwen_cf.get("temperature", 0.0),
        max_tokens=_answer_token_cap(qwen_cf["max_tokens"], transcript),
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
