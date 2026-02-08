
import sys
import os
import pandas as pd
import pyarrow.parquet as pq

# Add project root to path
here = os.getcwd().split("/")
while not os.path.exists(os.path.join("/".join(here),"__proj__.py")):
    here.pop()
abs_project_root_path = os.path.join("/".join(here))
sys.path.append(abs_project_root_path)

from fyp.fyp_config import fyp_cf
import fyp.data_io as data_io
from web_interface.explorer_backend import load_data

def inspect_data():
    study_name = "chenglong"
    print(f"Checking for {study_name}_recoded.parquet in cache...")
    
    if data_io.exists(storage_location="cache", filename=f"{study_name}_recoded.parquet"):
        print("File found.")
        
        # Load using data_io to see what we get
        df = data_io.load_parquet(storage_location="cache", filename=f"{study_name}_recoded.parquet")
        print("\n--- DataFrame Info ---")
        print(df.info())
        print("\n--- Dtypes ---")
        print(df.dtypes)
        
        # Inspect Parquet Schema directly
        print("\n--- Parquet Schema ---")
        try:
             # resolving path manually to use pq.read_schema
             primary, _, mode, _ = data_io._resolve_paths(storage_location="cache", filename=f"{study_name}_recoded.parquet")
             if mode == 'local':
                 schema = pq.read_schema(primary)
                 print(schema)
                 print("\nInput Schema Names:", schema.names)
                 print("Input Schema Types:", schema.types)
             else:
                 print("File is in GCS, skipping direct local read for schema (or implement GCS read).")
        except Exception as e:
            print(f"Could not read parquet schema directly: {e}")

        # Run load_data
        print("\n--- Running load_data ---")
        df_loaded, column_types = load_data(study_name, verbose=True)
        
        print("\n--- Inferred Column Types ---")
        for col, dtype in column_types.items():
            print(f"{col}: {dtype}")

    else:
        print("File NOT found in cache.")
        print(f"Cache path: {fyp_cf['paths']['cache']}")

if __name__ == "__main__":
    inspect_data()
