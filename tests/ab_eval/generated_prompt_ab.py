"""Prompt A/B: file prompt (use_generated_prompt=off) vs generated prompt (on).

Workstream E can serve the Gemini prompt either from the static file
(``new_prompt_002.txt``, the production default) or generated from the declarative
contract (``annotation_schema.build_prompt``). The generated prompt is faithful but
NOT byte-identical to the file, so this harness measures whether it yields
equivalent annotation CONTENT, holding everything else fixed (same model, temp=0,
penalties off, HIGH resolution, identical response_schema). The only difference
between arms is the system-instruction text.

Three arms so the file-vs-generated gap is read against the run-to-run floor
(temp=0 is reproducible but not perfectly deterministic, ~0.93):

  * file        — the static prompt file
  * file_b      — the static prompt file again (reproducibility pair)
  * generated   — build_prompt() from the contract

A field-level difference between file and generated is only "real" if it falls
BELOW the file-vs-file_b floor.

BILLABLE: 1 live Gemini call per (arm, video) = 3 x N calls. Usage:
    python tests/ab_eval/generated_prompt_ab.py --n 20 --seed 17 --workers 12
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _ab_common import compare_arms, refine_from_flat_dicts
from structured_annotator import annotate_structured, build_structured_config

import fyp.machine_annotation as ma
from fyp.annotation_schema import build_prompt, flatten_structured
from fyp.fyp_config import fyp_cf

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Production generation config for every arm; only the prompt differs.
_GEN = dict(temperature=0.0, use_penalties=False, media_resolution="HIGH")


def _annotate(vid: str, arm: str, prompt: str) -> dict:
    o = annotate_structured(vid, use_local_video_file=True, prompt_override=prompt, **_GEN)
    ok = isinstance(o.get("parsed"), dict)
    flat = flatten_structured(o["parsed"]) if ok else {}
    u = o.get("usage", {}) or {}
    return {
        "arm": arm,
        "rec": {"item_id": vid, **flat},
        "diag": {
            "item_id": vid, "ok": ok, "finish": str(o.get("finish_reason")),
            "total_tokens": u.get("total_tokens"), "error": o.get("error", ""),
        },
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(v, nd=3):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="file-vs-generated prompt A/B")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    with open(fyp_cf["machine"]["prompt"]) as f:
        file_prompt = f.read()
    gen_prompt = build_prompt()
    print(f"[prompt A/B] file prompt {len(file_prompt)} chars | generated {len(gen_prompt)} chars "
          f"| identical={file_prompt == gen_prompt}")

    arms = [("file", file_prompt), ("file_b", file_prompt), ("generated", gen_prompt)]
    comparisons = [
        ("file reproducibility floor", "file", "file_b"),
        ("file vs generated (content)", "file", "generated"),
    ]

    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    rng = random.Random(args.seed)
    rng.shuffle(mp4s)
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    print(f"[prompt A/B] {len(video_ids)} videos x {len(arms)} arms (seed={args.seed})")

    ma.initialize_machine()
    build_structured_config()  # warm the client

    recs: dict[str, list] = {a: [] for a, _ in arms}
    diags: dict[str, list] = {a: [] for a, _ in arms}
    tasks = [(vid, arm, prompt) for arm, prompt in arms for vid in video_ids]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_annotate, vid, arm, prompt): (vid, arm) for vid, arm, prompt in tasks}
        for done, fut in enumerate(as_completed(futs), start=1):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{done}/{len(tasks)}] ERROR: {exc}", flush=True)
                continue
            recs[r["arm"]].append(r["rec"])
            diags[r["arm"]].append(r["diag"])
            if done % 15 == 0:
                print(f"  ...{done}/{len(tasks)} calls done", flush=True)

    print("\n[prompt A/B] refining all arms...")
    dfs = {a: refine_from_flat_dicts(recs[a]) for a, _ in arms}

    validity = {}
    for a, _ in arms:
        ds = diags[a]
        n = max(1, len(ds))
        validity[a] = {
            "n": len(ds),
            "parse_ok_rate": sum(d["ok"] for d in ds) / n,
            "non_stop": sum(1 for d in ds if "STOP" not in d["finish"]),
            "finish_dist": dict(Counter(d["finish"] for d in ds)),
            "mean_total_tokens": _mean([d["total_tokens"] for d in ds]),
        }

    comps = {}
    for label, a, b in comparisons:
        rep = compare_arms(dfs[a], dfs[b])
        comps[label] = {"a": a, "b": b, "summary": rep["summary"], "columns": rep["columns"]}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"generated_prompt_ab_n{args.n}.json"
    with open(out, "w") as f:
        json.dump({"validity": validity,
                   "comparisons": {k: {"a": v["a"], "b": v["b"], "summary": v["summary"],
                                       "columns": v["columns"]} for k, v in comps.items()},
                   "diagnostics": diags, "args": vars(args)}, f, indent=2, default=str)
    for a, _ in arms:
        dfs[a].to_parquet(RESULTS_DIR / f"generated_prompt_ab_{a}_n{args.n}.parquet", index=False)

    print("\n" + "=" * 84)
    print("FILE vs GENERATED PROMPT A/B")
    print("=" * 84)
    print("VALIDITY per arm:")
    print(f"  {'arm':12} {'parseOK':>8} {'nonSTOP':>8} {'meanTok':>9}")
    for a, _ in arms:
        m = validity[a]
        print(f"  {a:12} {_fmt(m['parse_ok_rate'],3):>8} {m['non_stop']:>8} {_fmt(m['mean_total_tokens'],0):>9}")

    print("\nAGREEMENT (field-type-aware):")
    print(f"  {'comparison':32} {'enum':>7} {'jaccard':>8} {'numCorr':>8}")
    for label, _, _ in comparisons:
        s = comps[label]["summary"]
        print(f"  {label:32} {_fmt(s['mean_enum_agreement']):>7} {_fmt(s['mean_list_jaccard']):>8} "
              f"{_fmt(s['mean_numeric_correlation']):>8}")

    print("\nper-enum-column: file-vs-generated agreement vs the file-vs-file_b floor")
    print("  (a gap is only 'real' when gen falls clearly below floor):")
    gen_cols = comps["file vs generated (content)"]["columns"]
    floor_cols = comps["file reproducibility floor"]["columns"]
    enum_names = sorted(
        (c for c, d in gen_cols.items() if d.get("kind") == "enum"),
        key=lambda c: gen_cols[c].get("agreement") if gen_cols[c].get("agreement") is not None else 1,
    )
    print(f"  {'column':30} {'gen':>7} {'floor':>7}")
    for c in enum_names:
        g = gen_cols[c].get("agreement")
        fl = floor_cols.get(c, {}).get("agreement")
        print(f"  {c:30} {_fmt(g):>7} {_fmt(fl):>7}")
    print(f"\n  full report saved: {out}")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
