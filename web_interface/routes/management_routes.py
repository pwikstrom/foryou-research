from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from datetime import datetime
from fyp.fyp_config import fyp_cf, load_var_schema
import fyp.data_io as data_io
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.pca import calculate_scaled_pca_scores
from fyp.studies import init_study_defs, save_study_defs
from .. import explorer_backend as explorer
from ..data_service import get_viz_config, load_schema_metadata, study_cache, make_serializable, calculate_inter_coder_reliability

management_bp = Blueprint('management_bp', __name__)




def _calculate_stats(study_config, save_to_cache=True):
    """
    Calculate stats for a study using enrichment_status.parquet AND the study's specific recoded dataset.
    """


    if True:#try:
        study_name = study_config.get("STUDY_NAME")
        if not study_name:
             return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_donations": 0}

        # 1. Load Study Dataset (create if missing)
        recoded_fn = f"{study_name}_recoded.parquet"
        
        # Logic adapted from explorer_backend.load_data
        #if data_io.exists(storage_location="cache", filename=recoded_fn):
        #     # Load only needed columns
        #     df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)#, columns=["item_id", "D_donation_id"], verbose=True)
        if True:#else:
             # Force update of the study dataset for every change of the study definition
             print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
             # create_study_recoded_dataset returns the DF
             df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=save_to_cache, verbose=True)
             if df_study is not None:
                 # Keep only what we need if it returned full DF
                 df_study = df_study[["item_id", "D_donation_id"]]
        
        if df_study is None or df_study.empty:
            print(f"No data found for study '{study_name}'. Removing all cached files for this study.")
            data_io.remove(storage_location="cache", filename=f"{study_name}_recoded.parquet")
            data_io.remove(storage_location="cache", filename=f"{study_name}_explorer_metadata.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_viewer_metadata.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_comp_interpretations.json")
            data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")
            return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_donations": 0}

        # 2. Count Unique Donations
        unique_donations = df_study['D_donation_id'].nunique()
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
                    matched_status = df_status.loc[status_ids.isin(study_ids)].fillna(False).copy()
                except Exception as e:
                    print(f"Error during robust index matching: {e}. Falling back to standard matching.")
                    study_item_ids = df_study['item_id'].unique()
                    matched_status = df_status.loc[df_status.index.isin(study_item_ids)].fillna(False).copy()
            else:
                # Fallback if we couldn't get item_id column
                study_item_ids = df_study['item_id'].unique()
                matched_status = df_status.loc[df_status.index.isin(study_item_ids)].fillna(False).copy()
            
            to_scrape_list = matched_status[(~matched_status.scraped_ok & ~matched_status.scrape_fail)].index.to_list()
            to_annotate_list = matched_status[(matched_status.scraped_ok & ~matched_status.annotated_ok)].index.to_list()
            to_scrape_count = len(to_scrape_list)
            to_annotate_count = len(to_annotate_list)
            if data_io.exists(storage_location='cache', filename='to_scrape.json'):
                to_scrape_list_old = data_io.load_json(storage_location='cache', filename='to_scrape.json')
                to_scrape_list = list(set(to_scrape_list + to_scrape_list_old))
                data_io.save_json(storage_location='cache', filename='to_scrape.json', data=to_scrape_list)
            else:
                data_io.save_json(storage_location='cache', filename='to_scrape.json', data=to_scrape_list)
            if data_io.exists(storage_location='cache', filename='to_annotate.json'):
                to_annotate_list_old = data_io.load_json(storage_location='cache', filename='to_annotate.json')
                to_annotate_list = list(set(to_annotate_list + to_annotate_list_old))
                data_io.save_json(storage_location='cache', filename='to_annotate.json', data=to_annotate_list)
            else:
                data_io.save_json(storage_location='cache', filename='to_annotate.json', data=to_annotate_list)
            print(f"In scrape queue: {len(to_scrape_list)}  |  In annotation queue: {len(to_annotate_list)}")
            
            # Calculate counts
            if 'scraped_ok' in matched_status.columns:
                scraped_videos = int(matched_status['scraped_ok'].sum())
            if 'annotated_ok' in matched_status.columns:
                annotated_videos = int(matched_status['annotated_ok'].sum())
        
        return {
            "unique_videos": int(unique_videos),
            "scraped_videos": scraped_videos,
            "annotated_videos": annotated_videos,
            "unique_donations": int(unique_donations),
            "to_scrape_count": to_scrape_count if 'to_scrape_count' in locals() else 0,
            "to_annotate_count": to_annotate_count if 'to_annotate_count' in locals() else 0
        }

    if False:#except Exception as e:
        print(f"Error calculating stats: {e}")
        return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_donations": 0, "error": str(e)}







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
        df, col_types = explorer.load_data(fyp_cf, study_name, verbose=True)

        if df is not None:
            # 1. Viewer Metadata (Scraped OK)
            print(f"Generating viewer metadata for {study_name}...")
            df_viewer = df[df.scraped_ok].copy()
            viewer_meta = explorer.get_metadata(df_viewer, col_types)
            
            # Add filtering/display priorities
            viewer_meta = load_schema_metadata(viewer_meta)
            
            data_io.save_json(data=make_serializable(viewer_meta), storage_location="cache", filename=f"{study_name}_viewer_metadata.json", verbose=True)


            # 2. Explorer Metadata (Annotated OK)
            print(f"Generating explorer metadata for {study_name}...")
            df_explorer = df[df.annotated_ok].copy()
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
            
            data_io.save_json(data = make_serializable(explorer_meta), storage_location="cache", filename=f"{study_name}_explorer_metadata.json", verbose=True)



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





@management_bp.route('/api/manage/donations', methods=['GET'])
@login_required
def list_donations():
    if True:#try:
        # Load ddp_metadata from ddp_main
        if data_io.exists(storage_location="ddp_main", filename="ddp_metadata.parquet"):
            # Load only needed columns
            # Load using ignore_metadata to bypass list type errors
            # Request D_id and possible raw multindex name "('other', 'D_id')"
            df = data_io.load_parquet(
                storage_location="ddp_main", 
                filename="ddp_metadata.parquet", 
                #columns=["D_donation_id", "D_id", "('other', 'D_id')"], 
                verbose=True, 
            )
            

            # Filter for accepted donations
            if ('other', 'accepted') in df.columns:
                df = df[df[('other', 'accepted')]]
            
            donations = [f"D{u[2]:05} [{u[1]}]" for u in df[("other","D_id")].reset_index().to_records()]
            donations.sort()


            return jsonify(donations)
        else:
            print("ddp_metadata.parquet not found in ddp_main")
            return jsonify([])
            
    if False:#except Exception as e:
        print(f"Error listing donations: {e}")





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
    unique_donations = 0
    
    if enrichment_status is not None and not enrichment_status.empty:
        total_videos = len(enrichment_status)
        if 'scraped_ok' in enrichment_status.columns:
            scraped_videos = int(enrichment_status['scraped_ok'].sum())
        if 'annotated_ok' in enrichment_status.columns:
            annotated_videos = int(enrichment_status['annotated_ok'].sum())
    
    ddp_metadata = data_io.load_parquet(storage_location="ddp_main", filename="ddp_metadata.parquet")
    if ddp_metadata is not None and not ddp_metadata.empty:
        unique_donations = int(ddp_metadata[ddp_metadata[('other','accepted')]].index.nunique())
        
    
    # 2. Get Queue Lengths
    scrape_queue_len = 0
    annotate_queue_len = 0
    
    if data_io.exists(storage_location='cache', filename='to_scrape.json'):
        q = data_io.load_json(storage_location='cache', filename='to_scrape.json')
        if isinstance(q, list): scrape_queue_len = len(q)
        
    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)
        
    return jsonify({
        "total_videos": total_videos,
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_donations": unique_donations,
        "scrape_queue_len": scrape_queue_len,
        "annotate_queue_len": annotate_queue_len
    })






@management_bp.route('/api/manage/enrichment/empty_queues', methods=['POST'])
@login_required
def empty_enrichment_queues():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
        
    try:
        if data_io.exists(storage_location='cache', filename='to_scrape.json'):
            data_io.remove(storage_location='cache', filename='to_scrape.json')
            
        if data_io.exists(storage_location='cache', filename='to_annotate.json'):
            data_io.remove(storage_location='cache', filename='to_annotate.json')
            
        return jsonify({"status": "success", "message": "Queues emptied."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/schema/reload', methods=['POST'])
@login_required
def reload_schema():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        global fyp_cf
        fyp_cf = load_var_schema(fyp_cf, verbose=True)
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
