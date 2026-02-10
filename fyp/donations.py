#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import json
import textwrap
import pandas as pd
import re
import os
from collections import deque
import numpy as np
import datetime as _dt
from pathlib import Path
import subprocess
import shlex
import shutil

from fyp.types import convert_dtypes_to_pyarrow
from fyp.recode_variables import *
from fyp.calc_donation_stats import generate_personas
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs













def get_donation_metadata_from_aio_aws(
                        storage_location: str = "ddp_participants",
                        table_name: str = (
                            "data-donation-stack-"
                            "donationtablesmetadatatable1526CA1C-J3HP8RPY7RRW"
                        ),
                        use_local_time: bool = False,
                        verbose: bool = False):


    """
    Save the raw DynamoDB JSON into the project's local temp and
    the move to the ddp_participants' storage location (local or GCS depending on config).
    Requires AWS CLI to be installed and configured. *duh*
    """

    # Compute cut‑off time
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())
    file_stamp = now.strftime("%Y%m%d%H%M%S") 

    # Prepare destination
    filename = f"ddp_metadata_{file_stamp}.json"
    temp_file = os.path.join(fyp_cf["paths"]["temp"], filename)

    # Assemble the AWS CLI command
    scan_cmd = (
        "aws dynamodb scan "
        f"--table-name {shlex.quote(table_name)} "
        "--select ALL_ATTRIBUTES "
        "--page-size 500 "
        "--max-items 100000 "
        "--output json"
    )
    full_cmd = f"{scan_cmd} > {shlex.quote(str(temp_file))}"

    # Run it
    try:
        subprocess.run(full_cmd, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error downloading participant metadata running AWS CLI command: {e}")
        return None

    # move to permanent storage
    data_io.move(
        src_storage_location="temp",
        dst_storage_location=storage_location,
        filename=filename,
        verbose=verbose
    )








def get_recent_data_donations_from_aio_aws(
                    hours_back: int = 24,
                    table_name: str = (
                        "data-donation-stack-"
                        "donationtablesmetadatatable1526CA1C-J3HP8RPY7RRW"
                    ),
                    bucket: str = (
                        "data-donation-stack-"
                        "donationbucket71125dbb-woyvcojrhlcw"
                    ),
                    #campaign_name: str = "qut",
                    use_local_time: bool = False) -> None:
    """
    Scan the Donations metadata table for items whose *date* ("shareDate")
    is within the last ``hours_back`` hours and download the associated files
    to the project's 'ddp_raw' storage location (local or GCS depending on config).

    Parameters
    ----------
    hours_back : int
        How far back to look (in hours) from *now*.
    table_name, bucket, campaign_name : str, optional
        Override the defaults if your stack names ever change.
    use_local_time : bool, optional
        If ``True`` the cut‑off time is computed in your local time zone
        (Australia/Brisbane).  Otherwise UTC is used (default).

    Raises
    ------
    subprocess.CalledProcessError
        If any of the shell commands exit with a non‑zero status.
    """


    # ------------------------------------------------------------------
    # 1) Figure out the time window and format it the way the table stores it
    # ------------------------------------------------------------------
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())     # Brisbane local
    cutoff = now - _dt.timedelta(hours=hours_back)
    share_date = cutoff.replace(microsecond=0).isoformat()

    # ------------------------------------------------------------------
    # 2) Prepare temporary destination
    # ------------------------------------------------------------------
    # Use a specific temp folder for this batch

    temp_dir_path = os.path.join(fyp_cf["paths"]["temp"], f"download_batch_{now.strftime('%Y%m%d%H%M%S')}")
    dest = Path(temp_dir_path).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 3) Build the shell command (quote everything that may contain spaces)
    # ------------------------------------------------------------------
    scan_cmd = (
        "aws dynamodb scan "
        f"--table-name {shlex.quote(table_name)} "
        "--filter-expression "
        "\"consentProvided = :consent and #d >= :shareDate\" "
        "--expression-attribute-names "
        "'{\"#d\": \"date\"}' "
        "--expression-attribute-values "
        #f"'{{\":campaignName\": {{\"S\": \"{campaign_name}\"}}, "
        f"'{{\":consent\": {{\"BOOL\": true}}, "
        f"\":shareDate\": {{\"S\": \"{share_date}\"}}}}' "

        "--query 'Items[*].id.S'"
    )


    # We pipe the result through jq and xargs, then copy each object
    
    full_cmd = (
        f"{scan_cmd} | jq -r '.[]' "
        "| xargs -I {} "
        f"aws s3 cp \"s3://{bucket}/donation/{{}}\" {shlex.quote(str(dest))}"
    )
    
    # ------------------------------------------------------------------
    # 4) Run the download to temp
    # ------------------------------------------------------------------
    print(f"Downloading recent donations to temporary storage: {dest}")
    subprocess.run(full_cmd, shell=True, check=True)

    # ------------------------------------------------------------------
    # 5) Move/Upload files to ddp_raw storage
    # ------------------------------------------------------------------
    downloaded_files = os.listdir(dest)
    print(f"Moving {len(downloaded_files)} files to ddp_raw storage...")
    
    count = 0
    for filename in downloaded_files:
        val_path = dest / filename
        # Read the content
        with open(val_path, 'r') as f:
            try:
                # Assuming they are JSONs as per previous scripts?
                # ingest script treats them as JSONs
                data = json.load(f)
                
                # Use data_io to save (handles GCS upload + Local secondary)
                data_io.save_json(data, "ddp_raw", filename)
                count += 1
            except Exception as e:
                print(f"Failed to process/upload {filename}: {e}")

    print(f"Successfully processed {count} files.")

    # ------------------------------------------------------------------
    # 6) Cleanup Temp
    # ------------------------------------------------------------------
    try:
        shutil.rmtree(dest)
    except Exception as e:
        print(f"Warning: Failed to clean up temp directory {dest}: {e}")








def add_session_info_to_generic_event_log(ddp_log_in, session_id_counter = np.int64(10_000_000), session_time_limit=900, verbose=False):
    # attach session stats to donation events

    ddp_log = ddp_log_in.copy()

    all_sessions = []
        
    # Collect all updates, then apply in bulk at the end
    updates_list = []

    # initialize new columns
    ddp_log['session_id'] = pd.NA
    ddp_log['session_id'] = ddp_log['session_id'].astype("int64[pyarrow]")
    ddp_log['event_order_in_session'] = pd.NA
    ddp_log['event_order_in_session'] = ddp_log['event_order_in_session'].astype("int64[pyarrow]")
    ddp_log['event_pos_in_session'] = pd.NA
    ddp_log['event_pos_in_session'] = ddp_log['event_pos_in_session'].astype("double[pyarrow]")


    for one_donation_id,one_donation in ddp_log.groupby("collection_id"):

        watch = (one_donation.sort_values(['utc_timestamp','event_order_in_session'])).copy()

        watch['delta'] = watch['utc_timestamp'].shift(-1) - watch['utc_timestamp']
        # timedelta conversion to seconds
        watch['delta'] = watch['delta'].dt.total_seconds()



        # A new session starts when delta is X minutes or is NaN
        session_breaks = (watch['delta'].isna()) | (watch['delta'] > session_time_limit)

        # Cumsum creates incrementing session IDs at each break
        session_nums = session_breaks.astype(bool).cumsum()
        # Add the counter offset and assign
        watch['session_id'] = session_id_counter + session_nums
        
        
        # groupby().cumcount() gives sequential numbering within each session
        watch['event_order_in_session'] = watch.groupby('session_id').cumcount()
        # events at session breaks get -1, others keep their count
        watch.loc[session_breaks, 'event_order_in_session'] = 0

        session_stats = watch.groupby('session_id').agg(
            session_duration=('delta', 'sum'),
            session_start_ts=('utc_timestamp', 'min'),
            n_videos_in_session=('event_order_in_session', 'max'),
        )

        session_stats = session_stats.astype(int)
        session_stats["session_end_ts"] = session_stats["session_start_ts"] + session_stats["session_duration"]
        session_stats["collection_id"] = one_donation_id

        watch['n_videos_in_session'] = watch['session_id'].map(session_stats['n_videos_in_session'].to_dict())
        watch['event_pos_in_session'] = watch['event_order_in_session'] / watch['n_videos_in_session']
        #watch['event_pos_in_session'] = watch['event_pos_in_session'].fillna(-1).astype(float)

        session_stats["n_videos_in_session"] = session_stats["n_videos_in_session"]+1

        short = watch.loc[watch['delta'].between(0, session_time_limit), ['delta', 'session_id', 'event_order_in_session', 'event_pos_in_session']]

        # Store updates
        if len(short) > 0:
            updates_list.append(short)

        all_sessions += [session_stats]

    # Apply all updates at once
    if updates_list:
        all_updates = pd.concat(updates_list)
        ddp_log.loc[all_updates.index, 'session_id'] = all_updates['session_id']
        ddp_log.loc[all_updates.index, 'event_order_in_session'] = all_updates['event_order_in_session']
        ddp_log.loc[all_updates.index, 'event_pos_in_session'] = all_updates['event_pos_in_session']
        ddp_log.loc[all_updates.index, 'watch_duration'] = all_updates['delta']
    
    if verbose:
        print(f"Adding session stats to activity data {ddp_log.shape}. Unique collections: {ddp_log.collection_id.nunique()}")
        

    return ddp_log







def _add_session_info_to_ddp_log(ddp_log_in, session_id_counter = np.int64(10_000_000), verbose=False):
    # attach session stats to donation events

    from pandas import isna as pd_isna, concat
    import numpy as np

    ddp_log = ddp_log_in.copy()

    all_sessions = []
    if len(ddp_log) and ("D_donation_id" in ddp_log.columns):

        
        # Collect all updates, then apply in bulk at the end
        updates_list = []

        # initialize new columns
        ddp_log['session_id'] = pd.NA
        ddp_log['session_id'] = ddp_log['session_id'].astype("int64[pyarrow]")
        ddp_log['event_order_in_session'] = pd.NA
        ddp_log['event_order_in_session'] = ddp_log['event_order_in_session'].astype("int64[pyarrow]")
        ddp_log['event_pos_in_session'] = pd.NA
        ddp_log['event_pos_in_session'] = ddp_log['event_pos_in_session'].astype("double[pyarrow]")


        for one_donation_id,one_donation in ddp_log.groupby("D_donation_id"):

            watch = (one_donation.sort_values(['T_local_timestamp','event_order_in_session'])).copy()

            watch['delta'] = watch['T_local_timestamp'].shift(-1) - watch['T_local_timestamp']
            # timedelta conversion to seconds
            #print(watch[['delta','T_local_timestamp','event_order_in_session','session_id']].head(10))
            watch['delta'] = watch['delta'].dt.total_seconds()



            # A new session starts when delta is >15 minutes or is NaN
            session_breaks = (watch['delta'].isna()) | (watch['delta'] > 15*60)

            # Cumsum creates incrementing session IDs at each break
            session_nums = session_breaks.astype(bool).cumsum()
            # Add the counter offset and assign
            watch['session_id'] = session_id_counter + session_nums
            
            # Update counter for next donation
            session_id_counter = watch['session_id'].max() + 1
            
            # groupby().cumcount() gives sequential numbering within each session
            watch['event_order_in_session'] = watch.groupby('session_id').cumcount()
            # events at session breaks get -1, others keep their count
            watch.loc[session_breaks, 'event_order_in_session'] = 0

            session_stats = watch.groupby('session_id').agg(
                session_duration=('delta', 'sum'),
                session_start_ts=('T_local_timestamp', 'min'),
                n_videos_in_session=('event_order_in_session', 'max'),
            )

            session_stats = session_stats.astype(int)
            session_stats["session_end_ts"] = session_stats["session_start_ts"] + session_stats["session_duration"]
            session_stats["D_donation_id"] = one_donation_id

            watch['n_videos_in_session'] = watch['session_id'].map(session_stats['n_videos_in_session'].to_dict())
            watch['event_pos_in_session'] = watch['event_order_in_session'] / watch['n_videos_in_session']
            #watch['event_pos_in_session'] = watch['event_pos_in_session'].fillna(-1).astype(float)

            session_stats["n_videos_in_session"] = session_stats["n_videos_in_session"]+1

            short = watch.loc[watch['delta'].between(0, 15*60), ['delta', 'session_id', 'event_order_in_session', 'event_pos_in_session']]

            # Store updates
            if len(short) > 0:
                updates_list.append(short)

            all_sessions += [session_stats]

        # Apply all updates at once
        if updates_list:
            all_updates = concat(updates_list)
            ddp_log.loc[all_updates.index, 'session_id'] = all_updates['session_id']
            ddp_log.loc[all_updates.index, 'event_order_in_session'] = all_updates['event_order_in_session']
            ddp_log.loc[all_updates.index, 'event_pos_in_session'] = all_updates['event_pos_in_session']
            ddp_log.loc[all_updates.index, 'D_watch_duration'] = all_updates['delta']
        
        if verbose:
            print(f"Adding session stats to DDP data {ddp_log.shape}. Unique donations: {ddp_log.D_donation_id.nunique()}")
        
    else:
        if verbose:
            print("no ddp data")

    return ddp_log









def propagate_timestamps(
    df, 
    time_col='local_timestamp', 
    prop_col='primary_value', 
    item_col='item_id', 
    feature_col='feature_name', 
    target_feature='watch', 
    match_on_item_id=False, 
    status_col='propagation_status', 
    fill_missing_items=True
    ):

    """
    Propagates timestamps from non-target rows to the closest preceding target row indices.
    
    Args:
        df: Input DataFrame.
        time_col: Name of the timestamp column.
        item_col: Name of the item identifier column.
        feature_col: Name of the feature/category column.
        target_feature: The feature value to receive timestamps (e.g. 'A' or 'watch').
        match_on_item_id: If True, only propagates within the same item_id group. 
                          If False, propagates based on strict chronological order (index) ignoring item_id.
        status_col: Name of the new column to store row classification.
        fill_missing_items: If True, fills NA item_ids in non-orphan rows with the item_id of their target.
    """
    
    
    # Ensure preservation of original index for correct updates
    if not df.index.is_unique:
        print("Warning: Index is not unique. Resetting index for processing.")
        df = df.reset_index(drop=True)

    # CRITICAL: Logic relies on chronological order
    df = df.sort_values(by=time_col)
    
    # Initialize status column
    # Use object/string dtype to avoid float compatibility warnings
    df[status_col] = pd.Series([None] * len(df), dtype="string[pyarrow]") if "pyarrow" in str(df[prop_col].dtype) else pd.NA
    
    # Mark target rows
    df.loc[df[feature_col] == target_feature, status_col] = 'target'

    # 1. Identify dynamic features (all non-target non-NaN values)
    all_features = df[feature_col].dropna().unique()
    other_features = [f for f in all_features if f != target_feature]
    
    # Initialize these new columns
    prop_dtype = df[prop_col].dtype
    
    for col in other_features:
        if col not in df.columns:
            if 'pyarrow' in str(prop_dtype):
                 df[col] = pd.Series([None] * len(df), dtype=prop_dtype)
            else:
                 df[col] = pd.NA

    # 2. Map each row to the index of its closest preceding target row.
    df['target_idx_temp'] = df.index.to_series().where(df[feature_col] == target_feature)
    
    # Forward fill to propagate the index
    if match_on_item_id:
        df['target_idx_temp'] = df.groupby(item_col, dropna=False)['target_idx_temp'].ffill()
    else:
        df['target_idx_temp'] = df['target_idx_temp'].ffill()

    # Apply Item ID Fill
    if fill_missing_items:
        target_item_ids = df['target_idx_temp'].map(df[item_col])
        df[item_col] = df[item_col].fillna(target_item_ids)

    # Classification: Orphans
    relevant_mask = df[feature_col].isin(other_features)
    orphan_mask = relevant_mask & df['target_idx_temp'].isna()
    df.loc[orphan_mask, status_col] = 'orphan'

    # 3. Identify source rows (non-target) that have a valid target
    masked_rows = relevant_mask & (df['target_idx_temp'].notna())
    updates_subset = df[masked_rows]

    if updates_subset.empty:
        df.drop(columns=['target_idx_temp'], errors='ignore', inplace=True)
        return df

    # logic to distinguish propagated vs collision
    propagated_indices = updates_subset.groupby(['target_idx_temp', feature_col]).head(1).index
    df.loc[propagated_indices, status_col] = 'propagated'
    
    collision_indices = updates_subset.index.difference(propagated_indices)
    df.loc[collision_indices, status_col] = 'collision'

    # 4. Pivot
    pivot_updates = updates_subset.pivot_table(
        index='target_idx_temp', 
        columns=feature_col, 
        values=prop_col, 
        aggfunc='first'
    )
    
    # Ensure pivot_updates matches the target dtype to avoid update warnings
    # (pivot_table often returns 'object' for strings)
    if 'pyarrow' in str(prop_dtype):
        try:
            pivot_updates = pivot_updates.astype(prop_dtype)
        except Exception:
            pass # Keep as is if cast fails

    # 5. Update
    df.update(pivot_updates)



    # Post-processing types
    for q in other_features:
        if q in df.columns:
            non_na_values = df[q].dropna()
            if not non_na_values.empty:
                # Check the first non-NA value safely
                first_val = non_na_values.iloc[0]
                # Ensure it's a string before calling startswith
                if isinstance(first_val, str) and first_val.startswith("https://"):
                    # Convert to bool: notna() is True for existing URLs, False for NA
                    df[q] = df[q].notna().astype("bool[pyarrow]")
                else:
                    df[q] = df[q].astype("string[pyarrow]")
            else:
                 # If all empty, default to string[pyarrow]
                 df[q] = df[q].astype("string[pyarrow]")

    # Clean up
    df.drop(columns=['target_idx_temp','','login_event','search','followed_by','post'], errors='ignore', inplace=True)

    # Summary column: True if any propagated feature is present (not NA, not False, not "")
    existing_other_features = [c for c in other_features if c in df.columns]
    
    if existing_other_features:
        # Check for truthiness:
        # 1. notna()
        # 2. != False (for boolean cols)
        # 3. != "" (for string cols)
        
        # We can use applymap-like logic or masking.
        # Efficient approach: Check truthiness directly if types allow.
        # But for pyarrow backed bools, False is False. For strings, "" is "".
        # Let's use a mask.
        
        mask = df[existing_other_features].notna()
        
        # Apply stricter checks based on columns
        for col in existing_other_features:
            # If boolean, False is considered "empty"
            if pd.api.types.is_bool_dtype(df[col]):
                mask[col] &= (df[col] != False)
            # If string/object, "" is considered "empty"
            elif pd.api.types.is_string_dtype(df[col]) or pd.api.types.is_object_dtype(df[col]):
                mask[col] &= (df[col] != "")
        
        df['engagement'] = mask.any(axis=1)
    else:
        df['engagement'] = False


    return df







def flatten_single_tiktok_ddp_from_raw_file(
    filename: str = None,
    collection_id: str = None,
    collection_group: str = None,
    verbose: bool = False) -> dict:


    if data_io.exists(storage_location = "ddp_raw", filename = filename):
        donation_dict = data_io.load_json(storage_location = "ddp_raw", filename = filename)
    else:
        raise FileNotFoundError(f"File {filename} not found")

    ts_added_to_dataset = data_io.getmtime(storage_location = "ddp_raw", filename = filename)

    if collection_id is None:
        collection_id = os.path.basename(filename)


    donation_items = []

    # --- find list of dicts -------------
    stack = deque([(None, donation_dict)])       # (feature_name, current_obj)
    while stack:
        feature, obj = stack.pop()
        if isinstance(obj, list):          # this is an event list
            for item in obj:
                if isinstance(item, dict) and item:           # non-empty dict
                    donation_items.append({
                        "event_type":      (feature or '').lower(),
                        "variable_list":     [k.lower() for k in item.keys()],
                        "value_list":        list(item.values())
                    })
        elif isinstance(obj, dict):
            for k, v in obj.items():
                stack.append((k, v))

    # --- nothing found? bail out early ------------------------
    if not donation_items:
        print("ERROR: No collection items found in file", donation_id)
        return {}


    # -----------------------------------------------------
    # initialising the dataframe from the raw data. This is the df I'll be processing through this function
    all_ddp_events_df = pd.DataFrame.from_records(donation_items)
    all_ddp_events_df['collection_id'] = collection_id
    all_ddp_events_df['collection_id'] = all_ddp_events_df['collection_id'].astype("string[pyarrow]")


    # -----------------------------------------------------
    # keep rows that have at least one variable and contain 'date'
    mask_date = all_ddp_events_df['variable_list'].map(lambda lst: 'date' in lst)
    all_ddp_events_df = all_ddp_events_df[mask_date & (all_ddp_events_df['variable_list'].map(len) > 0)].copy()


    # -----------------------------------------------------
    # unpack the variable/value list

    # get the date
    all_ddp_events_df['date'] = pd.to_datetime(all_ddp_events_df['value_list'].str[0]).convert_dtypes(dtype_backend="pyarrow")

    # extract primary_label and primary_value
    try:
        all_ddp_events_df['primary_label'] = all_ddp_events_df['variable_list'].str[1].convert_dtypes(dtype_backend="pyarrow")
        all_ddp_events_df['extra_data'] = all_ddp_events_df['value_list'].str[1].convert_dtypes(dtype_backend="pyarrow")
    except:
        all_ddp_events_df['primary_label'] = pd.NA
        all_ddp_events_df['extra_data'] = pd.NA


    # -----------------------------------------------------
    # Extract item_id from the video_url
    item_ids_from_url = (
        all_ddp_events_df["extra_data"]
        .astype("string")
        .str.rsplit("/", n=2) # rsplit is cheaper than full split, only looks from the right
        .str[-2]
    )

    # keep only item_ids that are pure digit strings, everything else -> <NA>
    digits = item_ids_from_url.str.fullmatch(r"\d+")
    item_ids = item_ids_from_url.where(digits)

    mask = (
        (all_ddp_events_df["primary_label"]=="link")
        & (all_ddp_events_df["event_type"].notna())
    )
    all_ddp_events_df["item_id"] = item_ids.where(mask).convert_dtypes(dtype_backend="pyarrow")

    # nullify the extra_data column for rows where item_id was extracted
    mask = all_ddp_events_df["item_id"].notnull()
    all_ddp_events_df.loc[mask, "extra_data"] = pd.NA



    # -----------------------------------------------------
    # tiktok timestamps are a bit weird - convert date to seconds since epoch
    all_ddp_events_df['timestamp'] = (all_ddp_events_df['date'].astype("int64[pyarrow]") // 1_000_000_000).astype("int64[pyarrow]")


    # -----------------------------------------------------
    # rename feature_name to make the labels a bit clearer
    all_ddp_events_df["event_type"] = all_ddp_events_df["event_type"].map(
        {
            'videolist':'watch',
            'commentslist':'comment',
            'post':'post',
            'searchlist':'search',
            'fanslist':'followed_by',
            'following':'following',
            'itemfavoritelist':'fave',
            'favoritevideolist':'fave'
        }
    ).convert_dtypes(dtype_backend="pyarrow").copy()


    # -----------------------------------------------------
    # event_type is NA for login events - not sure why, but this changes that
    all_ddp_events_df.loc[all_ddp_events_df[all_ddp_events_df["primary_label"]=="ip"].index,"event_type"] = "login_event"


    # -----------------------------------------------------
    # rename timestamp to utc_timestamp
    if "utc_timestamp" not in all_ddp_events_df.columns:
        all_ddp_events_df = all_ddp_events_df.rename(columns={"timestamp": "utc_timestamp"})
    
    # 2. Ensure UTC Timestamp is valid Datetime
    if not pd.api.types.is_datetime64_any_dtype(all_ddp_events_df['utc_timestamp']):
        all_ddp_events_df["utc_timestamp"] = pd.to_datetime(all_ddp_events_df["utc_timestamp"], unit='s', utc=True)

    # 3. Infer Timezone Offset
    all_ddp_events_df["tz_offset"] = infer_timezone_offset(all_ddp_events_df["utc_timestamp"])
    all_ddp_events_df["tz_offset"] = all_ddp_events_df["tz_offset"].astype("int64[pyarrow]")

    all_ddp_events_df["utc_timestamp"] = all_ddp_events_df["utc_timestamp"].astype("timestamp[ns][pyarrow]")


    # -----------------------------------------------------
    del all_ddp_events_df['primary_label']
    del all_ddp_events_df['variable_list']
    del all_ddp_events_df['value_list']
    del all_ddp_events_df['date']
    all_ddp_events_df = all_ddp_events_df[((all_ddp_events_df["event_type"] != "watch") | (all_ddp_events_df["item_id"].notna()))].copy()


    # -----------------------------------------------------
    # Sort by timestamp and reset index
    all_ddp_events_df.sort_values("utc_timestamp", inplace=True)
    all_ddp_events_df.reset_index(drop=True, inplace=True)


    all_ddp_events_df['collection_group'] = pd.Series(collection_group, index=all_ddp_events_df.index, dtype="string[pyarrow]")


    all_ddp_events_df["ts_added_to_dataset"] = pd.to_datetime(ts_added_to_dataset, unit="s")
    all_ddp_events_df["ts_added_to_dataset"] = all_ddp_events_df["ts_added_to_dataset"].astype("timestamp[ns][pyarrow]")

    all_ddp_events_df["source_platform"] = pd.Series("tiktok", index=all_ddp_events_df.index, dtype="string[pyarrow]")
    all_ddp_events_df["data_source"] = pd.Series("ddp", index=all_ddp_events_df.index, dtype="string[pyarrow]")


    # -----------------------------------------------------
    # It seems like the data donation packages keep watch logs for a certain time back
    # in time, but they keep other engagement stats for longer. It is difficult to handle engagement stats without connection to a watch
    # event, so I remove all events before the first watch event. It feels a bit brutal to throw away data, but I'm not sure what else to do.
    first_watch_idx = all_ddp_events_df[all_ddp_events_df["event_type"] == "watch"].index[0]
    all_ddp_events_df = all_ddp_events_df.loc[first_watch_idx:].copy()


    # -----------------------------------------------------
    # Create temporary session ids to make some processing based on events that are very close to eachother in time
    all_ddp_events_df['delta'] = all_ddp_events_df['utc_timestamp'] - all_ddp_events_df['utc_timestamp'].shift(1)
    all_ddp_events_df['delta'] = all_ddp_events_df['delta'].dt.total_seconds()
    all_ddp_events_df['session_break'] = (all_ddp_events_df['delta'].isna()) | (all_ddp_events_df['delta'] > 180) # a very short time - only 3 minutes...

    # Cumsum creates incrementing session IDs at each break
    all_ddp_events_df['session_id'] = all_ddp_events_df['session_break'].astype(bool).cumsum()
        

    # -----------------------------------------------------
    # 1. Identify valid starting points (first non-NA item_id) per session
    # Any row with cumsum == 0 is before the first item_id in that session.
    has_item = all_ddp_events_df['item_id'].notna().astype(int)
    cumulative_items = has_item.groupby(all_ddp_events_df['session_id']).cumsum()

    # Filter out rows before the first item
    all_ddp_events_df = all_ddp_events_df[cumulative_items > 0].copy()

    # 2. Forward fill item_id within groups
    all_ddp_events_df['item_id'] = all_ddp_events_df.groupby('session_id')['item_id'].ffill()

    # 3. Filter short sessions (len <= 1)
    session_counts = all_ddp_events_df.groupby("session_id")["session_id"].transform("count")
    all_ddp_events_df = all_ddp_events_df[session_counts > 1].copy()


    all_ddp_events_df.drop(columns=["session_id", "session_break", "delta"], inplace=True)
    all_ddp_events_df.convert_dtypes(dtype_backend="pyarrow")

    return all_ddp_events_df












def refine_one_raw_ddp_log_from_dict(
    donation_id: str = None,
    donation_dict: dict = None,
    ts_added_to_dataset: _dt.datetime = None,
    verbose: bool = False) -> pd.DataFrame:


    donation_items = []

    # --- find list of dicts -------------
    stack = deque([(None, donation_dict)])       # (feature_name, current_obj)
    while stack:
        feature, obj = stack.pop()
        if isinstance(obj, list):          # this is an event list
            for item in obj:
                if isinstance(item, dict) and item:           # non-empty dict
                    donation_items.append({
                        "donation_id":       donation_id,
                        "feature_name":      (feature or '').replace('xxx','').lower(),
                        "variable_list":     [k.lower() for k in item.keys()],
                        "value_list":        list(item.values())
                    })
        elif isinstance(obj, dict):
            for k, v in obj.items():
                stack.append((k, v))

    # --- nothing found? bail out early ------------------------
    if not donation_items:
        print("ERROR: No collection items found in file", donation_id)
        return pd.DataFrame()


    # -----------------------------------------------------
    # initialising the dataframe from the raw data. This is the df I'll be processing through this function
    all_ddp_events_df = pd.DataFrame.from_records(donation_items)

    # this is an immutable id for each event in the donation file - it reflects the order in which the events were recorded in the raw file
    all_ddp_events_df["event_id"] = all_ddp_events_df.index.astype("uint64[pyarrow]")

    # retype donation_id to pyarrow string
    all_ddp_events_df['donation_id'] = all_ddp_events_df['donation_id'].convert_dtypes(dtype_backend="pyarrow")

    # add ts_added_to_dataset
    all_ddp_events_df["ts_added_to_dataset"] = ts_added_to_dataset


    # -----------------------------------------------------
    # keep rows that have at least one variable and contain 'date'
    mask_date = all_ddp_events_df['variable_list'].map(lambda lst: 'date' in lst)
    all_ddp_events_df = all_ddp_events_df[mask_date & (all_ddp_events_df['variable_list'].map(len) > 0)].copy()


    # -----------------------------------------------------
    # unpack the variable/value list

    # get the date
    all_ddp_events_df['date'] = pd.to_datetime(all_ddp_events_df['value_list'].str[0]).convert_dtypes(dtype_backend="pyarrow")

    # extract primary_label and primary_value
    try:
        all_ddp_events_df['primary_label'] = all_ddp_events_df['variable_list'].str[1].convert_dtypes(dtype_backend="pyarrow")
        all_ddp_events_df['primary_value'] = all_ddp_events_df['value_list'].str[1].convert_dtypes(dtype_backend="pyarrow")
    except:
        all_ddp_events_df['primary_label'] = pd.NA
        all_ddp_events_df['primary_value'] = pd.NA



    # -----------------------------------------------------
    # Extract item_id from the video_url
    item_ids_from_url = (
        all_ddp_events_df["primary_value"]
        .astype("string")
        .str.rsplit("/", n=2) # rsplit is cheaper than full split, only looks from the right
        .str[-2]
    )

    # keep only pure digit strings, everything else -> <NA>
    digits = item_ids_from_url.str.fullmatch(r"\d+")
    item_ids = item_ids_from_url.where(digits)

    mask = (
        (all_ddp_events_df["primary_label"]=="link")
        & (all_ddp_events_df["feature_name"].notna())
    )
    all_ddp_events_df["item_id"] = item_ids.where(mask).convert_dtypes(dtype_backend="pyarrow")


    # -----------------------------------------------------
    # convert date to seconds since epoch
    all_ddp_events_df['timestamp'] = (all_ddp_events_df['date'].astype("int64[pyarrow]") // 1_000_000_000).astype("int64[pyarrow]")
    del all_ddp_events_df['date'] # don't need this one any longer


    # -----------------------------------------------------
    # identify post events - this is silly since I will drop post events a little but further down
    post_events = [k for k in all_ddp_events_df.index if "whocanview" in all_ddp_events_df.loc[k,"variable_list"]]
    all_ddp_events_df.loc[post_events,"feature_name"] = "post"
    all_ddp_events_df.loc[post_events,"primary_label"] = "post_link"


    # -----------------------------------------------------
    # rename feature_name to make the labels a bit clearer
    all_ddp_events_df["feature_name"] = all_ddp_events_df["feature_name"].map(
        {
            'videolist':'watch',
            'commentslist':'comment',
            'post':'post',
            'searchlist':'search',
            'fanslist':'followed_by',
            'following':'following',
            'itemfavoritelist':'fave_item',
            'favoritevideolist':'fave_video'
        }
    ).convert_dtypes(dtype_backend="pyarrow").copy()


    # -----------------------------------------------------
    # Feature_name is NA for login events - not sure why, but this changes that
    all_ddp_events_df.loc[all_ddp_events_df[all_ddp_events_df["primary_label"]=="ip"].index,"feature_name"] = "login_event"
    print(f"Current shape: {all_ddp_events_df.shape}")
    print(f"The DDP events range from {all_ddp_events_df.timestamp.min()} -- {all_ddp_events_df.timestamp.max()}")


    # -----------------------------------------------------
    # extract local time features
    all_ddp_events_df = extract_local_time_features(
        some_events_df_in = all_ddp_events_df,
        kind_of_log = 'ddp',
        verbose = verbose)


    # -----------------------------------------------------
    # connect non-watch events to watch events where possible 
    all_ddp_events_df = propagate_timestamps(all_ddp_events_df)


    # -----------------------------------------------------
    # thos events which I at this stage have not been able to associate with an item ID have to go
    all_ddp_events_df = all_ddp_events_df[all_ddp_events_df.item_id.notna()].copy()


    # -----------------------------------------------------
    # rename columns
    all_ddp_events_df = all_ddp_events_df.rename(columns={c:"D_"+c if not c in ["item_id","event_id"] and not re.match(r"^[A-Z]_", c) else c for c in all_ddp_events_df.columns}).copy()
    all_ddp_events_df = rename_columns(all_ddp_events_df)


    # -----------------------------------------------------
    # Sort by timestamp and reset index
    all_ddp_events_df.sort_values("T_local_timestamp", inplace=True)
    all_ddp_events_df.reset_index(drop=True, inplace=True)


    # -----------------------------------------------------
    # assign session IDs etc. These are just placeholders for now,
    # Session IDs will be updated when donations are merged.
    all_ddp_events_df = _add_session_info_to_ddp_log(all_ddp_events_df, verbose=verbose)
    if verbose:
        print(f"Current shape: {all_ddp_events_df.shape}")


    # -----------------------------------------------------
    # only keep columns as defined by the variable schema
    dropped_vars_str = textwrap.wrap(", ".join(list(set(all_ddp_events_df.columns) - set(fyp_cf['var_schema'].variable_name))), width=120)
    relevant_cols = [c for c in fyp_cf['var_schema'].variable_name if c in all_ddp_events_df.columns]
    all_ddp_events_df = all_ddp_events_df[relevant_cols].copy()
    if verbose and dropped_vars_str:
        joined_vars = '\n'.join(dropped_vars_str)
        print(f"Dropped these columns, which are not in the variable schema:\n{joined_vars}\nCurrent shape: {all_ddp_events_df.shape}")
    


    # -----------------------------------------------------
    # recode variables by the variable schema
    all_ddp_events_df = recode_events_df(
        study_dataset = all_ddp_events_df,
        drop_single_value_cols = False,
        verbose = verbose
        )


    if verbose:
        print(f"Final shape of this collection: {all_ddp_events_df.shape}")
    return all_ddp_events_df











def refine_one_raw_ddp_log_file(
    filename: str | None = None,
    donation_id: str | None = None,
    verbose: bool = False):

    if donation_id is None:
        donation_id = os.path.basename(filename)


    # loading a json with the name == donation id
    donation_dict = data_io.load_json(
        storage_location="ddp_raw",
        filename=filename,
        verbose=verbose
    )

    mod_time_timestamp = data_io.getmtime(storage_location="ddp_raw", filename=filename)
    mod_time_timestamp = _dt.datetime.fromtimestamp(mod_time_timestamp)


    raw_data_donation_top_keys = list(donation_dict.keys())
    if 'ad_preferences' in raw_data_donation_top_keys or 'CONTENT_INTERACTION' in raw_data_donation_top_keys:
        if verbose:
            print(f"{filename} is not TikTok data, cannot process. Moving to archive...")
        data_io.move(
            src_storage_location='ddp_raw', 
            dst_storage_location="archive", 
            filename=filename, 
            verbose=verbose
        )
        return "[ERROR]: Not TikTok data"


    return refine_one_raw_ddp_log_from_dict(
        donation_id = donation_id,
        donation_dict = donation_dict,
        ts_added_to_dataset = mod_time_timestamp,
        verbose = verbose
    )





def refine_all_raw_ddp_logs_and_save(verbose=False):

    result = {}
    
    # -----------------------------------------------------
    # Get list of raw DDP files
    # raw files are json files and should have a json suffix. But some files don't
    # particularly those from the aio aws machine. I just assume that they
    # still are okay json files.  
    raw_ddp_files = data_io.listdir(
        storage_location="ddp_raw",
        return_absolute_path=False,
        verbose=False)
    raw_ddp_files = [u for u in raw_ddp_files if not u.startswith(".")]
    result["raw_files"] = len(raw_ddp_files)

    # -----------------------------------------------------
    # Get list of refined DDP files
    refined_ddp_files = data_io.listdir(
        storage_location="ddp_processed",
        return_absolute_path=False,
        verbose=False)
    refined_ddp_files = [u for u in refined_ddp_files if u.endswith(".parquet")]
    result["refined_files_before"] = len(refined_ddp_files)


    for u in raw_ddp_files:
        bn = os.path.basename(u)
        if bn+".parquet" in refined_ddp_files:
            continue

        print("------------------------------------------------")

        if verbose:
            print(f"Refining raw ddp file: {u}")
        new_flat = refine_one_raw_ddp_log_file(
            filename=u,
            verbose=verbose
            )

        if isinstance(new_flat, pd.DataFrame):
            data_io.save_parquet(df=new_flat, filename=bn+".parquet", storage_location="ddp_processed", verbose=verbose)
        else:
            pass
        

    refined_ddp_files = data_io.listdir(
        storage_location="ddp_processed",
        return_absolute_path=False,
        verbose=False)
    refined_ddp_files = [u for u in refined_ddp_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_ddp_files)

    return result







def _deser(value):
    # Convert DynamoDB JSON value → native Python.
    if "S" in value:          # string
        return value["S"]
    if "N" in value:          # number
        num = value["N"]
        return int(num) if num.isdigit() else float(num)
    if "BOOL" in value:       # boolean
        return bool(value["BOOL"])
    if "NULL" in value:       # explicit null
        return None
    if "L" in value:          # list
        return [_deser(v) for v in value["L"]]
    if "M" in value:          # map
        return {k: _deser(v) for k, v in value["M"].items()}
    # Anything else is kept verbatim
    return value






def generate_donation_metadata(
    ddp_events_df: pd.DataFrame | None = None,
    update_col: pd.Series | None = None,
    sort_by: str | None = None, 
    verbose: bool = False,
    save_to_disk_ok: bool = True,
    load_from_disk: bool = True,
    ) -> pd.DataFrame:
    """
    Generate or update donation metadata, either by calculating statistics from events 
    or by merging a specific column into existing metadata.

    Parameters
    ----------
    ddp_events_df : pandas.DataFrame, optional
        Events DataFrame used to calculate metadata statistics.
    update_col : pandas.Series, optional
        A Series representing a single column to update or add to existing metadata.
        The index must be 'D_donation_id'.
    sort_by : str, optional
        Column name to sort the resulting DataFrame by.
    verbose : bool, default False
        Whether to print progress and status messages.

    Returns
    -------
    pandas.DataFrame
        The resulting metadata DataFrame with donation IDs as index.
    """

    old_metadata_df = pd.DataFrame()
    if load_from_disk:
        if data_io.exists(storage_location="ddp_main", filename="ddp_metadata.parquet"):
            old_metadata_df = data_io.load_parquet(storage_location="ddp_main", filename="ddp_metadata.parquet")
            if verbose:
                print(f"Loaded existing metadata from storage. Shape: {old_metadata_df.shape}")
    else:
        if verbose:
            print("No calculated metadata found in storage")


    # if no events df is provided, check if there is an update column
    if ddp_events_df is None:
        if isinstance(update_col, pd.Series):
            print("Updating a single column | ", end="", flush=True)
            if update_col.index.name != "D_donation_id":
                update_col.index.name = "D_donation_id"
            if set(update_col.index) != set(old_metadata_df.index):
                print("Error: Update column index don't match the index of the existing metadata DF. Exiting.")
                return old_metadata_df
            if update_col.name in old_metadata_df.columns:
                print(f"Dropping existing column: {update_col.name} | ", end="", flush=True)
                old_metadata_df = old_metadata_df.drop(columns=[update_col.name])

            new_metadata_df = pd.merge(old_metadata_df, update_col, left_index=True, right_index=True, how="left")
            #new_metadata_df = new_metadata_df.sort_index(axis='columns').sort_values(('other','D_id')).copy()
            if save_to_disk_ok:
                data_io.save_parquet(df=new_metadata_df, storage_location="ddp_main", filename="ddp_metadata.parquet", verbose=verbose)
                print(f"Saved updated metadata. Shape: {new_metadata_df.shape}")
            return new_metadata_df

        else:
            print("No new data provided or update column is not a matching pandas Series. Returning old metadata.")
            return old_metadata_df


    donation_ids_in_the_incoming_df = set(ddp_events_df.D_donation_id.unique())


    if 'D_donation_id' not in ddp_events_df.columns:
        print("Shape of the donation stats DF: (0,0)")
        return pd.DataFrame()
    

    
    donation_ids_in_the_old_metadata_df = set(old_metadata_df.index)
    new_donations = donation_ids_in_the_incoming_df - donation_ids_in_the_old_metadata_df

    if len(new_donations) == 0:
        if verbose:
            print(f"No new donations found. Returning the existing metadata. Shape: {old_metadata_df.shape}")
        return old_metadata_df




    if verbose:
        print(f"Calculating metadata for {len(new_donations)} new donations")

    ddp_events_df_new = ddp_events_df[ddp_events_df.D_donation_id.isin(new_donations)].copy()


    df1 = ddp_events_df_new.groupby('D_donation_id')["D_feature_name"].value_counts().unstack().fillna(0).astype(int)
    df1['total'] = df1.sum(axis=1)
    if sort_by is None:
        df1 = df1.sort_values("total").copy()
    else:
        df1 = df1.sort_values(sort_by).copy()
    if verbose:
        print(f"Shape of the donation stats DF: {df1.shape}")
    df1.columns = pd.MultiIndex.from_product([['counts'], df1.columns])


    a = ddp_events_df_new[["D_donation_id","ts_added_to_dataset"]].drop_duplicates()
    b = a.set_index("D_donation_id", inplace=False)
    these_donation_dates = b.to_dict()["ts_added_to_dataset"]
    df1["other","ts_added_to_dataset"] = df1.index.map(lambda x: these_donation_dates[x])


    df1.sort_values(by=[("other","ts_added_to_dataset")], inplace=True)
    #df1["other","D_id"] = list(range(len(df1)))



    donation_personas = generate_personas(ddp_events_df_new)
    if not donation_personas.empty and "D_donation_id" in donation_personas.columns:
        donation_personas.set_index("D_donation_id", inplace=True)
        donation_personas.columns = pd.MultiIndex.from_product([['personas'], donation_personas.columns])


    if verbose:
        print("Checking participant metadata files...")
    participant_metadata = {}
    for participant_data_file in data_io.listdir(storage_location="ddp_participants"):
        if participant_data_file.endswith(".json"):
            participant_metadata_raw = data_io.load_json(storage_location="ddp_participants", filename=participant_data_file)
            if verbose:
                print(f"    Found {len(participant_metadata_raw['Items']):,} items in the file {participant_data_file}")
            for item in participant_metadata_raw.get("Items", []):
                    py_item = {k: _deser(v) for k, v in item.items()}
                    participant_metadata[py_item['id']] = py_item

    participant_metadata_df = pd.DataFrame(participant_metadata).T
    participant_metadata_df.drop(["url","iat","pk","id","exp","profile","schemaChanged","appliedSchema"],axis=1, inplace=True)
    participant_metadata_df.columns = pd.MultiIndex.from_product([['participants'], participant_metadata_df.columns])

    combined_ddp_metadata = pd.merge(df1, participant_metadata_df, left_index=True, right_index=True, how="left")

    if not donation_personas.empty:
        combined_ddp_metadata = pd.merge(combined_ddp_metadata, donation_personas, left_index=True, right_index=True, how="left")


    if old_metadata_df is not None:
        combined_ddp_metadata = pd.concat([old_metadata_df, combined_ddp_metadata], axis=0)
        
    if save_to_disk_ok:
        if verbose:
            print(f"Saving updated metadata to disk. Shape: {combined_ddp_metadata.shape}")
        data_io.save_parquet(df=combined_ddp_metadata, storage_location="ddp_main", filename="ddp_metadata.parquet", verbose=verbose)

    if verbose:
        print(f"Shape of the combined metadata DF: {combined_ddp_metadata.shape}")

    return combined_ddp_metadata




# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------








def _identify_similar_donations(
    donation_events: pd.DataFrame = None,
    overlap_threshold: float = 0.5
) -> dict:
    """
    Identify similar donations based on timestamp overlap.

    check for similarities in the donations by looking for the same timestamps in the donations. 
    The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates

    Parameters
    ----------
    donation_events : pandas.DataFrame
        DataFrame containing the donation events. Must contain 'D_donation_id' and 'T_local_timestamp' columns.
    overlap_threshold : float, default 0.5
        The threshold for timestamp overlap ratio to consider donations as similar.

    Returns
    -------
    dict
        A dictionary containing sets of donation IDs:
        - "drops": IDs of donations to be dropped.
        - "keepers": IDs of donations to keep.
    """

    if donation_events is None:
        raise ValueError("donation_events cannot be None")

    # dropping df cols and changing timestamp column to integers which makes set operations faster
    fine_events_df = donation_events[['D_donation_id','T_local_timestamp']].copy()
    fine_events_df['T_local_timestamp'] = fine_events_df['T_local_timestamp'].astype('int64') / 1e9


    # the logic is based on comparing sets of timestamps, assuming that it is unlikely that two donations have
    # the same collection of timestamps
    ts_sets = fine_events_df.groupby('D_donation_id', observed=False)['T_local_timestamp'].apply(set).to_dict()
    unique_donations = list(ts_sets.keys())
    unique_donations = sorted(unique_donations, key=lambda x: len(ts_sets[x]), reverse=False)

    drop_candidates = set()

    for don_a in unique_donations:
        if don_a not in drop_candidates:
            for don_b in unique_donations:
                if (don_a != don_b) and (don_b not in drop_candidates):
                    ts_overlap = len(ts_sets[don_a] & ts_sets[don_b]) / (min(len(ts_sets[don_b]), len(ts_sets[don_a])))   
                    if (ts_overlap > overlap_threshold):
                        if len(ts_sets[don_b]) > len(ts_sets[don_a]):
                            drop_candidates.add(don_a)
                        else:
                            drop_candidates.add(don_b)
                        break

    keeper_donations = set(unique_donations) - drop_candidates

    return {"drops": drop_candidates, "keepers": keeper_donations}












def consolidate_ddp_logs(
    force_consolidation: bool = False,
    consolidate_from_scratch: bool = False,
    return_saved_data: bool = True,
    exclude_rejected: bool = True,
    save_to_disk_ok: bool = True,
    verbose: bool = False,
) -> tuple[bool, pd.DataFrame]:
    """
    Consolidate and refine raw DDP logs into a processed format.

    This function orchestrates the refinement of raw logs and returns the consolidated df. 
    Note that it does not save the df to disk. It also updates the donation metadata df. This function
    is the singular place in the code where this dataframe is updated.

    Parameters
    ----------
    force_consolidation : bool, default False
        Flag to force the consolidation process.
    verbose : bool, default False
        If True, prints progress updates to the console.

    Returns
    -------
    tuple[bool, pd.DataFrame]
        A tuple containing a boolean indicating whether updates were performed
        and the resulting consolidated DataFrame.
    """


    top_verbose = True

    if top_verbose:
        print("Checking for new raw DDP logs that needs refining...")
    result = refine_all_raw_ddp_logs_and_save(verbose=verbose)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            print("    ...all files already refined.")
        else:
            print(f"    ...refined {result['refined_files_after'] - result['refined_files_before']} files.")


    # --------------------------------------------------------------------------------------
    # get a list of refined ddp files
    refined_ddp_files = data_io.listdir(
        storage_location="ddp_processed",
        return_absolute_path=False,
        verbose=False)
    refined_ddp_files = [u for u in refined_ddp_files if u.endswith(".parquet")]


    # ---------------------------------------------------------------
    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(storage_location="recoded",filename="dataset_meta.json",verbose=verbose):
        dataset_meta = data_io.load_json(storage_location="recoded",filename="dataset_meta.json",verbose=verbose)
        if verbose:
            print("Dataset metadata loaded")
    else:
        dataset_meta = {"donations": {"filenames": []}}
    latest_filename_list = dataset_meta.get("donations", {}).get("filenames", [])
    latest_donation_ids = [ fn.replace(".parquet","") for fn in latest_filename_list]
    if top_verbose:
        print(f"Number of donations in the latest successful run of this process: {len(latest_donation_ids)}")

    # get a list of accepted refined ddp files
    if data_io.exists(storage_location="ddp_main",filename="ddp_metadata.parquet",verbose=verbose):
        ddp_meta_file_exists = True
        ddp_meta = data_io.load_parquet(storage_location="ddp_main", filename="ddp_metadata.parquet")
        if exclude_rejected:
            rejected_refined_ddp_files = ddp_meta[~ddp_meta[('other','accepted')]].index.to_list()
            rejected_refined_ddp_files = [f"{u}.parquet" for u in rejected_refined_ddp_files]
        else:
            rejected_refined_ddp_files = []
        accepted_refined_ddp_files = [u for u in refined_ddp_files if u not in rejected_refined_ddp_files]
    else:
        ddp_meta_file_exists = False
        rejected_refined_ddp_files = []
        accepted_refined_ddp_files = []

    donations_recoded_file_exists = data_io.exists(storage_location="recoded",filename="donations_recoded.parquet",verbose=verbose)

    # if all files found in the refine folder are already accepted, then no need to consolidate
    if donations_recoded_file_exists and \
        ddp_meta_file_exists and \
        not force_consolidation and \
        set(accepted_refined_ddp_files) <= set(latest_filename_list):
        if top_verbose:
            print("No new refined DDP files found. No need to consolidate.")
        if return_saved_data:
            thing = data_io.load_parquet(storage_location="recoded", filename="donations_recoded.parquet")
            if verbose: print("Returning existing file.")
            return False, thing

        return False, None


    # --------------------------------------------------------------------------------------
    if top_verbose:
        print("Found new refined DDP files or no consolidated file exists. Consolidating...")
    many_ddp_logs = []


    # --------------------------------------------------------------------------------------
    # if I don't want to consolidate from scratch, I can load the existing data and only add the new files

    if donations_recoded_file_exists and ddp_meta_file_exists and not consolidate_from_scratch:
        # first look in cache, then look in recoded
        if data_io.exists(storage_location="cache",filename="core_donations.parquet",verbose=verbose):
            if top_verbose:
                print("Loading existing DDP logs from cache...", end="", flush=True)
            many_ddp_logs = [data_io.load_parquet(storage_location="cache", filename="core_donations.parquet")]
        elif data_io.exists(storage_location="recoded",filename="donations_recoded.parquet",verbose=verbose):
            if top_verbose:
                print("Loading existing DDP logs from main storage...", end="", flush=True)
            many_ddp_logs = [data_io.load_parquet(storage_location="recoded", filename="donations_recoded.parquet")]
        print(f"...done. Shape: {many_ddp_logs[0].shape}. Unique donations: {many_ddp_logs[0].D_donation_id.nunique()}.")

    # --------------------------------------------------------------------------------------
    # if there is a df in many_ddp_logs at this stage, it means that I found a previously consolidated df and that 
    # I don't want to force consolidation from scratch. So I only need to add the new files.
    if len(many_ddp_logs) == 1:
        # get a list of new refined files
        new_refined_files = [u for u in refined_ddp_files if u not in latest_filename_list and u not in rejected_refined_ddp_files]
        if top_verbose:
            print(f"Loading {len(new_refined_files)} new logs to concatenate with already concatenated donations...")

    # --------------------------------------------------------------------------------------
    # if many_ddp_logs is empty - it means I want to force consolidate from scratch or no previously 
    # consolidated donations exist - start from scratch and load all individual donations
    else:
        if top_verbose:
            if exclude_rejected:
                new_refined_files = [u for u in refined_ddp_files if u not in rejected_refined_ddp_files]
                print(f"Loading {len(new_refined_files)} (not previously rejected) donation logs to concatenate from scratch...")
            else:
                new_refined_files = refined_ddp_files
                print(f"Loading {len(new_refined_files)} donation logs to concatenate from scratch...")

    for u in new_refined_files:
        if top_verbose:
            print(".", end="", flush=True)
        many_ddp_logs.append(data_io.load_parquet(storage_location="ddp_processed", filename=u))
    if top_verbose:
        print()

    # --------------------------------------------------------------------------------------
    # concatenate all refined files
    if top_verbose:
        print(f"Concatenating {len(many_ddp_logs)} refined files...")

    # drop columns with all null values before concatenating
    for dl in many_ddp_logs:
        dd = dl.notnull().sum()
        dl.drop(dd[dd==0].index.tolist(), axis=1, inplace=True)

    concatenated_ddp_logs = pd.concat(many_ddp_logs)
    if top_verbose:
        print(f"    ...done - initial shape of the concatenated dataframe: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")



    # --------------------------------------------------------------------------------------
    # naive drop_dupes based on these three columns
    concatenated_ddp_logs = concatenated_ddp_logs.drop_duplicates(subset=["D_donation_id","T_local_timestamp","item_id"], keep="first").copy()
    if top_verbose:
        print(f"Shape after naive duplication drop: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")

    if set(concatenated_ddp_logs["D_donation_id"].unique()) == set(latest_donation_ids):
        if top_verbose:
            print("Donation dataset have not changed. Returning dataset. (1)")
        return concatenated_ddp_logs
        


    donation_counts = concatenated_ddp_logs.groupby('D_donation_id')["D_feature_name"].value_counts().unstack().fillna(0).astype(int)


    # --------------------------------------------------------------------------------------
    # create list of donations which has a very small number of watched videos
    too_small_donations = list(donation_counts[(donation_counts["watch"]<5)].index)
    too_small_donations = list(set(concatenated_ddp_logs.D_donation_id.unique()) & set(too_small_donations))

    if len(too_small_donations) > 0:
        if verbose:
            print(f"The following donations have fewer than 5 watch events and will be dropped: \n  - {'\n  - '.join(too_small_donations)}")

        concatenated_ddp_logs = concatenated_ddp_logs[~concatenated_ddp_logs.D_donation_id.isin(too_small_donations)].copy()
        if top_verbose:
            print(f"Shape after dropping donations with fewer than 5 watch events: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")


    if set(concatenated_ddp_logs["D_donation_id"].unique()) == set(latest_donation_ids):
        if top_verbose:
            print("Donation dataset have not changed. Returning dataset. (2)")
        return concatenated_ddp_logs
        


    # --------------------------------------------------------------------------------------
    # check for similarities among donations by comparing donations' sets of event timestamps. 
    if top_verbose:
        print(f"Identifying for similar/overlapping donations and only keeping the bigger one of these...")

    similarity_results = _identify_similar_donations(donation_events=concatenated_ddp_logs, overlap_threshold=0.2)

    accepted_new_refined_files = list(set(new_refined_files) & set(similarity_results["keepers"]))
    #if len(accepted_new_refined_files) > 0:
    # drop the events in these donations
    concatenated_ddp_logs = concatenated_ddp_logs[~concatenated_ddp_logs["D_donation_id"].isin(similarity_results["drops"])].copy()
    if top_verbose:
        if len(similarity_results["drops"])>0:
            print(f"Dropped {len(similarity_results["drops"])} donation(s) which were too similar to other donations.")

    if len(accepted_new_refined_files) > 0:
        if verbose:
            print(f"    ...done. New shape: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")
    else:
        if verbose:
            print("    ...done. No new donations were accepted. Returning dataset. (3)")
        return concatenated_ddp_logs
        

    # --------------------------------------------------------------------------------------
    # update the donation metadata with a column to signify which donations are accepted
    # and included in the dataset. This is necessary since the donation metadata df contains
    # all donations, even those that are overlapping or are too small to be included in the dataset.  
    if top_verbose:
        print("Updating 'accepted' status in donation metadata file...")
    accepted_donation_ids = concatenated_ddp_logs["D_donation_id"].unique()
    donation_metadata = generate_donation_metadata(
        ddp_events_df=concatenated_ddp_logs, 
        update_col=None,
        verbose=verbose,
        save_to_disk_ok=save_to_disk_ok,
        )
    accepted_col = pd.Series(donation_metadata.index.isin(accepted_donation_ids), index=donation_metadata.index, name=("other", "accepted"))
    _ = generate_donation_metadata(
        ddp_events_df=None, 
        update_col=accepted_col,
        verbose=verbose,
        save_to_disk_ok=save_to_disk_ok,
        )
    if top_verbose:
        print("    ...done updating donation metadata")


    # --------------------------------------------------------------------------------------
    # reset index
    concatenated_ddp_logs.reset_index(drop=True, inplace=True)


    if top_verbose:
        print(f"Recalculate session details to ensure that the session IDs are unique.")
    concatenated_ddp_logs = _add_session_info_to_ddp_log(concatenated_ddp_logs, verbose=verbose)
    if "session_id" in concatenated_ddp_logs.columns:
        concatenated_ddp_logs["session_id"] = concatenated_ddp_logs["session_id"].map(lambda x:f"SD{x:05}" if pd.notna(x) else pd.NA) # SD kind of indicates that this is a S-ession and D-onation


    # --------------------------------------------------------------------------------------
    # is this necessary - I don't know...
    concatenated_ddp_logs = convert_dtypes_to_pyarrow(concatenated_ddp_logs)


    # --------------------------------------------------------------------------------------
    # calculate memory usage
    memory_per_column = concatenated_ddp_logs.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        print(f"...done. Combined all logs into shape: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()} (memory usage: {total_memory_mb:.2f} MB)")


    # ---------------------------------------------------------------
    # update the dataset meta file
    if not "donations" in dataset_meta:
        dataset_meta["donations"] = {}
    dataset_meta["donations"]["filenames"] = refined_ddp_files
    if save_to_disk_ok:
        _ = data_io.save_json(dataset_meta, "recoded", "dataset_meta.json")


    return True, concatenated_ddp_logs












# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------








def load_donation_data(
    study_name = None, 
    all_data = None,
    verbose=False):



    if study_name is None:
        raise ValueError("!!! [DDP] study_name must be specified")

 
    print(f"    [DDP] Loading data for study...")
    
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

    sel = [("T_local_timestamp", ">=", START_DATE),("T_local_timestamp", "<=", END_DATE)]

    the_selected_donations = fyp_cf["study_defs"][study_name].get("SELECTED_DONATIONS",[])
    if len(the_selected_donations) > 0:
        the_selected_donations = [re.search(r'\[(.*?)\]', str(x)).group(1) if re.search(r'\[(.*?)\]', str(x)) else x for x in the_selected_donations]
        sel.append(("D_donation_id", "in", the_selected_donations))

    if all_data is None:
        if verbose:
            print(f"    [DDP] Loading donation events from main storage")
        out_df = data_io.load_parquet("recoded", "donations_recoded.parquet", filters=sel,verbose=verbose)

    else:
        if verbose:
            print(f"    [DDP] Selecting date range from cached donation data")
        cached_ddp_events_df = all_data.copy()
        out_df = cached_ddp_events_df[(cached_ddp_events_df.T_local_timestamp>=START_DATE) & (cached_ddp_events_df.T_local_timestamp<=END_DATE)].copy()

        if not "D_donation_id" in out_df.columns or not "T_local_timestamp" in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] No events found in date range. Returning None.")
            return None

        if len(the_selected_donations) > 0:
            out_df = out_df[out_df["D_donation_id"].isin(the_selected_donations)].copy()

        if not "D_donation_id" in out_df.columns or not "T_local_timestamp" in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] The selected donations have no events in the date range. Returning None.")
            return None

    print(f"    [DDP] ...done. | Shape: {out_df.shape} | Unique donations: {out_df.D_donation_id.nunique()} | Date range: {out_df.T_local_timestamp.min():%Y-%m-%d} -- {out_df.T_local_timestamp.max():%Y-%m-%d}")


    return out_df







def simple_sample_ddp_events(
    study_name = None, 
    all_ddp_events_df = None, 
    verbose=False):




    def _filter_and_sample(df, group_cols, x_threshold, y_samples):
        """
        Filters groups by size and samples rows.
        """
        # 1. Filter groups 
        group_sizes = df.groupby(group_cols)[group_cols[0]].transform('size')
        df_filtered = df[group_sizes >= x_threshold]
        
        # 2. Sampling
        sampled_indices = df_filtered.groupby(group_cols, group_keys=False).apply(
            lambda g: g.sample(n=min(len(g), y_samples), random_state=42),
            include_groups=False
        )

        result = df_filtered.loc[sampled_indices.index]

        return result


    
    if all_ddp_events_df is None:
        raise ValueError("[DD Sampling] all_ddp_events_df cannot be None")

    the_df = all_ddp_events_df.copy()

    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.

    grouping_factors = get_grouping_factors_from_var_schema(some_events_df = the_df, verbose=False)

    if len(grouping_factors) != 2:
        raise ValueError("!!! [DD Sampling] Group factors must be exactly 2")

    if not "D_donation_id" in grouping_factors:
        raise ValueError("!!! [DD Sampling] Group factors must include D_donation_id")

    # make sure D_donation_id is the first element 
    grouping_factors.remove("D_donation_id")
    grouping_factors = ["D_donation_id"] + grouping_factors

    if verbose:
        print(f"    [DD Sampling] Group factors: {grouping_factors}")

    if not "study_defs" in fyp_cf:
        init_study_defs()

    MIN_EVENTS_REQUIRED = fyp_cf["study_defs"][study_name].get("MIN_EVENT_COUNT_REQUIRED_PER_AGG_GROUP",10)
    MAX_EVENTS_SELECTED = fyp_cf["study_defs"][study_name].get("MAX_EVENT_COUNT_SELECTED_PER_AGG_GROUP",100)
    MIN_GROUP_COUNT_REQUIRED_PER_DONATION = fyp_cf["study_defs"][study_name].get("MIN_GROUP_COUNT_REQUIRED_PER_DONATION",10)
    MAX_GROUP_COUNT_SELECTED_PER_DONATION = fyp_cf["study_defs"][study_name].get("MAX_GROUP_COUNT_SELECTED_PER_DONATION",100)

    # sorting the events by donation and event id in order to have a replicable sample
    #donation_metadata_df = data_io.load_parquet(storage_location="ddp_main", filename="ddp_metadata.parquet")
    #donation_to_d_dict = donation_metadata_df[("other","D_id")].to_dict()

    #the_df["D_id"] = the_df["D_donation_id"].map(donation_to_d_dict)
    #the_df = the_df.sort_values(by=["D_id","event_id"])


    # Separate watch and non-watch events 
    all_watch_events_df = the_df[the_df.D_feature_name=="watch"].copy()
    all_nonwatch_events_df = the_df[the_df.D_feature_name!="watch"].copy()
    sample_frame_size = len(all_watch_events_df)

    if verbose:
        print(f"    [DD Sampling] Watch events: {len(all_watch_events_df):,}  |  Non-watch events: {len(all_nonwatch_events_df):,}")


    if verbose:
        print(f"    [DD Sampling] Dropping groups with less than {MIN_EVENTS_REQUIRED} events")
        print(f"    [DD Sampling] Sampling at most {MAX_EVENTS_SELECTED} events from each remaining group. This might take a moment...")
    # select agg groups with the required number of events
    ddp_watch_events_within_agg_group_size_limits = _filter_and_sample(all_watch_events_df, grouping_factors, MIN_EVENTS_REQUIRED, MAX_EVENTS_SELECTED)
    if verbose:
        sample_size = len(ddp_watch_events_within_agg_group_size_limits)
        if sample_frame_size > 0:
            print(f"    [DD Sampling] Watch events after sampling: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # build a df with unique pairs of the two group factors
    unique_group_factor_pairs = ddp_watch_events_within_agg_group_size_limits[grouping_factors].drop_duplicates()

    if verbose:
        print(f"    [DD Sampling] Dropping donations with less than {MIN_GROUP_COUNT_REQUIRED_PER_DONATION} groups within the limits")
        print(f"    [DD Sampling] Sampling at most {MAX_GROUP_COUNT_SELECTED_PER_DONATION} groups from each remaining donation. This might take a moment...")
    # select collections with a required number of groups
    donations_within_group_count_limits = _filter_and_sample(unique_group_factor_pairs, grouping_factors[:1], MIN_GROUP_COUNT_REQUIRED_PER_DONATION, MAX_GROUP_COUNT_SELECTED_PER_DONATION)
    if verbose:
        print(f"    [DD Sampling] Donations groups remaining after sampling: {len(donations_within_group_count_limits):,}")


    # ----------------------------------------------------------------------
    # find the watch events in the selected groups
    # 1. start with the events in the agg groups that meet the group size requirements and set the index to the group factors
    ddp_watch_events_in_candidate_groups = ddp_watch_events_within_agg_group_size_limits.set_index(grouping_factors)

    # 2. select the events in the groups that meet the group count requirements
    ddp_watch_events_in_selected_groups = ddp_watch_events_in_candidate_groups.loc[donations_within_group_count_limits.set_index(grouping_factors).index]
    ddp_watch_events_in_selected_groups = ddp_watch_events_in_selected_groups.reset_index()
    if verbose:
        sample_size = len(ddp_watch_events_in_selected_groups)
        if sample_frame_size > 0:
            print(f"    [DD Sampling] Watch events remaining in the sampled groups: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # ----------------------------------------------------------------------
    # find the non-watch events in the selected groups - note that since the non-watch events are not
    # sampled, there is a disproportional number of non-watch events in the sampled dataset compared 
    # to the number of watch events
    # 1. find all unique group factor pairs for the non-watch events
    unique_group_factor_pairs_for_nonwatch_events = all_nonwatch_events_df[grouping_factors].drop_duplicates()

    # 2. find the non-watch groups that are in the selected groups. This is necessary since there are some non-watch
    # groups that don't have any watch events, and I don't want these included in the sample
    nonwatch_groups = set(unique_group_factor_pairs_for_nonwatch_events.set_index(grouping_factors).index)
    selected_watch_groups = set(donations_within_group_count_limits.set_index(grouping_factors).index)
    selected_nonwatch_groups = list(nonwatch_groups & selected_watch_groups)

    selected_nonwatch_groups = pd.DataFrame(selected_nonwatch_groups, columns=grouping_factors)
    selected_nonwatch_groups = selected_nonwatch_groups.convert_dtypes(dtype_backend="pyarrow").set_index(grouping_factors).index

    mask = all_nonwatch_events_df.set_index(grouping_factors).index.isin(selected_nonwatch_groups)
    ddp_nonwatch_events_in_selected_groups = all_nonwatch_events_df[mask]
    if verbose:
        print(f"    [DD Sampling] Non-Watch events remaining in the selected groups: {len(ddp_nonwatch_events_in_selected_groups):,} (100% of original)")
    #print(ddp_nonwatch_events_in_selected_groups[:5])
    #ddp_nonwatch_events_in_selected_groups.reset_index(inplace=True)

    combined = pd.concat([ddp_watch_events_in_selected_groups, ddp_nonwatch_events_in_selected_groups])
    if verbose:
        print(f"    [DD Sampling] Combining the (not sampled) non-watch events with the sampled watch events with : {len(combined):,} in {len(combined[grouping_factors].drop_duplicates()):,} groups")
    combined.drop("D_id", axis=1, inplace=True, errors='ignore')


    enrichment_status_df = data_io.load_parquet(
        storage_location="recoded",
        filename="enrichment_status.parquet")
    

    combined_deduped = combined.drop_duplicates(subset="item_id", keep="first")[["item_id"]]

    combined_deduped_enrichment_status = pd.merge(left=combined_deduped, right=enrichment_status_df, left_on='item_id', right_index=True, how='left')

    enrichment_summary = combined_deduped_enrichment_status.select_dtypes(include=["bool"]).fillna(False).sum().to_dict()

    mapper = fyp_cf['var_schema'][['variable_name','display_name']].dropna().set_index('variable_name').to_dict()['display_name']

    print(f"    [DD Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[grouping_factors].drop_duplicates()):,} groups")
    print(f"    [DD Sampling] - Unique videos: {len(combined_deduped_enrichment_status):,}")
    for k in enrichment_summary:
        if len(combined_deduped_enrichment_status) > 0:
            print(f"    [DD Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} ({enrichment_summary[k]/len(combined_deduped_enrichment_status):.0%})")
        else:
            print(f"    [DD Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} (N/A)")

    return combined



















################################################################################
################################################################################
################################################################################
################################################################################
################################################################################


