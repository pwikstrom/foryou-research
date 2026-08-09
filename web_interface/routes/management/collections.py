"""Collection management endpoints (/api/manage/collections*, collection annotations)."""


import pandas as pd
from flask import jsonify, request
from flask_login import login_required

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
)
from fyp.ingest import registered_raw_locations
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
)
from fyp.studies import init_study_defs

from ... import activity_log
from ...data_service import (
    invalidate_collection_tags_cache,
)
from ...process_manager import (
    start_process,
)
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


def _find_raw_file_locations(raw_files: list[str]) -> list[tuple[str, str]]:
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
            manifests[loc] = data_io.load_json(
                storage_location=loc, filename="ingestion_manifest.json", verbose=False
            ) or {}
        else:
            manifests[loc] = {}

    for fn in raw_files_set:
        for loc in upload_locations:
            if fn in manifests[loc] or data_io.exists(storage_location=loc, filename=fn):
                found.append((loc, fn))
                break

    return found




def _affected_studies_for_collection(collection_id: str) -> list[str]:
    """Return the names of studies whose SELECTED_COLLECTIONS contains collection_id."""
    init_study_defs()
    out: list[str] = []
    for sname, sdef in (fyp_cf.get('study_defs') or {}).items():
        sel = sdef.get('SELECTED_COLLECTIONS') or []
        if collection_id in sel:
            out.append(sname)
    return out




@management_bp.route('/api/manage/collections/affected_studies', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def affected_studies_for_collection():
    """Return the studies that reference a given collection_id. Used by the
    delete-collection confirmation dialog to show what will be refreshed."""
    collection_id = (request.args.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400
    return jsonify({"studies": _affected_studies_for_collection(collection_id)})




@management_bp.route('/api/manage/collections/delete', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def delete_collection():
    """Dispatch a collection_delete Cloud Task. The actual delete (which loads
    and rewrites the 1+ GB collections_recoded.parquet) runs on the task-runner
    so the data-hub doesn't risk OOM or timeout. The UI polls /api/status for
    completion and reads the final result from the task's emitted data payload.
    """
    data = request.json or {}
    collection_id = (data.get("collection_id") or "").strip()
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    from fyp.fyp_config import COLLECTION_DELETE_SCRIPT

    success, msg = start_process(
        "collection_delete",
        COLLECTION_DELETE_SCRIPT,
        task_args={"collection_id": collection_id},
        started_by=_actor(),
    )
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.delete",
            target=collection_id,
        )
        return jsonify({
            "status": "started",
            "collection_id": collection_id,
            "message": msg,
        })
    return jsonify({"status": "error", "message": msg}), 409







@management_bp.route('/api/manage/collections', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def list_collections():

    if True:#try:
        # Load ddp_metadata from storage
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            df = data_io.load_parquet(
                storage_location="recoded", 
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet", 
                verbose=False,
            )
            
            # Filter for accepted collections
            if ('other', 'accepted') in df.columns:
                df = df[df[('other', 'accepted')]]
                
            # Load annotations
            annotations = {}
            if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
                annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")
                
            # Construct structured dictionaries
            collections = []
            
            # Make sure we don't have pd.NA or similar incompatible types for JSON serialization
            df = df.where(pd.notnull(df), None)
            
            # Helper to convert pandas/pyarrow types cleanly to standard Python types
            def safe_val(val):
                if pd.isna(val) or val is None:
                    return None
                if hasattr(val, "item"):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                if hasattr(val, "isoformat"):
                    return val.isoformat()
                return val

            for index, row in df.iterrows():
                # Use collection_id column if available, otherwise fall back to index
                if 'collection_id' in df.columns:
                    row_id = str(row['collection_id'])
                else:
                    row_id = str(index)
                item = {
                    "id": row_id,
                    "participants": {},
                    "other": {},
                    "personas": {}
                }
                
                # Fetch participant info
                for c in df.columns:
                    if c[0] == 'participants':
                        item['participants'][c[1]] = safe_val(row[c])
                    elif c[0] == 'other':
                        item['other'][c[1]] = safe_val(row[c])
                    elif c[0] == 'personas':
                        item['personas'][c[1]] = safe_val(row[c])
                        
                # Attach annotations (keyed by collection ID, not row index)
                ann = annotations.get(row_id, {})
                item['displayId'] = ann.get('display_collection_id', None)
                item['tags'] = ann.get('annotation_tags', [])
                item['hidden'] = ann.get('hidden', False)

                collections.append(item)

            return jsonify(collections)
        else:
            print(f"{COLLECTIONS_LABEL}_metadata.parquet not found")
            return jsonify([])


@management_bp.route('/api/manage/collection/save_annotation', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def save_collection_annotation():
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    collection_id = data.get('collection_id')
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    try:
        annotations = {}
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
            annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")

        annotations[str(collection_id)] = {
            "display_collection_id": data.get('display_collection_id', None),
            "annotation_tags": data.get('tags', []),
            "hidden": data.get('hidden', False)
        }

        data_io.save_json(
            data=annotations,
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        )
        invalidate_collection_tags_cache()

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.annotation.save",
            target=str(collection_id),
            details={
                "tags": data.get('tags', []),
                "hidden": bool(data.get('hidden', False)),
            },
        )
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return jsonify({"error": str(e)}), 500


