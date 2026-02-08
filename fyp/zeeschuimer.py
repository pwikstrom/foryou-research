#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import re
import pandas as pd
from copy import copy
from fyp.utils import extract_and_join_subkeys, clean_url, pretty_str_seconds
from fyp.recode_variables import recode_events_df, extract_local_time_features, rename_columns
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs, save_study_defs

import numpy as np
import subprocess
import textwrap
import datetime as _dt



############################################################################################################
###                     Process Zeeschuimer metadata
############################################################################################################







def refine_one_raw_zeeschuimer_log(
    item_list_or_ndjson_path: str | list[dict] = None,
    verbose: bool = False):

    if isinstance(item_list_or_ndjson_path, str):
        item_list = data_io.read_ndjson_file(storage_location="zeeschuimer_raw", filename = item_list_or_ndjson_path)
    elif isinstance(item_list_or_ndjson_path, list):
        item_list = item_list_or_ndjson_path
    else:
        print("Input must be a list of dictionaries or a path to an ndjson file.")
        return pd.DataFrame()
        
    # if the list is empty, return an empty dataframe
    if len(item_list) == 0:
        return pd.DataFrame()

    # normalize the list of dictionaries into a dataframe and convert the item_id to an integer
    zeeschuimer_logs_df = pd.json_normalize(item_list)

    # drop the items with corrupt item_ids
    zeeschuimer_logs_df = copy(zeeschuimer_logs_df[zeeschuimer_logs_df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)])

    zeeschuimer_logs_df.item_id = zeeschuimer_logs_df.item_id.astype("string[pyarrow]")


    # the TikTok data has a lot of variables that we don't
    # need or need to simplify. This dict is used to iterate over the columns in the DF
    # and indicate that the data should be simplified into a string (or dropped). 
    # The utility function extract_and_join_subkeys
    # is used to extract the subkeys and join them into a single string.
    columns_to_fix = {'data.contents': ['desc'],
    'data.video.bitrateInfo': [],
    'data.video.shareCover': [],
    'data.video.subtitleInfos': [],
    'data.challenges': ['desc'],
    'data.stickersOnItem': ['stickerText'],
    'data.textExtra': 'hashtagName',
    'data.ad_info.about_this_ad_info.about_this_ad_items': ["orientation_info"],
    'data.effectStickers': ['name'],
    'data.videoSuggestWordsList.video_suggest_words_struct': ['words'],
    'data.anchors': ['description','keyword'],
    'data.warnInfo': ['key'],
    'data.imagePost.cover.imageURL.urlList': [],
    'data.imagePost.images': [],
    'data.imagePost.shareCover.imageURL.urlList': []
    }


    if verbose:
        print(f"All observations in the zeeschuimer log. Shape: {zeeschuimer_logs_df.shape}")



    # iterate over the columns_in columns_to_fix
    for a_column_to_fix in columns_to_fix:
        # if the column is in the DF, apply the extract_and_join_subkeys function
        if a_column_to_fix in zeeschuimer_logs_df.columns:
            zeeschuimer_logs_df[a_column_to_fix] = zeeschuimer_logs_df[a_column_to_fix].apply(lambda x:extract_and_join_subkeys(x, columns_to_fix[a_column_to_fix]))
        
    # iterate over the columns_in columns_to_fix (again)
    for ff in columns_to_fix:
        # if the column is in the DF and the list of subkeys in 'columns_to_fix' is empty, drop the column
        if columns_to_fix[ff] == [] and ff in zeeschuimer_logs_df.columns:
            del zeeschuimer_logs_df[ff]
            
    # the column 'source_url' is a string that contains the url and lots of useful metadata
    # that we can extract into separate columns. This is done with the function clean_url
    # and the result is a dataframe with the source_url metadata as separate columns.
    source_details = []
    for ii in zeeschuimer_logs_df.index:
        source_details += [clean_url(zeeschuimer_logs_df['source_url'][ii])]        
    source_details = pd.DataFrame(source_details)

    # merge the source_details dataframe with the zeeschuimer_logs_df dataframe and drop the source_url column
    zeeschuimer_logs_df = pd.merge(left=zeeschuimer_logs_df, right=source_details, left_index=True, right_index=True)
    del zeeschuimer_logs_df["source_url"]

    # convert the 'data.createTime' and 'timestamp_collected' columns to datetime
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].astype(np.int64)
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].apply(lambda x:_dt.datetime.fromtimestamp(x))
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].astype(np.int64)
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].apply(lambda x: _dt.datetime.fromtimestamp(np.int64(x/1000)))

    # replace commas and newlines in object columns with spaces
    object_cols = [c for c in zeeschuimer_logs_df.columns if zeeschuimer_logs_df[c].dtype == 'object']
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace(","," ") if type(x)==str else x)
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace("\n"," ") if type(x) == str else x)

    # drop columns with mostly NAs.
    zeeschuimer_logs_df.dropna(axis='columns', thresh=int(len(zeeschuimer_logs_df)*0.95), inplace=True)

    # calculate local time features such as hour, day segment, weekday, etc
    zeeschuimer_logs_df = extract_local_time_features(
        some_events_df_in = zeeschuimer_logs_df,
        kind_of_log = 'baseline',
        verbose = verbose)


    # only keeping videos from the FYP page not the explore page
    zeeschuimer_logs_df = zeeschuimer_logs_df[zeeschuimer_logs_df.source_platform_url.isin(['https://www.tiktok.com/en','https://www.tiktok.com/','https://www.tiktok.com/foryou'])].copy()
    if verbose:
        print(f"Only keeping observations from TikTok's ForYou page, yielding shape {zeeschuimer_logs_df.shape}")


    # rename columns
    zeeschuimer_logs_df = zeeschuimer_logs_df.rename(columns={c:"B_"+c if not c in ["item_id","event_id"] and not re.match(r"^[A-Z]_", c) else c for c in zeeschuimer_logs_df.columns}).copy()
    zeeschuimer_logs_df = rename_columns(zeeschuimer_logs_df.copy()).copy()


    # Sort by timestamp and reset index
    zeeschuimer_logs_df.sort_values("T_local_timestamp", inplace=True)
    zeeschuimer_logs_df.reset_index(drop=True, inplace=True)


    # Assign session IDs: each script gets a unique session ID at merge - this is just a place holder
    if verbose:
        print("Adding session details to zeeschuimer data")
    zeeschuimer_logs_df["session_id"] = 1
    zeeschuimer_logs_df["event_order_in_session"] = zeeschuimer_logs_df.index
    zeeschuimer_logs_df["event_pos_in_session"] = (zeeschuimer_logs_df["event_order_in_session"]) / (len(zeeschuimer_logs_df)-1)
    

    if verbose:
        print(f"Current shape: {zeeschuimer_logs_df.shape}")

    # only keep columns as defined by the variable schema
    dropped_vars_str = textwrap.wrap(", ".join(list(set(zeeschuimer_logs_df.columns) - set(fyp_cf['var_schema'].variable_name))), width=120)
    relevant_baseline_cols = [c for c in fyp_cf['var_schema'].variable_name if c in zeeschuimer_logs_df.columns]
    zeeschuimer_logs_df = zeeschuimer_logs_df[relevant_baseline_cols].copy()

    if verbose and dropped_vars_str:
        joined_vars = '\n'.join(dropped_vars_str)
        print(f"Dropped these columns, which are not in the variable schema:\n{joined_vars}\nCurrent shape: {zeeschuimer_logs_df.shape}")

    zeeschuimer_logs_df = zeeschuimer_logs_df.copy()


    zeeschuimer_logs_df = recode_events_df(
        study_dataset = zeeschuimer_logs_df,
        drop_single_value_cols = False,
        verbose = verbose
        )
    

    if verbose:
        print(f"Final shape: {zeeschuimer_logs_df.shape}")
        print("------------------------------------------------\n\n")

    return zeeschuimer_logs_df









def refine_and_save_all_raw_zeeschuimer_logs(verbose=False):

    result = {}
    
    raw_zeeschuimer_files = data_io.listdir(
        storage_location="zeeschuimer_raw",
        return_absolute_path=False,
        verbose=False)
    raw_zeeschuimer_files = [u for u in raw_zeeschuimer_files if u.endswith(".ndjson")]
    result["raw_files"] = len(raw_zeeschuimer_files)

    refined_zeeschuimer_files = data_io.listdir(
        storage_location="zeeschuimer_refined",
        return_absolute_path=False,
        verbose=False)
    refined_zeeschuimer_files = [u for u in refined_zeeschuimer_files if u.endswith(".parquet")]
    result["refined_files_before"] = len(refined_zeeschuimer_files)

    #ff = []
    for u in raw_zeeschuimer_files:
        if u.endswith(".ndjson"):
            if u.replace(".ndjson",".parquet") in refined_zeeschuimer_files:
                #print("    [DATA_IO] Skipping already refined file: {}".format(u))
                continue
            if verbose:
                print(f"Refining: {u}")
            new_flat = refine_one_raw_zeeschuimer_log(
                item_list_or_ndjson_path = u,
                verbose=verbose)
            #ff += [new_flat.copy()]
            data_io.save_parquet(df=new_flat, filename=u.replace(".ndjson",".parquet"), storage_location="zeeschuimer_refined", verbose=verbose)

    refined_zeeschuimer_files = data_io.listdir(
        storage_location="zeeschuimer_refined",
        return_absolute_path=False,
        verbose=False)
    refined_zeeschuimer_files = [u for u in refined_zeeschuimer_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_zeeschuimer_files)

    return result













def consolidate_zeeschuimer_logs(
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose = False):


    top_verbose = True

    if top_verbose:
        print("Checking for new raw zeeschuimer logs that needs refining...")
    result = refine_and_save_all_raw_zeeschuimer_logs(verbose=verbose)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            print("    ...all files already refined.")
        else:
            print(f"    ...refined {result["refined_files_after"] - result["refined_files_before"]} files.")


    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="dataset_meta.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="dataset_meta.json",verbose=verbose)
        if verbose:
            print("Dataset meta loaded")
    else:
        dataset_meta = {"zeeschuimer": {"filenames": []}}

    refined_zeeschuimer_files = data_io.listdir(
        storage_location="zeeschuimer_refined",
        return_absolute_path=False,
        verbose=False)
    refined_zeeschuimer_files = [u for u in refined_zeeschuimer_files if u.endswith(".parquet")]


    latest_filename_list = dataset_meta.get("zeeschuimer", {}).get("filenames", [])

    if not force_consolidation and set(refined_zeeschuimer_files) == set(latest_filename_list):
        if top_verbose:
            print("No new refined zeeschuimer files found. No need to consolidate.")
            if return_saved_data:
                if verbose: print("Returning existing file.")
                return False, data_io.load_parquet(storage_location="recoded", filename="zeeschuimer_recoded.parquet")
        return False, None
    


    # load and concatenate all refined files
    if top_verbose:
        print(f"Loading refined zeeschuimer logs. Found {len(refined_zeeschuimer_files)} files...")
    many_zeeschuimer_logs = [data_io.load_parquet(storage_location="zeeschuimer_refined", filename=u) for u in refined_zeeschuimer_files]

    if top_verbose:
        print(f"Concatenating refined zeeschuimer logs...")
    concatenated_zeeschuimer_logs = pd.concat(many_zeeschuimer_logs).drop_duplicates(subset=["item_id","T_local_timestamp","T_tz_name"])

    # reset index
    concatenated_zeeschuimer_logs.reset_index(drop=True, inplace=True)

    # recalculate session_id to ensure that session IDs are unique
    session_id_mapper = {u:(i+100) for i,u in enumerate(concatenated_zeeschuimer_logs.B_log_script.unique())} # the number 100 is not important - it just didn't feel right to start at session zero
    concatenated_zeeschuimer_logs["session_id"] = concatenated_zeeschuimer_logs.B_log_script.map(session_id_mapper)
    concatenated_zeeschuimer_logs["session_id"] = concatenated_zeeschuimer_logs["session_id"].map(lambda x:f"SZ{x:05}").convert_dtypes(dtype_backend="pyarrow") # SZ kind of indicates that this is a S-ession and Z-eeschuimer

    memory_per_column = concatenated_zeeschuimer_logs.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)

    # dropping columns that are all NA
    check_all_na_columns = len(concatenated_zeeschuimer_logs) - concatenated_zeeschuimer_logs.isna().sum()
    concatenated_zeeschuimer_logs = concatenated_zeeschuimer_logs.drop(check_all_na_columns[check_all_na_columns==0].index, axis=1).copy()


    if top_verbose:
        print(f"...done. Concatenated all logs into shape: {concatenated_zeeschuimer_logs.shape} and memory usage: {total_memory_mb:.2f} MB")

    # update the dataset meta file
    if not "zeeschuimer" in dataset_meta:
        dataset_meta["zeeschuimer"] = {}
    dataset_meta["zeeschuimer"]["filenames"] = refined_zeeschuimer_files
    _ = data_io.save_json(data = dataset_meta, storage_location="recoded", filename="dataset_meta.json")

    return True, concatenated_zeeschuimer_logs












def get_baseline_info_as_string(the_raw_posts_df):
    n_items = len(the_raw_posts_df)
    the_string = ""
    the_string += "-"*40+ "\n"
    the_string += f"Baseline log info ({n_items:,} items):"+ "\n"
    the_string += "-"*40+ "\n"
    the_raw_posts_df = the_raw_posts_df.fillna("-")
    for i,c in enumerate([
        "timestamp_collected", "data.createTime", "source_platform_url", "source_url.browser_language", 
        "source_url.app_language", "source_url.cookie_enabled", 
        "source_url.language", "source_url.os", "source_url.region",
        "source_url.showAds", "source_url.tz_name", "source_url.user_is_login", "source_url.categoryType"
        ]):
        if c in the_raw_posts_df.columns:
            the_string += c.replace("source_url.","")
            if i<2:
                the_string += f": first: {min(the_raw_posts_df[c])}  |  last: {max(the_raw_posts_df[c])}"+ "\n"
            else:
                counted_values = the_raw_posts_df.value_counts(c)
                if len(counted_values) > 1:
                    the_string += f" ({len(counted_values)} unique values)\n   "
                    for j,cc in enumerate(counted_values.index):
                        if counted_values[cc]/n_items >= 0.01 and j<5:
                            if j>0:
                                the_string += "  |  "
                            the_string += f"{cc}: {counted_values[cc]/n_items:.0%}"
                    if len(counted_values) > 5:
                        the_string += "  |  ..."
                else:
                    the_string += f": {counted_values.index[0]}: 100%"
                the_string += "\n"
    the_string += "-"*40 + "\n"

    return the_string







def move_and_refine_recent_file(
    the_recent_file = None,
    the_script = None,
    verbose=False,
    move_it = True
    ):


    if the_recent_file is None:
        raise ValueError("the_recent_file must be a dictionary with a 'filename' key")


    # the filename of the latest zeeschuimer ndjson file in the firefox downloads folder
    latest_zee_ndjson_in_firefox_downloads = the_recent_file["filename"]
    print(f"Processing the latest Zeeschuimer log file {latest_zee_ndjson_in_firefox_downloads}")

    # create a filename for the zeeschuimer ndjson file that is more readable
    #better_zee_ndjson_fn = the_script+basename(latest_zee_ndjson_in_firefox_downloads.replace("zeeschuimer", ""))

    # move (and rename) the latest zeeschuimer ndjson file to the folder for raw zeeschuimer logs
    if move_it:
        data_io.move(
            src_storage_location = "firefox_downloads", 
            dst_storage_location = "zeeschuimer_raw", 
            filename = latest_zee_ndjson_in_firefox_downloads)


    # read the zeeschuimer log file from the new location and clean up the data
    raw_zee_log = data_io.read_ndjson_file(storage_location = "zeeschuimer_raw", filename = latest_zee_ndjson_in_firefox_downloads)
    refined_zee_log = refine_zeeschuimer_log(item_list_or_ndjson_path = raw_zee_log)

    # create a filename for the zeeschuimer processed file by just replacing the suffix
    zee_processed_fn = better_zee_ndjson_fn.replace(".ndjson",'.parquet')

    # make sure the filename for the processed file is unique
    r = 0
    while data_io.exists(storage_location = "zeeschuimer_refined", filename = zee_processed_fn):
        r += 1
        if r ==  1:
            zee_processed_fn = zee_processed_fn.replace('.parquet', f"_{r:04}.parquet")
        else:
            zee_processed_fn = zee_processed_fn.replace(f"_{r-1:04}.parquet", f"_{r:04}.parquet")


    # save the refined zeeschuimer log as a processed file
    print(f"Saving the log file as a DataFrame: '{zee_processed_fn}'.")

    data_io.save_parquet(df = refined_zee_log, storage_location = "zeeschuimer_refined", filename = zee_processed_fn, verbose=verbose)
    
    # print some info about what is in refined_zee_log
    print(get_baseline_info_as_string(refined_zee_log))





def get_baseline_log(the_script=None, 
                     how_recent=30,
                     verbose=False):


    start_time = _dt.datetime.now()
    print("\n"+"*"*100)

    if the_script is None:
        print(f"No script name provided. Looking for recent zeeschuimer files in {fyp_cf['paths']['firefox_downloads']}")
        the_script = "zee"
    else:
        if the_script.endswith(".scrpt"):
            the_script = the_script.replace(".scrpt", "")
    
        print(f"{start_time.strftime('%Y-%m-%d %H:%M:%S')}: Harvesting TikTok logs w Zeeschuimer & '{basename(the_script)}'")
        print("*"*100+"\n")

        print(f"Running '{basename(the_script)}' to control Firefox w Zeeschuimer extension to visit TikTok and generate logs...")
        subprocess.run([
            "osascript",
            the_script+".scrpt"
        ])
        end_time = _dt.datetime.now()
        print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Harvest w '{basename(the_script)}' completed in {pretty_str_seconds((end_time-start_time).total_seconds())}.")    

    the_script = basename(the_script)

    recent_files = data_io.get_recent_files(fyp_cf, "firefox_downloads",
                                        suffix=".ndjson",
                                        how_recent=how_recent)
    if len(recent_files) > 0:
        print(f"Found {len(recent_files)} recent Zeeschuimer file(s).")

        for recent_file in recent_files:
            print("=========================================================")
            print(f"Processing: {recent_file}")
            print("=========================================================")
            result = move_and_refine_recent_file(
                the_recent_file = recent_file,
                the_script = the_script,
                move_it = True,
                verbose = verbose
                )
            print("---------------------------------------------------------")
            return result
    else:
        print(f"Could not find a Zeeschuimer ndjson file in the firefox downloads folder.")


    end_time = _dt.datetime.now()
    print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Process completed in {pretty_str_seconds((end_time-start_time).total_seconds())}.")    
    print("Done\n"+"*"*80+"\n")

























def load_zeeschuimer_data(
    study_name = None,
    all_data = None,
    verbose = False):
    # load items from baseline logs


    if study_name is None:
        raise ValueError("study_name must be specified")
    
    if not "study_defs" in fyp_cf:
        init_study_defs()

    START_DATE = fyp_cf["study_defs"][study_name].get("START_DATE","1970-01-01")
    if isinstance(START_DATE, str):
        try:
            START_DATE = _dt.datetime.strptime(START_DATE, "%Y-%m-%d").date()
        except ValueError:
            START_DATE = _dt.datetime(1970,1,1).date()
    
    END_DATE = fyp_cf["study_defs"][study_name].get("END_DATE","2099-12-31")
    if isinstance(END_DATE, str):
        try:
            END_DATE = _dt.datetime.strptime(END_DATE, "%Y-%m-%d").date()
        except ValueError:
            END_DATE = _dt.datetime(2099,12,31).date()


    print("    [Zeeschuimer] Loading data for study...")

    if all_data is None:
        zee_data = data_io.load_parquet(storage_location="recoded", filename="zeeschuimer_recoded.parquet", verbose=verbose)
    else:
        zee_data = all_data.copy()

    if verbose:
        date_range = f"{zee_data.T_local_timestamp.min():%Y-%m-%d} -- {zee_data.T_local_timestamp.max():%Y-%m-%d}"
        print(f"    [Zeeschuimer] Data loaded (and added session stats): {zee_data.shape[0]:,} rows w date range {date_range}")
    

    zee_data = zee_data[(zee_data.T_local_timestamp>=START_DATE) & (zee_data.T_local_timestamp<=END_DATE)].copy()

    if not "T_local_timestamp" in zee_data.columns or len(zee_data) == 0:
        print(f"!!! [Zeeschuimer] No events found in date range. Returning None.")
        return None
    
    date_range = f"{zee_data.T_local_timestamp.min():%Y-%m-%d} -- {zee_data.T_local_timestamp.max():%Y-%m-%d}"
    
    print(f"    [Zeeschuimer] ...done. Selected date range: {date_range}. Observations: {zee_data.shape[0]:,}")
    

    return zee_data











