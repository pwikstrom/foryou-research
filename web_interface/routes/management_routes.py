from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
import pandas as pd
from datetime import datetime
import re
from ..hub_config import fyp_cf
import fyp.data_io as data_io
from fyp.fyp_main import initialize
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.pca import calculate_scaled_pca_scores
from .. import explorer_backend as explorer
from ..data_service import get_viz_config, load_schema_metadata, study_cache, make_serializable

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
        if data_io.exists(fyp_cf, storage_location="cache", filename=recoded_fn):
             # Load only needed columns
             df_study = data_io.load_parquet(fyp_cf, storage_location="cache", filename=recoded_fn)#, columns=["item_id", "D_donation_id"], verbose=True)
        else:
             # Force update of the study dataset for every change of the study definition
             print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
             # create_study_recoded_dataset returns the DF
             df_study = create_study_recoded_dataset(cf=fyp_cf, study_name=study_name, save_to_cache=save_to_cache, verbose=True)
             if df_study is not None:
                 # Keep only what we need if it returned full DF
                 df_study = df_study[["item_id", "D_donation_id"]]
        
        if df_study is None or df_study.empty:
             return {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_donations": 0}

        # 2. Count Unique Donations
        unique_donations = df_study['D_donation_id'].nunique()
        unique_videos = df_study['item_id'].nunique()

        # 3. Load Enrichment Status
        df_status = data_io.load_parquet(fyp_cf, storage_location="recoded", filename='enrichment_status.parquet')
        
        scraped_videos = 0
        annotated_videos = 0
        
        if df_status is not None and not df_status.empty:
            # Join or Filter
            # enrichment_status index is item_id. 
            # We can check which item_ids from study are in status and what their status is.
            
            # Ensure index alignment
            # df_study['item_id'] might be column. df_status index is item_id.
            
            # Filter status df to only include items in the study
            study_item_ids = df_study['item_id'].unique()
            # This might be faster using isin or merge
            
            # Create a shell DF from study IDs
            # Convert to same type if needed (usually int64 or string)
            # Assuming consistency.
            
            # Use data_io or pandas merge?
            # df_status is indexed by item_id (according to inspection)
            
            # Subset of status for this study
            matched_status = df_status.loc[df_status.index.isin(study_item_ids)].fillna(False).copy()
            

            to_scrape_count = (~matched_status.scraped_ok & ~matched_status.scrape_fail).sum()
            to_annotate_count = (matched_status.scraped_ok & ~matched_status.annotated_ok).sum()


            """if not matched_status.empty:
                if 'scraped_ok' in matched_status.columns:
                    scraped_videos = int(matched_status['scraped_ok'].sum())
                if 'annotated_ok' in matched_status.columns:
                    annotated_videos = int(matched_status['annotated_ok'].sum())
                
                # --- New Estimates ---
                # 1) Unique videos to scrape (scraped_ok == False & scraped_fail == False)
                # Ensure scraped_fail exists
                if 'scraped_fail' not in matched_status.columns:
                    matched_status['scraped_fail'] = False # Default assumption if missing
                
                if 'scraped_ok' in matched_status.columns:
                    to_scrape_mask = (~matched_status['scraped_ok']) & (~matched_status['scraped_fail'])
                    to_scrape_count = int(to_scrape_mask.sum())
                else:
                    to_scrape_count = 0
                    
                # 2) Unique scraped videos to annotate (scraped_ok == True & annotated_ok == False)
                if 'scraped_ok' in matched_status.columns and 'annotated_ok' in matched_status.columns:
                    to_annotate_mask = (matched_status['scraped_ok']) & (~matched_status['annotated_ok'])
                    to_annotate_count = int(to_annotate_mask.sum())
                else:
                    to_annotate_count = 0""" 
        
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
    # Load fresh from file to ensure we have latest (though fyp_cf["study_defs"] is usually loaded at init)
    # But since we are modifying it, we should reload or use the in-memory if we keep it updated.
    # The prompt implies we should read/write `studies.json`.
    
    # Reload to be safe
    study_defs_fn = "studies.json"
    if data_io.exists(fyp_cf, "studies", study_defs_fn):
        studies = data_io.load_json(fyp_cf, "studies", study_defs_fn)
    else:
        studies = {}

    # Convert to list with name included
    studies_list = []
    for name, config in studies.items():
        # Ensure name is in config
        config['STUDY_NAME'] = name
        
        # Check if we need to calculate stats (if missing)
        # Prompt says: "calculated when a study is updated ... otherwise read directly"
        # So here we just return what is there.
        studies_list.append(config)
        
    return jsonify(studies_list)




@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
def save_study():
    if not (current_user.is_admin() or current_user.role == 'researcher'):
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    # Load existing
    study_defs_fn = "studies.json"
    if data_io.exists(fyp_cf, "studies", study_defs_fn):
        studies = data_io.load_json(fyp_cf, "studies", study_defs_fn)
    else:
        studies = {}
        
    # Update config
    if study_name not in studies:
        studies[study_name] = {}

    studies[study_name].update(data)

    # this is first save to make sure that the study is saved if read by other processes
    data_io.save_json(fyp_cf, studies, "studies", study_defs_fn, verbose=True)
    fyp_cf['study_defs'] = studies
    

    # Calculate Stats
    print(f"Calculating stats for {study_name}...")
    stats = _calculate_stats(studies[study_name], save_to_cache=True)
    studies[study_name]['stats'] = stats
    studies[study_name]['last_updated'] = datetime.now().isoformat()
    
    # Save 
    data_io.save_json(fyp_cf, studies, "studies", study_defs_fn, verbose=True)
    
    # Update in-memory config if possible (optional but good for consistency)
    fyp_cf['study_defs'] = studies


@management_bp.route('/api/manage/studies/calculate_stats', methods=['POST'])
@login_required
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    if not (current_user.is_admin() or current_user.role == 'researcher'):
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    
    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    # 1. Backup existing config
    original_config = None
    if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
        original_config = fyp_cf['study_defs'][study_name].copy()
        
    # 2. Update with Request Data (Simulation)
    if 'study_defs' not in fyp_cf:
        fyp_cf['study_defs'] = {}
    
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


    # ----------------------------------------------------------------------
    # As the study dataset is updated, we should recalculate PCA as well
    # ----------------------------------------------------------------------
    if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_PCA.parquet"):
        data_io.remove(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_PCA.parquet")
    calculate_scaled_pca_scores(cf=fyp_cf, study_name=study_name, load_from_cache=True, save_to_cache=True)


    # ----------------------------------------------------------------------
    # --- Refresh Metadata (Viewer & Explorer) ---
    # ----------------------------------------------------------------------
    
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
        
        data_io.save_json(fyp_cf, make_serializable(viewer_meta), "cache", f"{study_name}_viewer_metadata.json", verbose=True)


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
            if data_io.exists(cf=fyp_cf, storage_location="cache", filename=the_recoded_file):
                explorer_meta['source_file'] = the_recoded_file
                mtime = datetime.fromtimestamp(data_io.getmtime(cf=fyp_cf, storage_location="cache", filename=the_recoded_file))
                explorer_meta['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
            else:
                 explorer_meta['source_file'] = "Unknown"
                 explorer_meta['source_file_modified'] = ""
        except Exception as e:
             explorer_meta['source_file'] = "Error"
             explorer_meta['source_file_modified'] = ""

        # Add filtering/display priorities
        explorer_meta = load_schema_metadata(explorer_meta)
        
        data_io.save_json(fyp_cf, make_serializable(explorer_meta), "cache", f"{study_name}_explorer_metadata.json", verbose=True)



    return jsonify({"status": "success", "study": studies[study_name]})





@management_bp.route('/api/manage/studies/delete', methods=['POST'])
@login_required
def delete_study():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized - Admin only"}), 403
        
    data = request.json
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    study_defs_fn = "studies.json"
    if data_io.exists(fyp_cf, "studies", study_defs_fn):
        studies = data_io.load_json(fyp_cf, "studies", study_defs_fn)
    else:
        return jsonify({"error": "No studies file found"}), 404
        
    if study_name in studies:
        del studies[study_name]
        data_io.save_json(fyp_cf, studies, "studies", study_defs_fn, verbose=True)
        fyp_cf['study_defs'] = studies
        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404





@management_bp.route('/api/manage/donations', methods=['GET'])
@login_required
def list_donations():
    if True:#try:
        # Load ddp_metadata from ddp_main
        if data_io.exists(fyp_cf, storage_location="ddp_main", filename="ddp_metadata.parquet"):
            # Load only needed columns
            # Load using ignore_metadata to bypass list type errors
            # Request D_id and possible raw multindex name "('other', 'D_id')"
            df = data_io.load_parquet(
                fyp_cf, 
                storage_location="ddp_main", 
                filename="ddp_metadata.parquet", 
                #columns=["D_donation_id", "D_id", "('other', 'D_id')"], 
                verbose=True, 
            )
            

            donations = [f"D{u[2]:05} [{u[1]}]" for u in df[("other","D_id")].reset_index().to_records()]
            donations.sort()


            return jsonify(donations)
        else:
            print("ddp_metadata.parquet not found in ddp_main")
            return jsonify([])
            
    if False:#except Exception as e:
        print(f"Error listing donations: {e}")
        return jsonify({"error": str(e)}), 500
