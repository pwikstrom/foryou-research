#!/usr/bin/env python3
"""CLI for the synthetic demo-dataset generator (S4 demo study).

The generation logic lives in ``fyp/ingest/demo_dataset.py`` (shared with the
one-click ``demo_dataset`` admin worker); this wrapper only parses arguments.

Usage:
    source .venv/bin/activate
    # Inspect / test: write all artifacts as plain files to a directory
    python scripts/generate_demo_dataset.py --emit-only tmp/demo_out
    # Install into the configured data store (local, or prod via
    # FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket>):
    python scripts/generate_demo_dataset.py --write
    # then: DM -> Ingestion -> Refresh, DM -> Refresh -> Consolidate & Refresh.
    # (The demo study definition is created by the worker path or by hand.)
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from fyp.ingest.demo_dataset import (  # noqa: E402
    DEFAULT_AS_OF,
    DEFAULT_DAYS,
    DEFAULT_DONORS,
    DEFAULT_SEED,
    emit_to_directory,
    generate,
    write_to_store,
)






def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic demo dataset.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--donors", type=int, default=DEFAULT_DONORS)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF,
                        help="Anchor date (YYYY-MM-DD); fixed default keeps output deterministic.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-only", metavar="OUTDIR",
                      help="Write artifacts as plain files to OUTDIR (no data store).")
    mode.add_argument("--write", action="store_true",
                      help="Install into the configured data store via data_io.")
    args = parser.parse_args()

    result = generate(seed=args.seed, donors=args.donors, days=args.days, as_of=args.as_of)
    if args.emit_only:
        emit_to_directory(result, args.emit_only)
    else:
        write_to_store(result)




if __name__ == "__main__":
    main()
