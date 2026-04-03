
from fyp.data_service import get_study_donations
import json

study = "dmrc_summer_mini"
print(f"Getting donations for {study}...")

donations = get_study_donations(study)
print(f"Found {len(donations)} donations.")
if donations:
    ids = [d.get('collection_id') for d in donations]
    print(f"First 5 IDs: {ids[:5]}")
    if ids:
        print(f"Sample Type: {type(ids[0])}")
else:
    print("No donations found!")
