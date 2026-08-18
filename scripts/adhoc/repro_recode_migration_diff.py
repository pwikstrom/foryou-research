"""Read-only old-vs-new recoded diff (Phase 0.4 bug-discovery gate).

Compares the pre-migration recoded store (backed up) against the freshly
re-recoded store, joined on item_id, and reports per-column: added/dropped
columns, dtype changes, null-rate changes, and value shifts (numeric corr/mean,
categorical change rate). Flags anything outside the expected change-set.

Writes nothing. Usage:
    python tests/repro_recode_migration_diff.py \
        [OLD.parquet] [NEW.parquet]
Defaults to the Phase 0 backup vs the live local recoded store.
"""
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


def _load_parquet(path: str) -> pd.DataFrame:
    """Read per-column to dodge the table-level pandas-metadata dtype error on
    list<string> columns. Numeric columns stay numeric; list columns become
    object Series of python lists."""
    table = pq.read_table(path)
    return pd.DataFrame({name: table.column(name).to_pandas()
                         for name in table.column_names})

OLD_DEFAULT = "tmp/premigration_backup/machine_annotations_recoded.OLD.parquet"
NEW_DEFAULT = os.path.expanduser("~/fyp_local/recoded/machine_annotations_recoded.parquet")

# Columns we EXPECT to change materially (documented in the plan); changes here
# are sanity-checked, not flagged. Everything else with a big shift is flagged.
EXPECTED_CHANGED = {
    "political_score", "sensitivity_score",   # now NaN where uncertain (no median impute)
    "speech_vs_music", "faces_age_estimate",  # retyped numeric
    "main_activity",                          # keeps the model phrase now
}
EXPECTED_DROPPED = {
    "scene_sentiments", "scene_sentiments_valence", "scene_sentiments_energy",
}

JOIN_KEY = "item_id"


def _nullrate(s: pd.Series) -> float:
    return float(s.isna().mean())


def main() -> int:
    old_path = sys.argv[1] if len(sys.argv) > 1 else OLD_DEFAULT
    new_path = sys.argv[2] if len(sys.argv) > 2 else NEW_DEFAULT
    print(f"OLD: {old_path}")
    print(f"NEW: {new_path}")
    if not (os.path.exists(old_path) and os.path.exists(new_path)):
        print("ERROR: one of the parquet files is missing.")
        return 2

    old = _load_parquet(old_path)
    new = _load_parquet(new_path)
    print(f"\nshape  OLD={old.shape}  NEW={new.shape}")

    if JOIN_KEY not in old.columns or JOIN_KEY not in new.columns:
        print(f"ERROR: join key {JOIN_KEY!r} missing.")
        return 2

    # item set diff
    o_ids, n_ids = set(old[JOIN_KEY]), set(new[JOIN_KEY])
    print(f"item_id  OLD={len(o_ids):,}  NEW={len(n_ids):,}  "
          f"only-old={len(o_ids - n_ids):,}  only-new={len(n_ids - o_ids):,}  "
          f"shared={len(o_ids & n_ids):,}")

    # column set diff
    oc, nc = set(old.columns), set(new.columns)
    dropped, added = sorted(oc - nc), sorted(nc - oc)
    print(f"\n--- columns DROPPED ({len(dropped)}) ---")
    for c in dropped:
        flag = "" if c in EXPECTED_DROPPED else "  <-- UNEXPECTED"
        print(f"  - {c}{flag}")
    print(f"--- columns ADDED ({len(added)}) ---")
    for c in added:
        print(f"  + {c}")

    # align on shared items + shared cols (one row per item_id in active store)
    old1 = old.drop_duplicates(JOIN_KEY).set_index(JOIN_KEY)
    new1 = new.drop_duplicates(JOIN_KEY).set_index(JOIN_KEY)
    shared_ids = sorted(o_ids & n_ids)
    old1 = old1.loc[shared_ids]
    new1 = new1.loc[shared_ids]

    shared_cols = [c for c in old.columns if c in nc and c != JOIN_KEY]
    print(f"\n--- per-column shift over {len(shared_ids):,} shared items "
          f"({len(shared_cols)} shared cols) ---")
    print(f"{'column':32} {'dtype old->new':22} {'null% o->n':14} {'shift':>8}  notes")
    flagged = []
    for c in shared_cols:
        so, sn = old1[c], new1[c]
        dt = f"{str(so.dtype)[:10]}->{str(sn.dtype)[:10]}"
        no, nn = _nullrate(so), _nullrate(sn)
        nulls = f"{no*100:5.1f}->{nn*100:5.1f}"
        is_num = pd.api.types.is_numeric_dtype(so) and pd.api.types.is_numeric_dtype(sn)
        if is_num:
            a = pd.to_numeric(so, errors="coerce")
            b = pd.to_numeric(sn, errors="coerce")
            both = a.notna() & b.notna()
            if both.sum() > 1 and a[both].std() > 0 and b[both].std() > 0:
                corr = float(np.corrcoef(a[both], b[both])[0, 1])
            else:
                corr = float("nan")
            shift = f"r={corr:.3f}" if corr == corr else "r=n/a"
            changed = float((~np.isclose(a.fillna(-9e9), b.fillna(-9e9))).mean())
        else:
            sa = so.astype("string").fillna("\x00")
            sb = sn.astype("string").fillna("\x00")
            changed = float((sa.values != sb.values).mean())
            shift = f"{changed*100:4.1f}%chg"
        big = changed > 0.05 or abs(nn - no) > 0.05 or (str(so.dtype) != str(sn.dtype))
        note = ""
        if big and c not in EXPECTED_CHANGED:
            note = "<-- review"
            flagged.append(c)
        elif big:
            note = "(expected)"
        print(f"{c[:32]:32} {dt:22} {nulls:14} {shift:>8}  {note}")

    print(f"\nFLAGGED (unexpected material change): {len(flagged)}")
    for c in flagged:
        print(f"  * {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
