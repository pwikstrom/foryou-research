"""One-off backfill for the new per-user ``created_at`` timestamp.

Sets ``created_at`` on every registered user record in the ``users`` store:

* ``info@foryouresearch.net`` -> ``2026-01-01T00:00:00+00:00`` (UTC)
* everyone else               -> ``2026-07-01T00:00:00+00:00`` (UTC)

Only files that are genuine user records (those carrying a ``username`` key) are
touched — the ``users`` location also holds ``roles.json``, ``*_log.json``,
``irrelevant_words.json`` and other non-user JSON, which are left alone. Runs
through ``data_io`` so it works against either the local filesystem or GCS,
depending on the active config.

Usage:
    python scripts/adhoc/backfill_created_at.py --dry-run   # preview
    python scripts/adhoc/backfill_created_at.py             # apply
"""

import argparse
import datetime

import fyp.data_io as data_io

STORAGE_LOCATION = "users"

SPECIAL_USER = "info@foryouresearch.net"

DEFAULT_CREATED_AT = datetime.datetime(
    2026, 7, 1, 0, 0, 0, tzinfo=datetime.timezone.utc
).isoformat()

SPECIAL_CREATED_AT = datetime.datetime(
    2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc
).isoformat()


def _is_user_file(filename: str) -> bool:
    """Return True if ``filename`` is a candidate user-record file.

    Mirrors the roster filter in ``UserManager.load_users`` — ``.json`` files
    that are neither the per-user ``_tags.json`` sidecar nor ``roles.json``. The
    definitive check (presence of a ``username`` key) happens after load.
    """
    return (
        filename.endswith(".json")
        and not filename.endswith("_tags.json")
        and filename != "roles.json"
    )


def backfill(dry_run: bool) -> None:
    """Backfill ``created_at`` for every user record in the store.

    Args:
        dry_run: When True, report the planned changes without writing.
    """
    files = data_io.listdir(
        storage_location=STORAGE_LOCATION, return_absolute_path=False
    )
    candidates = sorted(f for f in files if _is_user_file(f))

    updated = 0
    skipped_non_user = 0
    for filename in candidates:
        data = data_io.load_json(
            storage_location=STORAGE_LOCATION, filename=filename
        )
        if not data or "username" not in data:
            skipped_non_user += 1
            continue

        username = data["username"]
        created_at = (
            SPECIAL_CREATED_AT if username == SPECIAL_USER else DEFAULT_CREATED_AT
        )
        previous = data.get("created_at")
        data["created_at"] = created_at

        action = "DRY-RUN" if dry_run else "SET"
        print(
            f"[{action}] {username}: created_at {previous!r} -> {created_at!r}"
        )

        if not dry_run:
            data_io.save_json(
                data=data,
                storage_location=STORAGE_LOCATION,
                filename=filename,
            )
        updated += 1

    verb = "would update" if dry_run else "updated"
    print(
        f"\nDone: {verb} {updated} user record(s); "
        f"skipped {skipped_non_user} non-user JSON file(s)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the changes without writing any files.",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
