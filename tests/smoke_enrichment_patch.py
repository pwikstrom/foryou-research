"""Smoke test for the Phase 3 enrichment-only patch path.

Strategy:
  1. Baseline: run create_study_recoded_dataset with force_full_rebuild so the
     study has a known-good cached parquet + sidecar.
  2. Simulate an enrichment-only change by bumping the scrapes parquet mtime.
     The contents stay identical, so the merged output SHOULD be identical;
     but the fingerprint should differ and plan_refresh should pick
     'enrichment_patch'.
  3. Run create_study_recoded_dataset again and compare: action tag, row count,
     item_id set, and column-wise equality (on the intersection of columns).
"""

import os
import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

import pandas as pd
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs
from fyp.organize_datasets import (
    SCRAPES_LABEL,
    create_study_recoded_dataset,
    plan_refresh,
)


STUDY = sys.argv[1] if len(sys.argv) > 1 else "paper_three"


def _row_key(df: pd.DataFrame) -> tuple:
    return (
        len(df),
        len(set(df["item_id"].dropna().astype(str).unique())),
    )


def main() -> None:
    init_study_defs()
    if STUDY not in fyp_cf["study_defs"]:
        raise SystemExit(f"Study '{STUDY}' not found. Available: {list(fyp_cf['study_defs'].keys())}")

    print(f"\n=== Smoke: enrichment patch for '{STUDY}' ===\n")

    # --- Step 1: Baseline full rebuild ---
    print(">>> Step 1: force_full_rebuild baseline")
    t0 = time.perf_counter()
    df_base = create_study_recoded_dataset(
        study_name=STUDY, save_to_cache=True, force_full_rebuild=True, verbose=False,
    )
    t_base = time.perf_counter() - t0
    assert df_base is not None and not df_base.empty, "Baseline returned empty"
    assert df_base.attrs.get("refresh_action") == "full_rebuild", (
        f"Expected full_rebuild, got {df_base.attrs.get('refresh_action')!r}"
    )
    print(f"    baseline rows={len(df_base):,}  action={df_base.attrs.get('refresh_action')}  time={t_base:.2f}s")

    # --- Step 2: Simulate enrichment change by bumping scrapes parquet mtime ---
    scrapes_fn = f"{SCRAPES_LABEL}_recoded.parquet"
    print(f"\n>>> Step 2: bump mtime on '{scrapes_fn}' (contents unchanged)")

    from fyp.fyp_config import fyp_cf as _cf
    if _cf["data_io"].get("use_gcs_for_data"):
        raise SystemExit("Smoke test only supports local storage; rerun with use_gcs_for_data=false.")
    scrapes_path = os.path.join(_cf["paths"]["recoded"], scrapes_fn)
    if not os.path.exists(scrapes_path):
        raise SystemExit(f"Source scrapes parquet missing: {scrapes_path}")

    new_time = time.time() + 5.0  # advance 5s to guarantee a visible bump
    os.utime(scrapes_path, (new_time, new_time))
    print(f"    mtime bumped to {new_time}")

    # --- Step 3: plan_refresh should now want an enrichment_patch ---
    plan = plan_refresh(STUDY, verbose=False)
    print(f"\n>>> Step 3: plan after bump: action={plan['action']}  reasons={plan['reasons']}")
    assert plan["action"] == "enrichment_patch", (
        f"Expected enrichment_patch, got {plan['action']!r} (reasons={plan['reasons']})"
    )

    # --- Step 4: Run refresh — should take the patch path ---
    print("\n>>> Step 4: refresh via enrichment_patch path")
    t0 = time.perf_counter()
    df_patch = create_study_recoded_dataset(
        study_name=STUDY, save_to_cache=True, force_full_rebuild=False, verbose=False,
    )
    t_patch = time.perf_counter() - t0
    assert df_patch is not None and not df_patch.empty, "Patch returned empty"
    assert df_patch.attrs.get("refresh_action") == "enrichment_patch", (
        f"Expected enrichment_patch, got {df_patch.attrs.get('refresh_action')!r}"
    )
    print(f"    patch rows={len(df_patch):,}  action={df_patch.attrs.get('refresh_action')}  time={t_patch:.2f}s")

    # --- Step 5: Compare baseline vs patch ---
    print("\n>>> Step 5: compare baseline vs patch")
    base_key = _row_key(df_base)
    patch_key = _row_key(df_patch)
    assert base_key == patch_key, f"Row/item mismatch: base={base_key} patch={patch_key}"
    print(f"    row_count match: {base_key[0]:,}  item_id count match: {base_key[1]:,}")

    common = sorted(set(df_base.columns) & set(df_patch.columns))
    only_base = set(df_base.columns) - set(df_patch.columns)
    only_patch = set(df_patch.columns) - set(df_base.columns)
    print(f"    shared columns: {len(common)}  only in baseline: {sorted(only_base)}  only in patch: {sorted(only_patch)}")

    if "item_id" in df_base.columns and "local_timestamp" in df_base.columns:
        sort_cols = ["item_id", "local_timestamp"]
    else:
        sort_cols = list(df_base.columns[:2])
    b_sorted = df_base[common].sort_values(sort_cols).reset_index(drop=True)
    p_sorted = df_patch[common].sort_values(sort_cols).reset_index(drop=True)

    mismatches = []
    for col in common:
        try:
            if not b_sorted[col].equals(p_sorted[col]):
                mismatches.append(col)
        except Exception as exc:
            mismatches.append(f"{col}(err:{exc})")

    if mismatches:
        print(f"    !!! column mismatches ({len(mismatches)}): {mismatches[:10]}")
    else:
        print("    all shared columns match value-for-value")

    speedup = (t_base / t_patch) if t_patch > 0 else float("inf")
    print(f"\n=== Result: baseline={t_base:.2f}s  patch={t_patch:.2f}s  speedup={speedup:.1f}x  mismatches={len(mismatches)} ===")

    if mismatches:
        raise SystemExit(1)

    # --- Step 6: After the patch, the new sidecar should match current inputs,
    # so another refresh should short-circuit. ---
    print("\n>>> Step 6: follow-up refresh should short_circuit (sidecar was updated by patch)")
    plan_after = plan_refresh(STUDY, verbose=False)
    assert plan_after["action"] == "short_circuit", (
        f"Expected short_circuit after patch, got {plan_after['action']!r} (reasons={plan_after['reasons']})"
    )
    t0 = time.perf_counter()
    df_sc = create_study_recoded_dataset(
        study_name=STUDY, save_to_cache=True, force_full_rebuild=False, verbose=False,
    )
    t_sc = time.perf_counter() - t0
    assert df_sc is not None and df_sc.attrs.get("refresh_action") == "short_circuit", (
        f"Expected short_circuit action, got {df_sc.attrs.get('refresh_action')!r}"
    )
    print(f"    short_circuit rows={len(df_sc):,}  time={t_sc:.2f}s")
    print("\n=== All checks passed ===")


if __name__ == "__main__":
    main()
