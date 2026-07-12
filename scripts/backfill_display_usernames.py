#!/usr/bin/env python3
"""Backfill display usernames for existing users from their email addresses.

One-off migration for the "My stuff" profile feature: every user record gains
a ``display_username`` derived from the account email — the alphanumeric run
up to the first non-alphanumeric character, extended with subsequent runs
while shorter than 3 characters, capped at 15, first character upper-cased
(e.g. ``patrikwikstrom@gmail.com`` -> ``Patrikwikstrom``). Users that already
have a ``display_username`` are left untouched. Display usernames are UI-only
and not required to be unique, so derivation collisions are acceptable.

Usage:
    source .venv/bin/activate
    python scripts/backfill_display_usernames.py [--dry-run]

    # Against the production GCS user store (run from the deployed commit):
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/backfill_display_usernames.py
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import fyp.data_io as data_io


SKIP_FILENAMES = {"roles.json", "users.json", "users.json.migrated"}




def derive_display_username(email: str) -> str | None:
    """Derive a display username from an email address.

    Args:
        email: The account email address.

    Returns:
        The derived username, or None when no valid username can be built.
    """
    runs = re.findall(r"[A-Za-z0-9]+", email or "")
    if not runs:
        return None
    name = runs[0]
    i = 1
    while len(name) < 3 and i < len(runs):
        name += runs[i]
        i += 1
    if len(name) < 3:
        return None
    name = name[:15]
    return name[0].upper() + name[1:]




def backfill(dry_run: bool) -> None:
    """Assign a derived display_username to every user file lacking one.

    Args:
        dry_run: When True, report what would change without saving.
    """
    derived, skipped, failed = [], [], []

    for filename in sorted(data_io.listdir(storage_location="users", return_absolute_path=False)):
        if not filename.endswith(".json"):
            continue
        if filename in SKIP_FILENAMES or filename.endswith("_tags.json"):
            continue

        data = data_io.load_json(storage_location="users", filename=filename)
        if not data or "username" not in data:
            continue

        email = data["username"]
        if data.get("display_username"):
            skipped.append((email, data["display_username"]))
            continue

        name = derive_display_username(email)
        if not name:
            failed.append(email)
            continue

        derived.append((email, name))
        if not dry_run:
            data["display_username"] = name
            data_io.save_json(data=data, storage_location="users", filename=filename)

    verb = "Would derive" if dry_run else "Derived"
    print(f"\n{verb} ({len(derived)}):")
    for email, name in derived:
        print(f"  {email:<45} -> {name}")
    print(f"\nSkipped, already set ({len(skipped)}):")
    for email, name in skipped:
        print(f"  {email:<45} = {name}")
    if failed:
        print(f"\nFAILED to derive ({len(failed)}) — set manually via Admin:")
        for email in failed:
            print(f"  {email}")
    print(f"\nTotal: {len(derived)} derived, {len(skipped)} skipped, {len(failed)} failed."
          + (" (dry run — nothing saved)" if dry_run else ""))




if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without saving.")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
