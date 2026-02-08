
import sys
import os
# Adjust path to find fyp module
sys.path.append('/Users/<user>/GitHub_main/fyp_main_v02')

from fyp.fyp_config import fyp_cf
import fyp.data_io as data_io

print(f"Cache Path: {fyp_cf['paths'].get('cache')}")
print(f"Local Data Path: {fyp_cf['paths'].get('local_data')}")

print("\nFiles in Cache:")
try:
    files = data_io.listdir("cache")
    for f in files:
        if "recoded" in f or "metadata" in f:
            print(f)
except Exception as e:
    print(f"Error listing cache: {e}")

# Try to load a metadata file if one exists
print("\nChecking metadata content for D_donation_id:")
found = False
try:
    files = data_io.listdir("cache")
    for f in files:
        if f.endswith("_explorer_metadata.json"):
            print(f"Loading {f}...")
            meta = data_io.load_json("cache", f)
            if 'D_donation_id' in meta:
                print(f"D_donation_id type: {meta['D_donation_id'].get('type')}")
                vals = meta['D_donation_id'].get('values', [])
                print(f"D_donation_id value count: {len(vals)}")
                # print first few
                print(f"First 5 values: {vals[:5]}")
            else:
                print("D_donation_id not in metadata")
            found = True
            break
except Exception as e:
    print(f"Error checking metadata: {e}")

if not found:
    print("No metadata file found to inspect.")
