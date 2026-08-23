#!/usr/bin/env python3
"""Move demographic data off existing collections onto user accounts (one-off).

For every collection in ``recoded/collections_metadata.parquet`` that still
carries ``('participants', <demographic>)`` columns (email, name, age, country,
postCode, tiktokHandle, consentToContact — the AIO donor data):

* email matches an existing hub user  → link the collection to that account
  and fill any EMPTY profile fields (never overwrite);
* email, no such user                 → create a participant account under
  that email (no password; an admin can set one), link;
* demographics but no email           → create a placeholder account
  ``p-N@<[site].participant_placeholder_domain>``, link;
* nothing usable                      → leave the collection unlinked.

Then strip the demographic columns from the parquet (donation-level fields —
campaign, donationType, consentProvided, date — stay). Idempotent: links that
are already decided are left alone and a parquet without demographic columns
is left alone, so a second run is a no-op.

The production data lives in GCS. This script refuses to --apply unless the
configured storage resolves to GCS, so it can never rewrite the (flaky,
development-only) local data directory by accident:

    source .venv/bin/activate
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_collection_accounts.py            # dry run
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/migrate_collection_accounts.py --apply    # write

Run it only after the code that stops the ingest from re-adding the columns is
deployed. An apply first snapshots the parquet and the collections sidecar into
the ``archive`` location and writes a JSON report next to the parquet.
"""

import argparse
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
    args = parser.parse_args(argv)

    is_gcs, where = _storage_mode()
    print(f"Storage resolves to: {where}")
    if args.apply and not is_gcs and not args.allow_local:
        print("REFUSING to apply: storage is not GCS. The production data lives in the bucket; "
              "set FYP_FORCE_GCS=1 (and FYP_GCS_BUCKET_NAME), or pass --allow-local for a dev run.")
        return 2

    from web_interface.collection_accounts import migrate_existing_collections

    report = migrate_existing_collections(dry_run=not args.apply, log=print)

    created = report.get("created_accounts", [])
    placeholders = report.get("placeholders", [])
    if created:
        print(f"\nParticipant accounts {'to create' if not args.apply else 'created'} ({len(created)}):")
        for u in created:
            print(f"  {u}")
    if placeholders:
        print(f"\nPlaceholder accounts {'to create' if not args.apply else 'created'} ({len(placeholders)}):")
        for u in placeholders:
            print(f"  {u}")
    conflicts = report.get("conflicts", {})
    if conflicts:
        print(f"\nProfile conflicts (existing value kept) for {len(conflicts)} account(s):")
        for u, fields in conflicts.items():
            print(f"  {u}: {fields}")
    skipped = report.get("skipped", {})
    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for cid, why in skipped.items():
            print(f"  {cid}: {why}")
    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
