#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

from numpy import int64 as np_int64



############################################################################################################
###                     Process Zeeschuimer metadata
############################################################################################################

# read a file with one json object per line and return a list of dictionaries
def read_ndjson_file(cf = None, storage_location = None, file_name = None):
    from json import loads
    from fyp.fyp_main import init_config
    from os.path import join

    if cf is None:
        cf = init_config()

    fine_fn = file_name.replace("/","").replace(".ndjson","").split('-')[0]
    data = []
    with open(join(cf["paths"][storage_location], file_name), 'r') as file:
        for line in file:
            line = '{"label":"' + cf["misc"]["label"] + '",' + line[1:]
            line = '{"log_script":"' + fine_fn + '",' + line[1:]
            data.append(loads(line))
    return data






def refine_zeeschuimer_log(cf = None, item_list_or_ndjson_path: str | list[dict] = None):
    from pandas import DataFrame, json_normalize, merge
    from datetime import datetime
    from copy import copy
    from fyp.fyp_main import init_config, extract_and_join_subkeys, clean_url, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io
    import numpy as np

    if cf is None:
        cf = init_config()

    if isinstance(item_list_or_ndjson_path, str):
        item_list = read_ndjson_file(cf = cf, storage_location="zeeschuimer_raw", file_name = item_list_or_ndjson_path)
    elif isinstance(item_list_or_ndjson_path, list):
        item_list = item_list_or_ndjson_path
    else:
        print("Input must be a list of dictionaries or a path to an ndjson file.")
        return DataFrame()
        
    # if the list is empty, return an empty dataframe
    if len(item_list) == 0:
        return DataFrame()

    # normalize the list of dictionaries into a dataframe and convert the item_id to an integer
    zeeschuimer_logs_df = json_normalize(item_list)

    # drop the items with corrupt item_ids
    zeeschuimer_logs_df = copy(zeeschuimer_logs_df[zeeschuimer_logs_df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)])

    zeeschuimer_logs_df.item_id = zeeschuimer_logs_df.item_id.astype("string[pyarrow]")

    # drop these columns
    zeeschuimer_logs_df.drop(columns=["avatar", "secUid", "data.contents", "music.cover", "music.playUrl", "data.video"], errors="ignore", inplace=True)


    # the dataframe based zeeschuimer data has a lot of variables that we don't
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
    source_details = DataFrame(source_details)

    # merge the source_details dataframe with the zeeschuimer_logs_df dataframe and drop the source_url column
    zeeschuimer_logs_df = merge(left=zeeschuimer_logs_df, right=source_details, left_index=True, right_index=True)
    del zeeschuimer_logs_df["source_url"]

    # convert the 'data.createTime' and 'timestamp_collected' columns to datetime
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].astype(np.int64)
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].apply(lambda x:datetime.fromtimestamp(x))
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].astype(np.int64)
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].apply(lambda x: datetime.fromtimestamp(np.int64(x/1000)))

    # replace commas and newlines in object columns with spaces
    object_cols = [c for c in zeeschuimer_logs_df.columns if zeeschuimer_logs_df[c].dtype == 'object']
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace(","," ") if type(x)==str else x)
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace("\n"," ") if type(x) == str else x)


    zeeschuimer_logs_df = convert_dtypes_to_pyarrow(zeeschuimer_logs_df)

    return zeeschuimer_logs_df



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
    #from shutil import move
    from os.path import basename, join, exists
    import subprocess
    from datetime import datetime
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io

    if cf is None:
        cf = init_config()

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
    zee_processed_fn = better_zee_ndjson_fn.replace(".ndjson",cf['misc']['file_format'])

    # make sure the filename for the processed file is unique
    r = 0
    while data_io.exists(cf, "zeeschuimer_refined", zee_processed_fn):
        r += 1
        if r ==  1:
            zee_processed_fn = zee_processed_fn.replace(cf['misc']['file_format'], f"_{r:04}{cf['misc']['file_format']}")
        else:
            zee_processed_fn = zee_processed_fn.replace(f"_{r-1:04}{cf['misc']['file_format']}", f"_{r:04}{cf['misc']['file_format']}")


    # save the refined zeeschuimer log as a processed file
    print(f"Saving the log file as a DataFrame: '{zee_processed_fn}'.")

    data_io.save_parquet(cf, refined_zee_log, "zeeschuimer_refined", zee_processed_fn, verbose=verbose)
    
    # print some info about what is in refined_zee_log
    print(get_baseline_info_as_string(refined_zee_log))





def get_baseline_log(cf = None,
                     the_script=None, 
                     how_recent=30,
                     verbose=False):
    from os.path import basename, join
    import subprocess
    from datetime import datetime
    from fyp.fyp_main import init_config, pretty_str_seconds, get_recent_files

    if cf is None:
        cf = init_config()

    start_time = datetime.now()
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
        end_time = datetime.now()
        print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Harvest w '{basename(the_script)}' completed in {pretty_str_seconds((end_time-start_time).total_seconds())}.")    

    the_script = basename(the_script)

    recent_files = get_recent_files(fyp_cf, "firefox_downloads",
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


    end_time = datetime.now()
    print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Process completed in {pretty_str_seconds((end_time-start_time).total_seconds())}.")    
    print("Done\n"+"*"*80+"\n")








def process_baseline_for_complete_dataset(
    cf = None,
    baseline_log = None,
    session_id_counter = np_int64(0),
    verbose=False):

    from pandas import concat
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow

    if baseline_log is None or len(baseline_log) == 0:
        if verbose:
            print("No baseline log data available --> skipping baseline log processing. Returning None.")
        return None, session_id_counter

    if cf is None:
        cf = init_config()

    baseline_log_simple = baseline_log.rename(columns={c:"B_"+c if not c=="item_id" else c for c in baseline_log.columns}).copy()
    if verbose:
        print(f"The baseline log has shape: {baseline_log_simple.shape}")


    # VECTORIZED: No loop needed, use groupby operations directly
    if len(baseline_log_simple) and ("item_id" in baseline_log_simple.columns):
        # Sort by script and timestamp
        baseline_log_simple = baseline_log_simple.sort_values(["B_log_script", "B_local_timestamp"]).copy()
        
        # Assign session IDs: each script gets a unique session ID
        baseline_log_simple["session_id"] = session_id_counter + baseline_log_simple.groupby("B_log_script").ngroup() + 1
        session_id_counter = baseline_log_simple["session_id"].max() + 1
        
        # Event order within each session (vectorized)
        baseline_log_simple["event_order_in_session"] = baseline_log_simple.groupby("session_id").cumcount()
        
        # Event position (vectorized)
        n_videos_per_session = baseline_log_simple.groupby("session_id")["event_order_in_session"].transform("max")
        baseline_log_simple["event_pos_in_session"] = baseline_log_simple["event_order_in_session"] / n_videos_per_session.replace(0, 1)
        
        if verbose:
            print("Adding session stats to baseline data",baseline_log_simple.shape)
    else:
        if verbose:
            print("no baseline data available --> skipping session stats attachment. Returning None.")
        return None, session_id_counter

    #if verbose:
        #print("--"*60)


    if "var_scheme" in cf and not cf["var_scheme"].empty:
        vs = cf["var_scheme"]
        # TODO - this has to be changed to be less connected to specific variable names
        #
        # Determine baseline vars: those with 'role'='standard' (like B_anchors) or starting with B_ ? 
        # The original list had B_ challenges etc. In CSV they are defined.
        # Original List: item_id, T_local_timestamp... T_local_date, session_id, event_order..., event_pos...
        # AND B_challenges, B_anchors, ..., B_source_tz_name
        
        # We want: 
        # 1. Standard structural cols (item_id, T_..., session_..., event_...)
        # 2. All variables in scheme that start with B_
        
        structural_cols = [
            'item_id',
            'T_local_timestamp', 'T_local_weekday', 'T_local_week',
            'T_local_hour', 'T_local_day_segment', 'T_local_date',
            'session_id', 'event_order_in_session',
            'event_pos_in_session'
        ]
        
        b_vars = vs[vs['variable_name'].str.startswith('B_', na=False)]['variable_name'].tolist()
        relevant_baseline_cols = structural_cols + b_vars
        
        # Remove duplicates just in case
        relevant_baseline_cols = list(dict.fromkeys(relevant_baseline_cols))
    else:
        raise ValueError("var_scheme not found in config")


    baseline_log_simple = rename_columns(baseline_log_simple)

    relevant_baseline_cols = [c for c in relevant_baseline_cols if c in baseline_log_simple.columns]

    baseline_log_simple = baseline_log_simple[relevant_baseline_cols].copy()
    
    # Convert categorical columns to string to avoid fillna errors with categoricals
    #for col in baseline_log_simple.select_dtypes(include=['category']).columns:
    #    baseline_log_simple[col] = baseline_log_simple[col].astype(str)
    
    #baseline_log_simple = baseline_log_simple.fillna("").copy()
    #baseline_log_simple = _check_for_null_values_in_df(baseline_log_simple, verbose=verbose)


    #baseline_log_simple = convert_dtypes_to_pyarrow(baseline_log_simple, verbose=verbose)

    if verbose:
        print("Processed baseline for log export - shape:", baseline_log_simple.shape)
    return baseline_log_simple#, session_id_counter

  












def ingest_zeeschuimer_data(
    cf = None,
    verbose=False):
    # load items from baseline logs

    from pandas import concat, DataFrame
    from os.path import exists, join
    from os import remove, listdir
    from datetime import datetime
    from json import load as json_load
    from zoneinfo import ZoneInfo
    from fyp.fyp_main import init_config, connect_to_google, convert_dtypes_to_pyarrow
    from fyp.organize_datasets_OPTIMIZED import extract_local_time_features
    import fyp.data_io as data_io

    if cf is None:
        cf = init_config()
    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)
    
    
    print("Loading baseline logs...")

    list_of_zeeschuimer_logs = []
    okay_test_cases = []

    zeeschuimer_refined_files = [fn for fn in data_io.listdir(cf, "zeeschuimer_refined", verbose=verbose) if fn.endswith(cf['misc']['file_format'])]

    # loop to load all separate zeeschuimer refined files
    for fn in zeeschuimer_refined_files:
        
        zeeschuimer_candidate = data_io.load_parquet(cf, "zeeschuimer_refined", fn, verbose=verbose)

        empty_columns = []
        for c in zeeschuimer_candidate.columns:
            if zeeschuimer_candidate[c].isna().sum() / len(zeeschuimer_candidate) > 0.95:
                empty_columns.append(c)

        zeeschuimer_candidate.drop(empty_columns, axis=1, inplace=True)

        zeeschuimer_candidate = zeeschuimer_candidate.convert_dtypes(dtype_backend="pyarrow") # to be sure
        

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
        baseline_log = concat(list_of_zeeschuimer_logs)

        if verbose:
            print(f"...baseline log loaded (and added session stats): {baseline_log.shape[0]:,} rows w date range {baseline_log.timestamp_collected.min()} -- {baseline_log.timestamp_collected.max()}")
        
        baseline_log = baseline_log.drop_duplicates(subset=["item_id","timestamp_collected","source_url.tz_name"]).copy()
        if verbose:
            print(f"Dropped duplicates based on item_id, timestamp and location, yielding {baseline_log.shape[0]:,} rows")


        # only keeping videos from the FYP page not the explore page
        baseline_log = baseline_log[baseline_log.source_platform_url.isin(['https://www.tiktok.com/en','https://www.tiktok.com/','https://www.tiktok.com/foryou'])].copy()
        if verbose:
            print(f"Keeping baseline logs from TikTok's ForYou page, yielding {baseline_log.shape[0]:,} rows.")

        
        baseline_log.reset_index(drop=True, inplace=True)


        empty_columns = []
        for c in baseline_log.columns:
            if baseline_log[c].isna().sum() / len(baseline_log) > 0.8:
                empty_columns.append(c)

        baseline_log.drop(empty_columns, axis=1, inplace=True)

        baseline_log = extract_local_time_features(
            cf = cf,
            some_events_df_in = baseline_log,
            kind_of_log = 'baseline',
            verbose = verbose)

        baseline_log_simple, _ = process_baseline_for_complete_dataset(cf = cf, baseline_log = baseline_log, verbose=verbose)

        if verbose:
            print("Saving half-baked baseline events...")    
        baseline_log_simple = data_io.save_parquet(cf, baseline_log_simple, "zeeschuimer_main", "all_zeeschuimer_events.parquet", verbose=verbose)
    
    else:
        baseline_log_simple = DataFrame()

    return baseline_log_simple










def load_zeeschuimer_data(
    cf = None,
    study_name = None,
    verbose=False):
    # load items from baseline logs

    from datetime import datetime
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io

    if study_name is None:
        raise ValueError("study_name must be specified")
    
    if cf is None:
        cf = init_config()
    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)
    

    BASELINE_START_DATE = cf["study_defs"][study_name]["BASELINE_START_DATE"]
    if isinstance(BASELINE_START_DATE, str):
        BASELINE_START_DATE = datetime.strptime(BASELINE_START_DATE, "%Y-%m-%d")
    
    BASELINE_END_DATE = cf["study_defs"][study_name]["BASELINE_END_DATE"]
    if isinstance(BASELINE_END_DATE, str):
        BASELINE_END_DATE = datetime.strptime(BASELINE_END_DATE, "%Y-%m-%d")

    print("Loading baseline logs...")

    baseline_log = data_io.load_parquet(cf, "zeeschuimer_main", "all_zeeschuimer_events.parquet", verbose=verbose)

    if verbose:
        print(f"...baseline log loaded (and added session stats): {baseline_log.shape[0]:,} rows w date range {baseline_log.T_local_timestamp.min():%Y-%m-%d} -- {baseline_log.T_local_timestamp.max():%Y-%m-%d}")
    

    baseline_log = baseline_log[(baseline_log.T_local_timestamp>=BASELINE_START_DATE) & (baseline_log.T_local_timestamp<=BASELINE_END_DATE)].copy()
    if verbose:
        print("Baseline log selected date range:",baseline_log.T_local_timestamp.min(), " ---- ", baseline_log.T_local_timestamp.max(), "Shape:",baseline_log.shape)
    

    return {"data_baseline_log":baseline_log}











