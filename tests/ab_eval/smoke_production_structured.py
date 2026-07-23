"""End-to-end production-path smoke for structured output (Phase 2 productionize).

Flips the ``use_structured_output`` flag at runtime and drives the REAL
production annotation entrypoint (``annotate_from_video_id_list`` ->
``call_machine_threads`` -> ``call_machine`` -> ``refine_one_raw_annotation_batch``)
on a couple of local videos, in isolated storage so nothing real is written.

Validates the full integrated path: structured call -> raw saved with the
``structured`` marker -> refinement routed through the structured flattener ->
``annotated_ok``. Confirms the productionized wiring, not just the units.

THIS MAKES BILLABLE API CALLS (default 2 videos, ~$0.05). Local video only;
the GCS video URI path is the same proven code in call_machine but is not
exercised here.

Usage:
    python tests/ab_eval/smoke_production_structured.py --n 2
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _harness import isolated_storage

import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    args = ap.parse_args()

    mp4s = sorted(glob.glob(str(Path(fyp_cf["paths"]["media"]) / "*.mp4")))
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    if not video_ids:
        print("No local mp4s found.")
        return 1

    original_flag = fyp_cf["machine"]["gemini"].get("use_structured_output", False)
    fyp_cf["machine"]["gemini"]["use_structured_output"] = True
    print(f"Production path with use_structured_output=True on {video_ids}\n")
    try:
        with isolated_storage():
            ok_ids, fail_ids = ma.annotate_from_video_id_list(
                fine_list=video_ids,
                max_workers=2,
                refine_after_annotation=True,
                verbose=True,
            )
    finally:
        fyp_cf["machine"]["gemini"]["use_structured_output"] = original_flag

    print(f"\n=== ok={len(ok_ids)} fail={len(fail_ids)} ===")
    print(f"  ok_ids:   {ok_ids}")
    print(f"  fail_ids: {fail_ids}")
    success = len(ok_ids) == len(video_ids)
    print("RESULT:", "PASS - production structured path works end-to-end" if success
          else "PARTIAL/FAIL - check output above")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
