#!/usr/bin/env python3
"""Bring the stored activity data onto the 2026-09 engagement vocabulary (one-off).

What it does to ``recoded/collections_recoded.parquet`` — see
``fyp/ingest/migrations/engagement_vocabulary.py`` for the mechanics:

* ``following`` rows become ``follow``;
* TikTok ``fave`` rows that were bookmarks (``FavoriteVideoList`` in the raw
  export) become ``save`` — the raw export is read again for this, because
  the section a row came from was never stored;
* with ``--append-new-sections``, ``share`` rows are appended for TikTok raw
  files that still contain ``ShareHistoryList`` / ``RepostList`` (only
  exports that bypassed the review strip — a second pass, once the retag has
  been verified);
* with ``--recount-shares`` (2026-09-23), every stored TikTok ``share`` row
  is rebuilt from its raw export: one row per send, with the number of
  byte-identical records (a video sent to several friends at once) kept on
  the method as ``chat_head ×3``. The first append stored those records as
  separate rows and the next ingest's dedupe collapsed them without a count;
* every raw file's engagement is re-folded onto its play rows, so the
  ``extra_data`` tokens and ``link_method`` match the new vocabulary;
* every row is re-stamped with the active activity-contract version.

Idempotent: a second run finds nothing to rename or retag, re-folds to the
same tokens and reports zeros.

The production data lives in GCS. This script refuses to --apply unless the
configured storage resolves to GCS, so it can never rewrite the (flaky,
development-only) local data directory by accident:

    source .venv/bin/activate
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_engagement_vocabulary.py            # dry run
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_engagement_vocabulary.py --apply    # write
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_engagement_vocabulary.py --apply --append-new-sections
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_engagement_vocabulary.py --apply --recount-shares

Run it only after the code that emits the new vocabulary is deployed. An
apply first snapshots the parquet into the ``archive`` location and writes a
JSON report next to the parquet. Timelines caches regenerate on their own
(schema bump); study parquets rebuild from the collections-parquet fingerprint.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))


def _storage_mode() -> tuple[bool, str]:
    """Return ``(is_gcs, description)`` for the resolved data storage."""
    from fyp.fyp_config import fyp_cf

    use_gcs = bool(fyp_cf.get("data_io", {}).get("use_gcs_for_data"))
    if use_gcs:
        bucket = fyp_cf.get("data_io", {}).get("GCS_bucket_name") or "<unset>"
        prefix = fyp_cf.get("data_io", {}).get("gcs_data_prefix", "")
        return True, f"GCS bucket={bucket!r} prefix={prefix!r}"
    return False, f"LOCAL dir={fyp_cf.get('paths', {}).get('local_data')!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default is a dry run that only reports).")
    parser.add_argument("--allow-local", action="store_true",
                        help="Allow --apply against LOCAL storage (development/testing only).")
    parser.add_argument("--append-new-sections", action="store_true",
                        help="Also append share rows from raw exports that still carry them.")
    parser.add_argument("--recount-shares", action="store_true",
                        help="Rebuild every stored TikTok share row from its raw export: one row "
                             "per send, identical records counted on the method (chat_head ×3).")
    args = parser.parse_args(argv)

    is_gcs, where = _storage_mode()
    print(f"Storage resolves to: {where}")
    if args.apply and not is_gcs and not args.allow_local:
        print("REFUSING to apply: storage is not GCS. The production data lives in the bucket; "
              "set FYP_FORCE_GCS=1 (and FYP_GCS_BUCKET_NAME), or pass --allow-local for a dev run.")
        return 2

    import pandas as pd

    import fyp.data_io as data_io
    from fyp.ingest.migrations import engagement_vocabulary as mig
    from fyp.organize_datasets import COLLECTIONS_LABEL

    filename = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        print(f"No {filename} in 'recoded' — nothing to migrate.")
        return 0
    df = data_io.load_parquet(storage_location="recoded", filename=filename)
    print(f"Loaded {len(df):,} activity rows from {filename}")
    print("activity_type before:", dict(df["activity_type"].value_counts()))

    migrated, report = mig.migrate(df, append_new_sections=args.append_new_sections,
                                    recount_shares=args.recount_shares, log=print)

    print("\nactivity_type after: ", report["after"])
    print("fold tokens on play rows after:", report["fold_tokens_after"])
    if report["retag"]["missing_raw"]:
        print(f"\nRaw export not found for {len(report['retag']['missing_raw'])} file(s) — their fave rows were left as is:")
        for f in report["retag"]["missing_raw"]:
            print(f"  {f}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to write.")
        return 0

    snap = mig.snapshot_name()
    data_io.save_parquet(df=df, storage_location="archive", filename=snap)
    print(f"\nSnapshot of the pre-migration parquet written to archive/{snap}")
    data_io.save_parquet(df=migrated, storage_location="recoded", filename=filename)
    report["snapshot"] = snap
    report["applied_at"] = pd.Timestamp.utcnow().isoformat()
    report_name = f"engagement_vocabulary_migration_{pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%S')}.json"
    data_io.save_json(data=json.loads(json.dumps(report, default=str)), storage_location="recoded", filename=report_name)
    print(f"Wrote {len(migrated):,} rows to recoded/{filename}; report at recoded/{report_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
