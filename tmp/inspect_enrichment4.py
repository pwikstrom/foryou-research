import sys
import os
import pandas as pd

# Add the root directory to PYTHONPATH so fyp can be imported
sys.path.insert(0, os.path.abspath('.'))
import fyp.data_io as data_io
import json

try:
    print("\n--- Enrichment Status ---")
    if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
        df = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")
        print("Columns:", df.columns.tolist())
        if 'scrape_status' in df.columns and 'annotation_status' in df.columns:
            print("Status combinations:")
            print(df[['scrape_status', 'annotation_status']].drop_duplicates())
        elif 'scraped_ok' in df.columns and 'annotated_ok' in df.columns:
            print("Status combinations:")
            print(df[['scraped_ok', 'annotated_ok']].drop_duplicates())
            
        print("Sample row:")
        print(df.head(1).T)
    else:
        print("enrichment_status.parquet not found in recoded.")
except Exception as e:
    print("Error:", e)
