"""One-off script: set display_collection_id to 'AIO-nnnnn' for all AIO-tagged collections."""

import json
from pathlib import Path

ANNOTATIONS_PATH = Path("/Users/<user>/fyp_local/recoded/collection_annotations.json")

with open(ANNOTATIONS_PATH) as f:
    annotations = json.load(f)

# Collect all collection IDs tagged with "AIO", sorted for deterministic numbering
aio_ids = sorted(
    cid for cid, ann in annotations.items()
    if "AIO" in ann.get("annotation_tags", [])
)

print(f"Found {len(aio_ids)} AIO-tagged collections")

# Assign AIO-00001, AIO-00002, ...
for i, cid in enumerate(aio_ids, start=1):
    new_display_id = f"AIO-{i:05d}"
    old_display_id = annotations[cid].get("display_collection_id", "")
    annotations[cid]["display_collection_id"] = new_display_id
    print(f"  {cid[:20]}...  {old_display_id!r:30s} -> {new_display_id}")

# Write back
with open(ANNOTATIONS_PATH, "w") as f:
    json.dump(annotations, f, indent=2)

print(f"\nDone. Updated {len(aio_ids)} entries in {ANNOTATIONS_PATH}")
