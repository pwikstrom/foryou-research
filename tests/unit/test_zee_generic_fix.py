import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(dirname(__file__)))

import json

import pandas as pd

from fyp.fyp_config import fyp_cf

print("1. Schema prep")
from web_interface.data_service import load_schema_metadata

meta = {}
load_schema_metadata(meta)
viz_vars = meta.get('timeline_priority', [])
interval = "day"
collection_id = "Zee_generic"

def load_interval_df(u_interval):
    fname = f"timeline_{collection_id}_{u_interval}.parquet"
    if data_io.exists(storage_location="cache", filename=fname):
        return data_io.load_parquet(storage_location="cache", filename=fname)
    return None

import fyp.data_io as data_io

aggs = {}
for inv in ['day', 'week', 'month']:
    df_agg = load_interval_df(inv)
    if df_agg is not None:
         aggs[inv] = df_agg

df = aggs.get(interval)
df = df.sort_values(by='period')

print("Starting Loop")
for var in viz_vars:
    has_val = f"{var}_val" in df.columns
    if has_val:
        print("  Processing val for", var)
        # Using list comprehension instead of .where
        vals = [None if pd.isna(x) else float(x) for x in df[f"{var}_val"]]
        print("  Length:", len(vals))

print("DONE!")
