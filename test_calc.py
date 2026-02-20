import pandas as pd
from fyp.fyp_config import fyp_cf
import fyp.data_io as data_io
from fyp.organize_datasets import create_study_recoded_dataset
from fyp.studies import init_study_defs
import sys

init_study_defs()
study_name = "paper_one"

df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
if df_study is None or df_study.empty:
    print(f"Dataset for study '{study_name}' could not be generated.")
    sys.exit()

df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

if 'item_id' not in df_status.columns:
    df_status = df_status.reset_index()
    if 'index' in df_status.columns and 'item_id' not in df_status.columns:
        df_status = df_status.rename(columns={'index': 'item_id'})

# Get UNIQUE videos for the study
study_videos = df_study[['item_id']].drop_duplicates().copy()
study_status = study_videos.merge(df_status, on='item_id', how='left')

total_unique_videos = len(study_status)

# Masks
success_mask = study_status['scraped_ok'] == True

failed_mask = pd.Series(False, index=study_status.index)
if 'scrape_fail' in study_status.columns:
    failed_mask = study_status['scrape_fail'] == True
elif 'scraped_fail' in study_status.columns:
    failed_mask = study_status['scraped_fail'] == True

not_scraped = pd.isna(study_status['scraped_ok']) | (study_status['scraped_ok'] == False)

if 'scrape_fail' in study_status.columns:
    not_failed = pd.isna(study_status['scrape_fail']) | (study_status['scrape_fail'] == False)
    unscraped_mask = not_scraped & not_failed
elif 'scraped_fail' in study_status.columns:
    not_failed = pd.isna(study_status['scraped_fail']) | (study_status['scraped_fail'] == False)
    unscraped_mask = not_scraped & not_failed
else:
    unscraped_mask = not_scraped

successful_scrapes = success_mask.sum()
failed_scrapes = failed_mask.sum()
remaining_to_scrape = unscraped_mask.sum()

print(f"--- Study: {study_name} ---")
print(f"Total Unique Videos: {total_unique_videos}")
print(f"Successful Scrapes: {successful_scrapes}")
print(f"Failed Scrapes: {failed_scrapes}")
print(f"Remaining to Scrape: {remaining_to_scrape}")
print(f"Sum (success + fail + remaining): {successful_scrapes + failed_scrapes + remaining_to_scrape}")

print("\n--- Overlap Analysis ---")
overlap_success_fail = (success_mask & failed_mask).sum()
overlap_success_remain = (success_mask & unscraped_mask).sum()
overlap_fail_remain = (failed_mask & unscraped_mask).sum()

print(f"Overlap (Success & Fail): {overlap_success_fail}")
print(f"Overlap (Success & Remaining): {overlap_success_remain}")
print(f"Overlap (Fail & Remaining): {overlap_fail_remain}")

# Are there any duplicates in item_id in df_status?
print("\n--- df_status item_id duplicates ---")
print("Duplicates in enrichment_status item_id:", df_status.duplicated(subset=['item_id']).sum())
print("Duplicates in study_status item_id:", study_status.duplicated(subset=['item_id']).sum())
