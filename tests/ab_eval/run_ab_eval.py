"""Free-text vs structured-output A/B evaluation harness (Phase 2).

Modes:
  --mode offline   (FREE) Validates the comparison machinery with no API calls.
  --mode live      (BILLABLE) Re-annotates a sample of local videos under BOTH
                   the current free-text path and the structured path, refines
                   both through the identical recode downstream, and reports
                   field-type-aware metrics. Persists per-arm frames + per-item
                   diagnostics (finish_reason, parse status, tokens) and an
                   adjudication table of disagreeing categorical items.

Usage:
    python tests/ab_eval/run_ab_eval.py --mode offline --n 60
    python tests/ab_eval/run_ab_eval.py --mode live --n 30          # costs money
    python tests/ab_eval/run_ab_eval.py --mode live --n 30 --thinking-budget 8192
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import random
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _ab_common import compare_arms, distribution_table, refine_from_flat_dicts
from _harness import load_fixture, normalize_frame
from structured_annotator import annotate_structured

import fyp.machine_annotation as ma
from fyp.annotation_schema import build_prompt, flatten_structured
from fyp.fyp_config import fyp_cf

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SCORE_FIELDS = ("political_score", "sensitivity_score")
KEY_DISTRIBUTIONS = ("content_category", "type_of_story", "main_gender", "main_ethnicity")
ADJUDICATION_FIELDS = ("type_of_story", "content_category", "main_ethnicity")

# Live A/B compares OLD (arm A: free-text, FILE prompt) vs NEW (arm B: structured,
# generated prompt). run_live() pins the global flags + captures this prompt.
_NEW_PROMPT: str | None = None


def _to_structured_shape(nested: dict) -> dict:
    # Scores are plain integers in the current contract (no rationale).
    out = dict(nested)
    for key in SCORE_FIELDS:
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            with contextlib.suppress(ValueError):
                out[key] = int(float(val.split(", ", 1)[0].strip()))
    return out


def run_offline(n: int, seed: int) -> tuple:
    raw = [e for e in load_fixture().values() if e.get("response")]
    rng = random.Random(seed)
    rng.shuffle(raw)
    recs_a, recs_b = [], []
    for entry in raw:
        nested = ma.fuzzy_load_of_json_from_string(entry["response"])
        if not isinstance(nested, dict):
            continue
        flat_a = ma.flatten_one_machine_response(deepcopy(nested))
        if not isinstance(flat_a, dict):
            continue
        flat_b = flatten_structured(_to_structured_shape(nested))
        item_id = str(entry["item_id"])
        recs_a.append({"item_id": item_id, **flat_a})
        recs_b.append({"item_id": item_id, **flat_b})
        if len(recs_a) >= n:
            break
    return refine_from_flat_dicts(recs_a), refine_from_flat_dicts(recs_b), {}, []


def _annotate_one(vid: str, thinking_budget: int | None) -> dict:
    """Run both arms for one video; return records + per-item diagnostics."""
    a = ma.call_machine(video_id=vid, use_local_video_file=True)
    nested = ma.fuzzy_load_of_json_from_string(a.get("response") or "")
    flat_a = ma.flatten_one_machine_response(nested) if isinstance(nested, dict) else None
    a_ok = isinstance(flat_a, dict)
    if not a_ok:
        flat_a = {}

    b = annotate_structured(vid, use_local_video_file=True, thinking_budget=thinking_budget,
                            prompt_override=_NEW_PROMPT)
    b_ok = isinstance(b.get("parsed"), dict)
    flat_b = flatten_structured(b["parsed"]) if b_ok else {}

    return {
        "rec_a": {"item_id": vid, **flat_a},
        "rec_b": {"item_id": vid, **flat_b},
        "diag": {
            "item_id": vid,
            "a_finish": str(a.get("finish_reason")),
            "b_finish": str(b.get("finish_reason")),
            "a_parse_ok": a_ok,
            "b_parse_ok": b_ok,
            "b_error": b.get("error", ""),
            "b_tokens": (b.get("usage", {}) or {}).get("total_tokens"),
            "b_candidates_tokens": (b.get("usage", {}) or {}).get("candidates_tokens"),
            "b_thoughts_tokens": (b.get("usage", {}) or {}).get("thoughts_tokens"),
            "a_seconds": max(0.0, a.get("inference_duration", 0) or 0),
            "b_seconds": max(0.0, b.get("inference_duration", 0) or 0),
        },
    }


def run_live(n: int, seed: int, workers: int, thinking_budget: int | None) -> tuple:
    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    rng = random.Random(seed)
    rng.shuffle(mp4s)
    video_ids = [Path(p).stem for p in mp4s[:n]]

    global _NEW_PROMPT
    _NEW_PROMPT = build_prompt()  # NEW arm: generated prompt from the contract.
    # OLD arm (ma.call_machine) = free-text generation with the FILE prompt.
    fyp_cf["machine"]["gemini"]["use_structured_output"] = False
    fyp_cf["machine"]["gemini"]["use_generated_prompt"] = False

    ma.initialize_machine()
    from structured_annotator import build_structured_config

    build_structured_config(thinking_budget=thinking_budget)

    recs_a, recs_b, diags = [], [], []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_annotate_one, vid, thinking_budget): vid for vid in video_ids}
        for done, fut in enumerate(as_completed(futures), start=1):
            res = fut.result()
            d = res["diag"]
            flag = "" if d["b_parse_ok"] else f"  <-- B FAIL ({d['b_finish']})"
            print(f"  [{done}/{len(video_ids)}] {d['item_id']} "
                  f"A={d['a_finish']} B={d['b_finish']}{flag}", flush=True)
            recs_a.append(res["rec_a"])
            recs_b.append(res["rec_b"])
            diags.append(d)

    return refine_from_flat_dicts(recs_a), refine_from_flat_dicts(recs_b), {}, diags


def build_adjudication(df_a, df_b, fields) -> list[dict]:
    """Rows where the two arms disagree on a key categorical, with context."""
    from _ab_common import _normalize_cell

    a = df_a.drop_duplicates("item_id").set_index(df_a["item_id"].astype(str))
    b = df_b.drop_duplicates("item_id").set_index(df_b["item_id"].astype(str))
    common = sorted(set(a.index) & set(b.index))
    rows = []
    for item in common:
        for field in fields:
            if field not in a.columns or field not in b.columns:
                continue
            va, vb = a.loc[item, field], b.loc[item, field]
            if _normalize_cell(va) != _normalize_cell(vb):
                rows.append({
                    "item_id": item,
                    "field": field,
                    "free_text": _normalize_cell(va),
                    "structured": _normalize_cell(vb),
                    "video_story_free_text": str(a.loc[item].get("video_story", ""))[:200],
                })
    return rows


def _print_summary(report: dict, diags: list, df_a, df_b) -> None:
    s = report["summary"]
    print("\n" + "=" * 70)
    print(f"A/B SUMMARY  ({report['n_items']} items, {s['n_columns']} shared columns)")
    print("=" * 70)
    print(f"  annotated_ok rate     A(free-text)={s['annotated_ok_rate_a']}  "
          f"B(structured)={s['annotated_ok_rate_b']}")
    print(f"  enum agreement (mean):           {s['mean_enum_agreement']}")
    print(f"  list Jaccard (mean):             {s['mean_list_jaccard']}")
    print(f"  numeric correlation (mean):      {s['mean_numeric_correlation']}")
    print(f"  free-text coverage delta (B-A):  {s['mean_freetext_coverage_delta_b_minus_a']}")

    by_kind: dict[str, list] = {}
    for c, m in report["columns"].items():
        by_kind.setdefault(m["kind"], []).append((c, m))
    for kind in ("enum", "list", "numeric"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        keyf = {"enum": "agreement", "list": "mean_jaccard", "numeric": "correlation"}[kind]
        items = sorted(items, key=lambda kv: (kv[1].get(keyf) is None, kv[1].get(keyf) or 0))
        print(f"\n  {kind} columns (lowest first):")
        for c, m in items[:10]:
            val = m.get(keyf)
            val = "n/a" if val is None else f"{val:.2f}"
            print(f"    {c:40} {keyf}={val}  cov A={m['coverage_a']:.2f} B={m['coverage_b']:.2f}")

    if diags:
        fr = Counter(d["b_finish"] for d in diags)
        bfail = [d for d in diags if not d["b_parse_ok"]]
        print("\n  Structured finish_reason distribution:", dict(fr))
        if bfail:
            print(f"  Structured failures ({len(bfail)}):")
            for d in bfail[:8]:
                print(f"    {d['item_id']}  finish={d['b_finish']}  err={d['b_error'][:60]} "
                      f"cand_tok={d['b_candidates_tokens']} think_tok={d['b_thoughts_tokens']}")
        toks = [d["b_tokens"] for d in diags if d["b_tokens"]]
        a_s = sum(d["a_seconds"] for d in diags)
        b_s = sum(d["b_seconds"] for d in diags)
        print(f"  Timing: A={a_s:.0f}s  B={b_s:.0f}s | mean B tokens/video="
              f"{(sum(toks) / len(toks)):.0f}" if toks else "")

    print("\n  Key distributions:")
    for col in KEY_DISTRIBUTIONS:
        dt = distribution_table(df_a, df_b, col)
        if dt["arm_a"] or dt["arm_b"]:
            print(f"    {col}:\n      A: {dt['arm_a']}\n      B: {dt['arm_b']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offline", "live"], default="offline")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--thinking-budget", type=int, default=None,
                    help="cap structured thinking tokens (default: config value, -1=dynamic)")
    args = ap.parse_args()

    if args.mode == "offline":
        df_a, df_b, _cost, diags = run_offline(args.n, args.seed)
    else:
        print(f"LIVE A/B on {args.n} videos ({2 * args.n} billable calls), "
              f"thinking_budget={args.thinking_budget}\n")
        df_a, df_b, _cost, diags = run_live(args.n, args.seed, args.workers, args.thinking_budget)

    report = compare_arms(df_a, df_b)
    for col in KEY_DISTRIBUTIONS:
        report.setdefault("distributions", {})[col] = distribution_table(df_a, df_b, col)
    report["mode"] = args.mode
    report["thinking_budget"] = args.thinking_budget
    report["diagnostics"] = diags

    RESULTS_DIR.mkdir(exist_ok=True)
    tag = f"{args.mode}_n{args.n}"
    (RESULTS_DIR / f"ab_report_{tag}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    # Persist per-arm frames (normalized, round-trip-safe) for re-analysis.
    normalize_frame(df_a).reset_index().to_parquet(RESULTS_DIR / f"arm_a_{tag}.parquet", index=False)
    normalize_frame(df_b).reset_index().to_parquet(RESULTS_DIR / f"arm_b_{tag}.parquet", index=False)
    adj = build_adjudication(df_a, df_b, ADJUDICATION_FIELDS)
    (RESULTS_DIR / f"adjudication_{tag}.json").write_text(json.dumps(adj, indent=2), encoding="utf-8")

    _print_summary(report, diags, df_a, df_b)
    print(f"\n  report -> {RESULTS_DIR / f'ab_report_{tag}.json'}")
    print(f"  adjudication rows ({len(adj)}) -> {RESULTS_DIR / f'adjudication_{tag}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
