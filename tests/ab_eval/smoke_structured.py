"""Live smoke test for the structured-output annotator (Phase 2 spike).

Runs a constrained structured call against Gemini/Vertex on a handful of LOCAL
videos to validate end-to-end before building the full A/B harness: does the
schema get accepted, does decoding return valid conforming JSON, does it flatten
to the expected columns, and how many tokens does it cost?

THIS MAKES BILLABLE API CALLS. Default is 2 videos. Run only after sign-off.

Usage:
    python tests/ab_eval/smoke_structured.py --n 2
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from structured_annotator import annotate_structured

from fyp.annotation_schema import FIELD_SPECS, flatten_structured
from fyp.fyp_config import fyp_cf

# Approximate gemini-2.5-flash rates (USD per 1M tokens). Adjust to current
# pricing; used only for a rough cost readout, not billing.
_RATE_INPUT = 0.30 / 1_000_000
_RATE_OUTPUT = 2.50 / 1_000_000


def _pick_local_videos(n: int) -> list[str]:
    media_dir = fyp_cf["paths"]["media"]
    mp4s = sorted(glob.glob(os.path.join(media_dir, "*.mp4")))
    return [Path(p).stem for p in mp4s[:n]]


def _rough_cost(usage: dict) -> float:
    inp = usage.get("prompt_tokens") or 0
    out = (usage.get("candidates_tokens") or 0) + (usage.get("thoughts_tokens") or 0)
    return inp * _RATE_INPUT + out * _RATE_OUTPUT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2, help="number of videos (billable)")
    args = ap.parse_args()

    video_ids = _pick_local_videos(args.n)
    if not video_ids:
        print(f"No local mp4s in {fyp_cf['paths']['media']}")
        return 1

    print(f"Structured smoke on {len(video_ids)} local video(s): {video_ids}\n")
    expected_cols = {name for name, _node, _rule in FIELD_SPECS}

    ok = 0
    total_cost = 0.0
    for vid in video_ids:
        res = annotate_structured(vid, use_local_video_file=True, verbose=True)
        parsed = res.get("parsed")
        if isinstance(parsed, dict):
            ok += 1
            flat = flatten_structured(parsed)
            missing = sorted(expected_cols - set(parsed.keys()))
            print(
                f"      parsed fields: {len(parsed)}/{len(expected_cols)} "
                f"(missing: {missing or 'none'})"
            )
            print(f"      flattened columns: {len(flat)}")
            for k in ("type_of_story", "content_category", "main_activity",
                      "political_score", "scene_sentiments"):
                if k in flat:
                    print(f"        {k} = {flat[k]!r}"[:110])
        else:
            print(f"      PARSE FAILED: {res.get('error')}")
        cost = _rough_cost(res.get("usage", {}))
        total_cost += cost
        print(f"      ~cost: ${cost:.4f}\n")

    print(f"=== {ok}/{len(video_ids)} produced valid structured output "
          f"| approx total cost ${total_cost:.4f} ===")
    return 0 if ok == len(video_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
