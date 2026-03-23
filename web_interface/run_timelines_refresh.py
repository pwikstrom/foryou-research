import sys
from pathlib import Path
import pandas as pd
import json

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        # We can import from web_interface provided we are careful about dependencies
        # data_service uses fyp modules largely.
        from web_interface.data_service import check_and_update_timeline_cache, load_schema_metadata
        from fyp.studies import init_study_defs
        import fyp.data_io as data_io
        import argparse
        
        print("Starting Timeline Refresh Process...")
        
        # Init configuration
        init_study_defs()
        
        # Load schema metadata to get viz_vars
        meta = {}
        load_schema_metadata(meta)
        viz_vars = meta.get('timeline_priority', [])
        
        if 'machine_state' not in viz_vars:
            viz_vars = ['machine_state'] + viz_vars
            
        if not viz_vars:
            print("Warning: No timeline variables defined in schema (timeline_priority).")
        
        # Identify accepted donations
        all_donations = set()
        
        # Load from ddp_metadata.parquet
        if data_io.exists(storage_location="processed_activities", filename="ddp_metadata.parquet"):
            try:
                print("Loading ddp_metadata.parquet to identify accepted donations...")
                df = data_io.load_parquet(storage_location="processed_activities", filename="ddp_metadata.parquet", verbose=False)
                
                if df is not None and not df.empty:
                    # Filter for accepted donations
                    # The user specified the column is ('other', 'accepted')
                    # Check if it exists as a tuple (MultiIndex) or flattened
                    
                    found_col = False
                    if ('other', 'accepted') in df.columns:
                        print("Filtering for ('other', 'accepted') == True")
                        # Ensure boolean comparison, handling potential string/other types safely
                        accepted_mask = df[('other', 'accepted')] == True
                        all_donations = set(df[accepted_mask].index.astype(str))
                        found_col = True
                    elif 'other_accepted' in df.columns: # flatten fallback
                         print("Filtering for 'other_accepted' == True")
                         accepted_mask = df['other_accepted'] == True
                         all_donations = set(df[accepted_mask].index.astype(str))
                         found_col = True
                    
                    if not found_col:
                        print("Warning: Could not find 'accepted' column. Processing ALL donations.")
                        # Fallback to all index
                        if df.index.name == 'D_donation_id':
                             all_donations = set(df.index.astype(str))
                        elif 'D_donation_id' in df.columns:
                             all_donations = set(df['D_donation_id'].astype(str))
                             
            except Exception as e:
                print(f"Error loading ddp_metadata: {e}")
        
        # If still empty, maybe iterate studies?
        if not all_donations:
            print("No donations found in ddp_metadata. Checking studies...")
            # Fallback logic could go here if needed.
            
        print(f"Found {len(all_donations)} donations to process.")
        
        # --- PRELOAD ALL DATA FOR EFFICIENCY ---
        giant_df = None
        if all_donations:
            print("Preloading core datasets to optimize timeline compilation...")
            from fyp.organize_datasets import new_merge
            all_datasets = {}
            for k, f in [('donations', 'donations_recoded.parquet'), 
                         ('scrape', 'scrape_recoded.parquet'), 
                         ('machine_annotations', 'machine_annotations_recoded.parquet')]:
                 if data_io.exists(storage_location="recoded", filename=f):
                     all_datasets[k] = data_io.load_parquet(storage_location="recoded", filename=f, verbose=False)
                 else:
                     all_datasets[k] = pd.DataFrame()
            try:
                giant_df = new_merge(study_name=None, all_datasets=all_datasets, save_to_cache=False, verbose=False)
                if giant_df is not None and not giant_df.empty and 'D_donation_id' in giant_df.columns:
                    giant_df['D_donation_id'] = giant_df['D_donation_id'].astype(str)
                else:
                     print("Warning: giant_df is empty or missing 'D_donation_id'.")
            except Exception as e:
                print(f"Error merging core datasets: {e}")
                giant_df = None
        # ---------------------------------------
        
        valid_count = 0
        total = len(all_donations)
        
        for i, donation_id in enumerate(sorted(list(all_donations))):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Processing {donation_id} ({i+1}/{total})\" }}")
            print(f"Processing {donation_id}...")
            
            # Remove existing cache to force recalculation
            for interval in ['day', 'week', 'month']:
                filename = f"timeline_{donation_id}_{interval}.parquet"
                if data_io.exists(storage_location="cache", filename=filename):
                    data_io.remove(storage_location="cache", filename=filename)
            
            # Extract slice for this donation
            preloaded_slice = None
            if giant_df is not None and not giant_df.empty and 'D_donation_id' in giant_df.columns:
                preloaded_slice = giant_df[giant_df['D_donation_id'] == str(donation_id)]
            
            # Regenerate using the data_service logic
            # This function will regenerate if missing (which we just ensured)
            try:
                if check_and_update_timeline_cache(donation_id, viz_vars, preloaded_df=preloaded_slice):
                    valid_count += 1
            except Exception as e:
                print(f"Error processing {donation_id}: {e}")
                
        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print(f"Timeline refresh completed. {valid_count}/{total} updated successfully.")

    except ImportError as e:
        print(f"Import Error: {e}")
        # Fallback if web_interface module import fails due to path issues
        print("Ensure running from project root or correct python path.")
        sys.exit(1)
    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
