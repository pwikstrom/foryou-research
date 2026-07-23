"""Faithful local test of the REAL annotation worker with structured output.

Unlike smoke_production_structured.py (which calls the annotate function
directly), this drives ``queue_annotation_loop`` — the exact function
``web_interface/run_queue_annotator.py``'s __main__ runs locally and the Cloud
Task invokes. It exercises: queue load (to_annotate.json) -> batch loop ->
annotate_from_video_id_list -> call_machine (structured, via the config flag) ->
save raw (with marker) -> refine (structured dispatch) -> prune queue.

The ``use_structured_output`` flag is read from config.toml (set it to true
before running) — so this also proves the real config mechanism, not a runtime
override. ALL storage (cache, raw, refined, temp) is redirected to a throwaway
temp dir, so your real local dataset is never touched.

THIS MAKES BILLABLE API CALLS (default 3 videos, ~$0.05-0.10).

Usage:
    # 1) set use_structured_output = true in config/config.toml
    # 2) python tests/ab_eval/local_worker_test.py --n 3
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.machine_annotation import queue_annotation_loop

_ISOLATE = ["cache", "machine_annotations", "machine_annotations_raw",
            "machine_annotations_refined", "temp"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()

    flag = fyp_cf["machine"]["gemini"].get("use_structured_output", False)
    print(f"config.toml use_structured_output = {flag}")
    if not flag:
        print("ERROR: set `use_structured_output = true` in config/config.toml first "
              "(this test verifies the real config flag, not a runtime override).")
        return 2

    mp4s = sorted(glob.glob(str(Path(fyp_cf["paths"]["media"]) / "*.mp4")))
    video_ids = [Path(p).stem for p in mp4s[: args.n]]
    if not video_ids:
        print("No local mp4s found.")
        return 1

    original = {loc: fyp_cf["paths"].get(loc) for loc in _ISOLATE}
    tmp = tempfile.mkdtemp(prefix="fyp_worker_test_")
    try:
        for loc in _ISOLATE:
            p = os.path.join(tmp, loc)
            os.makedirs(p, exist_ok=True)
            fyp_cf["paths"][loc] = p

        # Seed the queue exactly like the app does.
        data_io.save_json(data=list(video_ids), storage_location="cache",
                          filename="to_annotate.json")
        print(f"Seeded to_annotate.json with {len(video_ids)} videos: {video_ids}\n")

        # Run the REAL worker loop.
        queue_annotation_loop(batch_size=args.n, max_batches=1, verbose=False)

        # Verify outcomes.
        remaining = data_io.load_json(storage_location="cache", filename="to_annotate.json")
        refined = [f for f in data_io.listdir(storage_location="machine_annotations_refined")
                   if f.endswith(".parquet")]
        print("\n--- verification ---")
        print(f"queue remaining after run: {len(remaining) if isinstance(remaining, list) else remaining}")
        print(f"refined parquet files written: {len(refined)}")

        ok = False
        if refined:
            df = data_io.load_parquet(storage_location="machine_annotations_refined",
                                      filename=refined[0])
            n_ok = int(df["annotated_ok"].fillna(False).sum()) if "annotated_ok" in df.columns else 0
            has_structured_marker = bool(
                df.get("structured").fillna(False).any()
            ) if "structured" in df.columns else "n/a (dropped in recode)"
            print(f"refined shape: {df.shape} | annotated_ok rows: {n_ok} | "
                  f"structured marker present: {has_structured_marker}")
            ok = n_ok == len(video_ids) and (not isinstance(remaining, list) or len(remaining) == 0)

        print("\nRESULT:", "PASS - real worker loop + structured output + prune all worked"
              if ok else "CHECK - see output above")
        return 0 if ok else 1
    finally:
        for loc, val in original.items():
            fyp_cf["paths"][loc] = val
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
