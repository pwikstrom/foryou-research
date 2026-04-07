import os
import json
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user
from datetime import datetime
from fyp.fyp_config import fyp_cf, load_var_schema
from fyp.ingest import get_main_collection
import fyp.data_io as data_io
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.pca import calculate_scaled_pca_scores
from fyp.studies import init_study_defs, save_study_defs
from .. import explorer_backend as explorer
import pandas as pd
from ..data_service import get_viz_config, load_schema_metadata, study_cache, make_serializable, calculate_inter_coder_reliability

management_bp = Blueprint('management_bp', __name__)




def _calculate_stats(study_config, save_to_cache=True):
    """
    Calculate stats for a study using enrichment_status.parquet AND the study's specific recoded dataset.
    """


    if True:#try:
        study_name = study_config.get("STUDY_NAME")
        if not study_name:
             return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0}

        # If no collections are selected, the study is empty — skip expensive computation
        selected = study_config.get("SELECTED_DONATIONS", [])
        if not selected:
             return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0}

        # 1. Load Study Dataset (create if missing)
        #recoded_fn = f"{study_name}_recoded.parquet"
        
        # Logic adapted from explorer_backend.load_data
        #if data_io.exists(storage_location="cache", filename=recoded_fn):
        #     # Load only needed columns
        #     df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)#, columns=["item_id", "collection_id"], verbose=True)
        if True:#else:
             # Force update of the study dataset for every change of the study definition
             print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
             # create_study_recoded_dataset returns the DF
             df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=save_to_cache, verbose=False)
             if df_study is not None:
                 # Keep only what we need if it returned full DF
                 df_study = df_study[["item_id", "collection_id"]]
        
        if df_study is None or df_study.empty:
            print(f"No data found for study '{study_name}'. Removing all cached files for this study.")
            data_io.remove(storage_location="cache", filename=f"{study_name}_recoded.parquet")
            data_io.remove(storage_location="cache", filename=f"{study_name}_explorer_metadata.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_viewer_metadata.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_comp_interpretations.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")
            return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0}

        # 2. Count Unique Donations
        unique_collections = df_study['collection_id'].nunique()
        unique_videos = df_study['item_id'].nunique()

        # 3. Load Enrichment Status
        df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
        
        scraped_videos = 0
        annotated_videos = 0
        
        if df_status is not None and not df_status.empty:
            
            # Filter status df to only include items in the study
            
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
                # Fallback if we couldn't get item_id column
                study_item_ids = df_study['item_id'].unique()
                matched_status = df_status.loc[df_status.index.isin(study_item_ids)].copy()
            
            # Calculate counts
            if 'scraped_ok' in matched_status.columns:
                scraped_videos = int(matched_status['scraped_ok'].fillna(False).sum())
            if 'annotated_ok' in matched_status.columns:
                annotated_videos = int(matched_status['annotated_ok'].fillna(False).sum())
        
        return {
            "unique_videos": int(unique_videos),
            "scraped_videos": scraped_videos,
            "annotated_videos": annotated_videos,
            "unique_collections": int(unique_collections)
        }

    if False:#except Exception as e:
        print(f"Error calculating stats: {e}")
        return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0, "error": str(e)}







@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
def list_studies():
    # Reload to be safe
    if not 'study_defs' in fyp_cf:
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

    studies[study_name].update(data)
    
    # Extract ephemeral flags (don't save to disk)
    refresh_pca_flag = data.pop('REFRESH_PCA', True)
    refresh_meta_flag = data.pop('REFRESH_METADATA', True)
    
    # Also clean them from the study object in memory just in case 'update' put them there
    # (Since 'data' was passed to update, they ARE in studies[study_name] now)
    studies[study_name].pop('REFRESH_PCA', None)
    studies[study_name].pop('REFRESH_METADATA', None)

    # Update in-memory config before calculating stats
    fyp_cf['study_defs'] = studies
    
    # Calculate Stats
    print(f"Calculating stats for {study_name}...")
    stats = _calculate_stats(studies[study_name], save_to_cache=True)
    studies[study_name]['stats'] = stats
    studies[study_name]['last_updated'] = datetime.now().isoformat()
    
    
    # Update in-memory config again (optional but good for consistency)
    fyp_cf['study_defs'] = studies
    save_study_defs()



    refresh_pca = refresh_pca_flag
    # ----------------------------------------------------------------------
    # As the study dataset is updated, we may want to recalculate PCA
    # ----------------------------------------------------------------------

    # regardless, I need to delete the existing PCA file, otherwise there will be a version mismatch
    # between the study data and the PCA 
    if data_io.exists(storage_location="cache", filename=f"{study_name}_PCA.parquet"):
        data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")

    if refresh_pca and stats['annotated_videos'] > 0:
        calculate_scaled_pca_scores(study_name=study_name, load_from_cache=True, save_to_cache=True)


    refresh_explorer_metadata = refresh_meta_flag
    # ----------------------------------------------------------------------
    # --- Refresh Metadata (Viewer & Explorer) ---
    # ----------------------------------------------------------------------

    # regardless, I need to invalidate the cache and delete the existing metadata file,
    # otherwise there will be a version mismatch between the study data and the metadata 
    if data_io.exists(storage_location="cache", filename=f"{study_name}_viewer_metadata.json"):
        data_io.remove(storage_location="cache", filename=f"{study_name}_viewer_metadata.json")
    if data_io.exists(storage_location="cache", filename=f"{study_name}_explorer_metadata.json"):
        data_io.remove(storage_location="cache", filename=f"{study_name}_explorer_metadata.json")

    # --- Invalidate RAM Cache ---
    with study_cache.lock:
        if study_name in study_cache.cache:
            # We cannot easily delete from LRUCache by key if it doesn't expose del, but popping with default works
            # cachetools LRUCache supports __delitem__
            try:
                del study_cache.cache[study_name]
                print(f"Invalidated RAM cache for {study_name}")
            except KeyError:
                pass

    if refresh_explorer_metadata and stats['unique_videos'] > 0:

        # --- Refresh Metadata (Viewer & Explorer) ---
        print(f"Loading fresh data for {study_name} to generate metadata...")
        # This reads the parquet file we just ensured allows existing (or recoded)
        df, col_types = explorer.load_data(study_name, verbose=False)

        if df is not None:
            # 1. Viewer Metadata (Annotated OK + Activity Filter)
            print(f"Generating viewer metadata for {study_name}...")
            df_viewer = df[df.annotated_ok].copy()
            df_viewer = df_viewer[df_viewer['activity_type'].isin(['play', 'observe'])]
            df_viewer = df_viewer[df_viewer['item_id'].notna()]
            viewer_meta = explorer.get_metadata(df_viewer, col_types)
            
            # Add filtering/display priorities
            viewer_meta = load_schema_metadata(viewer_meta)
            
            data_io.save_json(data=make_serializable(viewer_meta), storage_location="cache", filename=f"{study_name}_viewer_metadata.json", verbose=False)


            # 2. Explorer Metadata (Annotated OK + Activity Filter)
            print(f"Generating explorer metadata for {study_name}...")
            df_explorer = df[df.annotated_ok].copy()
            df_explorer = df_explorer[df_explorer['activity_type'].isin(['play', 'observe'])]
            df_explorer = df_explorer[df_explorer['item_id'].notna()]
            explorer_meta = explorer.get_metadata(df_explorer, col_types)
            
            # Calculate Total Stats for Explorer
            # We need the viz config for binning/logging rules
            viz_config = get_viz_config()
            stats_res = explorer.get_current_stats(df_explorer, col_types, viz_config=viz_config)
            explorer_meta['total_stats'] = stats_res['stats']
            
            # Inject Source File Info
            try:
                the_recoded_file = f"{study_name}_recoded.parquet"
                if data_io.exists(storage_location="cache", filename=the_recoded_file):
                    explorer_meta['source_file'] = the_recoded_file
                    mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file))
                    explorer_meta['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    explorer_meta['source_file'] = "Unknown"
                    explorer_meta['source_file_modified'] = ""
            except Exception as e:
                explorer_meta['source_file'] = "Error"
                explorer_meta['source_file_modified'] = ""

            # Add filtering/display priorities
            explorer_meta = load_schema_metadata(explorer_meta)
            
            data_io.save_json(data = make_serializable(explorer_meta), storage_location="cache", filename=f"{study_name}_explorer_metadata.json", verbose=False)



    return jsonify({"status": "success", "study": studies[study_name]})






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
        stats = _calculate_stats(data, save_to_cache=False) # The argument to _calculate_stats is actually just used for getting STUDY_NAME inside it (line 24)
        
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
        if data_io.exists(storage_location="recoded", filename="ddp_metadata.parquet"):
            df = data_io.load_parquet(
                storage_location="recoded", 
                filename="ddp_metadata.parquet", 
                verbose=False,
            )
            
            # Filter for accepted collections
            if ('other', 'accepted') in df.columns:
                df = df[df[('other', 'accepted')]]
                
            # Load annotations
            annotations = {}
            if data_io.exists(storage_location="recoded", filename="collection_annotations.json"):
                annotations = data_io.load_json(storage_location="recoded", filename="collection_annotations.json")
                
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
            print("ddp_metadata.parquet not found")
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
        if data_io.exists(storage_location="recoded", filename="collection_annotations.json"):
            annotations = data_io.load_json(storage_location="recoded", filename="collection_annotations.json")

        annotations[str(collection_id)] = {
            "display_collection_id": data.get('display_collection_id', None),
            "annotation_tags": data.get('tags', []),
            "hidden": data.get('hidden', False)
        }

        data_io.save_json(
            data=annotations,
            storage_location="recoded",
            filename="collection_annotations.json",
            verbose=False
        )

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

    # 1. Load Enrichment Status
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
    
    ddp_metadata = data_io.load_parquet(storage_location="recoded", filename="ddp_metadata.parquet")
    if ddp_metadata is not None and not ddp_metadata.empty:
        unique_collections = int(ddp_metadata[ddp_metadata[('other','accepted')]].index.nunique())
        
    
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
        "annotate_queue_len": annotate_queue_len
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
        elif queue_type == "annotate":
            if data_io.exists(storage_location='cache', filename='to_annotate.json'):
                data_io.remove(storage_location='cache', filename='to_annotate.json')
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

    try:
        from fyp.organize_datasets import consolidate_enrichment_data
        result = consolidate_enrichment_data(force_consolidation=False, verbose=False)
        if result is None:
            return jsonify({"status": "success", "message": "No new data to consolidate."})
        return jsonify({"status": "success", "message": "Enrichment data consolidated."})
    except Exception as e:
        print(f"Error consolidating enrichment data: {e}")
        return jsonify({"error": str(e)}), 500

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
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
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
    if data_io.exists(storage_location="recoded", filename="collection_annotations.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename="collection_annotations.json",
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
        filename="collection_annotations.json",
        verbose=False
    )





@management_bp.route('/api/manage/ingestion/metadata', methods=['GET'])
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    collection_ids: list[str] = []
    all_tags: set[str] = set()

    # Get collection IDs from processed activity data
    if data_io.exists(storage_location="recoded", filename="collections_recoded.parquet"):
        df = data_io.load_parquet(
            storage_location="recoded",
            filename="collections_recoded.parquet",
            verbose=False,
        )
        if df is not None and "collection_id" in df.columns:
            collection_ids = sorted(df["collection_id"].dropna().unique().tolist())

    # Get tags from annotations
    if data_io.exists(storage_location="recoded", filename="collection_annotations.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename="collection_annotations.json",
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
