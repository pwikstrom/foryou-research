"""media_resolution CONTENT A/B (Workstream C quality gate).

Annotates the same local videos twice with structured output, holding model /
prompt / schema / all other params fixed so the per-arm ``media_resolution`` is
the ONLY variable. Both arms are flattened + refined through the identical
recode downstream and compared field-by-field with the shared field-type-aware
metrics (enum agreement, list Jaccard, numeric correlation, free-text coverage),
highlighting resolution-sensitive fields and dumping side-by-side distributions.

Arms are parameterised:
  * ``--arm-a HIGH --arm-b LOW``  -> the real HIGH-vs-LOW quality test (default).
  * ``--arm-a HIGH --arm-b HIGH`` -> the HIGH-vs-HIGH CONTROL: with both arms at
    the same resolution, any disagreement is pure temperature=1.0 stochasticity.
    That is the "noise floor": a HIGH-vs-LOW gap only counts as real resolution
    loss where it falls BELOW this floor.

Arm A is the reference (``coverage_a``); arm B is the variant (``coverage_b``).

BILLABLE: 2 live Gemini calls per video. Usage:
    python tests/ab_eval/media_resolution_ab.py --n 80 --seed 17 --arm-a HIGH --arm-b LOW
    python tests/ab_eval/media_resolution_ab.py --n 80 --seed 17 --arm-a HIGH --arm-b HIGH
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _ab_common import compare_arms, distribution_table, refine_from_flat_dicts
from structured_annotator import annotate_structured, build_structured_config

import fyp.machine_annotation as ma
from fyp.annotation_schema import apply_conditional_rules, flatten_structured
from fyp.fyp_config import fyp_cf

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Columns most likely to degrade at LOW resolution (matched as name substrings,
# robust to recode renaming).
SENSITIVE_PATTERNS = ("text_overlay", "symbol", "brand", "object", "face", "ethnic", "scene")
DIST_COLS = ("text_overlays", "symbols_and_brands", "objects", "main_ethnicity", "faces_ethnicity")

# Approximate gemini-3-flash-preview rates ($/1M tokens) — rough cost line only.
_RATE_IN, _RATE_OUT = 0.50, 3.00


def _annotate_one(vid: str, res_a: str, res_b: str) -> dict:
    """Annotate one video under arm A and arm B resolutions; flatten both."""
    a = annotate_structured(vid, use_local_video_file=True, media_resolution=res_a)
    b = annotate_structured(vid, use_local_video_file=True, media_resolution=res_b)
    a_ok = isinstance(a.get("parsed"), dict)
    b_ok = isinstance(b.get("parsed"), dict)
    flat_a = apply_conditional_rules(flatten_structured(a["parsed"]), a["parsed"]) if a_ok else {}
    flat_b = apply_conditional_rules(flatten_structured(b["parsed"]), b["parsed"]) if b_ok else {}

    def _u(d, k):
        return (d.get("usage", {}) or {}).get(k)

    return {
        "rec_a": {"item_id": vid, **flat_a},
        "rec_b": {"item_id": vid, **flat_b},
        "diag": {
            "item_id": vid,
            "a_finish": str(a.get("finish_reason")), "b_finish": str(b.get("finish_reason")),
            "a_ok": a_ok, "b_ok": b_ok,
            "a_prompt_tokens": _u(a, "prompt_tokens"), "b_prompt_tokens": _u(b, "prompt_tokens"),
            "a_total_tokens": _u(a, "total_tokens"), "b_total_tokens": _u(b, "total_tokens"),
            "a_error": a.get("error", ""), "b_error": b.get("error", ""),
        },
    }


def _cost(prompt_tok, total_tok):
    if not prompt_tok or not total_tok:
        return 0.0
    out_tok = max(0, total_tok - prompt_tok)
    return prompt_tok * _RATE_IN / 1e6 + out_tok * _RATE_OUT / 1e6


def _fmt(v, nd=3):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="media_resolution content A/B")
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--arm-a", default="HIGH")
    ap.add_argument("--arm-b", default="LOW")
    args = ap.parse_args()
    res_a, res_b = args.arm_a.upper(), args.arm_b.upper()
    tag = f"{res_a}_{res_b}"

    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    rng = random.Random(args.seed)
    rng.shuffle(mp4s)
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    print(f"[media-res A/B] arm A={res_a}  arm B={res_b}  |  {len(video_ids)} videos (seed={args.seed})")

    ma.initialize_machine()
    build_structured_config(media_resolution=res_a)  # warm the schema build

    recs_a, recs_b, diags = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_annotate_one, vid, res_a, res_b): vid for vid in video_ids}
        for done, fut in enumerate(as_completed(futures), start=1):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{done}/{len(video_ids)}] ERROR: {exc}", flush=True)
                continue
            d = res["diag"]
            flag = "" if (d["a_ok"] and d["b_ok"]) else f"  <-- PARSE FAIL a={d['a_ok']} b={d['b_ok']}"
            print(f"  [{done}/{len(video_ids)}] {d['item_id']} "
                  f"A={d['a_finish']} B={d['b_finish']}{flag}", flush=True)
            recs_a.append(res["rec_a"])
            recs_b.append(res["rec_b"])
            diags.append(d)

    print("\n[media-res A/B] refining both arms through the shared recode downstream...")
    df_a = refine_from_flat_dicts(recs_a)
    df_b = refine_from_flat_dicts(recs_b)
    report = compare_arms(df_a, df_b)
    dists = {c: distribution_table(df_a, df_b, c) for c in DIST_COLS}

    a_fail = sum(1 for d in diags if not d["a_ok"])
    b_fail = sum(1 for d in diags if not d["b_ok"])
    a_prompt = sum(d["a_prompt_tokens"] or 0 for d in diags)
    b_prompt = sum(d["b_prompt_tokens"] or 0 for d in diags)
    a_total = sum(d["a_total_tokens"] or 0 for d in diags)
    b_total = sum(d["b_total_tokens"] or 0 for d in diags)
    n = max(1, len(diags))
    token_summary = {
        "n": len(diags), "arm_a": res_a, "arm_b": res_b,
        "a_parse_fail": a_fail, "b_parse_fail": b_fail,
        "a_mean_prompt_tokens": a_prompt / n, "b_mean_prompt_tokens": b_prompt / n,
        "prompt_token_reduction_pct": (100 * (a_prompt - b_prompt) / a_prompt) if a_prompt else None,
        "est_cost_a_usd": round(_cost(a_prompt, a_total), 4),
        "est_cost_b_usd": round(_cost(b_prompt, b_total), 4),
    }

    cols = report["columns"]
    sensitive = {c: m for c, m in cols.items() if any(p in c.lower() for p in SENSITIVE_PATTERNS)}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"media_res_ab_{tag}_n{len(diags)}.json"
    with open(out_path, "w") as f:
        json.dump({"report": report, "sensitive": sensitive, "distributions": dists,
                   "token_summary": token_summary, "diagnostics": diags, "args": vars(args)},
                  f, indent=2, default=str)
    df_a.to_parquet(RESULTS_DIR / f"media_res_ab_{tag}_A_n{len(diags)}.parquet", index=False)
    df_b.to_parquet(RESULTS_DIR / f"media_res_ab_{tag}_B_n{len(diags)}.parquet", index=False)

    s = report["summary"]
    print("\n" + "=" * 78)
    print(f"MEDIA RESOLUTION A/B  (A={res_a}, B={res_b})   n={report['n_items']} items compared")
    print("=" * 78)
    print(f"  parse failures:        A={a_fail}  B={b_fail}")
    print(f"  annotated_ok rate:     A={_fmt(s['annotated_ok_rate_a'])}  B={_fmt(s['annotated_ok_rate_b'])}")
    print(f"  mean prompt tokens:    A={token_summary['a_mean_prompt_tokens']:.0f}  "
          f"B={token_summary['b_mean_prompt_tokens']:.0f}  "
          f"(reduction {_fmt(token_summary['prompt_token_reduction_pct'],1)}%)")
    print(f"  est cost this run:     A=${token_summary['est_cost_a_usd']}  B=${token_summary['est_cost_b_usd']}")
    print("  --- overall agreement (A vs B) ---")
    print(f"  enum agreement (mean):       {_fmt(s['mean_enum_agreement'])}")
    print(f"  list Jaccard (mean):         {_fmt(s['mean_list_jaccard'])}")
    print(f"  numeric correlation (mean):  {_fmt(s['mean_numeric_correlation'])}")
    print(f"  freetext coverage Δ (B-A):   {_fmt(s['mean_freetext_coverage_delta_b_minus_a'])}")

    print("\n  --- RESOLUTION-SENSITIVE FIELDS (text / symbols / objects / faces / scenes) ---")
    print(f"  {'field':42} {'kind':8} {'metric':>9}  covA   covB")
    for c in sorted(sensitive):
        m = sensitive[c]
        kind = m["kind"]
        metric = {"enum": m.get("agreement"), "list": m.get("mean_jaccard"),
                  "numeric": m.get("correlation"), "freetext": None}.get(kind)
        print(f"  {c:42} {kind:8} {_fmt(metric):>9}  "
              f"{_fmt(m.get('coverage_a'),2):>5}  {_fmt(m.get('coverage_b'),2):>5}")

    print("\n  --- value distributions (top, A | B) ---")
    for c in DIST_COLS:
        d = dists.get(c, {})
        a = d.get("arm_a", {}); b = d.get("arm_b", {})
        if a or b:
            print(f"  [{c}]")
            print(f"      A: {dict(list(a.items())[:6])}")
            print(f"      B: {dict(list(b.items())[:6])}")

    print(f"\n  full report saved: {out_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
