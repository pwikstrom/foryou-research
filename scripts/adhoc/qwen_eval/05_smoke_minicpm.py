"""Smoke-test MiniCPM-o 4.5 via mlx-vlm before running the full eval arm.

Gates the MiniCPM eval run on three checks, using ONE speech-heavy video from
the existing eval set:
  1. generation completes and yields schema-conformant JSON (llguidance works
     with the MiniCPM tokenizer);
  2. no repetition degeneration;
  3. audio is actually consumed — the same item is annotated with and without
     the audio clip, and the with-audio transcript must differ materially
     (a model that ignores the audio input produces near-identical output).

Run in the dedicated mlx venv (NOT the project venv — no fyp import here):

    ~/qwen_eval_venv/bin/python scripts/adhoc/qwen_eval/05_smoke_minicpm.py
"""

import argparse
import importlib.util
import json
import os
import sys

import mlx.core as mx

DEFAULT_MODEL_ID = "mlx-community/MiniCPM-o-4_5-4bit"
# Speech-heavy item (1251-char Gemini transcript) — best audio-ablation probe.
DEFAULT_ITEM_ID = "7450201371153861934"

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "annotate_common", os.path.join(_here, "02_annotate_qwen.py"))
annotate_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(annotate_common)






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    ap.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    ap.add_argument("--item-id", default=DEFAULT_ITEM_ID)
    ap.add_argument("--max-frames", type=int, default=8)
    ap.add_argument("--frame-scale", type=int, default=448)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--repetition-penalty", type=float, default=1.08)
    args = ap.parse_args()

    with open(os.path.join(args.workdir, "eval_manifest.json")) as f:
        manifest = json.load(f)
    item = next(i for i in manifest if i["item_id"] == args.item_id)
    with open(os.path.join(args.workdir, "prompt.txt")) as f:
        base_prompt = f.read()
    with open(os.path.join(args.workdir, "response_schema.json")) as f:
        schema = json.load(f)

    import minicpmo_sanitize_fix  # noqa: F401  # fixes tower-dropping sanitize in mlx-vlm 0.6.5
    from mlx_vlm import load
    print(f"loading {args.model_id} ...")
    model, processor = load(args.model_id)
    config = model.config
    print(f"model loaded (peak mem {mx.get_peak_memory() / 1e9:.1f} GB)")

    results = {}
    for label, with_audio, addendum in (
        ("with_audio", True, annotate_common.PROMPT_ADDENDUM_AUDIO),
        ("no_audio", False, annotate_common.PROMPT_ADDENDUM_NO_AUDIO),
    ):
        args.with_audio = with_audio
        print(f"\n=== {label} run on {args.item_id} "
              f"({item['duration']:.0f}s) ===", flush=True)
        row = annotate_common.annotate_one(
            model, processor, config, item["video_file"], item["duration"],
            base_prompt + addendum, schema, args)
        print(f"  {row['error'] or 'ok'} in {row['inference_duration']:.1f}s, "
              f"{row.get('generation_tokens')} gen tokens, "
              f"peak mem {mx.get_peak_memory() / 1e9:.1f} GB")
        results[label] = row

    out_path = os.path.join(args.workdir, "minicpm_smoke.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1)

    ok = True
    for label, row in results.items():
        if row.get("error") or "parsed" not in row:
            print(f"FAIL: {label} run did not produce valid JSON: {row.get('error')}")
            ok = False
    if ok:
        req = set(schema.get("required", []) or schema.get("properties", {}))
        missing = req - set(results["with_audio"]["parsed"])
        if missing:
            print(f"FAIL: with_audio response missing schema fields: {sorted(missing)}")
            ok = False

    if ok:
        t_audio = str(results["with_audio"]["parsed"].get("transcript") or "")
        t_mute = str(results["no_audio"]["parsed"].get("transcript") or "")
        print(f"\nwith_audio transcript ({len(t_audio)} chars): {t_audio[:400]}")
        print(f"\nno_audio  transcript ({len(t_mute)} chars): {t_mute[:400]}")
        if len(t_audio) < 40:
            print("\nWARNING: with-audio transcript is near-empty on a "
                  "speech-heavy video — audio likely NOT consumed.")
            ok = False
        else:
            a, b = set(t_audio.lower().split()), set(t_mute.lower().split())
            overlap = len(a & b) / max(1, len(a | b))
            print(f"\ntranscript token jaccard with/without audio: {overlap:.2f} "
                  "(low = audio genuinely used)")

    print(f"\nsmoke result: {'PASS' if ok else 'FAIL'} -> {out_path}")
    sys.exit(0 if ok else 1)






if __name__ == "__main__":
    main()
