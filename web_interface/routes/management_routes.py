from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
import pandas as pd
from datetime import datetime
import re
from ..hub_config import fyp_cf
import fyp.data_io as data_io
from fyp.fyp_main import initialize
from fyp.organize_datasets import create_study_recoded_dataset

management_bp = Blueprint('management_bp', __name__)




def _calculate_stats(study_config):
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
        """if data_io.exists(fyp_cf, storage_location="cache", filename=recoded_fn):
             # Load only needed columns
             df_study = data_io.load_parquet(fyp_cf, storage_location="cache", filename=recoded_fn)#, columns=["item_id", "D_donation_id"], verbose=True)
        else:"""
        # Force update of the study dataset for every change of the study definition
        if True:
             print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
             # create_study_recoded_dataset returns the DF
             df_study = create_study_recoded_dataset(cf=fyp_cf, study_name=study_name, verbose=True)
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
            matched_status = df_status.loc[df_status.index.isin(study_item_ids)]
            
            if not matched_status.empty:
                if 'scraped_ok' in matched_status.columns:
                    scraped_videos = int(matched_status['scraped_ok'].sum())
                if 'annotated_ok' in matched_status.columns:
                    annotated_videos = int(matched_status['annotated_ok'].sum())
        
        return {
            "unique_videos": int(unique_videos),
            "scraped_videos": scraped_videos,
            "annotated_videos": annotated_videos,
            "unique_donations": int(unique_donations)
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
    stats = _calculate_stats(studies[study_name])
    studies[study_name]['stats'] = stats
    studies[study_name]['last_updated'] = datetime.now().isoformat()
    
    # Save 
    data_io.save_json(fyp_cf, studies, "studies", study_defs_fn, verbose=True)
    
    # Update in-memory config if possible (optional but good for consistency)
    fyp_cf['study_defs'] = studies

    # As the study dataset is updated, we should recalculate PCA as well
    if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_PCA.parquet"):
        data_io.remove(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_PCA.parquet")
    calculate_scaled_pca_scores(cf=fyp_cf, study_name=study_name, load_from_cache=True, save_to_cache=True)
    
    # As the study dataset is updated, we should recalculate the enriched dataset as well
    if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_viewer_metadata.parquet"):
        data_io.remove(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_viewer_metadata.parquet")
    # TODO: call calculate_scaled_pca_scores in data_routes.py with the appropriate arguments to generate a new parquet file

    # As the study dataset is updated, we should recalculate the enriched dataset as well
    if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_explorer_metadata.parquet"):
        data_io.remove(cf=fyp_cf, storage_location="cache", filename=f"{study_name}_explorer_metadata.parquet")
    # TODO: call calculate_scaled_pca_scores in data_routes.py with the appropriate arguments to generate a new parquet file



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
