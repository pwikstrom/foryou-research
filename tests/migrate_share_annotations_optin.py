#!/usr/bin/env python3
"""One-time migration: make annotation sharing opt-in.

``share_annotations`` used to default to on, so nearly every user file carries
an explicit ``true`` regardless of whether the user ever annotated anything.
This script flips ``settings.share_annotations`` to ``false`` for users who
have **no annotations** in their file (nothing to share), and leaves users
with annotations untouched so active sharers keep the feature.

Dry-run by default; pass ``--apply`` to write changes. By default it targets
whatever backend the users location resolves to locally; pass ``--gcs`` to
force the prod GCS user store (requires FYP_GCS_BUCKET_NAME).

Note: the web service caches the user roster in memory, so restart/redeploy
``fyp-data-hub`` after applying against prod.

Usage:
    python tests/migrate_share_annotations_optin.py                 # local dry run
    python tests/migrate_share_annotations_optin.py --gcs           # prod dry run
    python tests/migrate_share_annotations_optin.py --gcs --apply   # prod migration
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf


# Non-user JSON files living in the users location.
_NON_USER_FILES = {"roles.json", "admin_settings.json", "var_presentation.json"}






def is_user_file(filename: str, data: dict | None) -> bool:
    """A user file is a dict with auth fields; skips logs and site config."""
    if filename in _NON_USER_FILES or filename.endswith("_log.json"):
        return False
    if not isinstance(data, dict):
        return False
    return "password_hash" in data or "username" in data






def point_users_location_at_gcs() -> None:
    """Force the users location onto the prod GCS backend.

    Local config runs with ``use_gcs_for_data = false``, so ``initialize()``
    skips deriving ``gcs_paths``. Replicate that derivation here so the script
    can target the prod user store from a dev machine.
    """
    if not fyp_cf['data_io'].get('bucket'):
        sys.exit("No GCS bucket available — set FYP_GCS_BUCKET_NAME and retry.")
    fyp_cf['data_io']['use_gcs_for_data'] = True
    gcs_prefix = fyp_cf['data_io'].get('gcs_data_prefix', '')
    fyp_cf.setdefault('gcs_paths', {})
    for k, v in fyp_cf['paths'].items():
        if k in ('media', 'local_data'):
            continue
        if isinstance(v, str) and v.startswith(fyp_cf['paths']['local_data']):
            rel = os.path.relpath(v, fyp_cf['paths']['local_data'])
            gcs_path = gcs_prefix if rel == '.' else (f"{gcs_prefix}/{rel}" if gcs_prefix else rel)
            fyp_cf['gcs_paths'].setdefault(k, gcs_path)






def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes (default is a dry run)")
    parser.add_argument("--gcs", action="store_true",
                        help="Target the prod GCS user store instead of the local one")
    args = parser.parse_args()

    if args.gcs:
        point_users_location_at_gcs()

    filenames = [f for f in data_io.listdir(storage_location="users")
                 if f.endswith(".json")]

    flipped, kept_sharing, already_off, skipped = [], [], [], []

    for filename in sorted(filenames):
        try:
            data = data_io.load_json(storage_location="users", filename=filename)
        except Exception as e:
            print(f"  !! unreadable {filename}: {e}")
            skipped.append(filename)
            continue

        if not is_user_file(filename, data):
            skipped.append(filename)
            continue

        username = data.get("username", filename[:-len(".json")])
        settings = data.get("settings") or {}
        sharing = bool(settings.get("share_annotations"))
        has_annotations = bool(data.get("annotations"))

        if not sharing:
            already_off.append(username)
        elif has_annotations:
            kept_sharing.append(username)
        else:
            flipped.append(username)
            if args.apply:
                settings["share_annotations"] = False
                data["settings"] = settings
                data_io.save_json(data=data, storage_location="users", filename=filename)

    mode = "APPLIED" if args.apply else "DRY RUN — nothing written (use --apply)"
    print(f"\n{mode}")
    print(f"  flipped off (sharing on, no annotations): {len(flipped)}")
    for u in flipped:
        print(f"    - {u}")
    print(f"  kept sharing (has annotations): {len(kept_sharing)}")
    for u in kept_sharing:
        print(f"    - {u}")
    print(f"  already off: {len(already_off)}")
    print(f"  skipped (non-user files): {len(skipped)}")
    return 0






if __name__ == "__main__":
    sys.exit(main())
