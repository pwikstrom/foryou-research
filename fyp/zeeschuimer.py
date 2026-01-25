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
from fyp.fyp_main import initialize
from fyp.utils import extract_and_join_subkeys, clean_url, pretty_str_seconds
from fyp.recode_variables import recode_events_df, extract_local_time_features, rename_columns
import fyp.data_io as data_io
import numpy as np
import subprocess
import textwrap
import datetime as _dt



############################################################################################################
###                     Process Zeeschuimer metadata
############################################################################################################




"""def process_baseline_for_core_dataset(
    cf:dict = None,
    baseline_log:pd.DataFrame = None,
    session_id_counter:np.int64 = 0,
    verbose:bool = False):


    if baseline_log is None or len(baseline_log) == 0:
        if verbose:
            print("No baseline log data available --> skipping baseline log processing. Returning None.")
        return None, session_id_counter

    if cf is None:
        cf = initialize()

    baseline_log_simple = baseline_log.rename(columns={c:"B_"+c if not c=="item_id" else c for c in baseline_log.columns}).copy()
    if verbose:
        print(f"The baseline log has shape: {baseline_log_simple.shape}")


    if len(baseline_log_simple) and ("item_id" in baseline_log_simple.columns):
        # Sort by script and timestamp
        baseline_log_simple = baseline_log_simple.sort_values(["B_log_script", "B_local_timestamp"]).copy()
        
        # Assign session IDs: each script gets a unique session ID
        baseline_log_simple["session_id"] = session_id_counter + baseline_log_simple.groupby("B_log_script").ngroup() + 1
        session_id_counter = baseline_log_simple["session_id"].max() + 1
        
        # Event order within each session
        baseline_log_simple["event_order_in_session"] = baseline_log_simple.groupby("session_id").cumcount()
        
        # Event position
        n_videos_per_session = baseline_log_simple.groupby("session_id")["event_order_in_session"].transform("max")
        baseline_log_simple["event_pos_in_session"] = baseline_log_simple["event_order_in_session"] / n_videos_per_session.replace(0, 1)
        
        if verbose:
            print("Adding session stats to baseline data",baseline_log_simple.shape)
    else:
        if verbose:
            print("no baseline data available --> skipping session stats attachment. Returning None.")
        return None, session_id_counter


    baseline_log_simple = rename_columns(baseline_log_simple)

    print(set(cf['var_schema'].variable_name))
    print("\n")
    print(set(baseline_log_simple.columns))
    print("\n")
    print(set(cf['var_schema'].variable_name) & set(baseline_log_simple.columns))




    relevant_baseline_cols = [c for c in cf['var_schema'].variable_name if c in baseline_log_simple.columns]

    baseline_log_simple = baseline_log_simple[relevant_baseline_cols].copy()




    if verbose:
        print("Processed baseline for log export - shape:", baseline_log_simple.shape)
    return baseline_log_simple#, session_id_counter"""

  




def refine_one_raw_zeeschuimer_log(
    cf: dict = None,
    item_list_or_ndjson_path: str | list[dict] = None,
    verbose: bool = False):

    if cf is None:
        cf = initialize()

    if isinstance(item_list_or_ndjson_path, str):
        item_list = data_io.read_ndjson_file(cf = cf, storage_location="zeeschuimer_raw", filename = item_list_or_ndjson_path)
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
        cf = cf,
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
    dropped_vars_str = textwrap.wrap(", ".join(list(set(zeeschuimer_logs_df.columns) - set(cf['var_schema'].variable_name))), width=120)
    relevant_baseline_cols = [c for c in cf['var_schema'].variable_name if c in zeeschuimer_logs_df.columns]
    zeeschuimer_logs_df = zeeschuimer_logs_df[relevant_baseline_cols].copy()

    if verbose:
        print(f"Dropped these columns, which are not in the variable schema:\n{"\n".join(dropped_vars_str)}\nCurrent shape: {zeeschuimer_logs_df.shape}")

    zeeschuimer_logs_df = zeeschuimer_logs_df.copy()


    zeeschuimer_logs_df = recode_events_df(
        cf = cf,
        study_dataset = zeeschuimer_logs_df,
        drop_single_value_cols = False,
        verbose = verbose
        )
    

    if verbose:
        print(f"Final shape: {zeeschuimer_logs_df.shape}")
        print("------------------------------------------------\n\n")

    return zeeschuimer_logs_df









def refine_and_save_all_raw_zeeschuimer_logs(cf = None, verbose=False):

    if cf is None:
        cf = initialize()
    result = {}
    
    raw_zeeschuimer_files = data_io.listdir(
        cf=cf,
        storage_location="zeeschuimer_raw",
        return_absolute_path=False,
        verbose=False)
    raw_zeeschuimer_files = [u for u in raw_zeeschuimer_files if u.endswith(".ndjson")]
    result["raw_files"] = len(raw_zeeschuimer_files)

    refined_zeeschuimer_files = data_io.listdir(
        cf=cf,
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
                cf = cf,
                item_list_or_ndjson_path = u,
                verbose=verbose)
            #ff += [new_flat.copy()]
            data_io.save_parquet(cf=cf, df=new_flat, filename=u.replace(".ndjson",".parquet"), storage_location="zeeschuimer_refined", verbose=verbose)

    refined_zeeschuimer_files = data_io.listdir(
        cf=cf,
        storage_location="zeeschuimer_refined",
        return_absolute_path=False,
        verbose=False)
    refined_zeeschuimer_files = [u for u in refined_zeeschuimer_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_zeeschuimer_files)

    return result













def consolidate_zeeschuimer_logs(
    cf = None,
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose = False):

    if cf is None:
        cf = initialize()

    top_verbose = True

    if top_verbose:
        print("Checking for new raw zeeschuimer logs that needs refining...")
    result = refine_and_save_all_raw_zeeschuimer_logs(cf=cf, verbose=verbose)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            print("    ...all files already refined.")
        else:
            print(f"    ...refined {result["refined_files_after"] - result["refined_files_before"]} files.")


    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(cf=cf,storage_location="recoded",filename="dataset_meta.json",verbose=verbose):
        dataset_meta = data_io.load_json(cf=cf,storage_location="recoded",filename="dataset_meta.json",verbose=verbose)
        if verbose:
            print("Dataset meta loaded")
    else:
        dataset_meta = {"zeeschuimer": {"filenames": []}}

    refined_zeeschuimer_files = data_io.listdir(
        cf=cf,
        storage_location="zeeschuimer_refined",
        return_absolute_path=False,
        verbose=True)
    refined_zeeschuimer_files = [u for u in refined_zeeschuimer_files if u.endswith(".parquet")]


    latest_filename_list = dataset_meta.get("zeeschuimer", {}).get("filenames", [])

    if not force_consolidation and set(refined_zeeschuimer_files) == set(latest_filename_list):
        if top_verbose:
            print("No new refined zeeschuimer files found. No need to consolidate.")
            if return_saved_data:
                if verbose: print("Returning existing file.")
                return False, data_io.load_parquet(cf=cf, storage_location="recoded", filename="zeeschuimer_recoded.parquet")
        return False, None
    


    # load and concatenate all refined files
    if top_verbose:
        print(f"Loading refined zeeschuimer logs. Found {len(refined_zeeschuimer_files)} files...")
    many_zeeschuimer_logs = [data_io.load_parquet(cf=cf, storage_location="zeeschuimer_refined", filename=u) for u in refined_zeeschuimer_files]

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
    _ = data_io.save_json(cf, dataset_meta, "recoded", "dataset_meta.json")

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
    cf = None,
    the_recent_file = None,
    the_script = None,
    verbose=False,
    move_it = True
    ):

    if cf is None:
        cf = initialize()

    if the_recent_file is None:
        raise ValueError("the_recent_file must be a dictionary with a 'filename' key")


    # the filename of the latest zeeschuimer ndjson file in the firefox downloads folder
    latest_zee_ndjson_in_firefox_downloads = the_recent_file["filename"]
    print(f"Processing the latest Zeeschuimer log file {latest_zee_ndjson_in_firefox_downloads}")

    # create a filename for the zeeschuimer ndjson file that is more readable
    #better_zee_ndjson_fn = the_script+basename(latest_zee_ndjson_in_firefox_downloads.replace("zeeschuimer", ""))

    # move (and rename) the latest zeeschuimer ndjson file to the folder for raw zeeschuimer logs
    if move_it:
        data_io.move(cf, "firefox_downloads", "zeeschuimer_raw", latest_zee_ndjson_in_firefox_downloads)


    # read the zeeschuimer log file from the new location and clean up the data
    raw_zee_log = read_ndjson_file(cf = cf, file_path = latest_zee_ndjson_in_firefox_downloads)
    refined_zee_log = refine_zeeschuimer_log(cf = cf, item_list_or_ndjson_path = raw_zee_log)

    # create a filename for the zeeschuimer processed file by just replacing the suffix
    zee_processed_fn = better_zee_ndjson_fn.replace(".ndjson",'.parquet')

    # make sure the filename for the processed file is unique
    r = 0
    while data_io.exists(cf, "zeeschuimer_refined", zee_processed_fn):
        r += 1
        if r ==  1:
            zee_processed_fn = zee_processed_fn.replace('.parquet', f"_{r:04}.parquet")
        else:
            zee_processed_fn = zee_processed_fn.replace(f"_{r-1:04}.parquet", f"_{r:04}.parquet")


    # save the refined zeeschuimer log as a processed file
    print(f"Saving the log file as a DataFrame: '{zee_processed_fn}'.")

    data_io.save_parquet(cf, refined_zee_log, "zeeschuimer_refined", zee_processed_fn, verbose=verbose)
    
    # print some info about what is in refined_zee_log
    print(get_baseline_info_as_string(refined_zee_log))





def get_baseline_log(cf = None,
                     the_script=None, 
                     how_recent=30,
                     verbose=False):

    if cf is None:
        cf = initialize()

    start_time = _dt.datetime.now()
    print("\n"+"*"*100)

    if the_script is None:
        print(f"No script name provided. Looking for recent zeeschuimer files in {cf['paths']['firefox_downloads']}")
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
                cf = cf,
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



















"""def ingest_zeeschuimer_data(
    cf = None,
    verbose=False):
    # load items from baseline logs

    if cf is None:
        cf = initialize()
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)
    
    
    print("Loading baseline logs...")

    list_of_zeeschuimer_logs = []
    okay_test_cases = []

    zeeschuimer_refined_files = [fn for fn in data_io.listdir(cf, "zeeschuimer_refined", verbose=verbose) if fn.endswith('.parquet')]

    # loop to load all separate zeeschuimer refined files
    for fn in zeeschuimer_refined_files:
        
        zeeschuimer_candidate = data_io.load_parquet(cf, "zeeschuimer_refined", fn, verbose=verbose)
        
        test_cols = zeeschuimer_candidate[["item_id","timestamp_collected"]].reset_index(drop=True).sort_values("timestamp_collected").copy()
        duplicate_found = False
        for zl in okay_test_cases:
            if zl.shape == test_cols.shape:
                if (zl.index == test_cols.index).all() and (zl.columns == test_cols.columns).all():
                    if (test_cols == zl).all().all():
                        duplicate_found = True
                        if verbose:
                            print("   !! Found a duplicate zeeschuimer file. I'm not adding it to the collection...")
                        wow = test_cols.copy()
        if not duplicate_found:
            zeeschuimer_candidate = zeeschuimer_candidate.reset_index(drop=True).reset_index().rename(columns={"index":"event_order_in_session"})
            zeeschuimer_candidate["event_pos_in_session"] = zeeschuimer_candidate["event_order_in_session"] / max(1,len(zeeschuimer_candidate)-1)

            list_of_zeeschuimer_logs += [zeeschuimer_candidate]
            okay_test_cases += [test_cols]


    list_of_zeeschuimer_logs = sorted(list_of_zeeschuimer_logs,key=lambda x:x["timestamp_collected"].min())


    for i,zl in enumerate(list_of_zeeschuimer_logs):
        zl["session_id"] = i

    if len(list_of_zeeschuimer_logs)>0:
        baseline_log = pd.concat(list_of_zeeschuimer_logs)

        if verbose:
            print(f"...baseline log loaded (and added session stats): {baseline_log.shape[0]:,} rows w date range {baseline_log.timestamp_collected.min()} -- {baseline_log.timestamp_collected.max()}")
        
        baseline_log = baseline_log.drop_duplicates(subset=["item_id","timestamp_collected","source_url.tz_name"]).copy()
        if verbose:
            print(f"Dropped duplicates based on item_id, timestamp and collection timezone, yielding {baseline_log.shape[0]:,} rows")


        # only keeping videos from the FYP page not the explore page
        baseline_log = baseline_log[baseline_log.source_platform_url.isin(['https://www.tiktok.com/en','https://www.tiktok.com/','https://www.tiktok.com/foryou'])].copy()
        if verbose:
            print(f"Keeping baseline logs from TikTok's ForYou page, yielding {baseline_log.shape[0]:,} rows.")

        
        baseline_log.reset_index(drop=True, inplace=True)



        baseline_log = extract_local_time_features(
            cf = cf,
            some_events_df_in = baseline_log,
            kind_of_log = 'baseline',
            verbose = verbose)

        baseline_log_simple, _ = process_baseline_for_core_dataset(cf = cf, baseline_log = baseline_log, verbose=verbose)

        if verbose:
            print("Saving half-baked baseline events...")    
        baseline_log_simple = data_io.save_parquet(cf, baseline_log_simple, "zeeschuimer_main", "all_zeeschuimer_events.parquet", verbose=verbose)
    
    else:
        baseline_log_simple = pd.DataFrame()

    return baseline_log_simple"""










def load_zeeschuimer_data(
    cf = None,
    study_name = None,
    all_data = None,
    verbose = False):
    # load items from baseline logs


    if study_name is None:
        raise ValueError("study_name must be specified")
    
    if cf is None:
        cf = initialize()
    

    START_DATE = cf["study_defs"][study_name].get("START_DATE","1970-01-01")
    if isinstance(START_DATE, str):
        try:
            START_DATE = _dt.datetime.strptime(START_DATE, "%Y-%m-%d").date()
        except ValueError:
            START_DATE = _dt.datetime(1970,1,1).date()
    
    END_DATE = cf["study_defs"][study_name].get("END_DATE","2099-12-31")
    if isinstance(END_DATE, str):
        try:
            END_DATE = _dt.datetime.strptime(END_DATE, "%Y-%m-%d").date()
        except ValueError:
            END_DATE = _dt.datetime(2099,12,31).date()


    print("    [Zeeschuimer] Loading data for study...")

    if all_data is None:
        zee_data = data_io.load_parquet(cf, "recoded", "zeeschuimer_recoded.parquet", verbose=verbose)
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











