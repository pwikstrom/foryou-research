#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import fyp.fyp_main as fyp



############################################################################################################
###                     Process Zeeschuimer metadata
############################################################################################################

# read a file with one json object per line and return a list of dictionaries
def read_ndjson_file(file_path):
    from json import loads


    fine_fn = file_path.replace(fyp.cf["paths"]["zeeschuimer_raw"]+"/","").replace("/","").replace(".ndjson","").split('-')[0]
    data = []
    with open(file_path, 'r') as file:
        for line in file:
            line = '{"label":"' + fyp.cf["misc"]["label"] + '",' + line[1:]
            line = '{"log_script":"' + fine_fn + '",' + line[1:]
            data.append(loads(line))
    return data



def refine_zeeschuimer_log(item_list_or_ndjson_path: str | list[dict]):
    import pandas as pd
    from datetime import datetime
    from copy import copy

    if isinstance(item_list_or_ndjson_path, str):
        item_list = read_ndjson_file(item_list_or_ndjson_path)
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

    zeeschuimer_logs_df.item_id = zeeschuimer_logs_df.item_id.astype(int)

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
            zeeschuimer_logs_df[a_column_to_fix] = zeeschuimer_logs_df[a_column_to_fix].apply(lambda x:fyp.extract_and_join_subkeys(x, columns_to_fix[a_column_to_fix]))
        
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
        source_details += [fyp.clean_url(zeeschuimer_logs_df['source_url'][ii])]        
    source_details = pd.DataFrame(source_details)

    # merge the source_details dataframe with the zeeschuimer_logs_df dataframe and drop the source_url column
    zeeschuimer_logs_df = pd.merge(left=zeeschuimer_logs_df, right=source_details, left_index=True, right_index=True)
    del zeeschuimer_logs_df["source_url"]

    # convert the 'data.createTime' and 'timestamp_collected' columns to datetime
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].astype(int)
    zeeschuimer_logs_df["data.createTime"] = zeeschuimer_logs_df["data.createTime"].apply(lambda x:datetime.fromtimestamp(x))
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].astype(int)
    zeeschuimer_logs_df["timestamp_collected"] = zeeschuimer_logs_df["timestamp_collected"].apply(lambda x: datetime.fromtimestamp(int(x/1000)))

    # replace commas and newlines in object columns with spaces
    object_cols = [c for c in zeeschuimer_logs_df.columns if zeeschuimer_logs_df[c].dtype == 'object']
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace(","," ") if type(x)==str else x)
    zeeschuimer_logs_df[object_cols] = zeeschuimer_logs_df[object_cols].map(lambda x: x.replace("\n"," ") if type(x) == str else x)

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
    the_recent_file,
    the_script
    ):
    from shutil import move
    from os.path import basename, join, exists
    import subprocess
    from datetime import datetime

    # the filename of the latest zeeschuimer ndjson file in the firefox downloads folder
    latest_zee_ndjson_in_firefox_downloads = the_recent_file["filename"]
    print(f"Processing the latest Zeeschuimer log file {latest_zee_ndjson_in_firefox_downloads}")

    # create a filename for the zeeschuimer ndjson file that is more readable
    better_zee_ndjson_fn = the_script+basename(latest_zee_ndjson_in_firefox_downloads.replace("zeeschuimer", ""))

    # move (and rename) the latest zeeschuimer ndjson file to the folder for raw zeeschuimer logs
    new_zee_ndjson_path = join(fyp.cf["paths"]["zeeschuimer_raw"], better_zee_ndjson_fn)
    move(latest_zee_ndjson_in_firefox_downloads, new_zee_ndjson_path)

    # read the zeeschuimer log file from the new location and clean up the data
    raw_zee_log = read_ndjson_file(new_zee_ndjson_path)
    refined_zee_log = refine_zeeschuimer_log(raw_zee_log)

    # create a filename for the zeeschuimer pickle file by just replacing the suffix
    zee_pickle_fn = better_zee_ndjson_fn.replace(".ndjson",".pkl")

    # make sure the filename for the pickle file is unique
    r = 0
    while exists(join(fyp.cf["paths"]["zeeschuimer_refined"], zee_pickle_fn)):
        r += 1
        if r ==  1:
            zee_pickle_fn = zee_pickle_fn.replace(".pkl", f"_{r:04}.pkl")
        else:
            zee_pickle_fn = zee_pickle_fn.replace(f"_{r-1:04}.pkl", f"_{r:04}.pkl")

    # save the refined zeeschuimer log as a pickle file
    print(f"Saving the log file as a DataFrame: '{zee_pickle_fn}'.")
    refined_zee_log.to_pickle(join(fyp.cf["paths"]["zeeschuimer_refined"], zee_pickle_fn))
    
    # print some info about what is in refined_zee_log
    print(get_baseline_info_as_string(refined_zee_log))





def get_baseline_log(the_script=None, 
                     how_recent=30):
    from os.path import basename, join
    import subprocess
    from datetime import datetime


    start_time = datetime.now()
    print("\n"+"*"*100)

    if the_script is None:
        print(f"No script name provided. Looking for recent zeeschuimer files in {fyp.cf['paths']['firefox_downloads']}")
        the_script = "zeeschuimer"
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
        print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Harvest w '{basename(the_script)}' completed in {fyp.pretty_str_seconds((end_time-start_time).total_seconds())}.")    

    the_script = basename(the_script)

    recent_files = fyp.get_recent_files(fyp.cf["paths"]["firefox_downloads"],
                                        suffix=".ndjson",
                                        how_recent=how_recent)
    if len(recent_files) > 0:
        print(f"Found {len(recent_files)} recent Zeeschuimer file(s).")

        for recent_file in recent_files:
            print("=========================================================")
            print(f"Processing: {recent_file}")
            print("=========================================================")
            move_and_refine_recent_file(
                recent_file,
                the_script
                )
            print("---------------------------------------------------------")

        '''        if len(recent_files) > 1:
            print(f"I'm only processing the latest one. If you want to process the other files, \
                run the script again without any arguments.")

        # the filename of the latest zeeschuimer ndjson file in the firefox downloads folder
        latest_zee_ndjson_in_firefox_downloads = recent_files[0]["filename"]
        print(f"Processing the latest Zeeschuimer log file {latest_zee_ndjson_in_firefox_downloads}")

        # create a filename for the zeeschuimer ndjson file that is more readable
        better_zee_ndjson_fn = the_script+basename(latest_zee_ndjson_in_firefox_downloads.replace("zeeschuimer", ""))

        # move (and rename) the latest zeeschuimer ndjson file to the folder for raw zeeschuimer logs
        new_zee_ndjson_path = join(fyp.cf["paths"]["zeeschuimer_raw"], better_zee_ndjson_fn)
        move(latest_zee_ndjson_in_firefox_downloads, new_zee_ndjson_path)

        # read the zeeschuimer log file from the new location and clean up the data
        raw_zee_log = read_ndjson_file(new_zee_ndjson_path) # NOTE - only processing the latest file
        refined_zee_log = refine_zeeschuimer_log(raw_zee_log)

        # create a filename for the zeeschuimer pickle file by just replacing the suffix
        zee_pickle_fn = better_zee_ndjson_fn.replace(".ndjson",".pkl")

        # make sure the filename for the pickle file is unique
        r = 0
        while exists(join(fyp.cf["paths"]["zeeschuimer_refined"], zee_pickle_fn)):
            r += 1
            if r ==  1:
                zee_pickle_fn = zee_pickle_fn.replace(".pkl", f"_{r:04}.pkl")
            else:
                zee_pickle_fn = zee_pickle_fn.replace(f"_{r-1:04}.pkl", f"_{r:04}.pkl")

        # save the refined zeeschuimer log as a pickle file
        print(f"Saving the log file as a DataFrame: '{zee_pickle_fn}'.")
        refined_zee_log.to_pickle(join(fyp.cf["paths"]["zeeschuimer_refined"], zee_pickle_fn))
        
        # print some info about what is in refined_zee_log
        print(get_baseline_info_as_string(refined_zee_log))
        '''    
    else:
        print(f"Could not find a Zeeschuimer ndjson file in the firefox downloads folder.")

    end_time = datetime.now()
    print(f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}: Process completed in {fyp.pretty_str_seconds((end_time-start_time).total_seconds())}.")    
    print("Done\n"+"*"*80+"\n")



