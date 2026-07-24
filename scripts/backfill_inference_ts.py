#!/usr/bin/env python3
"""Backfill ``inference_ts`` into refined annotation parquets and the archive.

One-off migration for timeframe-based annotation-queue selection: the refine
step historically dropped the raw output's ``inference_ts`` (epoch seconds of
the Gemini call), so refined parquets and the
``{label}_all_versions.parquet`` archive carry no annotation timestamp. The
raw JSON batch files keep the value, so this script:

1. Streams every label-prefixed raw JSON in ``machine_annotations_raw`` and
   builds per-file ``item_id -> inference_ts`` maps (plus a global map keyed
   ``(source_platform, item_id, annotation_version)`` with an item-only
   fallback, mirroring the refine step's defaulting).
2. Stamps the matching refined parquet (raw ``.json`` -> refined ``.parquet``
   in ``machine_annotations_refined``) where ``inference_ts`` is missing or
   NA. A later force-reconsolidation then reproduces the same values.
3. Stamps NA ``inference_ts`` rows in the archive directly, so the timeframe
   selector works without waiting for a reconsolidation.

Idempotent — files already fully stamped are left untouched. Malformed raw
entries are tolerated and skipped.

Usage:
    source .venv/bin/activate
    python scripts/backfill_inference_ts.py [--dry-run]

    # Against the production GCS store (run from the deployed commit):
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/backfill_inference_ts.py
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))

import fyp.data_io as data_io
import fyp.scrape_queues as scrape_queues
from fyp.annotation import annotation_versioning
from fyp.fyp_config import fyp_cf


RAW_LOCATION = "machine_annotations_raw"
REFINED_LOCATION = "machine_annotations_refined"




def load_raw_ts_map(raw_filename: str) -> tuple[dict[str, int], dict, int]:
    """Extract per-item inference timestamps from one raw annotation JSON.

    Args:
        raw_filename: The raw batch file name in ``machine_annotations_raw``.

    Returns:
        A tuple ``(by_item, by_key, malformed)``: ``by_item`` maps
        ``item_id -> inference_ts`` for this file, ``by_key`` maps
        ``(source_platform, item_id, annotation_version) -> inference_ts``
        (defaults applied exactly like the refine step), and ``malformed``
        counts entries without a usable item_id/inference_ts pair.
    """
    by_item: dict[str, int] = {}
    by_key: dict = {}
    malformed = 0
    try:
        raw = data_io.load_json(storage_location=RAW_LOCATION, filename=raw_filename)
    except Exception as e:
        print(f"  WARNING: could not read {raw_filename}: {e}")
        return by_item, by_key, 1
    if not isinstance(raw, dict):
        return by_item, by_key, 1

    default_platform = scrape_queues.default_platform()
    for entry in raw.values():
        if not isinstance(entry, dict) or entry.get("item_id") is None:
            malformed += 1
            continue
        ts = entry.get("inference_ts")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            malformed += 1
            continue
        item_id = str(entry["item_id"])
        platform = str(entry.get("source_platform") or default_platform)
        version = entry.get("annotation_version", annotation_versioning.LEGACY_VERSION)
        by_item[item_id] = ts
        by_key[(platform, item_id, str(version))] = ts
    return by_item, by_key, malformed




def stamp_refined_file(refined_filename: str, by_item: dict[str, int], dry_run: bool) -> int:
    """Fill missing/NA ``inference_ts`` values in one refined parquet.

    Args:
        refined_filename: The refined parquet file name.
        by_item: ``item_id -> inference_ts`` from the matching raw file.
        dry_run: When True, report but do not save.

    Returns:
        The number of rows stamped (0 when the file is absent or already full).
    """
    if not data_io.exists(storage_location=REFINED_LOCATION, filename=refined_filename):
        return 0
    df = data_io.load_parquet(storage_location=REFINED_LOCATION, filename=refined_filename)
    if df is None or df.empty or "item_id" not in df.columns:
        return 0

    mapped = pd.to_numeric(
        df["item_id"].astype(str).map(by_item), errors="coerce"
    ).astype("int64[pyarrow]")
    if "inference_ts" in df.columns:
        existing = pd.to_numeric(df["inference_ts"], errors="coerce").astype("int64[pyarrow]")
        fill_mask = existing.isna() & mapped.notna()
        if not fill_mask.any():
            return 0
        df["inference_ts"] = existing.where(~fill_mask, mapped)
        stamped = int(fill_mask.sum())
    else:
        df["inference_ts"] = mapped
        stamped = int(mapped.notna().sum())
        if stamped == 0:
            return 0

    if not dry_run:
        data_io.save_parquet(df=df, storage_location=REFINED_LOCATION, filename=refined_filename)
    return stamped




def stamp_archive(by_key: dict, by_item: dict[str, int], dry_run: bool) -> int:
    """Fill missing/NA ``inference_ts`` values in the all-versions archive.

    Args:
        by_key: Global ``(source_platform, item_id, annotation_version) -> ts`` map.
        by_item: Global item-only fallback map.
        dry_run: When True, report but do not save.

    Returns:
        The number of archive rows stamped.
    """
    label = fyp_cf["labels"]["MACHINE_ANNOTATIONS_LABEL"]
    archive_fn = f"{label}_all_versions.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        print(f"No archive {archive_fn} found — skipping archive stamping.")
        return 0
    df = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if df is None or df.empty:
        return 0

    keys = list(zip(
        df.get("source_platform", pd.Series("", index=df.index)).astype(str),
        df["item_id"].astype(str),
        df.get("annotation_version", pd.Series("", index=df.index)).astype(str),
    ))
    mapped = pd.Series(
        [by_key.get(k, by_item.get(k[1])) for k in keys], index=df.index
    )
    mapped = pd.to_numeric(mapped, errors="coerce").astype("int64[pyarrow]")

    if "inference_ts" in df.columns:
        existing = pd.to_numeric(df["inference_ts"], errors="coerce").astype("int64[pyarrow]")
    else:
        existing = pd.Series(pd.NA, index=df.index, dtype="int64[pyarrow]")
    fill_mask = existing.isna() & mapped.notna()
    if not fill_mask.any():
        return 0
    df["inference_ts"] = existing.where(~fill_mask, mapped)

    if not dry_run:
        data_io.save_parquet(df=df, storage_location="recoded", filename=archive_fn)
    return int(fill_mask.sum())




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without saving.")
    args = parser.parse_args()

    label = fyp_cf["labels"]["MACHINE_ANNOTATIONS_LABEL"]
    raw_files = [
        fn for fn in data_io.listdir(storage_location=RAW_LOCATION)
        if fn.startswith(label) and fn.endswith(".json")
    ]
    print(f"Found {len(raw_files)} raw annotation file(s) in {RAW_LOCATION}.")

    global_by_key: dict = {}
    global_by_item: dict[str, int] = {}
    total_refined_stamped = 0
    total_malformed = 0
    for i, raw_fn in enumerate(sorted(raw_files), start=1):
        by_item, by_key, malformed = load_raw_ts_map(raw_fn)
        total_malformed += malformed
        global_by_key.update(by_key)
        global_by_item.update(by_item)
        refined_fn = raw_fn.replace(".json", ".parquet")
        stamped = stamp_refined_file(refined_fn, by_item, args.dry_run)
        total_refined_stamped += stamped
        if stamped:
            print(f"  [{i}/{len(raw_files)}] {refined_fn}: stamped {stamped} row(s)")

    archive_stamped = stamp_archive(global_by_key, global_by_item, args.dry_run)

    mode = "DRY RUN — nothing saved" if args.dry_run else "saved"
    print(
        f"Done ({mode}): {total_refined_stamped} refined row(s) and "
        f"{archive_stamped} archive row(s) stamped; "
        f"{total_malformed} malformed/skipped raw entr(ies)."
    )
    return 0




if __name__ == "__main__":
    sys.exit(main())
