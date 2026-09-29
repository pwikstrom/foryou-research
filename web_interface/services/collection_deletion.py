"""What deleting collections touches: their raw upload files and the studies that hold them.

Shared by the collection-management endpoints and the ``collection_delete``
worker.
"""

import fyp.core.data_io as data_io
from fyp.analysis.studies import init_study_defs
from fyp.core.fyp_config import fyp_cf
from fyp.ingest import registered_raw_locations


def find_raw_file_locations(raw_files: list[str]) -> list[tuple[str, str]]:
    """Return [(storage_location, filename), ...] for each raw file that still
    exists in any of the registered upload locations.

    The location list is derived from the collection-class registry
    (fyp.ingest.registered_raw_locations), so a new platform's upload location
    is probed automatically. Probes each location's ingestion_manifest.json
    first (fast path) and falls back to data_io.exists when the manifest is
    missing or out of sync. Files not found in any location are silently
    skipped — they were already moved or deleted previously.
    """
    found: list[tuple[str, str]] = []
    raw_files_set = set(raw_files)
    if not raw_files_set:
        return found

    upload_locations = registered_raw_locations()
    manifests: dict[str, dict] = {}
    for loc in upload_locations:
        if data_io.exists(storage_location=loc, filename="ingestion_manifest.json"):
            manifests[loc] = (
                data_io.load_json(
                    storage_location=loc, filename="ingestion_manifest.json", verbose=False
                )
                or {}
            )
        else:
            manifests[loc] = {}

    for fn in raw_files_set:
        for loc in upload_locations:
            if fn in manifests[loc] or data_io.exists(storage_location=loc, filename=fn):
                found.append((loc, fn))
                break

    return found


def affected_studies_for_collections(collection_ids) -> list[str]:
    """Return the names of studies whose SELECTED_COLLECTIONS contains any of
    ``collection_ids``. The union, so a bulk delete refreshes each affected
    study once rather than once per collection it happens to hold."""
    init_study_defs()
    wanted = {str(c) for c in collection_ids}
    out: list[str] = []
    for sname, sdef in (fyp_cf.get("study_defs") or {}).items():
        sel = {str(c) for c in (sdef.get("SELECTED_COLLECTIONS") or [])}
        if sel & wanted:
            out.append(sname)
    return out
