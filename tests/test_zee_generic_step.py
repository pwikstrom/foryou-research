import sys
from os.path import abspath, dirname, join
sys.path.insert(0, abspath(dirname(__file__)))

import json
import pandas as pd
from web_interface.data_service import get_timeline_data, check_and_update_timeline_cache, load_schema_metadata
from fyp.fyp_config import fyp_cf

print("1. Schema prep")
schema = fyp_cf.get('var_schema', {})
meta = {}
load_schema_metadata(meta)
viz_vars = meta.get('timeline_priority', [])
schema_map = meta.get('schema_map', {})

print("2. Check Cache")
check_and_update_timeline_cache("Zee_generic", viz_vars)

print("3. Get timeline data direct")
interval = "day"
collection_id = "Zee_generic"
period_counts = {}

def load_interval_df(u_interval):
    import fyp.data_io as data_io
    fname = f"timeline_{collection_id}_{u_interval}.parquet"
    if data_io.exists(storage_location="cache", filename=fname):
        return data_io.load_parquet(storage_location="cache", filename=fname)
    return None

import fyp.data_io as data_io
aggs = {}
for inv in ['day', 'week', 'month']:
    print("Loading", inv)
    df_agg = load_interval_df(inv)
    if df_agg is not None:
         period_counts[inv] = len(df_agg)
         aggs[inv] = df_agg
    else:
         period_counts[inv] = 0

print("4. Prepare Result")
df = aggs.get(interval)

print("5. Sort by period")
df = df.sort_values(by='period')
dates = df['period'].tolist()

date_labels = []
for d_str in dates:
    date_labels.append(str(d_str))

print("6. Loop vars")
variables = {}
for var in viz_vars:
    print("  var", var)
    has_val = f"{var}_val" in df.columns
    has_counts = f"{var}_counts" in df.columns
    
    if not has_val and not has_counts:
        continue
        
    use_log = False
    valid_counts = df.get(f"{var}_valid", pd.Series([0]*len(df))).tolist()
    video_counts = df['video_count'].tolist()
    
    if has_val:
        print("    val")
        vals = df[f"{var}_val"].where(pd.notnull(df[f"{var}_val"]), None).tolist()
    elif has_counts:
        print("    counts")
        counts_list = []
        global_cat_counts = {}
        for json_str in df[f"{var}_counts"]:
            try:
                if json_str and isinstance(json_str, str):
                    c_dict = json.loads(json_str)
                else:
                    c_dict = {}
            except:
                c_dict = {}
            counts_list.append(c_dict)
            for k, v in c_dict.items():
                global_cat_counts[k] = global_cat_counts.get(k, 0) + v
        top_cats = sorted(global_cat_counts.keys(), key=lambda x: global_cat_counts[x], reverse=True)

print("Done")
