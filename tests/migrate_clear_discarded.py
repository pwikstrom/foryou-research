#!/usr/bin/env python3
"""One-off migration helper: surgically remove entries from
``discarded_collection_files.json`` and run the standard ingest refresh so
the new clustering logic picks them up under the corrected rules.

Why this script (and not a blanket clear): the discard list mixes two kinds
of entries — files that genuinely failed to load (too few rows, malformed
JSON) and files that the old buggy clustering accidentally blacklisted.
There is no field that distinguishes them. A blanket clear surfaces
pre-existing pipeline bugs that the blacklist had been masking. Targeted
removal is reversible and safe — start with the files you know about (e.g.
the new ``VERIFY_*_May.json`` donations), confirm the result, and repeat
later for more.

Run modes:
  * ``--plan`` (default): dry-run. Loads the existing top, simulates removal
    of ``--files``, runs the new clustering against the result, prints the
    cluster's ``collection_id`` remap and the tag/study changes that would
    follow. Writes nothing.
  * ``--apply``: same simulation, then persists everything: rewrites
    ``discarded_collection_files``, ``collections_recoded``,
    ``collections_metadata``, ``collections_tags``, and ``studies``.

Always backs up the affected files to
``recoded/_backups/migration_<timestamp>/`` before mutating anything in
``--apply`` mode.

Defaults: if you don't pass ``--files``, the script targets the three new
May donations:
``VERIFY_Clara_May.json``, ``VERIFY_Karrie_May.json``, ``VERIFY_Wilma_May.json``.

Local execution only — production migration goes through the GCS-side
equivalent (edit the GCS discard list, deploy code, trigger
``ingest_refresh`` from the UI).
"""


import argparse
import datetime
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fyp import data_io, fyp_config
from fyp.ingest import (
    apply_cid_remap_to_metadata,
    get_main_collection,
)
from fyp.polars_ops import fast_vertical_concat


fyp_config.initialize()
from fyp.fyp_config import fyp_cf  # noqa: E402  (must follow initialize)


DEFAULT_FILES_TO_REMOVE = [
    "VERIFY_Clara_May.json",
    "VERIFY_Karrie_May.json",
    "VERIFY_Wilma_May.json",
]

BACKUP_FILES = [
    "discarded_collection_files.json",
    "collections_recoded.parquet",
    "collections_metadata.parquet",
    "collections_tags.json",
    "studies.json",
]


# Per-study cache files keyed by ``<study_name>_<suffix>`` under ``cache/``.
# Each suffix represents a derived artifact that depends on the study's
# ``SELECTED_COLLECTIONS`` and/or the source ``collections_recoded`` data.
STUDY_CACHE_SUFFIXES = [
    "_recoded.parquet",
    "_recoded.meta.json",
    "_PCA.parquet",
    "_explorer_metadata.json",
    "_comp_interpretations.json",
]


# Per-collection timeline cache filename patterns. ``{cid}`` is replaced
# with the (now orphaned) old collection_id; ``{interval}`` covers the
# intervals the timelines refresh produces.
TIMELINE_INTERVALS = ["day", "week", "month"]




def _stale_cache_files(cid_remap: dict[str, str], affected_studies: list[str]) -> list[Path]:
    """Return absolute paths to cache files that will be stale after the
    migration. Includes timeline caches keyed by orphaned cids and per-study
    derivatives for studies whose SELECTED_COLLECTIONS changed."""
    cache_dir = Path(fyp_cf["paths"]["cache"])
    paths: list[Path] = []

    # Timeline caches for orphaned cids.
    for old_cid in cid_remap:
        for interval in TIMELINE_INTERVALS:
            paths.append(cache_dir / f"timeline_{old_cid}_{interval}.parquet")
            paths.append(cache_dir / f"timeline_analysis_{old_cid}_{interval}.json")

    # Per-study derivatives for affected studies.
    for sname in affected_studies:
        for suffix in STUDY_CACHE_SUFFIXES:
            paths.append(cache_dir / f"{sname}{suffix}")

    return [p for p in paths if p.exists()]




def _backup(storage_location: str = "recoded") -> str:
    """Local-only backup. Production migration backs up GCS via gsutil."""
    base_dir = Path(fyp_cf["paths"][storage_location])
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = base_dir / "_backups" / f"migration_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Backing up to {backup_dir}/ ===")
    for fn in BACKUP_FILES:
        src = base_dir / fn
        if not src.exists():
            print(f"  - {fn}: not present, skipping")
            continue
        shutil.copy2(src, backup_dir / fn)
        print(f"  + {fn}")
    return str(backup_dir)




def _print_section(title: str) -> None:
    print(f"\n=== {title} ===")




def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist results. Without this, runs as a dry-run and writes nothing.",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=DEFAULT_FILES_TO_REMOVE,
        help=(
            "Filenames to remove from discarded_collection_files.json. Defaults "
            "to the three new VERIFY_*_May.json donations."
        ),
    )
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "PLAN (dry-run)"
    print(f"=== Migration mode: {mode} ===")
    print(f"Files to un-blacklist: {args.files}")

    if args.apply:
        _backup()

    main_coll = get_main_collection(verbose=True)
    discarded_before = list(main_coll.discarded_raw_files)
    print(f"\nDiscard list size before: {len(discarded_before)}")

    targets = set(args.files)
    not_in_list = sorted(targets - set(discarded_before))
    if not_in_list:
        print(
            f"WARNING: these files were NOT in the discard list — they will be "
            f"loaded normally if present in raw storage: {not_in_list}"
        )

    new_discarded = [f for f in discarded_before if f not in targets]
    main_coll.discarded_raw_files = new_discarded
    for sub in main_coll.collections:
        sub.discarded_raw_files = new_discarded

    print(f"Discard list size after removal: {len(new_discarded)}")
    print(f"Will attempt to load: {sorted(targets & set(discarded_before))}")

    _print_section("Loading existing processed top")
    main_coll.load_processed()
    rows_before = len(main_coll.data)
    print(f"Top before: {rows_before:,} rows")

    _print_section("Loading raw files (un-blacklisted ones included)")
    main_coll.load_raw()
    new_raw_total = sum(len(c.data) for c in main_coll.collections)
    print(f"Loaded {new_raw_total:,} raw activities across {len(main_coll.collections)} subcollections")

    _print_section("Processing subcollections")
    main_coll.process()
    new_processed_total = sum(
        len(c.data) for c in main_coll.collections if c.state == "processed"
    )
    print(f"Processed {new_processed_total:,} new activities ready to migrate")

    _print_section("Clustering combined dataset")
    processed_collections = [c for c in main_coll.collections if c.state == "processed"]
    if processed_collections:
        if rows_before > 0:
            main_coll.data = fast_vertical_concat(
                [main_coll.data] + [c.data for c in processed_collections]
            )
        else:
            main_coll.data = fast_vertical_concat(
                [c.data for c in processed_collections]
            )
        main_coll.state = "processed"
    rows_combined = len(main_coll.data)
    print(f"Combined: {rows_combined:,} rows")
    cid_remap = main_coll.identify_similar_file_content(drop_them=True)
    rows_after = len(main_coll.data)
    print(f"After clustering+dedupe: {rows_after:,} rows ({rows_after - rows_before:+,} vs starting top)")

    _print_section("collection_id remap (would be applied to tags/studies)")
    if cid_remap:
        for old, new in cid_remap.items():
            merged_rows = (main_coll.data["collection_id"] == new).sum()
            print(f"  {old}  ->  {new}   (merged-cluster rows: {merged_rows:,})")
    else:
        print("  (no clusters formed)")

    plan_summary = apply_cid_remap_to_metadata(cid_remap, save=False, verbose=False)
    print(
        f"\n  collections_tags.json: {len(plan_summary['tag_keys_renamed'])} renamed, "
        f"{len(plan_summary['tag_keys_merged'])} merged"
    )
    for old, new in plan_summary["tag_keys_renamed"]:
        print(f"    rename: {old}  ->  {new}")
    for old, new in plan_summary["tag_keys_merged"]:
        print(f"    merge:  {old}  ->  {new}")
    if plan_summary["unmapped_old_keys"]:
        print(
            f"  ({len(plan_summary['unmapped_old_keys'])} remap entries had no tag entry — informational)"
        )
    print(f"\n  studies.json: {len(plan_summary['studies_updated'])} studies affected")
    for sname in plan_summary["studies_updated"]:
        print(f"    - {sname}")

    # Stale-cache report (timeline + per-study derivatives).
    _print_section("Stale caches that will be invalidated")
    stale = _stale_cache_files(cid_remap, plan_summary["studies_updated"])
    if stale:
        for p in stale:
            print(f"  delete: {p}")
    else:
        print("  (none)")

    if not args.apply:
        print("\n=== Dry-run complete; no files written. Re-run with --apply to persist. ===")
        return

    _print_section("Applying changes")
    if cid_remap:
        applied_summary = apply_cid_remap_to_metadata(cid_remap, save=True, verbose=True)
        assert applied_summary == plan_summary, "metadata write diverged from plan"

    main_coll.add_local_time_features()
    main_coll.save_processed()

    # Delete stale caches *after* the parquet/tag/study writes so a crash
    # mid-migration doesn't leave caches missing without their source-of-truth
    # update having landed.
    if stale:
        for p in stale:
            try:
                p.unlink()
                print(f"  deleted: {p.name}")
            except OSError as e:
                print(f"  WARNING: failed to delete {p}: {e}")

    print("\n=== Migration applied. Backups available under recoded/_backups/. ===")
    print(
        "Note: the Cloud Run pipeline's broader refreshes (PCA, timelines, "
        "per-study recoded datasets) will rebuild lazily on first access. "
        "If you'd rather force a full rebuild now, trigger 'Refresh all "
        "studies' from the UI's data management tab."
    )




if __name__ == "__main__":
    main()
