
from fyp.data_service import get_study_s
import json

study = "dmrc_summer_mini"
print(f"Getting s for {study}...")

s = get_study_s(study)
print(f"Found {len(s)} s.")
if s:
    ids = [d.get('collection_id') for d in s]
    print(f"First 5 IDs: {ids[:5]}")
    if ids:
        print(f"Sample Type: {type(ids[0])}")
else:
    print("No s found!")
