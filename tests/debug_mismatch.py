
import pandas as pd
import json
import os

cache_dir = "/Users/<user>/fyp_local/cache"
study_name = "dmrc_summer_mini"
parquet_file = os.path.join(cache_dir, f"{study_name}_recoded.parquet")
metadata_file = os.path.join(cache_dir, f"{study_name}_explorer_metadata.json")

print(f"Comparing Parquet vs Metadata for {study_name}...")

# 1. Load "get_study_donations" equivalent
try:
    df = pd.read_parquet(parquet_file, engine='pyarrow', dtype_backend='pyarrow')
    print(f"Loaded parquet. Shape: {df.shape}")
    if 'D_donation_id' in df.columns:
        # Simulate get_study_donations logic EXACTLY
        donations_df = df[['D_donation_id']].drop_duplicates()
        valid_donation_ids = set()
        
        print(f"Iterating {len(donations_df)} rows...")
        sample_val = None
        for i, (idx, row) in enumerate(donations_df.iterrows()):
            val = row['D_donation_id']
            if i == 0:
                print(f"Row 0 value: {val!r}")
                print(f"Row 0 type: {type(val)}")
                print(f"Row 0 str(): {str(val)!r}")
                sample_val = str(val)
                
            if pd.notna(val):
                valid_donation_ids.add(str(val))
        
        print(f"Set of valid IDs (len): {len(valid_donation_ids)}")
        if sample_val:
             print(f"Sample Valid ID in Set: {sample_val!r}")
        
    else:
        print("D_donation_id NOT in parquet columns!")
        valid_donation_ids = set()

except Exception as e:
    print(f"Error reading parquet: {e}")
    valid_donation_ids = set()

# 2. Load Metadata
try:
    with open(metadata_file, 'r') as f:
        meta = json.load(f)
    
    if 'D_donation_id' in meta and 'values' in meta['D_donation_id']:
        meta_vals = meta['D_donation_id']['values']
        print(f"Metadata Values Count: {len(meta_vals)}")
        if meta_vals:
            print(f"Sample Metadata Value: '{meta_vals[0]['value']}' (Type: {type(meta_vals[0]['value'])})")
            
        # Simulate Filter
        filtered = [v for v in meta_vals if str(v['value']) in valid_donation_ids]
        print(f"Filtered Result Count: {len(filtered)}")
        
        if len(filtered) == 0:
            print("MISMATCH DETECTED! Zero items passed filter.")
            # Debug Mismatch
            if meta_vals and valid_donation_ids:
                ex_meta = str(meta_vals[0]['value'])
                print(f"Checking specific mismatch for: '{ex_meta}'")
                if ex_meta in valid_donation_ids:
                    print("  It's in the set!")
                else:
                    print("  NOT in the set.")
                    # Check for invisible chars
                    print(f"  Ordinals Metadata: {[ord(c) for c in ex_meta]}")
                    # Find closest match?
                    for v in list(valid_donation_ids)[:5]:
                        print(f"  Compare against: '{v}' Ords: {[ord(c) for c in v]}")
        else:
            print("Filter seems to work locally.")

    else:
        print("D_donation_id not in metadata.")

except Exception as e:
    print(f"Error reading metadata: {e}")
