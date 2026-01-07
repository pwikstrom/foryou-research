
from sys import path as sys_path
from os import getcwd
from os.path import exists, join

# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
here = getcwd().split("/")
while not exists(join("/".join(here),"__proj__.py")):
    here.pop()
abs_project_root_path = join("/".join(here))

# add project root path to PATH since the modules are located in the project structure
sys_path.append(abs_project_root_path)

import fyp
from fyp.fyp_main import initialize, connect_to_google
import fyp.data_io as data_io
import pandas as pd
from os import listdir 
from datetime import datetime

fyp_cf = initialize()
if fyp_cf['data_io']['use_gcs_for_data']:
    fyp_cf = connect_to_google(fyp_cf)


# download recent donations
print("Checking for new donations during the past week...")
fyp.download_recent_donations(
    hours_back=24*28,
    cf=fyp_cf)

print("---------------------------------------------------------------------------------------------")


# load raw JSON from raw file directory

raw_data = {}
donation_dates ={}
# List all files in the directory
# Use data_io.listdir to support GCS or Local transparency
for filename in data_io.listdir(fyp_cf, "ddp_raw"):
    # Check if the file is a JSON file
    
    mod_time_timestamp = data_io.getmtime(fyp_cf, "ddp_raw", filename)
    donation_dates[filename] = datetime.fromtimestamp(mod_time_timestamp)
    
    # Read the JSON file
    try:
        data = data_io.load_json(fyp_cf, "ddp_raw", filename)
        if data:
            raw_data[filename] = data
            
    except:
        print(f"failed to load {filename}")

print(f"\nFound {len(raw_data)} raw JSON files in the raw folder")

filenames_in_raw_folder = list(raw_data.keys())

print("---------------------------------------------------------------------------------------------")


# only keep JSON donations that are recognised as TikTok data logs

raw_donation_ids = list(raw_data.keys())
for donation_id in raw_donation_ids:
    raw_data_donation_top_keys = list(raw_data[donation_id].keys())
    if 'ad_preferences' in raw_data_donation_top_keys or 'CONTENT_INTERACTION' in raw_data_donation_top_keys:
        del raw_data[donation_id]

print(f"\nConfirmed {len(raw_data)} donations in the raw folder as TikTok data")

print("---------------------------------------------------------------------------------------------")


print("\nDropping exact duplicates among the new donations")
# drop duplicate donations
filtered_raw_data = fyp.drop_duplicates_donations(raw_data)
print(f"Number of donations after dropping duplicates: {len(filtered_raw_data)}")

print("---------------------------------------------------------------------------------------------")


processed_donations = []
for ff in filtered_raw_data.keys():
    # Construct expected filename
    parquet_filename = f"{ff}.parquet"

    if data_io.exists(fyp_cf, "ddp_processed", parquet_filename):
        print(f"Found a processed donation saved as parquet: {ff}. Loading...")
        donation_as_df = data_io.load_parquet(fyp_cf, "ddp_processed", parquet_filename)
    else:
        print(f"Found a new donation JSON: {ff}. Transforming to a dataframe...")
        # call the function to flatten the JSONs, turn them into a dataframe and fix them up a bit
        donation_as_df = fyp.transform_data_to_df({ff:filtered_raw_data[ff]}, donation_item_id=0)[0]
        # save_parquet handles type conversion
        data_io.save_parquet(fyp_cf, donation_as_df, "ddp_processed", parquet_filename)

    processed_donations.append(donation_as_df)

# bring all the processed donations together into a single dataframe
new_events_df = pd.concat(processed_donations, ignore_index=True)

print("---------------------------------------------------------------------------------------------")



if "donation_id" in new_events_df.columns:
    new_events_df["donation_date"] = new_events_df.donation_id.map(donation_dates)

# calculate the donation stats
new_donation_stats = fyp.calc_donated_items_stats(new_events_df)

print(f"Shape of the new events dataframe: {new_events_df.shape}")

print("---------------------------------------------------------------------------------------------")






# drop donations that have fewer watch events than a certain value (5)

if len(new_donation_stats) > 0:

    print(f"\nDropping donations with fewer than 5 watch events")
    
    # create list of donations to be dropped and drop donations which has a very small number of watched videos
    donations_to_drop = []
    
    donations_to_drop += list(new_donation_stats["counts"][(new_donation_stats["counts","watch"]<5)].index)

    # drop the donations we don't want
    new_events_df = new_events_df[~new_events_df.donation_id.isin(donations_to_drop)].copy()

    # recalculate the donation stats
    new_donation_stats = fyp.calc_donated_items_stats(new_events_df)

    print(f"Shape of the new events dataframe: {new_events_df.shape}")

    print("---------------------------------------------------------------------------------------------")





if len(new_donation_stats) > 0:

    print(f"\nOnly keeping one of multiple overlapping (similar) donations")
    
    # check for similarities between the new donations by looking for the same timestamps in the donations. 
    # The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates
    # first include all kinds of events, then exclude the watch events

    a1 = fyp.identify_similar_donations(new_events=new_events_df, old_events=new_events_df, dont_check_these_cols=[])
    a2 = fyp.identify_similar_donations(new_events=new_events_df, old_events=new_events_df, dont_check_these_cols=["watch"])
    new_donations_to_drop = (a1["new_drops"] | a2["new_drops"])
    old_donations_to_drop = (a1["old_drops"] | a2["old_drops"])
    donations_to_drop = new_donations_to_drop | old_donations_to_drop
    
    # drop the events in these donations
    new_events_df = new_events_df[~new_events_df.donation_id.isin(donations_to_drop)].copy()

    # calculate the donation stats again
    new_donation_stats = fyp.calc_donated_items_stats(new_events_df)

    print(f"Shape of the new events dataframe: {new_events_df.shape}")

    print("---------------------------------------------------------------------------------------------")






# drop duplicates based on a set of columns
deduped_donation_item_ids = new_events_df.drop([
    "value_list",
    "variable_list",
    "ts_jiggled"],axis=1).drop_duplicates(subset=[
        "donation_id",
        "feature_name",
        "date",
        "primary_label",
        "primary_value",
        "timestamp",
        "donation_date"]).index.unique()

print(f"Deduped donation item IDs: {len(deduped_donation_item_ids):,}")

print("---------------------------------------------------------------------------------------------")


print(f"Events in the dataframe before deduplication: {len(new_events_df):,}")
new_events_df = new_events_df[new_events_df.index.isin(deduped_donation_item_ids)].copy()
print(f"Deduplicating the updated events dataframe {len(new_events_df):,}")

print("---------------------------------------------------------------------------------------------")


print(f"Shape of the updated events dataframe: {new_events_df.shape}")

# calculate the donation stats again
merged_donation_stats = fyp.calc_donated_items_stats(new_events_df)

print("---------------------------------------------------------------------------------------------")





print("Saved the updated events dataframe to disk")
data_io.save_parquet(fyp_cf, new_events_df, "ddp_main", "all_participant_events.parquet")

print("---------------------------------------------------------------------------------------------")





