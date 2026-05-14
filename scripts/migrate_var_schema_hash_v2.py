"""One-shot migration: rewrite v1 var_schema_hash entries in study sidecars.

After narrowing :func:`fyp.recode_variables.compute_var_schema_hash` and
introducing the ``v2:`` version prefix, every existing sidecar contains a
v1-style 64-char hex hash that will never match the new function's output.
Without this migration, the first post-deploy refresh check would mark
every study stale and rebuild all study parquets.

This script walks every ``*_recoded.meta.json`` in the recoded-data
location and rewrites ``var_schema_hash`` to the *current* v2 hash for
sidecars that still hold an unprefixed (v1) value.  Idempotent: re-runs
on already-migrated sidecars are no-ops.

Run dry-run first:
    python scripts/migrate_var_schema_hash_v2.py --dry-run

Then apply:
    python scripts/migrate_var_schema_hash_v2.py --apply
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fyp import data_io
from fyp.recode_variables import compute_var_schema_hash, VAR_SCHEMA_HASH_VERSION

V1_HEX_RE = re.compile(r"^[0-9a-f]{64}$")



def _classify(hash_value):
    """Return ('v1' | 'v2' | 'empty' | 'unknown', value)."""
    if hash_value is None or hash_value == "":
        return "empty", hash_value
    s = str(hash_value)
    if s.startswith(f"{VAR_SCHEMA_HASH_VERSION}:"):
        return "v2", s
    if s == "empty":
        return "empty-sentinel", s
    if V1_HEX_RE.match(s):
        return "v1", s
    return "unknown", s



def migrate(dry_run: bool = True, verbose: bool = True) -> dict:
    """Walk sidecars and rewrite v1 hashes.  Returns counts by category."""
    counts = {"v1_rewritten": 0, "already_v2": 0, "empty": 0, "unknown": 0, "errors": 0}
    new_hash = compute_var_schema_hash()
    if not new_hash.startswith(f"{VAR_SCHEMA_HASH_VERSION}:"):
        raise RuntimeError(
            f"compute_var_schema_hash() returned {new_hash!r}; expected "
            f"prefix {VAR_SCHEMA_HASH_VERSION!r}. Refusing to migrate."
        )
    if verbose:
        print(f"Target hash: {new_hash}")
        print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")

    files = data_io.listdir(storage_location="cache")
    sidecars = [f for f in files if f.endswith("_recoded.meta.json")]
    if verbose:
        print(f"Found {len(sidecars)} sidecar files.")

    for fname in sidecars:
        try:
            sidecar = data_io.load_json(storage_location="cache", filename=fname)
        except Exception as e:
            counts["errors"] += 1
            print(f"  ! {fname}: load failed: {e}")
            continue
        category, current = _classify(sidecar.get("var_schema_hash"))
        if category == "v2":
            counts["already_v2"] += 1
            if verbose:
                print(f"  ✓ {fname}: already v2")
            continue
        if category in ("empty", "empty-sentinel"):
            counts["empty"] += 1
            if verbose:
                print(f"  · {fname}: empty hash — leaving as-is")
            continue
        if category == "unknown":
            counts["unknown"] += 1
            print(f"  ? {fname}: unrecognized hash format {current!r} — skipping")
            continue
        # v1 case
        counts["v1_rewritten"] += 1
        action = "would rewrite" if dry_run else "rewriting"
        print(f"  → {fname}: {action} {current[:8]}... → {new_hash}")
        if not dry_run:
            sidecar["var_schema_hash"] = new_hash
            data_io.save_json(data=sidecar, storage_location="cache", filename=fname)

    print()
    print("Summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return counts



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Report what would change; write nothing.")
    group.add_argument("--apply", action="store_true",
                       help="Rewrite v1 hashes to v2.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-file output.")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run, verbose=not args.quiet)



if __name__ == "__main__":
    main()
