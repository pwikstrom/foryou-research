"""Compare the Qwen arm against the Gemini reference with ab_eval's metrics.

Both arms' parsed structured responses go through the SAME in-memory chain
production/ab_eval uses: flatten_structured -> refine_from_flat_dicts ->
_reattach_contract_columns; then compare_arms gives scale-aware agreement.

Run locally (no GCS needed — all inputs are in the workdir):

    PYTHONPATH=. .venv/bin/python scripts/adhoc/qwen_eval/03_compare.py

Outputs (under --workdir): comparison.json, report.md
"""

import argparse
import json
import os

import pandas as pd

import fyp.annotation_contract as annotation_contract
import fyp.annotation_schema as sch
from fyp.annotation.ab_eval import (
    _reattach_contract_columns,
    _scale_map,
    compare_arms,
    contract_scale_map,
    refine_from_flat_dicts,
)

EYEBALL_COLS = ["transcript_no_repetitions", "video_story", "main_activity",
                "content_category", "text_overlays", "political_score",
                "sensitivity_score", "advertising", "aigc"]






def arm_frame(raw: dict[str, dict], contract: dict) -> pd.DataFrame:
    """Flatten + refine one arm's raw rows into a comparable frame."""
    flat_rows = []
    for item_id, row in raw.items():
        flat = {"item_id": str(item_id)}
        parsed = row.get("parsed")
        if isinstance(parsed, dict):
            flat.update(sch.flatten_structured(parsed, contract))
        flat_rows.append(flat)
    refined = refine_from_flat_dicts(flat_rows)
    return _reattach_contract_columns(refined, flat_rows, contract)






def render_report(cmp: dict, qwen_raw: dict, gemini_raw: dict,
                  df_g: pd.DataFrame, df_q: pd.DataFrame, title: str) -> str:
    """Markdown report: summary, per-field table, timings, eyeball sample."""
    lines = [f"# {title}", ""]

    summ = cmp.get("summary", {})
    lines += ["## Summary", "", "```json", json.dumps(summ, indent=1, default=str), "```", ""]

    lines += ["## Per-field agreement", "",
              "| field | kind | metric | value | both-filled n |",
              "|---|---|---|---|---|"]
    for col, info in sorted(cmp.get("columns", {}).items()):
        kind = info.get("kind", "?")
        if kind == "numeric":
            metric, value = "pearson_r / mean_abs_diff", (
                f"{info.get('pearson_r')} / {info.get('mean_abs_diff')}")
        elif kind == "list":
            metric, value = "mean_jaccard (filled)", (
                f"{info.get('mean_jaccard')} ({info.get('mean_jaccard_filled')})")
        elif kind == "text":
            metric, value = "coverage a/b", (
                f"{info.get('coverage_a')} / {info.get('coverage_b')}")
        else:
            metric, value = "agreement (filled)", (
                f"{info.get('agreement')} ({info.get('agreement_filled')})")
        lines.append(f"| {col} | {kind} | {metric} | {value} | {info.get('n_both', '')} |")
    lines.append("")

    durs = [r["inference_duration"] for r in qwen_raw.values()
            if not r.get("error") and r.get("inference_duration", -1) > 0]
    gdurs = [r.get("inference_duration") for r in gemini_raw.values()
             if isinstance(r.get("inference_duration"), (int, float)) and r["inference_duration"] > 0]
    lines += ["## Throughput", ""]
    if durs:
        lines.append(f"- Qwen local: mean {sum(durs) / len(durs):.0f}s/video "
                     f"(min {min(durs):.0f}s, max {max(durs):.0f}s, n={len(durs)})")
    if gdurs:
        lines.append(f"- Gemini API: mean {sum(gdurs) / len(gdurs):.0f}s/video (n={len(gdurs)})")
    lines.append("")

    lines += ["## Side-by-side sample", ""]
    df_g = df_g.set_index("item_id")
    df_q = df_q.set_index("item_id")
    shared = [i for i in df_g.index if i in df_q.index][:4]
    for iid in shared:
        lines.append(f"### item {iid}")
        for col in EYEBALL_COLS:
            gv = df_g.at[iid, col] if col in df_g.columns else "<missing>"
            qv = df_q.at[iid, col] if col in df_q.columns else "<missing>"
            lines += [f"- **{col}**", f"  - gemini: {gv}", f"  - qwen:   {qv}"]
        lines.append("")
    return "\n".join(lines)






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    ap.add_argument("--qwen-file", default="qwen_raw.json")
    ap.add_argument("--out-prefix", default="",
                    help="prefix for comparison.json / report.md filenames")
    ap.add_argument("--title",
                    default="Qwen3-VL-30B-A3B (4-bit, local) vs Gemini Flash 3.0 preview")
    args = ap.parse_args()

    with open(os.path.join(args.workdir, "gemini_reference.json")) as f:
        gemini_raw = json.load(f)
    with open(os.path.join(args.workdir, args.qwen_file)) as f:
        qwen_raw = json.load(f)

    contract = annotation_contract.load_contract()
    qwen_ok = {k: v for k, v in qwen_raw.items() if not v.get("error")}
    print(f"gemini rows: {len(gemini_raw)}, qwen ok rows: {len(qwen_ok)}/{len(qwen_raw)}")

    df_g = arm_frame(gemini_raw, contract)
    df_q = arm_frame(qwen_ok, contract)
    print(f"refined frames: gemini {df_g.shape}, qwen {df_q.shape}")

    scales = _scale_map()
    scales.update(contract_scale_map(contract))
    cmp = compare_arms(df_g, df_q, scales=scales)

    with open(os.path.join(args.workdir, f"{args.out_prefix}comparison.json"), "w") as f:
        json.dump(cmp, f, indent=1, default=str)
    report = render_report(cmp, qwen_raw, gemini_raw, df_g, df_q, args.title)
    report_path = os.path.join(args.workdir, f"{args.out_prefix}report.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"wrote {report_path}")






if __name__ == "__main__":
    main()
