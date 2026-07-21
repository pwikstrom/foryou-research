"""Annotate the full pilot eval set via the hosted Qwen omni API (DashScope).

Runs every item in ~/qwen_eval_work/eval_manifest.json through the DashScope
international OpenAI-compatible endpoint (default model qwen3.5-omni-flash)
with the production contract prompt + schema, and saves raw rows in the same
shape 03_compare.py expects for a challenger arm:

    {item_id: {parsed, error, inference_duration, usage, raw, model}}

Run (needs DASHSCOPE_API_KEY; venv only needs `requests`):

    .venv/bin/python scripts/adhoc/qwen_eval/06_annotate_qwen_api.py
    PYTHONPATH=. .venv/bin/python scripts/adhoc/qwen_eval/03_compare.py \
        --qwen-file qwen35_api_raw.json --out-prefix qwen35_api_ \
        --title "qwen3.5-omni-flash (DashScope API) vs Gemini Flash 3.0 preview"
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_backend_smoke import (  # noqa: E402
    build_prompt, call_qwen, load_fixtures, parse_json_response,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5-omni-flash")
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    ap.add_argument("--out", default="qwen35_api_raw.json")
    ap.add_argument("--retries", type=int, default=2,
                    help="re-attempts per item on error/unparseable output")
    args = ap.parse_args()

    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY not set")

    workdir = Path(args.workdir)
    contract_prompt, schema, items, _ = load_fixtures(workdir, n=10_000)
    prompt = build_prompt(contract_prompt, schema)
    out_path = workdir / args.out

    results: dict[str, dict] = {}
    if out_path.exists():
        results = json.loads(out_path.read_text())
        print(f"resuming: {len(results)} rows already saved")

    for i, item in enumerate(items, 1):
        iid = item["item_id"]
        if results.get(iid) and not results[iid].get("error"):
            continue
        text, err, usage, dt = "", "", {}, 0.0
        parsed = None
        for attempt in range(1 + args.retries):
            t0 = time.time()
            text, err, usage = call_qwen(key, args.model, prompt, item["video_file"])
            dt = time.time() - t0
            parsed = parse_json_response(text) if text else None
            if not err and parsed is not None:
                break
            print(f"  retry {attempt + 1}: err={err[:120]!r} "
                  f"parseable={parsed is not None}")
        if parsed is None and not err:
            err = f"unparseable output: {text[:200]}"
        results[iid] = {
            "item_id": iid, "model": args.model, "error": err,
            "inference_duration": round(dt, 1), "usage": usage,
            "parsed": parsed, "raw": text,
        }
        out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False))
        status = "ok" if parsed is not None else f"FAIL {err[:80]}"
        print(f"[{i}/{len(items)}] {iid} ({item['duration']:.0f}s) "
              f"{dt:5.1f}s in/out={usage.get('prompt_tokens')}/"
              f"{usage.get('completion_tokens')}  {status}")

    ok = sum(1 for r in results.values() if not r["error"])
    print(f"\ndone: {ok}/{len(results)} ok -> {out_path}")






if __name__ == "__main__":
    main()
