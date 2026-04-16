import sys

import pandas as pd

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import create_study_recoded_dataset

study_name = "paper_one"

df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

if 'item_id' not in df_status.columns:
    df_status = df_status.reset_index()
    if 'index' in df_status.columns and 'item_id' not in df_status.columns:
        df_status = df_status.rename(columns={'index': 'item_id'})

study_videos = df_study[['item_id', 'video_duration']].copy()
study_status = study_videos.merge(df_status, on='item_id', how='left')

is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)

if 'annotated_fail' in study_status.columns:
    not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
else:
    not_annotated_fail = True
    
unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail

max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
duration_ok = (study_status['video_duration'] < max_dur) | pd.isna(study_status['video_duration'])
unannotated_mask = unannotated_mask & duration_ok

unannotated_videos = study_status.loc[unannotated_mask, 'item_id'].tolist()
unannotated_videos = list(set(unannotated_videos))

print(f"Final calculate_to_annotate count for {study_name}: {len(unannotated_videos)}")
