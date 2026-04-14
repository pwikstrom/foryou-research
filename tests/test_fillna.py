import os
import sys
# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pandas as pd
from fyp import data_io
from fyp.fyp_config import initialize, fyp_cf

# initialize config
initialize()

print("--- Testing fillna(False) ---")
study_name = "BBC_Jacqui"
recoded_fn = f"{study_name}_recoded.parquet"
df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)
df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")
df_status = df_status.reset_index()

status_ids = df_status['item_id'].astype("string[pyarrow]")
study_ids = df_study['item_id'].astype("string[pyarrow]")
matched_status_1 = df_status.loc[status_ids.isin(study_ids)]

print("Without fillna:")
print("scraped_ok sum:", int(matched_status_1['scraped_ok'].sum()))
print("annotated_ok sum:", int(matched_status_1['annotated_ok'].sum()))

matched_status_2 = df_status.loc[status_ids.isin(study_ids)].fillna(False).copy()
print("With fillna(False):")
print("scraped_ok sum:", int(matched_status_2['scraped_ok'].sum()))
print("annotated_ok sum:", int(matched_status_2['annotated_ok'].sum()))

from web_interface.routes.management_routes import _calculate_stats
print("\n--- Testing _calculate_stats directly ---")
config = fyp_cf['study_defs'][study_name]
config['STUDY_NAME'] = study_name
stats, _ = _calculate_stats(config, save_to_cache=False)
print(f"Stats from _calculate_stats: {stats}")
