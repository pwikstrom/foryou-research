"""Resumable forced re-refine of all raw annotation batches (local).

Re-refines every raw file with the current pipeline, but SKIPS any whose
refined parquet was already (re)written at/after a cutoff timestamp — so an
interrupted run resumes cheaply instead of redoing finished files. Each file is
isolated: a failure is logged and the run continues (surfacing every problem
file in one pass) rather than aborting the whole batch.

Usage:
    python tests/repro_resumable_refine.py ["YYYY-MM-DD HH:MM"]
Cutoff defaults to "2026-06-23 14:28" (start of the post-fix run). Files refined
at/after the cutoff are treated as done.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fyp import fyp_config

fyp_config.initialize()

from fyp import data_io
import fyp.machine_annotation as ma
from fyp.fyp_config import fyp_cf


def main() -> int:
    cutoff_str = sys.argv[1] if len(sys.argv) > 1 else "2026-06-23 14:28"
    cutoff = time.mktime(time.strptime(cutoff_str, "%Y-%m-%d %H:%M"))
    refined_dir = os.path.join(
        fyp_cf["paths"]["local_data"],
        "machine_annotations", "machine_annotations_refined",
    )
    raw = [fn for fn in data_io.listdir(storage_location="machine_annotations_raw")
           if fn.startswith(ma.MACHINE_ANNOTATIONS_LABEL) and fn.endswith(".json")]
    print(f"cutoff={cutoff_str}  raw_files={len(raw)}  refined_dir={refined_dir}")

    done = skipped = failed = 0
    failures = []
    t0 = time.time()
    for i, fn in enumerate(raw):
        pq_path = os.path.join(refined_dir, fn.replace(".json", ".parquet"))
        if os.path.exists(pq_path) and os.path.getmtime(pq_path) >= cutoff:
            skipped += 1
            continue
        try:
            ma.refine_one_raw_annotation_batch(
                raw_outputs_from_machine=None,
                raw_json_filename=fn,
                verbose=False,
            )
            done += 1
        except Exception as exc:
            failed += 1
            failures.append((fn, repr(exc)[:200]))
            print(f"  FAIL {fn}: {repr(exc)[:200]}", flush=True)
        if (done + failed) % 5 == 0:
            print(f"  progress done={done} skipped={skipped} failed={failed} "
                  f"/ {len(raw)}  elapsed={round(time.time()-t0)}s", flush=True)

    print(f"\nDONE done={done} skipped={skipped} failed={failed} / {len(raw)} "
          f"elapsed={round(time.time()-t0)}s")
    if failures:
        print("FAILED FILES:")
        for fn, err in failures:
            print(f"  {fn}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
