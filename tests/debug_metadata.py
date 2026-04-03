
import json
import os

cache_dir = "/Users/<user>/fyp_local/cache"
study_name = "dmrc_summer_mini"
meta_file = os.path.join(cache_dir, f"{study_name}_explorer_metadata.json")

print(f"Inspecting {study_name} metadata for 'collection_id'...")

try:
    with open(meta_file, 'r') as f:
        meta = json.load(f)
    
    if 'collection_id' in meta:
        print("\n[Metadata] collection_id:")
        vals = meta['collection_id'].get('values', [])
        print(f"  Count: {len(vals)}")
        if vals:
             first_val = vals[0]['value']
             print(f"  First 5 values: {[v['value'] for v in vals[:5]]}")
             print(f"  Sample value type: {type(first_val)}")
    else:
        print("\n[Metadata] collection_id not found")

except Exception as e:
    print(f"Error reading metadata: {e}")
