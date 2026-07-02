"""Read-only check: do any registry versions reference fields the current
contracts no longer define?

For each version registry (annotation / scrape / activity), diff the field set
each registered version snapshotted (``field_digest`` keys, or the annotation
schema's flattened columns via ``field_metadata``) against the current
contract's columns. A non-empty diff means a retired field exists whose
metadata should be backfilled into the registry (annotation-style
``backfill_legacy_metadata``) so it stays contract-owned.

Usage:
    PYTHONPATH=. python tests/repro_registry_field_sets.py
    # against prod: K_SERVICE=x FYP_GCS_BUCKET_NAME=<bucket> PYTHONPATH=. python ...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fyp.fyp_config as fc

fc.initialize(verbose=False)

from fyp import (  # noqa: E402
    activity_contract,
    activity_versioning,
    annotation_contract,
    annotation_versioning,
    registry_metadata as rm,
    scrape_contract,
    scrape_versioning,
)


def check(name, versioning, contract_module, digest_key="field_digest"):
    current = set(rm.snapshot_field_metadata(contract_module).keys())
    registry = versioning.load_registry()
    print(f"\n== {name} == current contract columns: {len(current)}")
    for version, entry in registry.get("versions", {}).items():
        seen = set()
        fd = entry.get(digest_key)
        if isinstance(fd, dict):
            seen |= set(fd.keys())
        fm = entry.get("field_metadata")
        if isinstance(fm, dict):
            seen |= set(fm.keys())
        retired = sorted(seen - current)
        print(f"  {version}: fields={len(seen)} retired_vs_current={retired if retired else 'NONE'}")


check("annotation", annotation_versioning, annotation_contract)
check("scrape", scrape_versioning, scrape_contract)
check("activity", activity_versioning, activity_contract)
