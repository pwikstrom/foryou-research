import sys
import os

# Add the root directory to PYTHONPATH so fyp can be imported
sys.path.insert(0, os.path.abspath('.'))

import fyp.data_io as data_io
import json
import pandas as pd

try:
    # 1. Inspect user JSONs
    print("--- User JSONs ---")
    users_files = data_io.listdir(storage_location="users", return_absolute_path=False)
    for f in users_files:
        if f.endswith('.json') and f != "roles.json":
            user_data = data_io.load_json(storage_location="users", filename=f)
            if 'machine_annotation_votes' in user_data and user_data['machine_annotation_votes']:
                print(f"User {f} votes:")
                print(json.dumps(user_data['machine_annotation_votes'], indent=2))

    # 2. Inspect enrichment status
    print("\n--- Enrichment Status ---")
    if data_io.exists(storage_location="processed_activities", filename="enrichment_status.parquet"):
        df = data_io.load_parquet(storage_location="processed_activities", filename="enrichment_status.parquet")
        print("Columns:", df.columns.tolist())
        if 'scrape_status' in df.columns and 'annotation_status' in df.columns:
            print("Status combinations:")
            print(df[['scrape_status', 'annotation_status']].drop_duplicates())
        print("Sample row:")
        print(df.head(1).T)
    else:
        print("enrichment_status.parquet not found in processed_activities.")
except Exception as e:
    print("Error:", e)
