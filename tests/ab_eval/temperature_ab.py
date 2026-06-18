"""Temperature (+ repetition-penalty) A/B for the structured annotator.

Migrating to gemini-3-flash-preview raised temperature 0.0 -> 1.0 per Google's
guidance (sub-1.0 risks looping / degraded reasoning on thinking models). That
buys reasoning quality but costs run-to-run reproducibility. This harness
measures the trade directly by annotating the same videos under several configs
(all HIGH resolution), holding everything else fixed:

  * t1.0_noPen  — temperature 1.0, penalties off  (current production)
  * t0_noPen    — temperature 0.0, penalties off  (naive temp drop)
  * t0_pen      — temperature 0.0, penalties on    (the old anti-looping setup)
  * t0_pen_b    — temperature 0.0, penalties on    (reproducibility pair)

Per arm it reports LOOPING / VALIDITY diagnostics (parse-ok rate, non-STOP
finishes, output-token inflation, and a free-text repeat ratio that spikes when
the model loops). Then it compares arms with the shared field-type-aware
metrics: temp0-vs-temp0 = the temp=0 reproducibility floor; t0_pen vs t0_noPen =
whether penalties still matter; temp0 vs temp1.0 = how much content shifts.

BILLABLE: 1 live Gemini call per (arm, video). Usage:
    python tests/ab_eval/temperature_ab.py --n 80 --seed 17 --workers 20
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
from fyp.annotation_schema import apply_conditional_rules, flatten_structured
from fyp.fyp_config import fyp_cf

RESULTS_DIR = Path(__file__).resolve().parent / "results"

ARMS = [
    ("t1.0_noPen", dict(temperature=1.0, use_penalties=False, media_resolution="HIGH")),
    ("t0_noPen",   dict(temperature=0.0, use_penalties=False, media_resolution="HIGH")),
    ("t0_pen",     dict(temperature=0.0, use_penalties=True,  media_resolution="HIGH")),
    ("t0_pen_b",   dict(temperature=0.0, use_penalties=True,  media_resolution="HIGH")),
]
COMPARISONS = [
    ("temp=0 reproducibility (penON)",      "t0_pen",     "t0_pen_b"),
    ("penalty effect @ temp=0",             "t0_noPen",   "t0_pen"),
    ("temp=0(penON) vs temp=1.0 content",   "t1.0_noPen", "t0_pen"),
    ("temp=0(noPen) vs temp=1.0 content",   "t1.0_noPen", "t0_noPen"),
]


def _repeat_ratio(parsed) -> float | None:
    """Fraction of repeated word-trigrams in the model's free text (loop signal)."""
    if not isinstance(parsed, dict):
        return None
    parts: list[str] = []
    tr = parsed.get("transcript")
    if isinstance(tr, list):
        parts += [s.get("text", "") for s in tr if isinstance(s, dict)]
    parts.append(str(parsed.get("video_story", "")))
    sc = parsed.get("scenes")
    if isinstance(sc, list):
        parts += [s.get("description", "") for s in sc if isinstance(s, dict)]
    for k, v in parsed.items():
        if k.startswith(("cultural_representation", "ideological", "framing")):
            parts.append(str(v))
    words = " ".join(p for p in parts if p).lower().split()
    if len(words) < 6:
        return 0.0
    tris = [tuple(words[i:i + 3]) for i in range(len(words) - 2)]
    return round(1 - len(set(tris)) / len(tris), 4)


def _annotate(vid: str, arm: str, kwargs: dict) -> dict:
    o = annotate_structured(vid, use_local_video_file=True, **kwargs)
    ok = isinstance(o.get("parsed"), dict)
    flat = apply_conditional_rules(flatten_structured(o["parsed"]), o["parsed"]) if ok else {}
    u = o.get("usage", {}) or {}
    return {
        "arm": arm,
        "rec": {"item_id": vid, **flat},
        "diag": {
            "item_id": vid, "ok": ok, "finish": str(o.get("finish_reason")),
            "candidates_tokens": u.get("candidates_tokens"), "thoughts_tokens": u.get("thoughts_tokens"),
            "total_tokens": u.get("total_tokens"), "repeat_ratio": _repeat_ratio(o.get("parsed")),
            "error": o.get("error", ""),
        },
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(v, nd=3):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="temperature/penalty A/B")
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    rng = random.Random(args.seed)
    rng.shuffle(mp4s)
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    print(f"[temp A/B] {len(video_ids)} videos x {len(ARMS)} arms (seed={args.seed})")

    ma.initialize_machine()
    build_structured_config()  # warm

    recs: dict[str, list] = {a: [] for a, _ in ARMS}
    diags: dict[str, list] = {a: [] for a, _ in ARMS}
    tasks = [(vid, arm, kw) for arm, kw in ARMS for vid in video_ids]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_annotate, vid, arm, kw): (vid, arm) for vid, arm, kw in tasks}
        for done, fut in enumerate(as_completed(futs), start=1):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{done}/{len(tasks)}] ERROR: {exc}", flush=True)
                continue
            recs[r["arm"]].append(r["rec"])
            diags[r["arm"]].append(r["diag"])
            if done % 40 == 0:
                print(f"  ...{done}/{len(tasks)} calls done", flush=True)

    print("\n[temp A/B] refining all arms...")
    dfs = {a: refine_from_flat_dicts(recs[a]) for a, _ in ARMS}

    # ---- per-arm looping / validity ----
    loop = {}
    for a, _ in ARMS:
        ds = diags[a]
        n = max(1, len(ds))
        loop[a] = {
            "n": len(ds),
            "parse_ok_rate": sum(d["ok"] for d in ds) / n,
            "non_stop": sum(1 for d in ds if "STOP" not in d["finish"]),
            "finish_dist": dict(Counter(d["finish"] for d in ds)),
            "mean_candidates_tokens": _mean([d["candidates_tokens"] for d in ds]),
            "mean_thoughts_tokens": _mean([d["thoughts_tokens"] for d in ds]),
            "mean_repeat_ratio": _mean([d["repeat_ratio"] for d in ds]),
            "max_repeat_ratio": max([d["repeat_ratio"] for d in ds if d["repeat_ratio"] is not None], default=None),
        }

    # ---- comparisons ----
    comps = {}
    for label, a, b in COMPARISONS:
        rep = compare_arms(dfs[a], dfs[b])
        comps[label] = {"a": a, "b": b, "summary": rep["summary"], "columns": rep["columns"]}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"temperature_ab_n{args.n}.json"
    with open(out, "w") as f:
        json.dump({"loop": loop, "comparisons": {k: {"a": v["a"], "b": v["b"], "summary": v["summary"]}
                                                 for k, v in comps.items()},
                   "diagnostics": diags, "args": vars(args)}, f, indent=2, default=str)
    for a, _ in ARMS:
        dfs[a].to_parquet(RESULTS_DIR / f"temperature_ab_{a}_n{args.n}.parquet", index=False)

    # ---- summary ----
    print("\n" + "=" * 84)
    print("TEMPERATURE / PENALTY A/B")
    print("=" * 84)
    print("LOOPING / VALIDITY per arm:")
    print(f"  {'arm':12} {'parseOK':>8} {'nonSTOP':>8} {'meanOutTok':>11} {'meanThink':>10} {'repeat':>7} {'maxRep':>7}")
    for a, _ in ARMS:
        m = loop[a]
        print(f"  {a:12} {_fmt(m['parse_ok_rate'],3):>8} {m['non_stop']:>8} "
              f"{_fmt(m['mean_candidates_tokens'],0):>11} {_fmt(m['mean_thoughts_tokens'],0):>10} "
              f"{_fmt(m['mean_repeat_ratio'],3):>7} {_fmt(m['max_repeat_ratio'],3):>7}")
        if m["finish_dist"]:
            nonstop = {k: v for k, v in m["finish_dist"].items() if "STOP" not in k}
            if nonstop:
                print(f"               non-STOP finishes: {nonstop}")

    print("\nAGREEMENT (field-type-aware):")
    print(f"  {'comparison':38} {'enum':>7} {'jaccard':>8} {'numCorr':>8}")
    for label, _, _ in COMPARISONS:
        s = comps[label]["summary"]
        print(f"  {label:38} {_fmt(s['mean_enum_agreement']):>7} "
              f"{_fmt(s['mean_list_jaccard']):>8} {_fmt(s['mean_numeric_correlation']):>8}")
    print("\n  (temp=1.0 reproducibility floor from the earlier HIGH-vs-HIGH control: enum 0.887)")
    print(f"\n  full report saved: {out}")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
