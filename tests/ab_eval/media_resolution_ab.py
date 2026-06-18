"""HIGH vs LOW media_resolution CONTENT A/B (Workstream C quality gate).

Annotates the same local videos twice with structured output — once at
``media_resolution=HIGH``, once at ``LOW`` — holding model / prompt / schema /
all other params fixed, so resolution is the ONLY variable. Both arms are
flattened + refined through the identical recode downstream and compared
field-by-field with the shared field-type-aware metrics (enum agreement, list
Jaccard, numeric correlation, free-text coverage).

Arm A = HIGH (reference), Arm B = LOW. So in the report ``coverage_a`` is HIGH
and ``coverage_b`` is LOW; a LOW < HIGH coverage gap on text/face fields is the
signal that LOW is dropping fine detail.

BILLABLE: 2 live Gemini calls per video. Usage:
    python tests/ab_eval/media_resolution_ab.py --n 80 --seed 17 --workers 20
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
# Columns to dump side-by-side value distributions for.
DIST_COLS = ("text_overlays", "symbols_and_brands", "objects", "main_ethnicity", "faces_ethnicity")

# Approximate gemini-3-flash-preview rates ($/1M tokens) — for a rough cost line only.
_RATE_IN, _RATE_OUT = 0.50, 3.00


def _annotate_one(vid: str) -> dict:
    """Annotate one video at HIGH and LOW; return both flattened records + diag."""
    hi = annotate_structured(vid, use_local_video_file=True, media_resolution="HIGH")
    lo = annotate_structured(vid, use_local_video_file=True, media_resolution="LOW")
    hi_ok = isinstance(hi.get("parsed"), dict)
    lo_ok = isinstance(lo.get("parsed"), dict)
    flat_hi = apply_conditional_rules(flatten_structured(hi["parsed"]), hi["parsed"]) if hi_ok else {}
    flat_lo = apply_conditional_rules(flatten_structured(lo["parsed"]), lo["parsed"]) if lo_ok else {}

    def _u(d, k):
        return (d.get("usage", {}) or {}).get(k)

    return {
        "rec_hi": {"item_id": vid, **flat_hi},
        "rec_lo": {"item_id": vid, **flat_lo},
        "diag": {
            "item_id": vid,
            "hi_finish": str(hi.get("finish_reason")), "lo_finish": str(lo.get("finish_reason")),
            "hi_ok": hi_ok, "lo_ok": lo_ok,
            "hi_prompt_tokens": _u(hi, "prompt_tokens"), "lo_prompt_tokens": _u(lo, "prompt_tokens"),
            "hi_total_tokens": _u(hi, "total_tokens"), "lo_total_tokens": _u(lo, "total_tokens"),
            "hi_error": hi.get("error", ""), "lo_error": lo.get("error", ""),
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
    ap = argparse.ArgumentParser(description="HIGH vs LOW media_resolution content A/B")
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    rng = random.Random(args.seed)
    rng.shuffle(mp4s)
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    print(f"[media-res A/B] {len(video_ids)} videos from {media_dir} (seed={args.seed})")

    ma.initialize_machine()
    build_structured_config(media_resolution="HIGH")  # warm the schema build

    recs_hi, recs_lo, diags = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_annotate_one, vid): vid for vid in video_ids}
        for done, fut in enumerate(as_completed(futures), start=1):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{done}/{len(video_ids)}] ERROR: {exc}", flush=True)
                continue
            d = res["diag"]
            flag = "" if (d["hi_ok"] and d["lo_ok"]) else f"  <-- PARSE FAIL hi={d['hi_ok']} lo={d['lo_ok']}"
            print(f"  [{done}/{len(video_ids)}] {d['item_id']} "
                  f"HI={d['hi_finish']} LO={d['lo_finish']}{flag}", flush=True)
            recs_hi.append(res["rec_hi"])
            recs_lo.append(res["rec_lo"])
            diags.append(d)

    print("\n[media-res A/B] refining both arms through the shared recode downstream...")
    df_hi = refine_from_flat_dicts(recs_hi)
    df_lo = refine_from_flat_dicts(recs_lo)
    report = compare_arms(df_hi, df_lo)               # A = HIGH, B = LOW
    dists = {c: distribution_table(df_hi, df_lo, c) for c in DIST_COLS}

    # ---- token / cost + parse reliability ----
    hi_fail = sum(1 for d in diags if not d["hi_ok"])
    lo_fail = sum(1 for d in diags if not d["lo_ok"])
    hi_prompt = sum(d["hi_prompt_tokens"] or 0 for d in diags)
    lo_prompt = sum(d["lo_prompt_tokens"] or 0 for d in diags)
    hi_total = sum(d["hi_total_tokens"] or 0 for d in diags)
    lo_total = sum(d["lo_total_tokens"] or 0 for d in diags)
    n = max(1, len(diags))
    token_summary = {
        "n": len(diags),
        "hi_parse_fail": hi_fail, "lo_parse_fail": lo_fail,
        "hi_mean_prompt_tokens": hi_prompt / n, "lo_mean_prompt_tokens": lo_prompt / n,
        "hi_mean_total_tokens": hi_total / n, "lo_mean_total_tokens": lo_total / n,
        "prompt_token_reduction_pct": (100 * (hi_prompt - lo_prompt) / hi_prompt) if hi_prompt else None,
        "est_cost_high_usd": round(_cost(hi_prompt, hi_total), 4),
        "est_cost_low_usd": round(_cost(lo_prompt, lo_total), 4),
    }

    cols = report["columns"]
    sensitive = {c: m for c, m in cols.items()
                 if any(p in c.lower() for p in SENSITIVE_PATTERNS)}

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"media_res_ab_n{len(diags)}.json"
    with open(out_path, "w") as f:
        json.dump({"report": report, "sensitive": sensitive, "distributions": dists,
                   "token_summary": token_summary, "diagnostics": diags,
                   "args": vars(args)}, f, indent=2, default=str)
    df_hi.to_parquet(RESULTS_DIR / f"media_res_ab_HIGH_n{len(diags)}.parquet", index=False)
    df_lo.to_parquet(RESULTS_DIR / f"media_res_ab_LOW_n{len(diags)}.parquet", index=False)

    # ---- readable summary ----
    s = report["summary"]
    print("\n" + "=" * 78)
    print(f"MEDIA RESOLUTION A/B  (A=HIGH, B=LOW)   n={report['n_items']} items compared")
    print("=" * 78)
    print(f"  parse failures:        HIGH={hi_fail}  LOW={lo_fail}")
    print(f"  annotated_ok rate:     HIGH={_fmt(s['annotated_ok_rate_a'])}  LOW={_fmt(s['annotated_ok_rate_b'])}")
    print(f"  mean prompt tokens:    HIGH={token_summary['hi_mean_prompt_tokens']:.0f}  "
          f"LOW={token_summary['lo_mean_prompt_tokens']:.0f}  "
          f"(reduction {_fmt(token_summary['prompt_token_reduction_pct'],1)}%)")
    print(f"  est cost this run:     HIGH=${token_summary['est_cost_high_usd']}  LOW=${token_summary['est_cost_low_usd']}")
    print("  --- overall agreement (HIGH vs LOW) ---")
    print(f"  enum agreement (mean):       {_fmt(s['mean_enum_agreement'])}")
    print(f"  list Jaccard (mean):         {_fmt(s['mean_list_jaccard'])}")
    print(f"  numeric correlation (mean):  {_fmt(s['mean_numeric_correlation'])}")
    print(f"  freetext coverage Δ (LOW-HIGH): {_fmt(s['mean_freetext_coverage_delta_b_minus_a'])}")

    print("\n  --- RESOLUTION-SENSITIVE FIELDS (text / symbols / objects / faces / scenes) ---")
    print(f"  {'field':42} {'kind':8} {'metric':>9}  covHIGH covLOW")
    for c in sorted(sensitive):
        m = sensitive[c]
        kind = m["kind"]
        metric = {"enum": m.get("agreement"), "list": m.get("mean_jaccard"),
                  "numeric": m.get("correlation"), "freetext": None}.get(kind)
        print(f"  {c:42} {kind:8} {_fmt(metric):>9}  "
              f"{_fmt(m.get('coverage_a'),2):>6} {_fmt(m.get('coverage_b'),2):>6}")

    print("\n  --- value distributions (top, HIGH | LOW) ---")
    for c in DIST_COLS:
        d = dists.get(c, {})
        a = d.get("arm_a", {}); b = d.get("arm_b", {})
        if a or b:
            print(f"  [{c}]")
            print(f"      HIGH: {dict(list(a.items())[:6])}")
            print(f"      LOW : {dict(list(b.items())[:6])}")

    print(f"\n  full report saved: {out_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
