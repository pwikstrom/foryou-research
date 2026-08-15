#!/usr/bin/env python3
"""Repair failed-scrape records whose whole entry was stringified into the id.

**The corruption.** Commit 337e374 changed the on-disk failed-scrapes record
from bare item-id strings to ``{"item_id", "category"}`` dicts. It was merged
locally but never deployed, so a local drain wrote the new dict shape while
production still ran the old loader, whose consolidation step did
``list(set(map(str, failed_scrapes)))`` — turning every dict into its Python
repr and putting that repr in the id position::

    "{'item_id': '7000000000000000001', 'category': None}"

Every consumer of ``load_failed_scrapes()`` then matches nothing:
``enrichment_status.scrape_fail`` reads 100% NA, the merge-derived
``scraped_fail`` column reads all-False, and the scraper no longer recognises
items it has already given up on.

**The repair.** Item ids and categories both survive inside the repr, so this
script parses them back out. By default it rewrites the record as bare id
strings, which is the shape that is safe under *both* code versions: the old
loader's ``str()`` is a no-op on a string, so a repaired file cannot be
re-mangled by an un-deployed production service. The recovered categories are
not discarded — they are written to a sidecar in the ``archive`` location,
under a name the loader never picks up, so they can be restored in full once
337e374 is deployed::

    # 1. Now, deploy-independent — restores every consumer of the id set:
    python scripts/repair_failed_scrapes_record.py --apply

    # 2. Later, after 337e374 is deployed — restores the categories too:
    python scripts/repair_failed_scrapes_record.py --shape records --apply

Run 2 only after the deploy: the dict shape is what the old loader mangles.

The record file is rewritten in place, so the number of files in ``scrape``
never changes and no consolidation-and-archive cycle is triggered mid-repair.
Nothing outside the failed-scrapes record and the archive backups is touched —
in particular the annotation queue and annotation artifacts are untouched, so
this is safe to run while an annotation worker is going.

Usage:
    source .venv/bin/activate

    # Dry run against production GCS (reads only, prints the plan):
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> \
        python scripts/repair_failed_scrapes_record.py

    # Apply:
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> \
        python scripts/repair_failed_scrapes_record.py --apply
"""

import argparse
import ast
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import fyp.data_io as data_io
from fyp.scrape.scrape import _failed_scrapes_label

SIDECAR_PREFIX = "recovered_scrape_failure_categories"
BACKUP_PREFIX = "backup"






def parse_entry(entry) -> tuple[str, str | None] | None:
    """Normalise one on-disk record into ``(item_id, category)``.

    Handles all three shapes that can be present: the current dict form, the
    legacy bare-id string, and the mangled repr-of-dict produced by the old
    loader running over new-format records.

    Args:
        entry: One element of a failed-scrapes JSON file.

    Returns:
        The ``(item_id, category)`` pair, or None when the entry is a repr that
        cannot be parsed back into a record.
    """
    if isinstance(entry, dict):
        item_id = entry.get("item_id")
        return (str(item_id), entry.get("category")) if item_id is not None else None

    text = str(entry)
    if text.startswith("{") and "item_id" in text:
        try:
            record = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(record, dict):
            return None
        item_id = record.get("item_id")
        return (str(item_id), record.get("category")) if item_id is not None else None

    return (text, None)






def load_records(verbose: bool) -> tuple[dict[str, str | None], dict[str, int], list[str]]:
    """Read every failed-scrapes file and recover ``{item_id: category}``.

    Deliberately does NOT call ``load_failed_scrapes()`` — that helper is the
    thing being repaired, and on some versions its consolidation branch would
    rewrite the very files this script is inspecting.

    Args:
        verbose: Print each file as it is read.

    Returns:
        A tuple of (records, per-shape counts, source filenames).
    """
    filenames = [
        fn for fn in data_io.listdir(storage_location="scrape")
        if fn.startswith(_failed_scrapes_label())
    ]
    if not filenames:
        raise RuntimeError("No failed-scrapes files found in the 'scrape' location.")

    records: dict[str, str | None] = {}
    counts = {"dict": 0, "bare": 0, "mangled": 0, "unparseable": 0}

    for filename in sorted(filenames):
        if verbose:
            print(f"  reading {filename}")
        raw = data_io.load_json(storage_location="scrape", filename=filename)
        if not isinstance(raw, list):
            print(f"  WARNING: '{filename}' is not a JSON list — skipped")
            continue

        for entry in raw:
            if isinstance(entry, dict):
                shape = "dict"
            elif str(entry).startswith("{") and "item_id" in str(entry):
                shape = "mangled"
            else:
                shape = "bare"

            parsed = parse_entry(entry)
            if parsed is None:
                counts["unparseable"] += 1
                continue
            counts[shape] += 1

            item_id, category = parsed
            # A known category always wins over an unknown one, so re-running
            # after a partial repair never downgrades a record.
            if category is not None or item_id not in records:
                records[item_id] = category

    return records, counts, sorted(filenames)






def sanity_check(records: dict[str, str | None]) -> list[str]:
    """Return recovered ids that do not look like real item ids.

    A leftover brace means a repr slipped through the parser; an implausible
    length means the id itself was damaged before this script ever saw it.
    """
    suspect = []
    for item_id in records:
        if "{" in item_id or "'" in item_id or not item_id or not (5 <= len(item_id) <= 30):
            suspect.append(item_id)
    return suspect






def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shape", choices=("ids", "records"), default="ids",
                        help="Output shape: 'ids' (bare strings, safe under the old "
                             "loader — the default) or 'records' (dicts with categories, "
                             "only after 337e374 is deployed).")
    parser.add_argument("--apply", action="store_true",
                        help="Write the repair (default: dry run).")
    parser.add_argument("--verbose", action="store_true", help="Print each file read.")
    args = parser.parse_args()

    if not os.environ.get("FYP_FORCE_GCS") and not os.environ.get("K_SERVICE"):
        print("Refusing to run against local storage. Re-run with:\n"
              "  FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python "
              f"scripts/{Path(__file__).name} ...")
        return 2

    records, counts, filenames = load_records(args.verbose)

    print(f"Source files ({len(filenames)}): {', '.join(filenames)}")
    print(f"  entries already in record shape: {counts['dict']:,}")
    print(f"  entries in legacy bare-id shape: {counts['bare']:,}")
    print(f"  entries MANGLED (repr in the id): {counts['mangled']:,}")
    if counts["unparseable"]:
        print(f"  entries unparseable, dropped:    {counts['unparseable']:,}")
    print(f"  unique item ids recovered:       {len(records):,}")

    categorised = {k: v for k, v in records.items() if v is not None}
    if categorised:
        by_category: dict[str, int] = {}
        for category in categorised.values():
            by_category[category] = by_category.get(category, 0) + 1
        print(f"  of which carry a category:       {len(categorised):,}")
        for category, n in sorted(by_category.items(), key=lambda kv: -kv[1]):
            print(f"      {category:<28} {n:>7,}")

    suspect = sanity_check(records)
    if suspect:
        print(f"\nABORT: {len(suspect):,} recovered ids do not look like item ids, e.g.:")
        for item_id in suspect[:5]:
            print(f"  {item_id!r}")
        print("Nothing was written. Investigate before repairing.")
        return 1

    if counts["mangled"] == 0 and args.shape == "ids":
        print("\nNothing is mangled — the record is already in a safe shape.")
        if not args.apply:
            return 0

    stamp = "".join(c for c in str(datetime.now()) if c in "0123456789")
    target = sorted(filenames)[-1]

    if args.shape == "ids":
        payload = sorted(records)
        shape_note = "bare id strings (safe under both loader versions)"
    else:
        payload = [{"item_id": k, "category": v} for k, v in sorted(records.items())]
        shape_note = "record dicts (requires 337e374 to be deployed)"

    print("\nPlan:")
    print(f"  1. back up {len(filenames)} source file(s) to 'archive' as {BACKUP_PREFIX}_<name>")
    print(f"  2. write the {len(categorised):,} recovered categories to "
          f"'archive' as {SIDECAR_PREFIX}_{stamp}.json")
    print(f"  3. overwrite '{target}' in place with {len(payload):,} {shape_note}")
    if len(filenames) > 1:
        print(f"  4. move the other {len(filenames) - 1} source file(s) to 'archive'")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    for filename in filenames:
        original = data_io.load_json(storage_location="scrape", filename=filename)
        data_io.save_json(data=original, storage_location="archive",
                          filename=f"{BACKUP_PREFIX}_{filename}")
        print(f"  backed up {filename}")

    if categorised:
        sidecar = [{"item_id": k, "category": v} for k, v in sorted(categorised.items())]
        data_io.save_json(data=sidecar, storage_location="archive",
                          filename=f"{SIDECAR_PREFIX}_{stamp}.json")
        print(f"  wrote category sidecar ({len(sidecar):,} records)")

    data_io.save_json(data=payload, storage_location="scrape", filename=target)
    print(f"  rewrote {target} with {len(payload):,} entries")

    for filename in filenames:
        if filename != target:
            data_io.move(src_storage_location="scrape", dst_storage_location="archive",
                         filename=filename)
            print(f"  archived redundant source {filename}")

    # Verify through the real loader, which is what every consumer calls.
    from fyp.scrape import load_failed_scrapes

    reloaded = load_failed_scrapes()
    bad = [v for v in reloaded if "{" in str(v)]
    print(f"\nVerification via load_failed_scrapes(): {len(reloaded):,} ids, "
          f"{len(bad):,} still mangled")
    if bad or len(reloaded) != len(records):
        print("WARNING: post-repair state does not match expectations.")
        return 1

    print("Repair complete. Run a Consolidate & Refresh to rebuild "
          "enrichment_status.scrape_fail and the derived scraped_fail column.")
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
