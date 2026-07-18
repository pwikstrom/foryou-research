"""Annotate the eval-set videos locally with Qwen3-VL via mlx-vlm.

Mirrors production: the generated contract prompt as system instruction,
temperature 0, and the contract's JSON schema enforced by llguidance
constrained decoding.

NOTE — frame sampling, not the mp4: mlx-vlm's native video path is broken for
Qwen3-VL on this setup (0.6.4 and git main both emit degenerate output like
"8888..." while the same frames as images work fine). So each video is
represented as N frames sampled evenly with ffmpeg and passed as an ordered
multi-image input — the same approach Ollama users take. Consequences: the
model sees no audio track and no precise timestamps.

Run in the dedicated mlx venv (NOT the project venv — no fyp import here):

    ~/qwen_eval_venv/bin/python scripts/adhoc/qwen_eval/02_annotate_qwen.py

Outputs (under --workdir):
    qwen_raw.json — {item_id: {response, parsed?, error, seconds, tokens}}
"""

import argparse
import gc
import json
import math
import os
import shutil
import subprocess
import tempfile
import time

import mlx.core as mx
from mlx_vlm import load
from mlx_vlm.generate import generate
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.structured import build_json_schema_logits_processor

DEFAULT_MODEL_ID = "mlx-community/Qwen3-VL-30B-A3B-Instruct-4bit"

# Qwen3-VL receives sampled frames only — it has no access to the audio track
# (Gemini does). Without this addendum the model hallucinates a transcript and
# tends to fall into an unbounded repetition loop that exhausts the token
# budget before any other field is generated.
PROMPT_ADDENDUM_NO_AUDIO = (
    "\n\nIMPORTANT (this backend receives sampled video frames only, no "
    "audio): you cannot hear the audio track. For 'transcript', report only "
    "speech that is visible as on-screen captions/subtitles; otherwise use an "
    "empty string. Never repeat the same sentence more than twice. For "
    "'audio_summary', estimate from visual cues only and keep it brief."
)

# Omni variant: the audio track is provided as a separate clip alongside the
# frames. The repetition guard is kept — the loop failure mode is a property of
# the constrained transcript field, not of the missing audio alone.
PROMPT_ADDENDUM_AUDIO = (
    "\n\nIMPORTANT (this backend receives sampled video frames plus the "
    "video's full audio track as a separate audio clip): transcribe the "
    "speech you hear for 'transcript'. Never repeat the same sentence more "
    "than twice."
)






def sample_frames(video_file: str, duration: float, out_dir: str,
                  fps: float, max_frames: int, scale: int) -> list[str]:
    """Extract evenly-spaced frames with ffmpeg; return their paths in order."""
    n = max(2, min(max_frames, math.ceil(duration * fps)))
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






def extract_audio(video_file: str, out_dir: str) -> str | None:
    """Extract the mono 16 kHz audio track; return its path or None (no audio)."""
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






def annotate_one(model, processor, config, video_file: str, duration: float,
                 system_prompt: str, schema: dict, args) -> dict:
    """Annotate one video from sampled frames (+ audio track); return a raw-row dict."""
    frames_dir = tempfile.mkdtemp(prefix="qwen_frames_")
    try:
        frames = sample_frames(video_file, duration, frames_dir,
                               args.fps, args.max_frames, args.frame_scale)
        audio = extract_audio(video_file, frames_dir) if args.with_audio else None
        user_text = (f"These are {len(frames)} frames sampled evenly, in order, "
                     f"from one short video (duration {duration:.0f} seconds)"
                     + (" together with the video's audio track" if audio else "")
                     + ". Analyze this video")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        prompt = apply_chat_template(
            processor, config, messages,
            num_images=len(frames), num_audios=1 if audio else 0,
            enable_thinking=False,
        )
        tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        logits_processor = build_json_schema_logits_processor(tokenizer, schema)

        start = time.time()
        result = generate(
            model, processor, prompt,
            image=frames,
            audio=[audio] if audio else None,
            temperature=0.0, max_tokens=args.max_tokens,
            repetition_penalty=args.repetition_penalty, repetition_context_size=60,
            logits_processors=[logits_processor],
            verbose=False,
        )
        elapsed = time.time() - start
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)

    text = result.text if hasattr(result, "text") else str(result)
    row = {
        "model": args.model_id,
        "structured": True,
        "n_frames": len(frames),
        "with_audio": bool(audio),
        "inference_duration": elapsed,
        "response": text,
        "error": "",
        "prompt_tokens": getattr(result, "prompt_tokens", None),
        "generation_tokens": getattr(result, "generation_tokens", None),
    }
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("response is not a JSON object")
        row["parsed"] = parsed
    except (json.JSONDecodeError, ValueError) as exc:
        row["error"] = f"parse: {exc}"
    return row






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    ap.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    ap.add_argument("--with-audio", action="store_true",
                    help="extract and pass the audio track (Omni models only)")
    ap.add_argument("--out", default="qwen_raw.json")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--max-frames", type=int, default=16)
    ap.add_argument("--frame-scale", type=int, default=560)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--repetition-penalty", type=float, default=1.08)
    ap.add_argument("--limit", type=int, default=0, help="annotate only the first N items")
    args = ap.parse_args()

    addendum = PROMPT_ADDENDUM_AUDIO if args.with_audio else PROMPT_ADDENDUM_NO_AUDIO
    with open(os.path.join(args.workdir, "eval_manifest.json")) as f:
        manifest = json.load(f)
    with open(os.path.join(args.workdir, "prompt.txt")) as f:
        system_prompt = f.read() + addendum
    with open(os.path.join(args.workdir, "response_schema.json")) as f:
        schema = json.load(f)
    if args.limit:
        manifest = manifest[: args.limit]

    out_path = os.path.join(args.workdir, args.out)
    results: dict[str, dict] = {}
    if os.path.exists(out_path):
        with open(out_path) as f:
            results = json.load(f)

    if "qwen" in args.model_id.lower():
        # Patches broken Omni decode positions + audio-mask scaling in
        # mlx-vlm 0.6.5. Qwen-specific: must not run for other model families.
        import qwen3_omni_rope_fix  # noqa: F401
    elif "minicpm" in args.model_id.lower():
        # Patches the sanitizer to accept mlx-community's pre-sanitized
        # checkpoint naming (mlx-vlm 0.6.5 drops the towers otherwise).
        import minicpmo_sanitize_fix  # noqa: F401

    print(f"loading {args.model_id} ...")
    model, processor = load(args.model_id)
    config = model.config
    print("model loaded")

    for i, item in enumerate(manifest):
        item_id = item["item_id"]
        if item_id in results and not results[item_id].get("error"):
            continue
        print(f"[{i + 1}/{len(manifest)}] {item_id} ({item['duration']:.0f}s) ...", flush=True)
        try:
            row = annotate_one(model, processor, config, item["video_file"],
                               item["duration"], system_prompt, schema, args)
        except Exception as exc:
            row = {"model": args.model_id, "error": f"{type(exc).__name__}: {exc}",
                   "response": "", "inference_duration": -1}
        row["item_id"] = item_id
        row["source_platform"] = item["source_platform"]
        results[item_id] = row
        with open(out_path, "w") as f:
            json.dump(results, f, indent=1)
        status = row["error"] or "ok"
        print(f"    -> {status} in {row['inference_duration']:.1f}s "
              f"(peak mem {mx.get_peak_memory() / 1e9:.1f} GB)", flush=True)
        gc.collect()

    ok = sum(1 for r in results.values() if not r.get("error"))
    print(f"done: {ok}/{len(manifest)} ok -> {out_path}")






if __name__ == "__main__":
    main()
