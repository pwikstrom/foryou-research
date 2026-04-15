import os
import json
import time as _time
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user
from datetime import datetime, timezone
from fyp.fyp_config import fyp_cf, load_var_schema
from fyp.ingest import get_main_collection
import fyp.data_io as data_io
from fyp.organize_datasets import create_study_recoded_dataset, SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL, COLLECTIONS_LABEL
from fyp.pca import calculate_scaled_pca_scores
from fyp.studies import init_study_defs, save_study_defs
from .. import explorer_backend as explorer
from ..process_manager import processes, process_stats, load_process_stats, save_process_stats, start_process, CLOUD_TASK_ELIGIBLE
from ..task_status import is_cloud_run
from ..data_service import invalidate_collection_tags_cache
import pandas as pd
from ..data_service import get_viz_config, load_schema_metadata, study_cache, make_serializable, calculate_inter_coder_reliability

management_bp = Blueprint('management_bp', __name__)






def _calculate_stats(study_config, save_to_cache=True) -> tuple[dict, pd.DataFrame | None]:
    """Calculate stats for a study using enrichment_status.parquet AND the study's specific recoded dataset.

    Returns:
        Tuple of (stats_dict, full_recoded_dataframe). The DataFrame is None when no data exists.
    """

    empty_stats = {"total_activities": 0, "unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0}

    study_name = study_config.get("STUDY_NAME")
    if not study_name:
         return empty_stats, None

    # If no collections are selected, the study is empty — skip expensive computation
    selected = study_config.get("SELECTED_COLLECTIONS", [])
    if not selected:
         return empty_stats, None

    _t_total = _time.perf_counter()

    # 1. Load enrichment status once (used for both dataset creation and stats matching).
    # We previously tried backgrounding this load, but simple_sample_collection_events
    # reloaded the parquet for its diagnostic summary, defeating the parallelism and
    # causing a duplicate read. Serial load + pass-through is simpler and lets callees
    # reuse the DataFrame without a second GCS round-trip.
    _t_phase = _time.perf_counter()
    df_status = None
    if data_io.exists(storage_location="recoded", filename='enrichment_status.parquet'):
        df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
    _t_status = _time.perf_counter() - _t_phase

    # 2. Create the recoded dataset, passing enrichment_status to avoid reloading.
    print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
    _t_phase = _time.perf_counter()
    df_study = create_study_recoded_dataset(
        study_name=study_name, save_to_cache=save_to_cache,
        enrichment_status=df_status, verbose=False)
    _t_recode = _time.perf_counter() - _t_phase

    if df_study is None or df_study.empty:
        print(f"No data found for study '{study_name}'. Removing all cached files for this study.")
        data_io.remove(storage_location="cache", filename=f"{study_name}_recoded.parquet")
        data_io.remove(storage_location="cache", filename=f"{study_name}_explorer_metadata.json")
        data_io.remove(storage_location="cache", filename=f"{study_name}_comp_interpretations.json")
        data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")
        return empty_stats, None

    # 3. Count unique items
    _t_phase = _time.perf_counter()
    total_activities = len(df_study)
    unique_collections = df_study['collection_id'].nunique()
    unique_videos = df_study['item_id'].nunique()

    # 4. Match against enrichment status for scrape/annotation counts
    scraped_videos = 0
    annotated_videos = 0

    if df_status is not None and not df_status.empty:
        # Robust alignment: Ensure item_id is a column and use PyArrow strings
        if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
            df_status = df_status.reset_index()

        if 'item_id' in df_status.columns:
            try:
                status_ids = df_status['item_id'].astype("string[pyarrow]")
                study_ids = df_study['item_id'].astype("string[pyarrow]")
                matched_status = df_status.loc[status_ids.isin(study_ids)].copy()
            except Exception as e:
                print(f"Error during robust index matching: {e}. Falling back to standard matching.")
                study_item_ids = df_study['item_id'].unique()
                matched_status = df_status.loc[df_status.index.isin(study_item_ids)].copy()
        else:
            study_item_ids = df_study['item_id'].unique()
            matched_status = df_status.loc[df_status.index.isin(study_item_ids)].copy()

        if 'scraped_ok' in matched_status.columns:
            scraped_videos = int(matched_status['scraped_ok'].fillna(False).sum())
        if 'annotated_ok' in matched_status.columns:
            annotated_videos = int(matched_status['annotated_ok'].fillna(False).sum())

    stats = {
        "total_activities": int(total_activities),
        "unique_videos": int(unique_videos),
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_collections": int(unique_collections)
    }

    _t_count = _time.perf_counter() - _t_phase
    _t_stats_total = _time.perf_counter() - _t_total
    print(
        f"[STATS][TIMING] study={study_name} "
        f"status_load={_t_status:.2f}s recode={_t_recode:.2f}s "
        f"count={_t_count:.2f}s total={_t_stats_total:.2f}s"
    )

    return stats, df_study







@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
def list_studies():
    # Always reload from disk/GCS to pick up changes made by the task-runner service
    init_study_defs()

    studies = fyp_cf['study_defs']

    # Convert to list with name included
    studies_list = []
    
    # User Access Logic
    # Admin sees everything.
    # Others (Researcher/Viewer) see only studies where they are listed in USER_ACCESS
    is_admin = current_user.is_admin()
    username = current_user.username
    
    for name, config in studies.items():
        # Ensure name is in config
        config['STUDY_NAME'] = name
        
        if is_admin:
            studies_list.append(config)
        else:
            # Check USER_ACCESS for this study
            user_access = config.get("USER_ACCESS", [])
            # user_access should be a list of ROLES (e.g. ['viewer', 'researcher', 'student']) or ['all']
            if isinstance(user_access, list) and (current_user.role in user_access or 'all' in user_access):
                studies_list.append(config)
        
    return jsonify(studies_list)







@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
def save_study():
    global fyp_cf

    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
        
        if not changed_keys:
             # If exact same definition, return early
             return jsonify({"status": "no_change", "message": "No changes to save."})

        # If only USER_ACCESS changed, we don't need to recalculate anything
        if len(changed_keys) == 1 and changed_keys[0] == 'USER_ACCESS':
            print(f"Only USER_ACCESS changed for {study_name}. Saving without recalculation.")
            studies[study_name]['USER_ACCESS'] = data['USER_ACCESS']
            fyp_cf['study_defs'] = studies
            save_study_defs()
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

    # Update timestamp and save definition to disk
    studies[study_name]['last_updated'] = datetime.now(timezone.utc).isoformat()
    fyp_cf['study_defs'] = studies
    save_study_defs()

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
        # Local dev: run synchronously as before
        from web_interface.run_study_refresh import run_study_refresh
        from web_interface.task_status import LocalStatusReporter

        reporter = LocalStatusReporter("study_refresh")
        try:
            run_study_refresh(reporter=reporter, task_args=task_args)
            reporter.complete()
        except Exception as e:
            print(f"Study refresh failed: {e}")
            reporter.fail(str(e))

        # Reload study defs to get updated stats
        init_study_defs()
        studies = fyp_cf['study_defs']

        return jsonify({"status": "success", "study": studies.get(study_name, {})})






@management_bp.route('/api/manage/studies/calculate_stats', methods=['POST'])
@login_required
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    global fyp_cf
    
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    # 1. Backup existing config
    original_config = None
    if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
        original_config = fyp_cf['study_defs'][study_name].copy()
        
    
    # If this is a new study (not in defs), we add it. 
    # If existing, we overwrite.
    fyp_cf['study_defs'][study_name] = data
    
    try:
        # 3. specific instruction: "Force update of the study dataset"
        # The logic in _calculate_stats calls create_study_recoded_dataset
        stats, _ = _calculate_stats(data, save_to_cache=False)

        return jsonify({"status": "success", "stats": stats})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 4. Revert config
        if original_config is not None:
             fyp_cf['study_defs'][study_name] = original_config
        else:
             # If it was new, remove it? 
             # Or keep it? Safer to remove if it wasn't there.
             if study_name in fyp_cf['study_defs']:
                  del fyp_cf['study_defs'][study_name]





@management_bp.route('/api/manage/studies/delete', methods=['POST'])
@login_required
def delete_study():
    global fyp_cf
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized - Admin only"}), 403
        
    data = request.json
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    init_study_defs()
    if not 'study_defs' in fyp_cf:
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

        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404





@management_bp.route('/api/manage/collections', methods=['GET'])
@login_required
def list_collections():
    
    if not current_user.is_admin():
         return jsonify([])

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
            
    if False:#except Exception as e:
        print(f"Error listing collections: {e}")


@management_bp.route('/api/manage/collection/save_annotation', methods=['POST'])
@login_required
def save_collection_annotation():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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

        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    if not current_user.is_admin():
         return jsonify({"error": "Unauthorized"}), 403

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
        
    
    # 2. Get Queue Lengths
    scrape_queue_len = 0
    annotate_queue_len = 0
    
    if data_io.exists(storage_location='cache', filename='to_scrape.json'):
        try:
            q = data_io.load_json(storage_location='cache', filename='to_scrape.json')
            if isinstance(q, list): scrape_queue_len = len(q)
        except Exception:
            pass
        
    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)
        
    return jsonify({
        "total_videos": total_videos,
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_collections": unique_collections,
        "scrape_queue_len": scrape_queue_len,
        "annotate_queue_len": annotate_queue_len,
        "consolidate_stats": {
            **process_stats.get("consolidate_enrichment", {}),
            **processes.get("consolidate_enrichment", {}).get("data", {})
        } or None,
        "scraper_last_success": process_stats.get("queue_scraper", {}).get("last_success"),
        "annotator_last_success": process_stats.get("queue_annotator", {}).get("last_success"),
    })






@management_bp.route('/api/manage/enrichment/empty_queue/<queue_type>', methods=['POST'])
@login_required
def empty_enrichment_queue(queue_type):
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
        
    try:
        if queue_type == "scrape":
            if data_io.exists(storage_location='cache', filename='to_scrape.json'):
                data_io.remove(storage_location='cache', filename='to_scrape.json')
            load_process_stats()
            if "scrape_queue_len" in process_stats.get("queue_scraper", {}):
                process_stats["queue_scraper"]["scrape_queue_len"] = 0
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
@login_required
def queue_voted_videos():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        from web_interface.security import user_manager
        
        # 1. Gather all votes across all users
        all_votes = {} # dict of collection_id -> set of periods
        for user in user_manager.users.values():
            if not user.machine_annotation_votes:
                continue
            for coll_id, periods in user.machine_annotation_votes.items():
                if coll_id not in all_votes:
                    all_votes[coll_id] = set()
                all_votes[coll_id].update(periods)
                
        if not all_votes:
            return jsonify({"status": "no_votes", "message": "No votes found for machine annotation."})

        # 2. Map periods to item_ids 
        from fyp.organize_datasets import create_collection_unified_dataset
        import pandas as pd
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

        new_scrape = []
        new_annotate = []

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
                    
                    # Same logic from user request
                    if not is_scraped and not scrape_fail:
                        new_scrape.append(item)
                    elif is_scraped and not is_annotated and not annotated_fail:
                        new_annotate.append(item)
                else:
                    # Item not in enrichment status -> hasn't been scraped yet
                    new_scrape.append(item)
        else:
            # No enrichment file -> everything needs scraping
            new_scrape = list(target_item_ids)

        new_scrape = list(set(new_scrape))
        new_annotate = list(set(new_annotate))

        # 4. Append to Queues
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

        if new_scrape:
             # We store scrape targets globally 
             current_scrape = load_queue("to_scrape.json")
             current_scrape.extend(new_scrape)
             save_queue("to_scrape.json", current_scrape)

        if new_annotate:
             current_annotate = load_queue("to_annotate.json")
             current_annotate.extend(new_annotate)
             save_queue("to_annotate.json", current_annotate)

        return jsonify({
            "status": "success", 
            "added_to_scrape": len(new_scrape),
            "added_to_annotate": len(new_annotate)
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@login_required
def calculate_to_scrape():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    study_name = data.get("study_name")
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
            
            if 'scrape_fail' in study_status.columns:
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

        # Append target payload to global scrape queue
        current_queue = []
        if data_io.exists(storage_location="cache", filename="to_scrape.json"):
            try:
                q = data_io.load_json(storage_location="cache", filename="to_scrape.json")
                if isinstance(q, list): current_queue = q
            except Exception:
                pass
                
        current_queue.extend(unscraped_videos)
        current_queue = list(set(current_queue))
        
        data_io.save_json(
            data=current_queue,
            storage_location="cache",
            filename="to_scrape.json"
        )

        return jsonify({"status": "success", "videos_to_scrape": len(current_queue)})

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@login_required
def calculate_to_annotate():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    study_name = data.get("study_name")
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

            if 'video_duration' in df_study.columns:
                study_videos = df_study[['item_id', 'video_duration']].copy()
            else:
                study_videos = df_study[['item_id']].copy()
                
            study_status = study_videos.merge(df_status, on='item_id', how='left')
            
            is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
            
            if 'annotated_ok' in study_status.columns:
                not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)
            else:
                not_annotated_ok = True
                
            if 'annotated_fail' in study_status.columns:
                not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
            else:
                not_annotated_fail = True
                
            unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail
            
            if 'video_duration' in study_status.columns:
                max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
                duration_ok = (study_status['video_duration'] < max_dur) | pd.isna(study_status['video_duration'])
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

        return jsonify({"status": "success", "videos_to_annotate": len(current_queue)})

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@login_required
def api_consolidate_enrichment():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    from fyp.fyp_config import CONSOLIDATE_ENRICHMENT_SCRIPT

    proc_state = processes.get("consolidate_enrichment", {})
    if proc_state.get("proc") is not None and proc_state["proc"].poll() is None:
        return jsonify({"status": "error", "message": "Consolidation already running"}), 409

    data = request.json or {}
    task_args = {}
    if data.get("force"):
        task_args["force_consolidation"] = True

    success, msg = start_process("consolidate_enrichment", CONSOLIDATE_ENRICHMENT_SCRIPT,
                                 task_args=task_args if task_args else None)
    if success:
        return jsonify({"status": "started", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409



def _evaluate_consolidation_staleness() -> dict:
    """Return impact/freshness for the latest consolidation, clearing stale impact.

    Reloads process_stats from GCS, inspects the stored consolidation_impact,
    and removes it when every downstream process has run successfully since the
    impact timestamp. Returns a dict with ``has_impact``, ``impact``, and a
    per-process ``processes`` map — safe to call from any endpoint that needs
    to reason about whether the consolidation impact panel should be visible.
    """
    load_process_stats()

    consolidate_entry = process_stats.get("consolidate_enrichment", {})
    impact = consolidate_entry.get("consolidation_impact")

    if not impact or not impact.get("timestamp"):
        return {"has_impact": False, "impact": None, "processes": {}}

    impact_ts = impact["timestamp"]
    affected_studies = impact.get("affected_study_names", [])
    affected_collections = impact.get("affected_collection_ids", [])

    downstream = {
        "recode_refresh_studies": {
            "label": "Study Definitions",
            "affected": affected_studies,
        },
        "meta_refresh_groups": {
            "label": "Explore Metadata",
            "affected": affected_studies,
        },
        "timelines_refresh": {
            "label": "Timelines",
            "affected": affected_collections,
        },
        "pca_refresh": {
            "label": "Correlations",
            "affected": affected_studies,
        },
    }

    result = {}
    all_fresh = True
    for proc_name, info in downstream.items():
        last_success = process_stats.get(proc_name, {}).get("last_success")
        stale = not last_success or last_success < impact_ts
        result[proc_name] = {
            "stale": stale,
            "label": info["label"],
            "affected": info["affected"],
        }
        if stale:
            all_fresh = False

    if all_fresh:
        consolidate_entry.pop("consolidation_impact", None)
        process_stats["consolidate_enrichment"] = consolidate_entry
        save_process_stats()
        # Also drop the in-memory copy. get_enrichment_stats merges
        # process_stats with processes[name]["data"] when building its
        # response, so a lingering in-memory copy would re-surface the
        # impact panel even after we popped it from process_stats.
        in_memory_data = processes.get("consolidate_enrichment", {}).get("data")
        if isinstance(in_memory_data, dict):
            in_memory_data.pop("consolidation_impact", None)
        return {"has_impact": False, "impact": impact, "processes": result}

    return {"has_impact": True, "impact": impact, "processes": result}




@management_bp.route('/api/manage/refresh/staleness', methods=['GET'])
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
    })


@management_bp.route('/api/manage/schema/reload', methods=['POST'])
@login_required
def reload_schema():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        global fyp_cf
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        return jsonify({"status": "success", "message": "Variable schema reloaded successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/inter_coder_reliability', methods=['GET'])
@login_required
def get_inter_coder_reliability():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        results = calculate_inter_coder_reliability()
        if "error" in results:
             return jsonify(results), 400
        return jsonify(results)
    except Exception as e:
        print(f"Error calculating reliability: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/ingestion/sources', methods=['GET'])
@login_required
def get_ingestion_sources():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        main_collection = get_main_collection(verbose=False)
        sources = []
        total_pending = 0
        for col in main_collection.collections:
            pending = 0
            manifest_fn = "ingestion_manifest.json"
            if col.raw_path and data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
                manifest = data_io.load_json(
                    storage_location=col.raw_path, filename=manifest_fn, verbose=False
                ) or {}
                pending = len(manifest)
            total_pending += pending
            sources.append({
                "source_platform": col.source_platform,
                "data_source": col.data_source,
                "raw_path": col.raw_path,
                "class_name": col.__class__.__name__,
                "pending_files": pending,
                "ingestion_mode": getattr(col, "ingestion_mode", "upload"),
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/fetch_aio', methods=['POST'])
@login_required
def fetch_aio_data():
    """Trigger download of recent AIO donations and metadata from AWS."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from fyp.donations import (
            get_recent_data_donations_from_aio_aws,
            get_donation_metadata_from_aio_aws,
        )
        hours_back = 24
        if request.is_json and request.json:
            hours_back = request.json.get('hours_back', 24)

        get_recent_data_donations_from_aio_aws(
            hours_back=hours_back,
            storage_location="aio_raw",
        )
        get_donation_metadata_from_aio_aws(verbose=True)

        return jsonify({"status": "success", "message": f"Fetched AIO donations from last {hours_back} hours."})
    except Exception as e:
        print(f"Error fetching AIO data: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/upload', methods=['POST'])
@login_required
def upload_ingestion_file():
    """Upload one or more raw files with optional collection_id and tags metadata.

    Accepts form fields:
        files: one or more files (also accepts legacy single 'file' key)
        raw_path: storage location key (e.g. 'ddp_raw')
        collection_id: explicit collection ID (used when collection_id_mode is 'single')
        collection_id_mode: 'single' | 'per_file' (default 'per_file')
        tags: JSON-encoded list of tag strings
    """
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    # Accept both multi-file ('files') and legacy single-file ('file') keys
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400

    raw_path_key = request.form.get('raw_path')
    if not raw_path_key:
        return jsonify({"error": "raw_path missing"}), 400

    # Resolve storage location key to absolute path
    resolved_path = fyp_cf['paths'].get(raw_path_key, raw_path_key)
    if not os.path.exists(resolved_path):
        try:
            os.makedirs(resolved_path, exist_ok=True)
        except Exception as e:
            return jsonify({"error": f"Failed to create directory: {e}"}), 500

    collection_id = request.form.get('collection_id', '').strip()
    collection_id_mode = request.form.get('collection_id_mode', 'per_file')
    tags_json = request.form.get('tags', '[]')
    try:
        tags = json.loads(tags_json) if tags_json else []
    except json.JSONDecodeError:
        tags = []

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
            save_path = os.path.join(resolved_path, filename)
            file.save(save_path)

            if collection_id_mode == "single" and collection_id:
                file_collection_id = collection_id
            else:
                file_collection_id = os.path.splitext(filename)[0]

            manifest[filename] = {
                "collection_id": file_collection_id,
                "tags": tags,
            }
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

        return jsonify({
            "status": "success",
            "message": f"{len(uploaded)} file(s) uploaded.",
            "files": uploaded,
        })
    except Exception as e:
        print(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/refresh-collection-metadata', methods=['POST'])
@login_required
def refresh_collection_metadata():
    """Regenerate _metadata.parquet from scratch using all events."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        from fyp.donations import generate_collection_metadata
        from fyp.organize_datasets import COLLECTIONS_LABEL

        # Preserve columns that are set outside generate_collection_metadata
        # (e.g. ('other','accepted') is set during ingestion, not during metadata generation)
        old_metadata = None
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            old_metadata = data_io.load_parquet(
                storage_location="recoded",
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
                verbose=False)

        events_df = data_io.load_parquet(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
            verbose=False)
        if events_df is None or events_df.empty:
            return jsonify({"error": "No events data found"}), 404

        result = generate_collection_metadata(
            collections_df=events_df,
            load_from_disk=False,
            verbose=True)

        # Restore preserved columns from old metadata
        if old_metadata is not None and not old_metadata.empty:
            preserved_cols = [c for c in old_metadata.columns if c not in result.columns]
            if preserved_cols:
                result = pd.merge(result, old_metadata[preserved_cols],
                                  left_index=True, right_index=True, how='left')
                # Save the merged result
                data_io.save_parquet(df=result, storage_location="recoded",
                                    filename=f"{COLLECTIONS_LABEL}_metadata.parquet", verbose=True)

        return jsonify({
            "status": "success",
            "message": f"Metadata regenerated for {len(result)} collections."
        })
    except Exception as e:
        print(f"Error refreshing collection metadata: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/ingestion/refresh', methods=['POST'])
@login_required
def refresh_ingestion_collection():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        main_collection = get_main_collection(verbose=True)
        main_collection.refresh_collection()
        return jsonify({"status": "success", "message": "Collection refreshed successfully."})
    except Exception as e:
        print(f"Error refreshing collection: {e}")
        return jsonify({"error": str(e)}), 500





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
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""

    from fyp.organize_datasets import COLLECTIONS_LABEL

    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    collection_ids: list[str] = []
    all_tags: set[str] = set()

    # Get collection IDs from processed activity data
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_recoded.parquet"):
        df = data_io.load_parquet(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
            verbose=False,
        )
        if df is not None and "collection_id" in df.columns:
            collection_ids = sorted(df["collection_id"].dropna().unique().tolist())

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

    return jsonify({
        "status": "success",
        "collection_ids": collection_ids,
        "tags": sorted(list(all_tags)),
    })
