#!/usr/bin/env python3
"""Bootstrap the structure-drift baselines from already-uploaded raw files.

Learns the structure fingerprint + raw stats of every raw file currently in
each registered platform's upload location — including files the ingestion
ledger skips, since history is exactly what the baseline should describe.
Read-only over the raw files; only writes ``structure_baselines.json``.

Processed-stat distributions (kept_ratio, null_item_id_frac, ...) are NOT
bootstrapped — they accumulate organically over subsequent ingest refreshes
and stay gated by ``MIN_ACCEPTED_FOR_STAT_CHECKS`` until then.

Usage:
    source .venv/bin/activate
    python scripts/bootstrap_structure_baselines.py
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import fyp.data_io as data_io
from fyp import structure_sentinel
from fyp.ingest import get_main_collection


MANIFEST_FILENAME = "ingestion_manifest.json"




def bootstrap() -> None:
    """Fingerprint every raw file per registered sub-collection and learn it."""
    main_collection = get_main_collection(verbose=True)
    baselines = structure_sentinel.load_baselines()

    for sub in main_collection.collections:
        if not sub.raw_path:
            continue
        key = structure_sentinel.baseline_key(sub.source_platform, sub.data_source)
        baseline = baselines["baselines"].setdefault(key, structure_sentinel._empty_baseline())
        try:
            filenames = [
                fn for fn in data_io.listdir(sub.raw_path)
                if not fn.startswith(".") and fn != MANIFEST_FILENAME
            ]
        except Exception as exc:
            print(f"[{key}] cannot list '{sub.raw_path}': {exc}")
            continue

        print(f"[{key}] {len(filenames)} raw file(s) in '{sub.raw_path}'")
        for fn in filenames:
            if fn in baseline["learned_files"]:
                print(f"[{key}]   {fn}: already learned, skipping")
                continue
            try:
                fingerprint = sub.fingerprint_raw(fn)
                df = sub.load_single_raw(fn)
            except Exception as exc:
                print(f"[{key}]   {fn}: parse failed ({exc}), skipping")
                continue
            if len(df) < sub.min_required_rows_per_raw_file:
                print(f"[{key}]   {fn}: too few rows ({len(df)}), skipping")
                continue
            try:
                size_bytes = data_io.getsize(storage_location=sub.raw_path, filename=fn)
            except Exception:
                size_bytes = None
            raw_stats = structure_sentinel.compute_raw_stats(df, size_bytes)
            if fingerprint and fingerprint.get("stats"):
                raw_stats.update(fingerprint["stats"])
            structure_sentinel.learn_file(baseline, fingerprint, raw_stats, None, fn)
            print(f"[{key}]   {fn}: learned ({len(df):,} rows, "
                  f"{len(fingerprint.get('key_paths', []))} key paths)")

        print(f"[{key}] baseline now covers {baseline['n_accepted']} accepted file(s)")

    structure_sentinel.save_baselines(baselines)
    print("Saved structure_baselines.json")




if __name__ == "__main__":
    argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).parse_args()
    bootstrap()
