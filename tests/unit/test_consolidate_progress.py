#!/usr/bin/env python3
"""Verify consolidate_enrichment_data emits sub-progress at phase boundaries.

The consolidate step used to sit frozen at 10% because the heavy work ran with no
reporter. consolidate_enrichment_data now takes a plain (pct, msg) progress_cb and
fires it at each phase boundary (15/40/65/85/95). This stubs the heavy sub-steps
so the test is cost-free and never touches real data.

Usage:
    python tests/unit/test_consolidate_progress.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import fyp.fyp_config  # noqa: F401
from fyp.fyp_config import fyp_cf
import pandas as pd
import fyp.organize_datasets as od


def _run_capture(with_impact: bool):
    """Stub the heavy sub-steps; return the list of progress percents emitted."""
    changed = {"vid1"} if with_impact else set()

    orig = {
        "anno": od.consolidate_and_save_refined_annotations,
        "scrape": od.consolidate_and_save_scrape_data,
        "status": od.update_enrichment_status,
        "load": od.data_io.load_parquet,
    }
    # Annotations / scrape return (new_data, df, new_ids).
    od.consolidate_and_save_refined_annotations = lambda **k: (bool(changed), pd.DataFrame(), set(changed))
    od.consolidate_and_save_scrape_data = lambda **k: (False, pd.DataFrame(), set())
    od.update_enrichment_status = lambda **k: None
    # collections frame: non-empty with the changed item so the impact branch runs.
    od.data_io.load_parquet = lambda **k: pd.DataFrame(
        {"item_id": ["vid1"], od.collection_id_column: ["c1"]}
    )
    # Avoid init_study_defs side effects — pre-seed an empty study map.
    fyp_cf["study_defs"] = {}

    percents = []
    try:
        od.consolidate_enrichment_data(
            force_consolidation=False,
            verbose=False,
            progress_cb=lambda pct, msg: percents.append(pct),
        )
    finally:
        od.consolidate_and_save_refined_annotations = orig["anno"]
        od.consolidate_and_save_scrape_data = orig["scrape"]
        od.update_enrichment_status = orig["status"]
        od.data_io.load_parquet = orig["load"]
    return percents


def main():
    failures = []

    # With changed items → impact branch runs → 85 included.
    p = _run_capture(with_impact=True)
    print(f"  with impact: {p}")
    for milestone in (15, 40, 65, 85, 95):
        ok = milestone in p
        print(f"    [{'PASS' if ok else 'FAIL'}] emits {milestone}%")
        if not ok:
            failures.append(f"missing {milestone}% (with impact)")
    if p != sorted(p):
        failures.append("progress not monotonic (with impact)")
        print("    [FAIL] monotonic")
    else:
        print("    [PASS] monotonic")

    # No changed items → impact branch skipped → 85 absent, others present.
    p2 = _run_capture(with_impact=False)
    print(f"  no impact: {p2}")
    for milestone in (15, 40, 65, 95):
        ok = milestone in p2
        print(f"    [{'PASS' if ok else 'FAIL'}] emits {milestone}%")
        if not ok:
            failures.append(f"missing {milestone}% (no impact)")
    if 85 in p2:
        failures.append("85% emitted despite no impact")
        print("    [FAIL] 85% correctly skipped")
    else:
        print("    [PASS] 85% correctly skipped")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All consolidate-progress checks passed.")


if __name__ == "__main__":
    main()
