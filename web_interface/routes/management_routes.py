import json
import os
import threading
import time as _time
from datetime import UTC, datetime

import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

import fyp.data_io as data_io
import fyp.scrape_queues as scrape_queues
from fyp.platform_scraper import get_scraper
from fyp.fyp_config import (
    fyp_cf,
    load_var_schema,
)
from fyp.recode_variables import (
    SEMANTIC_COLUMNS,
    VAR_SCHEMA_ROLES,
    VAR_SCHEMA_SCALES,
    compute_var_schema_hash,
)
import fyp.annotation_versioning as annotation_versioning
from fyp.ingest import get_main_collection, parse_donor_timezone, registered_raw_locations
from fyp.machine_annotation import rebuild_active_annotations_from_archive
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    create_study_recoded_dataset,
)
from fyp.studies import init_study_defs, save_study_defs

from .. import activity_log
from ..data_service import (
    invalidate_collection_tags_cache,
    load_display_id_map,
    study_cache,
)
from ..process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
)
from ..permissions import permission_required
from ..task_status import is_cloud_run


management_bp = Blueprint('management_bp', __name__)

# Non-route helpers were extracted to web_interface/services/ (Phase 7b).
# They are imported back here — and re-exported — because management_routes is
# the stable import surface for other route modules, the run_* workers and the
# tests.
from ..services.preview_cache import (  # noqa: E402
    _PREVIEW_CACHE_TTL_S,  # noqa: F401
    _build_lock_for,  # noqa: F401
    _cache_frame_in_memory,  # noqa: F401
    _collections_hash,  # noqa: F401
    _event_window_mask,  # noqa: F401
    _get_enrichment_status_cached,  # noqa: F401
    _get_prepared_frame_cached,  # noqa: F401
    _load_collections_window,  # noqa: F401
    _load_enrichment_status_min,  # noqa: F401
    _load_prepared_from_disk,  # noqa: F401
    _load_study_raw_window,  # noqa: F401
    _prepare_preview_frame,  # noqa: F401
    _prewarm_preview_frame,  # noqa: F401
    _preview_build_locks,  # noqa: F401
    _preview_cache_lock,  # noqa: F401
    _preview_frame_cache,  # noqa: F401
    _preview_frame_filename,  # noqa: F401
    _preview_frame_key,  # noqa: F401
    _preview_sources_mtime,  # noqa: F401
    _preview_status_cache,  # noqa: F401
    _preview_warming,  # noqa: F401
    _prune_disk_frames,  # noqa: F401
    _read_cached_frame,  # noqa: F401
    _save_prepared_to_disk,  # noqa: F401
)
from ..services.stats_service import (  # noqa: E402
    LARGE_STUDY_THRESHOLD,  # noqa: F401
    SPARSE_CELL_MIN_ACTIVITIES,  # noqa: F401
    _calculate_stats,  # noqa: F401
    _compute_universe_enrichment,  # noqa: F401
    _daily_counts,  # noqa: F401
    _derive_study_issues,  # noqa: F401
    _estimate_from_prepared,  # noqa: F401
    _evaluate_consolidation_staleness,  # noqa: F401
    _filter_to_event_windows,  # noqa: F401
    _filter_to_play_observe,  # noqa: F401
    _load_collection_event_windows,  # noqa: F401
    _universe_from_prepared,  # noqa: F401
)
from ..services.worker_status import (  # noqa: E402
    PIPELINE_STEPS_ORDER,  # noqa: F401
    _actor,  # noqa: F401
    _build_pipeline_step_view,  # noqa: F401
    _cached_cookie_health,  # noqa: F401
    _is_worker_running,  # noqa: F401
    _workers_blocking_consolidate,  # noqa: F401
)


@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
@permission_required('tab.data_management.studies', 'tab.my_stuff.my_studies')
def list_studies():
    # Always reload from disk/GCS to pick up changes made by the task-runner service
    init_study_defs()

    studies = fyp_cf['study_defs']

    studies_list = []

    # Visibility:
    #   - Admin / users with the Define Studies sub-page permission are "study
    #     managers" — they see every study regardless of per-study USER_ACCESS.
    #   - Users who only have the My Studies tab see the curated subset: studies
    #     where their role appears in USER_ACCESS (or USER_ACCESS contains "all").
    from web_interface.permissions import user_has_permission
    is_manager = (
        current_user.is_admin()
        or user_has_permission(current_user, 'tab.data_management.studies')
    )

    for name, config in studies.items():
        config['STUDY_NAME'] = name

        if is_manager:
            studies_list.append(config)
        else:
            user_access = config.get("USER_ACCESS", [])
            if isinstance(user_access, list) and (
                current_user.role in user_access or 'all' in user_access
            ):
                studies_list.append(config)

    return jsonify(studies_list)







@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def save_study():
    global fyp_cf

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    
    # load studies from disk into memory - overwrite whatever was there previously
    init_study_defs()
    studies = fyp_cf['study_defs'].copy()

    # If updating an existing study, check for actual changes
    if study_name in studies:
        existing_config = studies[study_name]

        # Compare incoming data with existing config
        changed_keys = []
        for key, value in data.copy().items(): # Use copy to safely iterate
            # key might be REFRESH_PCA/REFRESH_METADATA - these shouldn't count as study def changes but separate flags.
            if key in ['REFRESH_PCA', 'REFRESH_METADATA', 'stats']:
                continue

            if key not in existing_config or existing_config[key] != value:
                changed_keys.append(key)
                #print(f"Change detected in {key}: {existing_config.get(key)} -> {value}") # Debug

        # Note: we deliberately do NOT short-circuit when changed_keys is empty.
        # The study's cached artifacts depend on collection/scrape/annotation
        # parquets that can change out-of-band (e.g. new activities ingested
        # into an existing collection_id). run_study_refresh fingerprints those
        # inputs via the sidecar and short-circuits itself when they really are
        # unchanged, so an unnecessary save here becomes a cheap no-op — and a
        # necessary one actually rebuilds.

        # If only USER_ACCESS changed, we don't need to recalculate anything
        if len(changed_keys) == 1 and changed_keys[0] == 'USER_ACCESS':
            print(f"Only USER_ACCESS changed for {study_name}. Saving without recalculation.")
            studies[study_name]['USER_ACCESS'] = data['USER_ACCESS']
            fyp_cf['study_defs'] = studies
            save_study_defs()
            activity_log.record(
                actor=_actor(),
                category=activity_log.CATEGORY_DATA_MANAGEMENT,
                action="study.save",
                target=study_name,
                details={"changed": ["USER_ACCESS"]},
            )
            return jsonify({"status": "success", "study": studies[study_name]})

    # Update config
    if study_name not in studies:
        studies[study_name] = {}

    # Preserve existing stats and last_updated — form data doesn't include these
    # and the Cloud Task will recalculate them asynchronously.
    existing_stats = studies[study_name].get('stats')
    existing_last_updated = studies[study_name].get('last_updated')

    studies[study_name].update(data)

    if existing_stats and 'stats' not in data:
        studies[study_name]['stats'] = existing_stats
    if existing_last_updated and 'last_updated' not in data:
        studies[study_name]['last_updated'] = existing_last_updated
    
    # Extract ephemeral flags (don't save to disk)
    refresh_pca_flag = data.pop('REFRESH_PCA', True)
    refresh_meta_flag = data.pop('REFRESH_METADATA', True)
    
    # Also clean them from the study object in memory just in case 'update' put them there
    # (Since 'data' was passed to update, they ARE in studies[study_name] now)
    studies[study_name].pop('REFRESH_PCA', None)
    studies[study_name].pop('REFRESH_METADATA', None)

    # Invalidate the cached daily-activities snapshot — the saved definition
    # may now have different collections, so the cache would mislead the
    # modal on reopen until a fresh /daily_activities call refreshes it.
    studies[study_name].pop('cached_daily_activities', None)

    # Update timestamp and save definition to disk
    studies[study_name]['last_updated'] = datetime.now(UTC).isoformat()
    fyp_cf['study_defs'] = studies
    save_study_defs()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="study.save",
        target=study_name,
        details={"definition_only": bool(data.get("definition_only"))},
    )

    # Definition-only save: skip heavy refresh (used by "Check data counts" for new studies)
    if data.get("definition_only"):
        return jsonify({"status": "success", "study": studies[study_name]})

    # --- Dispatch heavy refresh work ---
    task_args = {
        "study_name": study_name,
        "refresh_pca": refresh_pca_flag,
        "refresh_metadata": refresh_meta_flag,
    }

    if is_cloud_run():
        # On Cloud Run: dispatch as a Cloud Task and return immediately
        success, msg = start_process("study_refresh", None, task_args=task_args)
        if success:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatched",
                "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
            })
        else:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatch_failed",
                "message": f"Study saved, but background refresh failed to start: {msg}",
            })
    else:
        # Local dev: dispatch to a background thread so the HTTP response can
        # return immediately. The client's `_pollStudyRefresh` will watch the
        # in-process status via `/api/status/study_refresh/<name>`.
        import threading as _threading

        from web_interface.run_study_refresh import run_study_refresh
        from web_interface.task_status import LocalThreadStatusReporter

        status_key = f"study_refresh__{study_name}"
        reporter = LocalThreadStatusReporter(status_key)

        def _run_in_thread():
            try:
                run_study_refresh(reporter=reporter, task_args=task_args)
                reporter.complete()
            except Exception as e:
                print(f"Study refresh failed: {e}")
                reporter.fail(str(e))

        _threading.Thread(target=_run_in_thread, daemon=True, name=status_key).start()

        return jsonify({
            "status": "success",
            "study": studies[study_name],
            "refresh_status": "dispatched",
            "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
        })






@management_bp.route('/api/manage/studies/calculate_stats', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    global fyp_cf
    
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    # Live previews (auto-update on every slider/date tweak) pass PREVIEW_ONLY so we
    # compute-and-return without persisting: no global-config mutation (which is not
    # thread-safe under rapid concurrent calls) and no save_study_defs() write per
    # tweak. The saved stats are refreshed on an explicit Save (run_study_refresh).
    preview_only = bool(data.get("PREVIEW_ONLY"))

    # 1. Backup + install config only when we intend to persist.
    original_config = None
    if not preview_only:
        if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
            original_config = fyp_cf['study_defs'][study_name].copy()
        # If this is a new study (not in defs), we add it. If existing, we overwrite.
        fyp_cf['study_defs'][study_name] = data

    stats_to_persist: dict | None = None
    try:
        # Fast preview path: approximate the sampling counts analytically instead of
        # running the full create_study_recoded_dataset (scrape/annotation merge +
        # random sample). The persisted study build still uses _calculate_stats via
        # run_study_refresh; only this on-demand check uses the heuristic.
        #
        # A preprocessed frame (keyed by the collection set) is cached in process, so the
        # tweak-and-recheck loop (changing only the date range or sampling thresholds)
        # reuses it with no I/O and no per-row recomputation — just masks and a groupby.
        selected = data.get("SELECTED_COLLECTIONS") or []
        df_status = _get_enrichment_status_cached()
        frame = _get_prepared_frame_cached(selected, df_status)

        stats, included_per_day, sparse_cells, total_cells, sampling_report = _estimate_from_prepared(frame, data)
        stats_to_persist = stats

        # Pre-sampling potentials + universe mosaic, both derived from the same frame.
        #   items.potential     = activities in study (how many activities map to items)
        #   scraped.potential    = items in study
        #   annotated.potential  = scraped items in study
        pot_activities, pot_active_days, universe, has_total_days = _universe_from_prepared(frame, data)
        potentials = {
            "collections": len(selected),
            "activities": pot_activities,
            "active_days": pot_active_days,
            "items": int(stats.get("total_activities", 0)),
            "scraped": int(stats.get("unique_videos", 0)),
            "annotated": int(stats.get("scraped_videos", 0)),
        }

        if isinstance(stats, dict):
            stats["universe"] = universe

        issues = _derive_study_issues(stats, sparse_cells, total_cells, has_total_days, sampling_report)

        return jsonify({
            "status": "success",
            "stats": stats,
            "potentials": potentials,
            "universe": universe,
            "included_per_day": included_per_day,
            "issues": issues,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 4. Revert config + persist fresh stats (skipped entirely for live previews,
        # which never touched the global config). Persisting seeds the mosaic on reopen.
        if not preview_only:
            if original_config is not None:
                 if stats_to_persist:
                      original_config['stats'] = stats_to_persist
                 fyp_cf['study_defs'][study_name] = original_config
                 if stats_to_persist:
                      try:
                           save_study_defs()
                      except Exception as _save_err:
                           print(f"[calculate_study_stats] non-fatal: failed to persist stats for '{study_name}': {_save_err}")
            else:
                 # If it was new, remove it (it wasn't there before).
                 if study_name in fyp_cf['study_defs']:
                      del fyp_cf['study_defs'][study_name]




@management_bp.route('/api/manage/studies/prewarm_check', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def prewarm_study_check():
    """Warm the preview frame for a collection set ahead of the first 'Check' press.

    The modal calls this on open and whenever the collection selection changes, so the
    (possibly slow) build / disk-load happens during the user's think-time instead of on
    the first button press. Returns immediately; the work runs in a background thread.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    if not selected:
        return jsonify({"status": "noop"}), 200

    key = _preview_frame_key(selected)
    with _preview_cache_lock:
        warm = _preview_frame_cache.get(key)
        ready = warm is not None and (_time.monotonic() - warm[0]) < _PREVIEW_CACHE_TTL_S
        in_flight = key in _preview_warming
    if ready:
        return jsonify({"status": "ready"}), 200
    if in_flight:
        return jsonify({"status": "warming"}), 202

    threading.Thread(target=_prewarm_preview_frame, args=(list(selected),), daemon=True, name="prewarm_check").start()
    return jsonify({"status": "warming"}), 202





@management_bp.route('/api/manage/studies/daily_activities', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def daily_activities():
    """Return activities-per-day across a set of collections for the modal chart.

    Lightweight: reads only `collection_id` + `local_timestamp` columns from
    `collections_recoded.parquet` with a pushdown filter on the selected IDs.
    No date-range filter — the chart shows the full span so the user can pick
    a window visually.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    study_name = data.get("STUDY_NAME")

    if not selected:
        return jsonify({"status": "success", "total_per_day": []})

    filename = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return jsonify({"status": "success", "total_per_day": []})

    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=["collection_id", "local_timestamp", "activity_type"],
            filters=[("collection_id", "in", selected)],
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    windows = _load_collection_event_windows(selected)
    df = _filter_to_event_windows(df, windows)
    df = _filter_to_play_observe(df)

    potentials = {
        "collections": len(selected),
        "activities": 0,
        "active_days": 0,
    }
    if df is not None and not df.empty:
        potentials["activities"] = int(len(df))
        potentials["active_days"] = int(pd.to_datetime(df["local_timestamp"], errors="coerce").dropna().dt.date.nunique())

    total_per_day = _daily_counts(df)
    collections_hash = _collections_hash(selected)

    # Cache on the saved study so subsequent modal opens render the chart
    # instantly. Only persist when the incoming selection matches the saved
    # SELECTED_COLLECTIONS — otherwise the user is editing an unsaved state
    # and caching that would mislead on reopen.
    if study_name:
        saved = fyp_cf.get('study_defs', {}).get(study_name)
        if isinstance(saved, dict):
            saved_hash = _collections_hash(saved.get('SELECTED_COLLECTIONS'))
            if saved_hash == collections_hash:
                saved['cached_daily_activities'] = {
                    "total_per_day": total_per_day,
                    "potentials": potentials,
                    "collections_hash": collections_hash,
                    "computed_at": datetime.now(UTC).isoformat(),
                }
                try:
                    save_study_defs()
                except Exception as _save_err:
                    print(f"[daily_activities] non-fatal: failed to persist cache for '{study_name}': {_save_err}")

    return jsonify({
        "status": "success",
        "total_per_day": total_per_day,
        "potentials": potentials,
        "collections_hash": collections_hash,
    })




@management_bp.route('/api/manage/studies/delete', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def delete_study():
    global fyp_cf
    data = request.json
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    init_study_defs()
    if 'study_defs' not in fyp_cf:
        return jsonify({"error": "No study defs found"}), 404
        
    if study_name in fyp_cf['study_defs']:
        del fyp_cf['study_defs'][study_name]
        save_study_defs()

        for cached_file in [
            f"{study_name}_recoded.parquet",
            f"{study_name}_explorer_metadata.json",
            f"{study_name}_comp_interpretations.json",
            f"{study_name}_PCA.parquet",
        ]:
            data_io.remove(storage_location="cache", filename=cached_file)

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="study.delete",
            target=study_name,
        )
        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404




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


@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@permission_required('tab.data_management.enrichment')
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    # Reload process_stats from GCS so we pick up task-runner writes, and
    # drop any consolidation_impact that has already been fully resolved by
    # downstream refreshes — otherwise the impact panel lingers forever when
    # the UI never happens to call /api/manage/refresh/staleness.
    _evaluate_consolidation_staleness()

    # 1. Load Enrichment Status
    enrichment_status = None
    if data_io.exists(storage_location="recoded", filename='enrichment_status.parquet'):
        enrichment_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')

    total_videos = 0
    scraped_videos = 0
    annotated_videos = 0
    unique_collections = 0

    if enrichment_status is not None and not enrichment_status.empty:
        total_videos = len(enrichment_status)
        if 'scraped_ok' in enrichment_status.columns:
            scraped_videos = int(enrichment_status['scraped_ok'].sum())
        if 'annotated_ok' in enrichment_status.columns:
            annotated_videos = int(enrichment_status['annotated_ok'].sum())

    ddp_metadata = None
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
        ddp_metadata = data_io.load_parquet(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
    if ddp_metadata is not None and not ddp_metadata.empty:
        if ('other', 'accepted') in ddp_metadata.columns:
            unique_collections = int(ddp_metadata[ddp_metadata[('other','accepted')]].index.nunique())
        else:
            unique_collections = int(ddp_metadata.index.nunique())
        
    
    # 2. Get Queue Lengths (per-platform scrape queues + their total)
    scrape_queues_by_platform: dict[str, int] = {}
    annotate_queue_len = 0

    try:
        scrape_queues_by_platform = scrape_queues.queue_lengths()
    except Exception:
        pass
    scrape_queue_len = sum(scrape_queues_by_platform.values())

    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)
        
    # Backstop: resolve a forked fan-out (meta‖pca‖timelines) whose dropped leaf
    # left it un-finalized. The event-driven barrier may miss this if every
    # surviving leaf finished before the grace window; this poll-driven call
    # flips a never-started leaf to "failed" and finalizes once grace passes.
    # No-op when no fan-out is active; Cloud Run only (local mode never forks).
    if is_cloud_run():
        try:
            from .process_routes import resolve_forked_pipeline
            resolve_forked_pipeline()
        except Exception as e:
            print(f"[status] resolve_forked_pipeline failed: {e}")

    consolidate_entry = process_stats.get("consolidate_enrichment", {})

    # Is any consolidate-pipeline step currently running? Used by the UI to
    # pick up live stage progress after a page reload mid-pipeline. The
    # pipeline_in_flight flag covers the brief gap between one step completing
    # and the next step booting up (when no step is technically "running").
    pipeline_step_names = ["consolidate_enrichment"] + PIPELINE_STEPS_ORDER
    any_step_running = any(_is_worker_running(n) for n in pipeline_step_names)
    flag_in_flight = bool(consolidate_entry.get("pipeline_in_flight"))

    # Stale-flag cleanup: a server restart mid-pipeline leaves the flag set
    # with no orchestrator thread to clear it. If the flag is on but nothing
    # is running AND the consolidate step completed >60s ago (longer than
    # any plausible inter-step gap), treat the pipeline as abandoned and
    # clear the flag so the UI stops showing "in flight" forever.
    if flag_in_flight and not any_step_running:
        last_end = consolidate_entry.get("last_run_end_time")
        stale = False
        if last_end:
            try:
                end_dt = datetime.fromisoformat(last_end)
                if (datetime.now(UTC) - end_dt).total_seconds() > 60:
                    stale = True
            except (ValueError, TypeError):
                stale = True
        else:
            stale = True
        if stale:
            consolidate_entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = consolidate_entry
            save_process_stats()
            flag_in_flight = False

    pipeline_active = flag_in_flight or any_step_running

    return jsonify({
        "total_videos": total_videos,
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_collections": unique_collections,
        "scrape_queue_len": scrape_queue_len,
        "scrape_queues": scrape_queues_by_platform,
        "cookie_health": {
            p: _cached_cookie_health(p)
            for p in scrape_queues.registered_platforms()
        },
        "annotate_queue_len": annotate_queue_len,
        "consolidate_stats": {
            **consolidate_entry,
            **processes.get("consolidate_enrichment", {}).get("data", {})
        } or None,
        "consolidate_auto_armed": bool(consolidate_entry.get("auto_armed")),
        "consolidate_auto_armed_auto_refresh": bool(consolidate_entry.get("auto_armed_auto_refresh")),
        "consolidate_pipeline_active": pipeline_active,
        "pipeline_steps": _build_pipeline_step_view(pipeline_active),
        "last_pipeline_partial": bool(consolidate_entry.get("last_pipeline_partial")),
        "last_pipeline_failed_at": consolidate_entry.get("last_pipeline_failed_at"),
        "workers_blocking_consolidate": _workers_blocking_consolidate(),
        "scraper_last_success": max(
            (
                process_stats.get(f"queue_scraper_{p}", {}).get("last_success")
                or process_stats.get("queue_scraper", {}).get("last_success")
                or ""
                for p in scrape_queues_by_platform or ["tiktok"]
            ),
            default=None,
        ) or None,
        "annotator_last_success": process_stats.get("queue_annotator", {}).get("last_success"),
    })






@management_bp.route('/api/manage/enrichment/empty_queue/<queue_type>', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def empty_enrichment_queue(queue_type):
    try:
        if queue_type == "scrape":
            # Optional {"platform": ...} in the body targets one platform's
            # queue; default empties every registered platform's queue.
            body = request.get_json(silent=True) or {}
            requested = body.get("platform")
            targets = [requested] if requested else scrape_queues.registered_platforms()
            for platform in targets:
                scrape_queues.remove_scrape_queue(platform)
            load_process_stats()
            stats_changed = False
            for platform in targets:
                entry = process_stats.get(f"queue_scraper_{platform}", {})
                if "scrape_queue_len" in entry:
                    entry["scrape_queue_len"] = 0
                    stats_changed = True
            # Legacy pre-rename entry, harmless to zero alongside.
            if "scrape_queue_len" in process_stats.get("queue_scraper", {}):
                process_stats["queue_scraper"]["scrape_queue_len"] = 0
                stats_changed = True
            if stats_changed:
                save_process_stats()
        elif queue_type == "annotate":
            if data_io.exists(storage_location='cache', filename='to_annotate.json'):
                data_io.remove(storage_location='cache', filename='to_annotate.json')
            load_process_stats()
            if "annotate_queue_len" in process_stats.get("queue_annotator", {}):
                process_stats["queue_annotator"]["annotate_queue_len"] = 0
                save_process_stats()
        else:
            return jsonify({"error": "Invalid queue type"}), 400

        return jsonify({"status": "success", "message": f"{queue_type.capitalize()} queue emptied."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/queue_voted', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def queue_voted_videos():
    try:
        from web_interface.security import user_manager
        
        # 1. Gather all votes across all users
        all_votes = {} # dict of collection_id -> set of periods
        for user in user_manager.get_all_users().values():
            if not user.machine_annotation_votes:
                continue
            for coll_id, periods in user.machine_annotation_votes.items():
                if coll_id not in all_votes:
                    all_votes[coll_id] = set()
                all_votes[coll_id].update(periods)
                
        if not all_votes:
            return jsonify({"status": "no_votes", "message": "No votes found for machine annotation."})

        # 2. Map periods to item_ids 
        import pandas as pd

        from fyp.organize_datasets import create_collection_unified_dataset
        target_item_ids = set()
        
        for coll_id, periods in all_votes.items():
            try:
                # Need to load using standard DDP logic since timeline cache aggregates and removes item_id
                df_collection = create_collection_unified_dataset(collection_id=coll_id, verbose=False)
                
                if df_collection is not None and not df_collection.empty and 'item_id' in df_collection.columns and 'local_date' in df_collection.columns:
                    # Time periods can be 'YYYY-MM-DD' or 'YYYY-Wxx' or 'YYYY-MM'
                    ts_series = pd.to_datetime(df_collection['local_date'], errors='coerce')
                    
                    for p in periods:
                        # yyyy-mm-dd
                        if len(p) == 10 and p.count('-') == 2:
                            match_mask = ts_series.dt.strftime('%Y-%m-%d') == p
                        # yyyy-mm
                        elif len(p) == 7 and p.count('-') == 1:
                            match_mask = ts_series.dt.strftime('%Y-%m') == p
                        # yyyy-Wxx
                        elif 'W' in p:
                            # pandas isocalendar week
                            def format_week(dt):
                                if pd.isna(dt): return ""
                                iso = dt.isocalendar()
                                return f"{iso.year}-W{iso.week:02d}"
                            match_mask = ts_series.apply(format_week) == p
                        else:
                            continue # Unknown format
                        
                        hits = df_collection.loc[match_mask, 'item_id'].dropna().unique().tolist()
                        target_item_ids.update(hits)
                        
            except Exception as e:
                print(f"Error processing timeline for collection {coll_id}: {e}")

        if not target_item_ids:
             return jsonify({"status": "no_matches", "message": "No specific videos matched the voted time periods."})

        # 3. Check Enrichment Status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
             df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        default_platform = scrape_queues.default_platform()
        new_scrape = []
        new_annotate = []
        item_platform: dict[str, str] = {}

        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                     df_status = df_status.rename(columns={'index': 'item_id'})

            # Convert status ids to set for fast lookup
            status_records = df_status.set_index('item_id').to_dict('index')

            for item in target_item_ids:
                if item in status_records:
                    rec = status_records[item]
                    is_scraped = rec.get('scraped_ok', False)
                    is_annotated = rec.get('annotated_ok', False)
                    scrape_fail = rec.get('scrape_fail', False)
                    annotated_fail = rec.get('annotated_fail', False)
                    has_media = rec.get('video_downloaded', False)
                    plat = rec.get('source_platform')
                    item_platform[item] = plat if isinstance(plat, str) and plat else default_platform

                    # Annotation needs an mp4: metadata-only items (e.g.
                    # YouTube long-form past the media duration cap) are not
                    # annotatable and stay out of the queue.
                    if not is_scraped and not scrape_fail:
                        new_scrape.append(item)
                    elif is_scraped and has_media and not is_annotated and not annotated_fail:
                        new_annotate.append(item)
                else:
                    # Item not in enrichment status -> hasn't been scraped yet
                    new_scrape.append(item)
        else:
            # No enrichment file -> everything needs scraping
            new_scrape = list(target_item_ids)

        new_scrape = list(set(new_scrape))
        new_annotate = list(set(new_annotate))

        # 4. Append to Queues (scrape queues are per-platform). Platforms
        # without a scrape-contract block have no worker to drain a queue, so
        # their items are skipped instead of stranded in an orphan file.
        added_to_scrape: dict[str, int] = {}
        if new_scrape:
            scrapeable = set(scrape_queues.registered_platforms())
            by_platform: dict[str, list[str]] = {}
            for item in new_scrape:
                by_platform.setdefault(item_platform.get(item, default_platform), []).append(item)
            for platform, items in by_platform.items():
                if platform not in scrapeable:
                    print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                    continue
                scrape_queues.append_to_scrape_queue(platform, items)
                added_to_scrape[platform] = len(items)

        def load_queue(fname):
            if data_io.exists(storage_location="cache", filename=fname):
                try:
                    q = data_io.load_json(storage_location="cache", filename=fname)
                    if isinstance(q, list): return q
                except Exception:
                     pass
            return []

        def save_queue(fname, q):
            # deduplicate and save
            q_clean = list(set(q))
            data_io.save_json(data=q_clean, storage_location="cache", filename=fname)

        if new_annotate:
             current_annotate = load_queue("to_annotate.json")
             current_annotate.extend(new_annotate)
             save_queue("to_annotate.json", current_annotate)

        return jsonify({
            "status": "success",
            "added_to_scrape": len(new_scrape),
            "added_to_scrape_by_platform": added_to_scrape,
            "added_to_annotate": len(new_annotate)
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_scrape():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    retry_missing_media = bool(data.get("retry_missing_media", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None

        if data_io.exists(storage_location="cache", filename=recoded_fn):
            # Load only the required column if possible, but load_parquet loads all if columns not provided properly or we can just load the whole file.
            # Actually, calculate_to_scrape only really needs item_id. The full load is fine as the files are usually small enough, but let's just load it.
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)

        if df_study is None or df_study.empty:
            # If not cached or empty, generate from scratch
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)

        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unscraped_videos = []
        if df_status is not None and not df_status.empty:
            # item_id is usually the index in enrichment_status
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                # If index was unnamed, it might become 'index'
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            # Map enrichment_status to our study videos
            study_videos = df_study[['item_id']].copy()
            study_status = study_videos.merge(df_status, on='item_id', how='left')

            # Find videos where scraped_ok is fundamentally False or NaN AND scrape_fail is fundamentally False or NaN
            not_scraped = pd.isna(study_status['scraped_ok']) | (study_status['scraped_ok'] == False)

            # When retry_failed is set, include items that previously failed
            # by dropping the scrape_fail filter — the user is asking us to
            # re-attempt them regardless of past outcome.
            if retry_failed:
                unscraped_mask = not_scraped
            elif 'scrape_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scrape_fail']) | (study_status['scrape_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            elif 'scraped_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scraped_fail']) | (study_status['scraped_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            else:
                unscraped_mask = not_scraped

            unscraped_videos = study_status.loc[unscraped_mask, 'item_id'].dropna().tolist()
        else:
            unscraped_videos = df_study['item_id'].dropna().tolist()

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unscraped_videos = list({str(v) for v in unscraped_videos})

        # Media-gap backfill: items scraped OK but whose media never landed
        # (e.g. a rate-limited media phase saved metadata-only) can't be found
        # via scraped_ok — pick them straight from the study frame. Items over
        # the platform's media duration cap are metadata-only by design and
        # excluded; unknown durations pass (the media phase decides).
        if retry_missing_media and {'scraped_ok', 'video_downloaded', 'item_id'} <= set(df_study.columns):
            per_item = df_study.drop_duplicates(subset=['item_id'])
            gap_mask = (
                (per_item['scraped_ok'].fillna(False) == True)
                & ~(per_item['video_downloaded'].fillna(False) == True)
            )
            gap = per_item[gap_mask]
            gap_platforms = (
                gap['source_platform'].fillna(scrape_queues.default_platform())
                if 'source_platform' in gap.columns
                else pd.Series(scrape_queues.default_platform(), index=gap.index)
            )
            media_gap_videos: set[str] = set()
            for gap_platform, grp in gap.groupby(gap_platforms):
                try:
                    cap = get_scraper(str(gap_platform)).media_duration_cap()
                except Exception:
                    continue  # no scraper registered for this platform
                if 'duration' in grp.columns:
                    dur = pd.to_numeric(grp['duration'], errors='coerce')
                    grp = grp[dur.isna() | (dur <= cap)]
                media_gap_videos |= {str(v) for v in grp['item_id'].dropna()}
            if media_gap_videos:
                print(f"Retry-missing-media: adding {len(media_gap_videos)} scraped-ok "
                      f"items without media to the queue(s).")
            unscraped_videos = list(set(unscraped_videos) | media_gap_videos)

        # Append to the per-platform scrape queues. The study frame carries
        # source_platform per event row; an item never spans platforms.
        default_platform = scrape_queues.default_platform()
        item_platform: dict[str, str] = {}
        if 'source_platform' in df_study.columns:
            plat_map = (
                df_study[['item_id', 'source_platform']]
                .dropna(subset=['item_id'])
                .drop_duplicates(subset=['item_id'])
            )
            item_platform = {
                str(i): (str(p) if isinstance(p, str) and p else default_platform)
                for i, p in zip(plat_map['item_id'], plat_map['source_platform'])
            }

        by_platform: dict[str, list[str]] = {}
        for vid in unscraped_videos:
            by_platform.setdefault(item_platform.get(vid, default_platform), []).append(vid)

        # Platforms without a scrape-contract block have no worker to drain a
        # queue, so their items are skipped instead of stranded in an orphan file.
        scrapeable = set(scrape_queues.registered_platforms())
        queue_len_by_platform: dict[str, int] = {}
        skipped_by_platform: dict[str, int] = {}
        for platform, items in by_platform.items():
            if platform not in scrapeable:
                skipped_by_platform[platform] = len(items)
                print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                continue
            queue_len_by_platform[platform] = scrape_queues.append_to_scrape_queue(platform, items)

        return jsonify({
            "status": "success",
            "videos_to_scrape": sum(queue_len_by_platform.values()),
            "videos_to_scrape_by_platform": queue_len_by_platform,
            "skipped_unscrapeable_by_platform": skipped_by_platform,
        })

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_annotate():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        from fyp.fyp_config import fyp_cf

        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None
        
        if data_io.exists(storage_location="cache", filename=recoded_fn):
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)
            
        if df_study is None or df_study.empty:
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
            
        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unannotated_videos = []
        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            if 'duration' in df_study.columns:
                study_videos = df_study[['item_id', 'duration']].copy()
            else:
                study_videos = df_study[['item_id']].copy()
                
            study_status = study_videos.merge(df_status, on='item_id', how='left')
            
            is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
            
            if 'annotated_ok' in study_status.columns:
                not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)
            else:
                not_annotated_ok = True

            # When retry_failed is set, ignore the annotated_fail column so
            # items that previously failed annotation are re-queued.
            if retry_failed:
                not_annotated_fail = True
            elif 'annotated_fail' in study_status.columns:
                not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
            else:
                not_annotated_fail = True

            unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail

            # Annotation needs an mp4: metadata-only items (e.g. YouTube
            # long-form past the media duration cap) are not annotatable.
            if 'video_downloaded' in study_status.columns:
                unannotated_mask = unannotated_mask & (study_status['video_downloaded'].fillna(False) == True)

            if 'duration' in study_status.columns:
                max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
                duration_ok = (study_status['duration'] < max_dur) | pd.isna(study_status['duration'])
                unannotated_mask = unannotated_mask & duration_ok

            unannotated_videos = study_status.loc[unannotated_mask, 'item_id'].dropna().tolist()
        else:
            unannotated_videos = []

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unannotated_videos = list({str(v) for v in unannotated_videos})

        # Append target payload to global annotate queue
        current_queue = []
        if data_io.exists(storage_location="cache", filename="to_annotate.json"):
            try:
                q = data_io.load_json(storage_location="cache", filename="to_annotate.json")
                if isinstance(q, list): current_queue = q
            except Exception:
                pass
                
        current_queue.extend(unannotated_videos)
        current_queue = list(set(current_queue))

        data_io.save_json(
            data=current_queue,
            storage_location="cache",
            filename="to_annotate.json"
        )

        return jsonify({
            "status": "success",
            "videos_to_annotate": len(current_queue),
        })

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_enrichment():
    from fyp.fyp_config import CONSOLIDATE_ENRICHMENT_SCRIPT

    if _is_worker_running("consolidate_enrichment"):
        return jsonify({"status": "error", "message": "Consolidation already running"}), 409

    data = request.json or {}
    force = bool(data.get("force"))
    # auto_refresh defaults to True — the button means "consolidate + fix the
    # consolidation impact automatically". Force Reconsolidate skips the
    # downstream chain by default to keep it debuggable.
    auto_refresh = bool(data.get("auto_refresh", not force))

    blocking = _workers_blocking_consolidate()
    if blocking:
        if force:
            return jsonify({
                "status": "error",
                "message": f"Cannot force reconsolidate while {', '.join(blocking)} running.",
            }), 409

        # Arm instead of firing — pipeline kicks off when workers go idle.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry["auto_armed"] = True
        entry["auto_armed_force"] = False
        entry["auto_armed_auto_refresh"] = auto_refresh
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
        return jsonify({
            "status": "armed",
            "message": f"Waiting for {', '.join(blocking)} to finish.",
            "blocking": blocking,
        })

    task_args: dict = {}
    if force:
        task_args["force_consolidation"] = True
    if auto_refresh:
        task_args["auto_refresh"] = True

    # Firing now — clear any stale armed flag and seed a pipeline-plan marker so
    # the step list shows the live "Consolidate enrichment data" step from the
    # very first poll (steps=[] until the worker computes the real downstream
    # plan; _build_pipeline_step_view renders a present-but-empty plan). Without
    # this the list only appears after consolidation finishes and the user sees
    # only a text line during the (long) consolidation phase.
    now_iso = datetime.now(UTC).isoformat()
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    entry["pipeline_plan"] = {
        "steps": [],
        "started_ts": now_iso,
        "mode": "refresh" if auto_refresh else "consolidate_only",
    }
    entry["last_pipeline_partial"] = False
    entry["last_pipeline_failed_at"] = None
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    success, msg = start_process("consolidate_enrichment", CONSOLIDATE_ENRICHMENT_SCRIPT,
                                 task_args=task_args if task_args else None)
    if success:
        # start_process resets the in-memory ::DATA:: copy; mirror the marker
        # there too so the local-dev overlay in _build_pipeline_step_view agrees
        # with process_stats (no-op on Cloud Run, where there is no subprocess).
        mem = processes.get("consolidate_enrichment", {}).get("data")
        if isinstance(mem, dict):
            mem["pipeline_plan"] = entry["pipeline_plan"]
        return jsonify({"status": "started", "message": msg})
    else:
        # Dispatch failed — don't leave a phantom plan marker behind.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry.pop("pipeline_plan", None)
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
        return jsonify({"status": "error", "message": msg}), 409


@management_bp.route('/api/manage/enrichment/consolidate/disarm', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_disarm():
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    was_armed = bool(entry.get("auto_armed"))
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()
    return jsonify({"status": "disarmed", "was_armed": was_armed})


@management_bp.route('/api/manage/enrichment/refresh-downstream', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_refresh_downstream():
    """Re-run the downstream refresh pipeline for the stored consolidation impact.

    Powers the "Refresh All Affected" button. It runs the SAME pipeline as the
    consolidate auto-refresh — embeddings → video_map → recode → {meta ‖ pca ‖
    timelines} — against the impact recorded by a prior Consolidate Only run, so
    the niche steps the old per-button cascade skipped are now included and in
    the right order. Writes ``pipeline_plan`` so the step list renders, then
    dispatches via the Cloud Tasks chain (Cloud Run) or the local sequential
    orchestrator (dev).
    """
    from web_interface.run_consolidate_enrichment import (
        _build_downstream_pipeline,
        build_pipeline_chain,
    )

    load_process_stats()
    ps_entry = process_stats.get("consolidate_enrichment", {})
    mem = processes.get("consolidate_enrichment", {}).get("data", {}) or {}
    impact = ps_entry.get("consolidation_impact") or mem.get("consolidation_impact")
    if not impact:
        return jsonify({"status": "noop", "message": "No consolidation impact to refresh."})

    # Don't start on top of a running pipeline.
    if ps_entry.get("pipeline_in_flight") or any(
        _is_worker_running(n) for n in (["consolidate_enrichment"] + PIPELINE_STEPS_ORDER)
    ):
        return jsonify({"status": "error", "message": "A refresh pipeline is already running."}), 409

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        return jsonify({"status": "noop", "message": "Nothing to refresh."})

    now_iso = datetime.now(UTC).isoformat()
    ps_entry["pipeline_plan"] = {"steps": [p["task"] for p in pipeline], "started_ts": now_iso}
    ps_entry["last_pipeline_partial"] = False
    ps_entry["last_pipeline_failed_at"] = None
    ps_entry["last_pipeline_summary"] = "Pipeline in progress — refreshing caches..."
    ps_entry["last_pipeline_summary_ts"] = now_iso
    ps_entry["pipeline_in_flight"] = True
    process_stats["consolidate_enrichment"] = ps_entry
    save_process_stats()

    # In local dev the consolidate worker's last ::DATA:: emission lingers in
    # processes["consolidate_enrichment"]["data"] and the stats / step-view
    # endpoints overlay it on top of process_stats. After a "Consolidate Only"
    # run that emission carries pipeline_plan=None, which would shadow the fresh
    # plan just written and hide the step list. Mirror the new plan into the
    # in-memory copy so both stores agree (no-op on Cloud Run, where there is no
    # in-process consolidate subprocess).
    mem = processes.get("consolidate_enrichment", {}).get("data")
    if isinstance(mem, dict):
        mem["pipeline_plan"] = ps_entry["pipeline_plan"]
        mem["last_pipeline_partial"] = False
        mem["last_pipeline_failed_at"] = None

    if is_cloud_run():
        from ..process_manager import _dispatch_cloud_task
        chain = build_pipeline_chain(pipeline)
        success, msg = _dispatch_cloud_task(chain["next_task"], chain["next_task_args"])
        if not success:
            # Roll back the in-flight flag so the UI doesn't hang.
            load_process_stats()
            entry = process_stats.get("consolidate_enrichment", {})
            entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = entry
            save_process_stats()
            return jsonify({"status": "error", "message": f"Dispatch failed: {msg}"}), 409
    else:
        import threading

        from ..process_manager import _run_local_downstream_pipeline
        threading.Thread(
            target=_run_local_downstream_pipeline, args=(impact,), daemon=True
        ).start()

    return jsonify({"status": "started", "message": "Downstream refresh started."})



@management_bp.route('/api/manage/refresh/staleness', methods=['GET'])
@permission_required('tab.data_management.refresh')
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact."""
    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
    })


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert the schema DataFrame to a list of plain-dict records,
    coercing nulls to empty strings so the JSON payload is stable shape.
    """
    out: list[dict] = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            val = row[col]
            try:
                if pd.isna(val):
                    rec[col] = ""
                    continue
            except (TypeError, ValueError):
                pass
            rec[col] = "" if val is None else str(val)
        out.append(rec)
    return out



def _var_schema_admin_enabled() -> bool:
    """Off-switch for the schema admin UI.

    Defaults to True; set ``[features].var_schema_admin = false`` in
    ``config.toml`` to disable without redeploying.  Permission gate
    (``tab.admin.schema``) is still required on top of this.
    """
    features = fyp_cf.get("features") or {}
    return bool(features.get("var_schema_admin", True))



@management_bp.route('/api/manage/annotation-versions', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def list_annotation_versions():
    """List recorded annotation versions and the active one."""
    try:
        return jsonify({
            "versions": annotation_versioning.list_versions(),
            "active": annotation_versioning.get_active_version(),
            "current": annotation_versioning.current_annotation_version(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/<version>', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_annotation_version(version):
    """Return one version's full record, including its prompt + schema snapshot."""
    try:
        registry = annotation_versioning.load_registry()
        info = registry.get("versions", {}).get(version)
        if info is None:
            return jsonify({"error": "unknown version"}), 404
        # The legacy version predates per-version prompt snapshots; surface the
        # historical file-based prompt so "View" isn't empty for it.
        if version == annotation_versioning.LEGACY_VERSION and not info.get("prompt_text"):
            legacy_prompt = annotation_versioning.legacy_prompt_text()
            if legacy_prompt:
                info = {**info, "prompt_text": legacy_prompt}
        return jsonify({
            "version": version,
            "active": registry.get("active") == version,
            "record": info,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/activate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def activate_annotation_version():
    """Activate a version and rebuild the global active dataset.

    Updates the registry, re-derives ``machine_annotations_recoded.parquet`` from
    the version archive (fast — no re-refinement), and clears the study RAM
    cache. Per-study datasets still need a study refresh to fully reflect the
    activation.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")
        if not version:
            return jsonify({"error": "version is required"}), 400
        try:
            annotation_versioning.promote_version(version)
        except KeyError:
            return jsonify({"error": f"unknown version: {version}"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        rebuilt = rebuild_active_annotations_from_archive(verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_version.activate",
            details={"version": version, "active_rows": rebuilt},
        )
        return jsonify({
            "ok": True,
            "active": version,
            "active_rows": rebuilt,
            "note": "Global active annotations rebuilt. Refresh studies to apply to per-study datasets.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _annotation_contract_impact(cand_contract: dict) -> dict:
    """Predict the version impact of activating ``cand_contract``.

    Renders the candidate prompt + response schema exactly the way the annotator
    would and compares the resulting ``av_`` descriptor to the current one, so
    the admin sees "metadata-only — no new version" vs "a new version will be
    minted" before confirming. Also reports the field-name delta.
    """
    from fyp import annotation_contract as ac
    from fyp import annotation_schema as sch

    machine = fyp_cf["machine"]
    model = machine.get("model")
    gen_params = {k: machine.get(k) for k in annotation_versioning._VERSION_GEN_PARAM_KEYS}

    cur = annotation_versioning.current_version_descriptor(fresh=True)
    cand_prompt = sch.build_prompt(cand_contract)
    cand_schema = sch.get_annotation_json_schema(cand_contract)
    cand = annotation_versioning.build_version_descriptor(model, cand_prompt, cand_schema, gen_params)

    cur_names = {f.get("name") for f in ac.load_contract().get("fields", [])}
    cand_names = {f.get("name") for f in cand_contract.get("fields", [])}
    version_changed = cand["annotation_version"] != cur.get("annotation_version")
    return {
        "current_version": cur.get("annotation_version"),
        "candidate_version": cand["annotation_version"],
        "prompt_changed": cand["prompt_hash"] != cur.get("prompt_hash"),
        "schema_changed": cand["schema_hash"] != cur.get("schema_hash"),
        "version_changed": version_changed,
        "metadata_only": not version_changed,
        "fields_added": sorted(n for n in (cand_names - cur_names) if n),
        "fields_removed": sorted(n for n in (cur_names - cand_names) if n),
        "use_generated_prompt": True,
        "use_structured_output": True,
    }




@management_bp.route('/api/manage/annotation-contract', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_annotation_contract():
    """Return the effective-contract status for the admin card."""
    try:
        from fyp import annotation_contract as ac

        status = ac.contract_status()
        return jsonify({
            **status,
            "current_version": annotation_versioning.current_annotation_version(),
            "runtime_filename": ac.RUNTIME_FILENAME,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/download', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def download_annotation_contract():
    """Download the effective contract (runtime file if present, else baked)."""
    try:
        from flask import Response
        from fyp import annotation_contract as ac

        text = ac.effective_contract_text()
        return Response(
            text,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{ac.RUNTIME_FILENAME}"'},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/parsed', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_annotation_contract_parsed():
    """Return the effective contract as a parsed dict, for form-editor hydration.

    The dict is exactly the parsed-TOML shape the pipeline consumes, so the
    editor's model can never diverge from what ``build_prompt`` /
    ``build_response_schema`` see. ``help`` carries the editor's per-input help
    texts (``config/annotation_contract_help.toml``).
    """
    try:
        from fyp import annotation_contract as ac

        text = ac.effective_contract_text()
        contract, errors = ac.parse_and_validate(text)
        if contract is None:
            return jsonify({"error": "effective contract does not parse", "errors": errors}), 500
        status = ac.contract_status()
        try:
            from fyp.recode_variables import VAR_SCHEMA_ROLES, VAR_SCHEMA_SCALES
            roles, scales = list(VAR_SCHEMA_ROLES), list(VAR_SCHEMA_SCALES)
        except Exception:
            roles, scales = [], []
        return jsonify({
            "contract": contract,
            "etag": status.get("etag"),
            "source": status.get("source"),
            "errors": errors,
            "help": ac.contract_help(),
            "roles": roles,
            "scales": scales,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/preview', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def preview_annotation_contract():
    """Render a candidate contract's prompt + response schema, without side effects.

    Body: ``{"contract": {...}}`` (the parsed-dict shape). Returns
    ``{valid, prompt, schema}`` on success or ``{valid: False, errors}`` when
    the candidate fails validation — always HTTP 200, so the editor's
    debounced live preview can show errors inline without console noise.
    Never touches the live snapshot (explicit-contract rendering seam).
    """
    try:
        from fyp import annotation_contract as ac
        from fyp import annotation_schema as sch

        body = request.get_json(silent=True) or {}
        cand = body.get('contract')
        if not isinstance(cand, dict):
            return jsonify({"error": "body must include a 'contract' object"}), 400
        errors = ac.validate_contract(cand)
        if errors:
            return jsonify({"valid": False, "errors": errors})
        return jsonify({
            "valid": True,
            "prompt": sch.build_prompt(cand),
            "schema": sch.get_annotation_json_schema(cand),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def upload_annotation_contract():
    """Validate + (optionally confirm) an uploaded annotation contract.

    Two-step: without ``confirm`` this validates the TOML and returns a
    version-impact report (dry run); with ``confirm`` it etag-guards, backs up
    the previous runtime contract, persists the new one, refreshes the snapshot,
    and rebuilds the in-memory schema. The candidate arrives as a multipart
    ``file``, a ``text`` form/JSON field, or a JSON ``contract`` dict (the form
    editor) — the latter is serialized to TOML server-side against the current
    effective text so comments on untouched keys survive, then flows through
    the exact same validate → impact → confirm pipeline.
    """
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import annotation_contract as ac

        json_body = request.get_json(silent=True) or {}

        # 1. Candidate TOML text — multipart file wins, else a raw text field,
        #    else a parsed-dict 'contract' payload serialized server-side.
        text = None
        original_filename = None
        files = [f for f in (request.files.getlist('file') + request.files.getlist('files')) if f and f.filename]
        if files:
            original_filename = secure_filename(files[0].filename)
            try:
                text = files[0].read().decode('utf-8')
            except UnicodeDecodeError:
                return jsonify({"error": "file is not valid UTF-8 text"}), 400
        else:
            text = request.form.get('text') or json_body.get('text')
            if not text and isinstance(json_body.get('contract'), dict):
                try:
                    text = ac.serialize_contract(
                        json_body['contract'], base_text=ac.effective_contract_text()
                    )
                except ValueError as e:
                    return jsonify({"valid": False, "errors": [str(e)]}), 400
                original_filename = "(form editor)"
        if not text or not text.strip():
            return jsonify({"error": "no contract text provided"}), 400

        # 2. Validate before doing anything else.
        cand, errors = ac.parse_and_validate(text)
        if errors:
            return jsonify({"valid": False, "errors": errors}), 400

        # 3. Version-impact dry-run report.
        impact = _annotation_contract_impact(cand)

        def _flag(v) -> bool:
            return str(v).strip().lower() in ('1', 'true', 'yes')

        confirm = _flag(request.form.get('confirm', '')) or bool(json_body.get('confirm'))
        if not confirm:
            return jsonify({"valid": True, "confirm_required": True, "impact": impact})

        # 4. Confirm: etag guard against a concurrent change.
        expected_etag = request.form.get('expected_etag') or json_body.get('expected_etag')
        current_etag = ac.contract_status().get("etag")
        if expected_etag and current_etag and expected_etag != current_etag:
            return jsonify({
                "error": "conflict",
                "message": "The contract changed since you loaded it. Reload and retry.",
                "etag": current_etag,
            }), 409

        # 5. Back up the existing runtime contract (if any) before overwriting.
        backup_name = None
        if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
            prev = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
            if prev is not None:
                ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup_name = f"{ac.BACKUP_PREFIX}{ts}.toml"
                data_io.save_text(prev, storage_location=ac.RUNTIME_LOCATION, filename=backup_name)

        # 6. Persist the new contract + audit metadata.
        data_io.save_text(text, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        data_io.save_json(
            data={
                "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "updated_by": _actor(),
                "original_filename": original_filename,
            },
            storage_location=ac.RUNTIME_LOCATION,
            filename=ac.RUNTIME_META_FILENAME,
        )

        # 7. Refresh the snapshot + rebuild the schema so overlays pick up new
        #    metadata; clear the study RAM cache (recode/metadata may change).
        ac.refresh_runtime_contract()
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_contract.upload",
            details={
                "impact": impact,
                "backup": backup_name,
                "original_filename": original_filename,
            },
        )
        new_status = ac.contract_status()
        return jsonify({
            "ok": True,
            "source": new_status.get("source"),
            "etag": new_status.get("etag"),
            "impact": impact,
            "backup": backup_name,
            "note": (
                "Contract activated. A new annotation version will be minted on the "
                "next annotation run if the prompt/schema changed; activate it when ready."
                if impact.get("version_changed")
                else "Contract activated (metadata-only change — no new annotation version)."
            ),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/annotation-contract/revert', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def revert_annotation_contract():
    """Revert to the baked contract by archiving + removing the runtime file."""
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import annotation_contract as ac

        if not data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
            return jsonify({"ok": True, "source": "baked", "note": "Already on the baked contract."})

        backup_name = None
        prev = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        if prev is not None:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_name = f"{ac.BACKUP_PREFIX}{ts}.toml"
            data_io.save_text(prev, storage_location=ac.RUNTIME_LOCATION, filename=backup_name)

        data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
        if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME):
            data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)

        ac.refresh_runtime_contract()
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_contract.revert",
            details={"backup": backup_name},
        )
        return jsonify({
            "ok": True,
            "source": ac.contract_status().get("source"),
            "backup": backup_name,
            "note": "Reverted to the baked contract.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ---------------------------------------------------------------------------
# A/B contract evaluation (candidates, eval set, runs). See fyp/ab_eval.py.
# All results live in the isolated 'ab_eval' storage location — never in the
# machine-annotation archive or studies.
# ---------------------------------------------------------------------------


@management_bp.route('/api/manage/ab-candidates', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def list_ab_candidates():
    """List stored candidate contracts (metadata only, newest first)."""
    try:
        from fyp import ab_eval

        return jsonify({"candidates": ab_eval.list_candidates()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_ab_candidate():
    """Create/overwrite a named candidate contract.

    Body: ``{name, text | contract, note?, overwrite?}`` — ``text`` is raw
    TOML; a ``contract`` dict is serialized server-side against the current
    effective text (the form editor's save-as-candidate path). The candidate
    is validated and stamped with its etag + predicted ``av_`` version.
    """
    try:
        from fyp import ab_eval
        from fyp import annotation_contract as ac

        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or "").strip()
        text = body.get('text')
        if not text and isinstance(body.get('contract'), dict):
            try:
                text = ac.serialize_contract(body['contract'], base_text=ac.effective_contract_text())
            except ValueError as e:
                return jsonify({"valid": False, "errors": [str(e)]}), 400
        if not text or not str(text).strip():
            return jsonify({"error": "no contract text provided"}), 400

        cand, errors = ac.parse_and_validate(text)
        if errors:
            return jsonify({"valid": False, "errors": errors}), 400

        candidate_version = _annotation_contract_impact(cand).get("candidate_version")
        try:
            meta = ab_eval.save_candidate(
                name, text, actor=_actor(), note=str(body.get('note') or ""),
                overwrite=bool(body.get('overwrite')), candidate_version=candidate_version,
            )
        except FileExistsError:
            return jsonify({"error": f"candidate '{name}' exists — pass overwrite=true"}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        activity_log.record(actor=_actor(), category="admin",
                            action="ab_candidate.save", details={"name": name})
        return jsonify({"ok": True, "meta": meta})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_ab_candidate(name):
    """Return one candidate's text + parsed contract + metadata."""
    try:
        from fyp import ab_eval

        try:
            return jsonify(ab_eval.load_candidate(name))
        except FileNotFoundError:
            return jsonify({"error": f"candidate '{name}' not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>', methods=['DELETE'])
@permission_required('tab.admin.schema')
@login_required
def delete_ab_candidate(name):
    """Delete a candidate contract."""
    try:
        from fyp import ab_eval

        removed = ab_eval.delete_candidate(name)
        if removed:
            activity_log.record(actor=_actor(), category="admin",
                                action="ab_candidate.delete", details={"name": name})
        return jsonify({"ok": removed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-candidates/<name>/activate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def activate_ab_candidate(name):
    """Dry-run a candidate for activation (the graduation path).

    Returns the candidate's TOML text + the standard version-impact report;
    the UI then drives the NORMAL contract-confirm POST with that text, so
    graduation is exactly the upload flow (etag guard, backup, versioning).
    """
    try:
        from fyp import ab_eval
        from fyp import annotation_contract as ac

        try:
            cand = ab_eval.load_candidate(name)
        except FileNotFoundError:
            return jsonify({"error": f"candidate '{name}' not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 422

        impact = _annotation_contract_impact(cand["contract"])
        return jsonify({
            "name": name,
            "text": cand["text"],
            "impact": impact,
            "current_etag": ac.contract_status().get("etag"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def list_ab_eval_sets():
    """Return every named evaluation set plus the active one."""
    try:
        from fyp import ab_eval

        return jsonify(ab_eval.list_eval_sets())
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def create_ab_eval_set():
    """Create a new (optionally cloned) evaluation set. Body: ``{name, copy_from?}``."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        name = str(body.get('name') or "").strip()
        try:
            record = ab_eval.create_eval_set(
                name, copy_from=body.get('copy_from') or None, actor=_actor())
        except FileExistsError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin",
                            action="ab_eval_set.create", details={"name": name})
        return jsonify({"ok": True, **record})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>/rename', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def rename_ab_eval_set(name):
    """Rename an evaluation set. Body: ``{new_name}``."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        new_name = str(body.get('new_name') or "").strip()
        try:
            record = ab_eval.rename_eval_set(name, new_name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except FileExistsError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin", action="ab_eval_set.rename",
                            details={"name": name, "new_name": new_name})
        return jsonify({"ok": True, **record})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>/activate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def activate_ab_eval_set(name):
    """Make ``name`` the active evaluation set (the one a run uses)."""
    try:
        from fyp import ab_eval

        try:
            ab_eval.set_active_eval_set(name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        stored = ab_eval.load_eval_set()
        return jsonify({
            **stored,
            "resolved": ab_eval.resolve_items(stored.get("item_ids", [])),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-sets/<name>', methods=['DELETE'])
@permission_required('tab.admin.schema')
@login_required
def delete_ab_eval_set(name):
    """Delete an evaluation set (never the last remaining one)."""
    try:
        from fyp import ab_eval

        try:
            result = ab_eval.delete_eval_set(name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        activity_log.record(actor=_actor(), category="admin",
                            action="ab_eval_set.delete", details={"name": name})
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_ab_eval_set():
    """Return one eval set (``?name=`` or the active one) with per-item flags."""
    try:
        from fyp import ab_eval

        stored = ab_eval.load_eval_set(request.args.get('name') or None)
        return jsonify({
            **stored,
            "resolved": ab_eval.resolve_items(stored.get("item_ids", [])),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_ab_eval_set():
    """Persist one eval set's items. Body: ``{item_ids, name?, note?}``. Capped."""
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        item_ids = body.get('item_ids')
        if not isinstance(item_ids, list):
            return jsonify({"error": "body must include an 'item_ids' list"}), 400
        try:
            stored = ab_eval.save_eval_set(item_ids, actor=_actor(),
                                           note=str(body.get('note') or ""),
                                           name=body.get('name') or None)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        resolved = ab_eval.resolve_items(stored["item_ids"])
        not_downloaded = [r["item_id"] for r in resolved if r["downloaded"] is False]
        activity_log.record(actor=_actor(), category="admin", action="ab_eval_set.save",
                            details={"name": stored["name"],
                                     "n_items": len(stored["item_ids"])})
        return jsonify({**stored, "resolved": resolved, "not_downloaded": not_downloaded})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval-set/sample', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def sample_ab_eval_set():
    """Sample N downloaded item ids (stratified by platform) WITHOUT persisting.

    Body: ``{n, platforms?, seed?}``. The UI merges/edits the returned ids and
    then saves the set explicitly.
    """
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        try:
            n = int(body.get('n') or 10)
        except (TypeError, ValueError):
            return jsonify({"error": "'n' must be an integer"}), 400
        platforms = body.get('platforms') if isinstance(body.get('platforms'), list) else None
        seed = body.get('seed')
        item_ids = ab_eval.sample_items(n, platforms=platforms,
                                        seed=int(seed) if seed is not None else None)
        return jsonify({"item_ids": item_ids,
                        "resolved": ab_eval.resolve_items(item_ids)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/estimate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def estimate_ab_eval():
    """Estimate a run's Gemini call count for the confirm dialog.

    Body: ``{candidate_names, include_live}``.
    """
    try:
        from fyp import ab_eval

        body = request.get_json(silent=True) or {}
        names = body.get('candidate_names') or []
        n_arms = len(names) + (1 if body.get('include_live') else 0)
        stored = ab_eval.load_eval_set()
        n_items = len(stored.get("item_ids", []))
        return jsonify({
            "n_items": n_items,
            "n_arms": n_arms,
            "n_calls": n_items * n_arms,
            "eval_set": stored.get("name"),
            "max_items": ab_eval.MAX_EVAL_ITEMS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/run', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def start_ab_eval_run():
    """Start an A/B evaluation run as the ``ab_eval`` background task.

    Body: ``{candidate_names, include_live}``. Mints the run id here so the
    UI can follow the run immediately; the worker snapshots each arm's
    contract text at start.
    """
    try:
        from fyp import ab_eval
        from fyp.fyp_config import AB_EVAL_SCRIPT

        # Explicit gate on top of start_process's own check: one A/B run at a
        # time (a second concurrent run would double the Gemini spend and race
        # on the runs index).
        if _is_worker_running("ab_eval"):
            return jsonify({"status": "error",
                            "message": "An A/B evaluation run is already in progress."}), 409

        body = request.get_json(silent=True) or {}
        names = body.get('candidate_names') or []
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        include_live = bool(body.get('include_live'))
        if not names and not include_live:
            return jsonify({"error": "select at least one candidate or include the live contract"}), 400
        for name in names:
            if not ab_eval.validate_candidate_name(name):
                return jsonify({"error": f"invalid candidate name '{name}'"}), 400
        stored = ab_eval.load_eval_set(body.get('eval_set') or None)
        item_ids = stored.get("item_ids", [])
        if not item_ids:
            return jsonify({"error": "the evaluation set is empty — curate it first"}), 400

        run_id = ab_eval.new_run_id()
        run_name = str(body.get('name') or "").strip()[:60]
        task_args = {
            "run_id": run_id,
            "name": run_name,
            "candidate_names": names,
            "include_live": include_live,
            "eval_set": stored.get("name"),
            "started_by": _actor(),
        }
        success, msg = start_process("ab_eval", AB_EVAL_SCRIPT, task_args=task_args)
        if not success:
            return jsonify({"status": "error", "message": msg}), 409
        activity_log.record(actor=_actor(), category="admin", action="ab_eval.run",
                            details={"run_id": run_id, "candidates": names,
                                     "include_live": include_live,
                                     "eval_set": stored.get("name"),
                                     "n_items": len(item_ids)})
        return jsonify({"status": "started", "run_id": run_id, "message": msg,
                        "eval_set": stored.get("name")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def list_ab_eval_runs():
    """Return the runs index (newest first)."""
    try:
        from fyp import ab_eval

        return jsonify({"runs": ab_eval.load_runs_index()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_ab_eval_run(run_id):
    """Return one run's manifest + comparison report + human-input block."""
    try:
        from fyp import ab_eval, human_eval

        run = ab_eval.load_run(run_id)
        if not run.get("manifest"):
            return jsonify({"error": f"run '{run_id}' not found"}), 404
        try:
            run["human"] = human_eval.load_human(run_id)
        except Exception:
            run["human"] = None
        return jsonify(run)
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>/rows', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_ab_eval_run_rows(run_id):
    """Return one arm's refined rows (JSON-safe) for the side-by-side view.

    ``arm`` may also be ``human:<username>`` — a submitted coder of the run's
    coding task, served as rows so human input renders like any other arm.
    """
    try:
        from fyp import ab_eval, human_eval

        arm = str(request.args.get('arm') or "").strip()
        if not arm:
            return jsonify({"error": "pass ?arm=<arm name>"}), 400
        if arm.startswith("human:"):
            username = arm[len("human:"):]
            task = human_eval.load_task(run_id, "coding")
            if task is None or username not in task.get("coders", {}):
                return jsonify({"error": f"no coder '{username}' on run '{run_id}'"}), 404
            rows = human_eval.coder_rows(run_id, "coding", username)
        else:
            try:
                rows = ab_eval.load_run_rows(run_id, arm)
            except Exception:
                return jsonify({"error": f"no rows for run '{run_id}' arm '{arm}'"}), 404
        return jsonify({"run_id": run_id, "arm": arm, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ab-eval/runs/<run_id>', methods=['DELETE'])
@permission_required('tab.admin.schema')
@login_required
def delete_ab_eval_run(run_id):
    """Delete a run's artifacts."""
    try:
        from fyp import ab_eval

        removed = ab_eval.delete_run(run_id)
        if removed:
            activity_log.record(actor=_actor(), category="admin",
                                action="ab_eval.run_delete", details={"run_id": run_id})
        return jsonify({"ok": removed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/studies/<study>/annotation-version', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def set_study_annotation_version(study):
    """Pin (or clear) a study's annotation_version for reproducibility.

    A pinned study merges against that version's annotations (read strictly from
    the archive) instead of the active dataset. Pass a falsy ``version`` to clear
    the pin. The study must be refreshed to rebuild its dataset.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")  # falsy clears the pin
        if "study_defs" not in fyp_cf:
            init_study_defs()
        if study not in fyp_cf["study_defs"]:
            return jsonify({"error": f"unknown study: {study}"}), 404
        if version:
            registry = annotation_versioning.load_registry()
            if version not in registry.get("versions", {}):
                return jsonify({"error": f"unknown version: {version}"}), 404
            fyp_cf["study_defs"][study]["annotation_version"] = version
        else:
            fyp_cf["study_defs"][study].pop("annotation_version", None)
        save_study_defs()
        study_cache.invalidate(study)
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="study.pin_annotation_version",
            details={"study": study, "version": version or None},
        )
        return jsonify({
            "ok": True,
            "study": study,
            "annotation_version": version or None,
            "note": "Refresh the study to rebuild its dataset against this version.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _contract_locked_map(df) -> dict:
    """Return ``{variable_name: {metadata, section}}`` for contract-owned cells.

    ``metadata`` is True when a contract owns the row's role/scale/display_name/
    description — the annotation contract's flattened Gemini columns, or the
    scrape / activity / derived contracts' canonical columns. ``section`` is True
    for every Gemini-origin row (all forced under "AI Annotations") and for every
    scrape / activity / derived contract column (whose section those contracts
    own). The admin editor renders these cells read-only. Degrades to ``{}`` if no
    contract can be loaded, so the editor never breaks on a contract error.
    """
    annotation_cols: set = set()
    scrape_cols: set = set()
    activity_cols: set = set()
    derived_cols: set = set()
    try:
        from fyp import annotation_contract as ac

        annotation_cols = set(ac.contract_column_metadata(ac.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import scrape_contract as sc

        scrape_cols = set(sc.contract_column_metadata(sc.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import activity_contract as acy

        activity_cols = set(acy.contract_column_metadata(acy.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import derived_contract as dc

        derived_cols = set(dc.contract_column_metadata(dc.load_contract()).keys())
    except Exception:
        pass
    # Legacy fields owned by past-version registry snapshots (e.g. trend /
    # australian_relevance for annotation; any future retired scrape/activity
    # field) — contract-owned/read-only, and badged "legacy" in the editor. A
    # field a CURRENT contract still owns is NOT legacy.
    legacy_cols: set = set()
    try:
        from fyp import annotation_versioning as av

        legacy_cols |= set(av.union_field_metadata().keys()) - annotation_cols
    except Exception:
        pass
    try:
        from fyp import scrape_versioning as sv

        legacy_scrape = set(sv.union_field_metadata().keys()) - scrape_cols
        legacy_cols |= legacy_scrape
        scrape_cols |= legacy_scrape
    except Exception:
        pass
    try:
        from fyp import activity_versioning as av_act

        legacy_activity = set(av_act.union_field_metadata().keys()) - activity_cols
        legacy_cols |= legacy_activity
        activity_cols |= legacy_activity
    except Exception:
        pass
    if not (annotation_cols or scrape_cols or activity_cols or derived_cols or legacy_cols):
        return {}
    section_owned_cols = scrape_cols | activity_cols | derived_cols
    locked: dict = {}
    for _, row in df.iterrows():
        vn = str(row.get("variable_name", ""))
        src = str(row.get("source", "")).strip()
        is_gemini = src == "Gemini" or src.startswith("derived: Gemini")
        section_owned = vn in section_owned_cols
        is_legacy = vn in legacy_cols
        meta_owned = (
            vn in annotation_cols or vn in scrape_cols
            or vn in activity_cols or vn in derived_cols or is_legacy
        )
        if meta_owned or is_gemini:
            entry = {"metadata": meta_owned, "section": is_gemini or section_owned}
            if is_legacy:
                entry["legacy"] = True
            locked[vn] = entry
    return locked




@management_bp.route('/api/manage/schema', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_schema():
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    """Return the current schema for the admin editor.

    ``?force_reload=1`` re-reads ``var_schema.csv`` from disk/GCS before
    responding so the editor's Reload button picks up direct edits made
    outside the UI (e.g. ``gsutil cp``).  The initial tab load and the
    post-save refresh omit the flag — they only need in-memory state.
    """
    try:
        from fyp import var_presentation as vp
        from fyp import annotation_contract as ac

        if request.args.get("force_reload") in ("1", "true", "yes"):
            global fyp_cf
            fyp_cf = load_var_schema(fyp_cf, verbose=False)
        df = fyp_cf["var_schema"]
        presentation = vp.load_presentation() or vp.empty_presentation()
        # The annotation contract can be edited at runtime; reflect its live
        # source so the read-only tooltips point at the right place.
        ac_source = ac.contract_status().get("source")
        contract_path = (
            f"{ac.RUNTIME_FILENAME} (runtime)" if ac_source == "runtime"
            else "config/annotation_contract.toml (baked)"
        )
        return jsonify({
            "rows": _df_to_records(df),
            "columns": list(df.columns),
            "semantic_columns": list(SEMANTIC_COLUMNS),
            "enums": {
                "role": sorted(VAR_SCHEMA_ROLES),
                "scale": sorted(VAR_SCHEMA_SCALES),
            },
            "contract_locked": _contract_locked_map(df),
            "contract_path": contract_path,
            "scrape_contract_path": "config/scrape_contract.toml",
            # The presentation store is the only admin-editable payload left
            # (the metadata is contract-owned); its etag guards saves.
            "presentation": presentation.get("surfaces", {}),
            "prio_columns": dict(vp.SURFACE_TO_PRIO_COLUMN),
            "etag": vp.compute_presentation_etag(presentation),
            "current_hash": compute_var_schema_hash(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/schema/validate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def validate_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/schema', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/presentation', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_presentation_endpoint():
    """Persist the global web-surface membership flags (the admin defaults).

    Body: ``{"surfaces": {filter|timeline|viz|display: [variable_name, ...]},
    "etag": <presentation etag from GET /api/manage/schema>}``. Refuses on a
    stale etag (409) or unknown variable names (400). Presentation edits can
    never change the study hash — asserted server-side as a guard.
    """
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import var_presentation as vp

        body = request.get_json(force=True, silent=False) or {}
        surfaces = body.get("surfaces")
        etag = body.get("etag")
        if not isinstance(surfaces, dict):
            return jsonify({"error": "surfaces must be an object"}), 400
        known = set(fyp_cf["var_schema"]["variable_name"].astype("string"))
        unknown = sorted({
            n for names in surfaces.values() if isinstance(names, list)
            for n in names if n not in known
        })
        if unknown:
            return jsonify({"error": "unknown variables", "unknown": unknown}), 400

        old_hash = compute_var_schema_hash()
        try:
            result = vp.save_presentation(surfaces, expected_etag=etag, updated_by=_actor())
        except vp.PresentationConflict as e:
            return jsonify({
                "error": "conflict",
                "message": str(e),
                "etag": vp.compute_presentation_etag(),
            }), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        new_hash = compute_var_schema_hash()
        hash_changed = new_hash != old_hash
        if hash_changed:
            # Presentation flags are excluded from the hash by design; a change
            # here means something else drifted — surface it loudly.
            print(f"WARNING: presentation save changed the schema hash ({old_hash[:16]} -> {new_hash[:16]}).")
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="var_presentation.save",
            details={"hash_changed": hash_changed},
        )
        return jsonify({"etag": result["etag"], "hash_changed": hash_changed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/ingestion/sources', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_sources():
    try:
        main_collection = get_main_collection(verbose=False)
        sources = []
        total_pending = 0
        for col in main_collection.collections:
            files: list[dict] = []
            manifest_fn = "ingestion_manifest.json"
            if col.raw_path and data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
                manifest = data_io.load_json(
                    storage_location=col.raw_path, filename=manifest_fn, verbose=False
                ) or {}
                for fn, meta in manifest.items():
                    files.append({
                        "filename": fn,
                        "collection_id": (meta or {}).get("collection_id"),
                        "tags": (meta or {}).get("tags") or [],
                        "tz": (meta or {}).get("tz"),
                    })
            files.sort(key=lambda f: f["filename"])
            pending = len(files)
            total_pending += pending
            sources.append({
                "source_platform": col.source_platform,
                "data_source": col.data_source,
                "raw_path": col.raw_path,
                "class_name": col.__class__.__name__,
                "pending_files": pending,
                "files": files,
                "ingestion_mode": getattr(col, "ingestion_mode", "upload"),
                "zip_member_suffixes": col.zip_member_suffixes(),
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/fetch_aio', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def fetch_aio_data():
    """Trigger download of recent AIO donations and metadata from AWS."""
    from fyp.fyp_config import AIO_FETCH_SCRIPT

    hours_back = 24
    if request.is_json and request.json:
        hours_back = int(request.json.get('hours_back', 24))

    success, msg = start_process(
        "aio_fetch",
        AIO_FETCH_SCRIPT,
        task_args={"hours_back": hours_back},
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409

@management_bp.route('/api/manage/ingestion/upload', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def upload_ingestion_file():
    """Upload one or more raw files with optional collection_id and tags metadata.

    Accepts form fields:
        files: one or more files (also accepts legacy single 'file' key)
        raw_path: storage location key (e.g. 'ddp_raw')
        collection_id: explicit collection ID (used when collection_id_mode is 'single')
        collection_id_mode: 'single' | 'per_file' (default 'per_file')
        tags: JSON-encoded list of tag strings
        tz: optional donor timezone (IANA name like 'Asia/Kolkata' or a fixed
            offset like '+05:30') — the authoritative source for local-time
            conversion, overriding any ambiguous timezone label in the export.
    """
    # Accept both multi-file ('files') and legacy single-file ('file') keys
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400

    raw_path_key = request.form.get('raw_path')
    if not raw_path_key:
        return jsonify({"error": "raw_path missing"}), 400

    # Stage uploads in the local temp dir, then hand off to data_io.move()
    # which routes to GCS (production) or the configured local data dir
    # (dev). Writing directly to the resolved local path skipped GCS
    # entirely on Cloud Run, so manifests pointed at files that only ever
    # lived on the request-handling container's ephemeral filesystem.
    temp_dir = fyp_cf['paths']['temp']
    os.makedirs(temp_dir, exist_ok=True)

    collection_id = request.form.get('collection_id', '').strip()
    collection_id_mode = request.form.get('collection_id_mode', 'per_file')
    tags_json = request.form.get('tags', '[]')
    try:
        tags = json.loads(tags_json) if tags_json else []
    except json.JSONDecodeError:
        tags = []

    # Optional donor timezone (IANA name or fixed offset). Validated here so a
    # typo is rejected at upload rather than silently ignored at ingest time.
    donor_tz = request.form.get('tz', '').strip()
    if donor_tz and parse_donor_timezone(donor_tz) is None:
        return jsonify({
            "error": f"Unrecognised timezone '{donor_tz}'. Use an IANA name "
                     f"(e.g. 'Asia/Kolkata') or a fixed offset (e.g. '+05:30').",
        }), 400

    # Load or create the ingestion manifest for this raw_path
    manifest_fn = "ingestion_manifest.json"
    manifest: dict = {}
    if data_io.exists(storage_location=raw_path_key, filename=manifest_fn):
        manifest = data_io.load_json(
            storage_location=raw_path_key, filename=manifest_fn, verbose=False
        ) or {}

    try:
        uploaded = []
        for file in files:
            if file.filename == '':
                continue
            filename = secure_filename(file.filename)
            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)

            data_io.move(
                src_storage_location="temp",
                dst_storage_location=raw_path_key,
                filename=filename,
                verbose=False,
            )
            # data_io.move() swallows GCS upload failures silently, so confirm
            # the file actually landed before we record it in the manifest.
            if not data_io.exists(storage_location=raw_path_key, filename=filename):
                return jsonify({
                    "error": f"Upload of '{filename}' to '{raw_path_key}' did not persist.",
                }), 500

            if collection_id_mode == "single" and collection_id:
                file_collection_id = collection_id
            else:
                file_collection_id = os.path.splitext(filename)[0]

            manifest[filename] = {
                "collection_id": file_collection_id,
                "tags": tags,
            }
            if donor_tz:
                manifest[filename]["tz"] = donor_tz
            uploaded.append(filename)

        # Save updated manifest
        data_io.save_json(
            data=manifest,
            storage_location=raw_path_key,
            filename=manifest_fn,
            verbose=False
        )

        # Pre-populate collection_annotations.json with tags for each unique collection_id
        if tags:
            _prepopulate_annotations(manifest, tags)

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.upload",
            target=raw_path_key,
            details={
                "files": uploaded,
                "tags": tags,
                "collection_id_mode": collection_id_mode,
                "tz": donor_tz or None,
            },
        )
        return jsonify({
            "status": "success",
            "message": f"{len(uploaded)} file(s) uploaded.",
            "files": uploaded,
        })
    except Exception as e:
        print(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/refresh-collection-metadata', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_collection_metadata():
    """Regenerate _metadata.parquet from scratch using all events."""
    from fyp.fyp_config import COLLECTION_METADATA_REFRESH_SCRIPT

    success, msg = start_process(
        "collection_metadata_refresh",
        COLLECTION_METADATA_REFRESH_SCRIPT,
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409



@management_bp.route('/api/manage/ingestion/refresh', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_ingestion_collection():
    from fyp.fyp_config import INGEST_REFRESH_SCRIPT

    success, msg = start_process("ingest_refresh", INGEST_REFRESH_SCRIPT)
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.refresh",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409




@management_bp.route('/api/manage/ingestion/ledger/unskip', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def unskip_ingestion_ledger_entry():
    """Drop a single filename from the ingestion ledger so it will be
    re-scanned on the next ingestion run. The raw file on disk is left
    untouched.
    """
    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    main_collection = get_main_collection(verbose=False)
    removed = main_collection.remove_from_ledger(filename)
    if not removed:
        return jsonify({
            "status": "noop",
            "message": f"'{filename}' was not in the ledger.",
        })

    main_collection.save_ledger()
    return jsonify({
        "status": "success",
        "message": f"'{filename}' removed from the ledger. It will be rescanned on the next ingestion run.",
    })




@management_bp.route('/api/manage/ingestion/structure/warnings', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_warnings():
    """List structure-drift verdicts awaiting review (quarantined + warned files)."""
    from fyp import structure_sentinel

    try:
        return jsonify(structure_sentinel.review_queue())
    except Exception as e:
        print(f"Error loading structure warnings: {e}")
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ingestion/structure/approve', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_approve():
    """Approve a quarantined file: fold its structure into the learned baseline
    and drop its ledger entry so the next ingestion run ingests it.
    """
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.approve_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

    main_collection = get_main_collection(verbose=False)
    main_collection.remove_from_ledger(filename)
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_approve",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' approved — its structure is now part of the baseline and it will be ingested on the next refresh.",
    })




@management_bp.route('/api/manage/ingestion/structure/reject', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_reject():
    """Reject a quarantined file: mark it manually excluded so it never ingests."""
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.reject_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404

    main_collection = get_main_collection(verbose=False)
    if not main_collection.set_ledger_outcome(
        filename, "manually_excluded", note="rejected via structure review"
    ):
        # No ledger entry yet (e.g. the refresh that quarantined it failed
        # before saving) — stamp one directly so the file is still excluded.
        main_collection.update_ledger([{
            "filename": filename,
            "outcome": "manually_excluded",
            "raw_rows": (entry.get("raw_stats") or {}).get("raw_rows") or 0,
            "final_rows": 0,
            "canonical_collection_id": None,
            "merged_with_siblings": [],
            "platform": entry.get("platform"),
            "source": entry.get("source"),
            "notes": "rejected via structure review",
        }])
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_reject",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' rejected — it is excluded from future ingestion runs.",
    })


@management_bp.route('/api/manage/ingestion/clear_pending', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def clear_pending_uploads():
    """Drop every pending upload across every registered ingester: delete each
    file from its raw_path storage and reset its manifest to an empty dict.
    Lightweight (no parquet I/O), so safe to run inline on the data-hub.
    """
    main_collection = get_main_collection(verbose=False)
    manifest_fn = "ingestion_manifest.json"

    cleared: list[dict] = []
    failures: list[dict] = []
    total_removed = 0

    for col in main_collection.collections:
        if not col.raw_path:
            continue
        if not data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
            continue
        manifest = data_io.load_json(
            storage_location=col.raw_path, filename=manifest_fn, verbose=False
        ) or {}
        if not manifest:
            continue

        removed_here: list[str] = []
        for fn in list(manifest.keys()):
            try:
                if data_io.exists(storage_location=col.raw_path, filename=fn):
                    data_io.remove(storage_location=col.raw_path, filename=fn)
                removed_here.append(fn)
            except Exception as e:
                failures.append({"raw_path": col.raw_path, "filename": fn, "error": str(e)})
                print(f"[clear_pending_uploads] failed to remove {col.raw_path}/{fn}: {e}")

        try:
            data_io.save_json(
                data={},
                storage_location=col.raw_path,
                filename=manifest_fn,
                verbose=False,
            )
        except Exception as e:
            failures.append({"raw_path": col.raw_path, "filename": manifest_fn, "error": str(e)})
            print(f"[clear_pending_uploads] failed to reset manifest for {col.raw_path}: {e}")

        cleared.append({
            "raw_path": col.raw_path,
            "class_name": col.__class__.__name__,
            "removed_files": removed_here,
        })
        total_removed += len(removed_here)

    return jsonify({
        "status": "success",
        "total_removed": total_removed,
        "cleared": cleared,
        "failures": failures,
    })





def _prepopulate_annotations(manifest: dict, tags: list[str]) -> None:
    """Merge tags into collection_annotations.json for each unique collection_id in the manifest."""
    annotations: dict = {}
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        ) or {}

    seen_ids: set = set()
    for _filename, meta in manifest.items():
        cid = meta.get("collection_id")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            existing = annotations.get(cid, {})
            existing_tags = existing.get("annotation_tags", [])
            merged_tags = sorted(set(existing_tags + tags))
            annotations[cid] = {
                "display_collection_id": existing.get("display_collection_id"),
                "annotation_tags": merged_tags,
                "hidden": existing.get("hidden", False),
            }

    data_io.save_json(
        data=annotations,
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_tags.json",
        verbose=False
    )
    invalidate_collection_tags_cache()





@management_bp.route('/api/manage/ingestion/metadata', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""

    from fyp.organize_datasets import COLLECTIONS_LABEL

    collection_ids: list[str] = []
    all_tags: set[str] = set()

    # Get collection IDs from the per-collection metadata parquet — small
    # enough to read in milliseconds, vs. the multi-GB recoded parquet which
    # would block the upload modal for ~5s while the user waits.
    metadata_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if data_io.exists(storage_location="recoded", filename=metadata_fn):
        md = data_io.load_parquet(
            storage_location="recoded",
            filename=metadata_fn,
            verbose=False,
        )
        if md is not None and not md.empty:
            if "collection_id" in md.columns:
                collection_ids = sorted(md["collection_id"].dropna().astype(str).unique().tolist())
            else:
                collection_ids = sorted(str(idx) for idx in md.index.dropna().unique().tolist())

    # Get tags from annotations
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False,
        ) or {}
        for ann in annotations.values():
            for tag in ann.get("annotation_tags", []):
                all_tags.add(tag)

    display_ids = load_display_id_map()

    return jsonify({
        "status": "success",
        "collection_ids": collection_ids,
        "display_ids": display_ids,
        "tags": sorted(list(all_tags)),
    })
