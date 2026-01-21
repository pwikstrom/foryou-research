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

from fyp.fyp_main import convert_dtypes_to_pyarrow, initialize
from fyp.recode_variables import *
from fyp.calc_donation_stats import generate_personas
import fyp.data_io as data_io

from collections import deque
import numpy as np

import datetime as _dt
from pathlib import Path
import subprocess
from shlex import quote as shlex_quote
from shutil import rmtree as shutil_rmtree
from os.path import join as local_join
from os import listdir as local_listdir
from pathlib import Path
import datetime as _dt





"""
def _remove_link_events_with_corrupt_links(some_events_df):

    non_video_ddp_events_df = some_events_df[some_events_df["primary_label"] != "link"].copy()
    video_ddp_events_df = some_events_df[some_events_df["primary_label"] == "link"].copy()
    
    # Vectorized string length calculation
    url_lengths = video_ddp_events_df.primary_value.str.len()
    most_common_url_length = int(url_lengths.value_counts().index[0])
    
    video_ddp_events_df = video_ddp_events_df[url_lengths == most_common_url_length].copy()
    some_events_df = pd.concat([video_ddp_events_df, non_video_ddp_events_df])

    return some_events_df

"""







def get_donation_metadata_from_aio_aws(
                        cf = None,
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

    if cf is None:
        cf = initialize()

    # Compute cut‑off time
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())
    file_stamp = now.strftime("%Y%m%d%H%M%S") 

    # Prepare destination
    filename = f"ddp_metadata_{file_stamp}.json"
    temp_file = local_join(cf["paths"]["temp"], filename)

    # Assemble the AWS CLI command
    scan_cmd = (
        "aws dynamodb scan "
        f"--table-name {shlex_quote(table_name)} "
        "--select ALL_ATTRIBUTES "
        "--page-size 500 "
        "--max-items 100000 "
        "--output json"
    )
    full_cmd = f"{scan_cmd} > {shlex_quote(str(temp_file))}"

    # Run it
    try:
        subprocess.run(full_cmd, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error downloading participant metadata running AWS CLI command: {e}")
        return None

    # move to permanent storage
    data_io.move(
        cf=cf,
        src_storage_location="temp",
        dst_storage_location=storage_location,
        filename=filename,
        verbose=verbose
    )








def get_recent_data_donations_from_aio_aws(
                    cf: dict = None,
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
    cf : dict, optional
        Project configuration. If not provided, it will be initialized.
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



    if cf is None:
        cf = initialize()

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

    temp_dir_path = local_join(cf["paths"]["temp"], f"download_batch_{now.strftime('%Y%m%d%H%M%S')}")
    dest = Path(temp_dir_path).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 3) Build the shell command (quote everything that may contain spaces)
    # ------------------------------------------------------------------
    scan_cmd = (
        "aws dynamodb scan "
        f"--table-name {shlex_quote(table_name)} "
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
        f"aws s3 cp \"s3://{bucket}/donation/{{}}\" {shlex_quote(str(dest))}"
    )
    
    # ------------------------------------------------------------------
    # 4) Run the download to temp
    # ------------------------------------------------------------------
    print(f"Downloading recent donations to temporary storage: {dest}")
    subprocess.run(full_cmd, shell=True, check=True)

    # ------------------------------------------------------------------
    # 5) Move/Upload files to ddp_raw storage
    # ------------------------------------------------------------------
    downloaded_files = local_listdir(dest)
    print(f"Transferring {len(downloaded_files)} files to ddp_raw storage...")
    
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
                data_io.save_json(cf, data, "ddp_raw", filename)
                count += 1
            except Exception as e:
                print(f"Failed to process/upload {filename}: {e}")

    print(f"Successfully processed {count} files.")

    # ------------------------------------------------------------------
    # 6) Cleanup Temp
    # ------------------------------------------------------------------
    try:
        shutil_rmtree(dest)
    except Exception as e:
        print(f"Warning: Failed to clean up temp directory {dest}: {e}")









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
            print("Adding session stats to DDP data",ddp_log.shape)
        
    else:
        if verbose:
            print("no ddp data")

    return ddp_log









def refine_one_raw_ddp_log(
    cf: dict = None, 
    donation_id: str = None,
    verbose: bool = False):

    if cf is None:
        cf = initialize()

    # loading a json with the name == donation id
    donation_dict = data_io.load_json(
        cf=cf, 
        storage_location="ddp_raw",
        filename=donation_id,
        verbose=verbose
    )

    mod_time_timestamp = data_io.getmtime(cf=cf, storage_location="ddp_raw", filename=donation_id)
    mod_time_timestamp = _dt.datetime.fromtimestamp(mod_time_timestamp)


    raw_data_donation_top_keys = list(donation_dict.keys())
    if 'ad_preferences' in raw_data_donation_top_keys or 'CONTENT_INTERACTION' in raw_data_donation_top_keys:
        if verbose:
            print(f"{donation_id} is not TikTok data, cannot process this one")
        return "[ERROR]: Not TikTok data"


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
        return ["ERROR: No donation items found in file", donation_id]

    all_ddp_events_df = pd.DataFrame.from_records(donation_items)

    # this is an immutable id for each event in the donation file - it reflects the order in which the events were recorded in the raw file
    all_ddp_events_df["event_id"] = all_ddp_events_df.index.astype("uint64[pyarrow]")

    # --- process the dataframe ---------------------------

    # keep rows that have at least one variable and contain 'date'
    mask_date = all_ddp_events_df['variable_list'].map(lambda lst: 'date' in lst)
    all_ddp_events_df = all_ddp_events_df[mask_date & (all_ddp_events_df['variable_list'].map(len) > 0)].copy()

    # extract primary_label and primary_value from variable_list and value_list
    all_ddp_events_df['primary_label'] = all_ddp_events_df['variable_list'].str[1].convert_dtypes(dtype_backend="pyarrow")
    all_ddp_events_df['primary_value'] = all_ddp_events_df['value_list'].str[1].convert_dtypes(dtype_backend="pyarrow")

    # get the date from the value list
    all_ddp_events_df['date'] = pd.to_datetime(all_ddp_events_df['value_list'].str[0]).convert_dtypes(dtype_backend="pyarrow")

    # type donation_id to pyarrow string
    all_ddp_events_df['donation_id'] = all_ddp_events_df['donation_id'].convert_dtypes(dtype_backend="pyarrow")


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
    # to ns → s int
    all_ddp_events_df['timestamp'] = (all_ddp_events_df['date'].astype("int64[pyarrow]") // 1_000_000_000).astype("int64[pyarrow]")

    # add random noise to the timestamp. Useful when sorting events to make sure no event happens simultaneously
    #all_ddp_events_df['ts_jiggled'] = all_ddp_events_df['date'].astype("int64[pyarrow]") + np.random.randint(-10_000, 10_000, size=len(all_ddp_events_df))
    del all_ddp_events_df['date']

    # Extract sample_id from ts_jiggled (I'm not sure if I'm using this any longer)
    #all_ddp_events_df["sample_id"] = all_ddp_events_df.ts_jiggled.astype(str).str[-4:].astype("int64[pyarrow]")

    # -----------------------------------------------------
    # identify post events
    post_events = [k for k in all_ddp_events_df.index if "whocanview" in all_ddp_events_df.loc[k,"variable_list"]]
    all_ddp_events_df.loc[post_events,"feature_name"] = "post"
    all_ddp_events_df.loc[post_events,"primary_label"] = "post_link"


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


    # Feature_name is NA for login events - not sure why, but this changes that
    all_ddp_events_df.loc[all_ddp_events_df[all_ddp_events_df["primary_label"]=="ip"].index,"feature_name"] = "login_event"


    print(f"Current shape: {all_ddp_events_df.shape}")
    print(f"The DDP events range from {all_ddp_events_df.timestamp.min()} -- {all_ddp_events_df.timestamp.max()}")


    all_ddp_events_df = extract_local_time_features(
        cf = cf,
        some_events_df_in = all_ddp_events_df,
        kind_of_log = 'ddp',
        verbose = verbose)


    
    # rename columns
    all_ddp_events_df = all_ddp_events_df.rename(columns={c:"D_"+c if not c in ["item_id","event_id"] and not re.match(r"^[A-Z]_", c) else c for c in all_ddp_events_df.columns}).copy()
    all_ddp_events_df = rename_columns(all_ddp_events_df)

    # Sort by timestamp and reset index
    all_ddp_events_df.sort_values("T_local_timestamp", inplace=True)
    all_ddp_events_df.reset_index(drop=True, inplace=True)


    # assign session IDs etc. These are just placeholders for now,
    # Session IDs will be updated when donations are merged.
    all_ddp_events_df = _add_session_info_to_ddp_log(all_ddp_events_df, verbose=verbose)


    if verbose:
        print(f"Current shape: {all_ddp_events_df.shape}")




    # only keep columns as defined by the variable schema
    dropped_vars_str = textwrap.wrap(", ".join(list(set(all_ddp_events_df.columns) - set(cf['var_schema'].variable_name))), width=120)
    relevant_cols = [c for c in cf['var_schema'].variable_name if c in all_ddp_events_df.columns]
    all_ddp_events_df = all_ddp_events_df[relevant_cols].copy()

    if verbose:
        print(f"Dropped these columns, which are not in the variable schema:\n{"\n".join(dropped_vars_str)}\nCurrent shape: {all_ddp_events_df.shape}")
    

    all_ddp_events_df["date_added_to_dataset"] = mod_time_timestamp

    all_ddp_events_df = recode_events_df(
        cf = cf,
        study_dataset = all_ddp_events_df,
        drop_single_value_cols = False,
        verbose = verbose
        )




    if verbose:
        print(f"Final shape: {all_ddp_events_df.shape}")
        print("------------------------------------------------\n\n")


    return all_ddp_events_df








def refine_all_raw_ddp_logs_and_save(cf = None, verbose=False):

    if cf is None:
        cf = initialize()
    result = {}
    
    raw_ddp_files = data_io.listdir(
        cf=cf,
        storage_location="ddp_raw",
        return_absolute_path=False,
        verbose=False)
    raw_ddp_files = [u for u in raw_ddp_files if not u.startswith(".")]



    result["raw_files"] = len(raw_ddp_files)

    refined_ddp_files = data_io.listdir(
        cf=cf,
        storage_location="ddp_processed",
        return_absolute_path=False,
        verbose=False)
    refined_ddp_files = [u for u in refined_ddp_files if u.endswith(".parquet")]
    result["refined_files_before"] = len(refined_ddp_files)

    for u in raw_ddp_files:
        if u+".parquet" in refined_ddp_files:
            continue


        if verbose:
            print(f"Refining: {u}")
        new_flat = refine_one_raw_ddp_log(
            cf=cf,
            donation_id=u,
            verbose=verbose
            )

        if isinstance(new_flat, pd.DataFrame):
            data_io.save_parquet(cf=cf, df=new_flat, filename=u+".parquet", storage_location="ddp_processed", verbose=verbose)
        else:
            pass

    refined_ddp_files = data_io.listdir(
        cf=cf,
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
    cf: dict | None = None, 
    ddp_events_df: pd.DataFrame | None = None,
    update_col: pd.Series | None = None,
    sort_by: str | None = None, 
    verbose: bool = False
    ) -> pd.DataFrame:
    """
    Generate or update donation metadata, either by calculating statistics from events 
    or by merging a specific column into existing metadata.

    Parameters
    ----------
    cf : dict, optional
        Configuration dictionary. If None, initializes via initialize().
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

    if cf is None:
        cf = initialize()


    if data_io.exists(cf=cf, storage_location="ddp_main", filename="ddp_metadata.parquet"):
        old_metadata_df = data_io.load_parquet(cf=cf, storage_location="ddp_main", filename="ddp_metadata.parquet")
        if verbose:
            print(f"Loaded existing metadata from storage. Shape: {old_metadata_df.shape}")
    else:
        if verbose:
            print("No calculated metadata found in storage")
        old_metadata_df = pd.DataFrame()


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
            new_metadata_df = new_metadata_df.sort_index(axis='columns').sort_values(('other','D_id')).copy()
            data_io.save_parquet(cf=cf, df=new_metadata_df, storage_location="ddp_main", filename="ddp_metadata.parquet", verbose=verbose)
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


    a = ddp_events_df_new[["D_donation_id","date_added_to_dataset"]].drop_duplicates()
    b = a.set_index("D_donation_id", inplace=False)
    these_donation_dates = b.to_dict()["date_added_to_dataset"]
    df1["other","date_added_to_dataset"] = df1.index.map(lambda x: these_donation_dates[x])


    df1.sort_values(by=[("other","date_added_to_dataset")], inplace=True)
    df1["other","D_id"] = list(range(len(df1)))



    donation_personas = generate_personas(ddp_events_df_new)
    donation_personas.set_index("D_donation_id", inplace=True)
    donation_personas.columns = pd.MultiIndex.from_product([['personas'], donation_personas.columns])


    if verbose:
        print("Checking all participant metadata files ")
    participant_metadata = {}
    for participant_data_file in data_io.listdir(cf=cf, storage_location="ddp_participants"):
        if participant_data_file.endswith(".json"):
            participant_metadata_raw = data_io.load_json(cf=cf, storage_location="ddp_participants", filename=participant_data_file)
            if verbose:
                print(f"P {len(participant_metadata_raw['Items'])} items in the file {participant_data_file}")
            for item in participant_metadata_raw.get("Items", []):
                    py_item = {k: _deser(v) for k, v in item.items()}
                    participant_metadata[py_item['id']] = py_item

    participant_metadata_df = pd.DataFrame(participant_metadata).T
    participant_metadata_df.drop(["url","iat","pk","id","exp","profile","schemaChanged","appliedSchema"],axis=1, inplace=True)
    participant_metadata_df.columns = pd.MultiIndex.from_product([['participants'], participant_metadata_df.columns])

    combined_ddp_metadata = pd.merge(df1, participant_metadata_df, left_index=True, right_index=True, how="left")
    combined_ddp_metadata = pd.merge(combined_ddp_metadata, donation_personas, left_index=True, right_index=True, how="left")


    if old_metadata_df is not None:
        if verbose:
            print(f"Adding {len(combined_ddp_metadata)} rows to the existing metadata DF")
        combined_ddp_metadata = pd.concat([old_metadata_df, combined_ddp_metadata], axis=0)
        
    combined_ddp_metadata = combined_ddp_metadata.sort_index(axis='columns').sort_values(('other','D_id')).copy()
    data_io.save_parquet(cf=cf, df=combined_ddp_metadata, storage_location="ddp_main", filename="ddp_metadata.parquet", verbose=verbose)

    if verbose:
        print(f"Shape of the combined metadata DF: {combined_ddp_metadata.shape}")

    return combined_ddp_metadata




# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------







def _identify_similar_donations(
    new_events: pd.DataFrame = None,
    old_events: pd.DataFrame = None,
    dont_check_these_cols: list = [],
    overlap_threshold: float = 0.5
) -> dict:
    """
    Identify similar donations based on timestamp overlap.

    check for similarities in the donations by looking for the same timestamps in the donations. 
    The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates


    This function compares the timestamps of events in new donations against old donations (or within new donations themselves)
    to identify potential duplicates or highly similar donations.

    Parameters
    ----------
    new_events : pandas.DataFrame
        DataFrame containing the new donation events. Must contain 'D_donation_id',  'D_feature_name', and 'T_local_timestamp' columns.
    old_events : pandas.DataFrame, optional
        DataFrame containing existing donation events to compare against. If None, compares new_events against itself.
    dont_check_these_cols : list, optional
        List of feature names to exclude from the comparison.
    overlap_threshold : float, default 0.5
        The threshold for timestamp overlap ratio to consider donations as similar.

    Returns
    -------
    dict
        A dictionary containing sets of donation IDs:
        - "new_drops": IDs of new donations to be dropped.
        - "old_drops": IDs of old donations to be dropped.
        - "keepers": IDs of donations to keep.
    """

    if new_events is None:
        raise ValueError("new_events cannot be None")
    new_events_ts_dict = {}
    fine_events_df = new_events[~new_events["D_feature_name"].isin(dont_check_these_cols)].copy()
    for d,i in fine_events_df.groupby('D_donation_id'):
        new_events_ts_dict[d] = set([j for j in i['T_local_timestamp'].values])
    
    if old_events is not None:
        old_events_ts_dict = {}
        fine_events_df = old_events[~old_events["D_feature_name"].isin(dont_check_these_cols)].copy()
        for d,i in fine_events_df.groupby('D_donation_id'):
            old_events_ts_dict[d] = set([j for j in i['T_local_timestamp'].values])
    else:
        old_events_ts_dict = new_events_ts_dict.copy()

    new_drop_candidates = set()
    old_drop_candidates = set()
    keeper_donations = set()

    unique_new_donations = list(new_events_ts_dict.keys())
    unique_old_donations = list(old_events_ts_dict.keys())

    unique_new_donations = sorted(unique_new_donations, key=lambda x: len(new_events_ts_dict[x]), reverse=False)
    unique_old_donations = sorted(unique_old_donations, key=lambda x: len(old_events_ts_dict[x]), reverse=False)

    for a_new_donation in unique_new_donations:
        if a_new_donation not in (new_drop_candidates | old_drop_candidates):
            for an_old_donation in unique_old_donations:
                if (a_new_donation != an_old_donation) and (an_old_donation not in (new_drop_candidates | old_drop_candidates)):
                    ts_overlap = len(new_events_ts_dict[a_new_donation] & old_events_ts_dict[an_old_donation]) / \
                                                                (min(len(old_events_ts_dict[an_old_donation]), len(new_events_ts_dict[a_new_donation])))   
                    if (ts_overlap > overlap_threshold):
                        if len(old_events_ts_dict[an_old_donation]) > len(new_events_ts_dict[a_new_donation]):
                            new_drop_candidates.add(a_new_donation)
                            keeper_donations.add(an_old_donation)
                            #print(f"Dropping new donation: {a_new_donation} and {an_old_donation} with overlap {ts_overlap}")
                        else:
                            old_drop_candidates.add(an_old_donation)
                            keeper_donations.add(a_new_donation)
                            #print(f"Dropping old donation: {an_old_donation} and {a_new_donation} with overlap {ts_overlap}")

                        break
    
    return {"new_drops": new_drop_candidates, "old_drops": old_drop_candidates, "keepers": keeper_donations}












def consolidate_ddp_logs(
    cf: dict | None = None,
    force_consolidation: bool = False,
    return_saved_data: bool = True,
    verbose: bool = False,
) -> tuple[bool, pd.DataFrame]:
    """
    Consolidate and refine raw DDP logs into a processed format.

    This function orchestrates the refinement of raw logs and returns the consolidated df. 
    Note that it does not save the df to disk. It also updates the donation metadata df. This function
    is the singular place in the code where this dataframe is updated.

    Parameters
    ----------
    cf : dict | None, optional
        Configuration dictionary. If None, initializes a new configuration.
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

    if cf is None:
        cf = initialize()

    top_verbose = True

    if top_verbose:
        print("Checking for new raw DDP logs that needs refining...")
    result = refine_all_raw_ddp_logs_and_save(cf=cf, verbose=verbose)
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            print("    ...all files already refined.")
        else:
            print(f"    ...refined {result["refined_files_after"] - result["refined_files_before"]} files.")


    # --------------------------------------------------------------------------------------
    refined_ddp_files = data_io.listdir(
        cf=cf,
        storage_location="ddp_processed",
        return_absolute_path=False,
        verbose=False)
    refined_ddp_files = [u for u in refined_ddp_files if u.endswith(".parquet")]


    # ---------------------------------------------------------------
    # check if there are any changes in the relevant folder compared to last time this process was run.    
    if data_io.exists(cf=cf,storage_location="recoded",filename="dataset_meta.json",verbose=verbose):
        dataset_meta = data_io.load_json(cf=cf,storage_location="recoded",filename="dataset_meta.json",verbose=verbose)
        if verbose:
            print("Dataset meta loaded")
    else:
        dataset_meta = {"donations": {"filenames": []}}

    latest_filename_list = dataset_meta.get("donations", {}).get("filenames", [])
    if not force_consolidation and set(latest_filename_list) == set(refined_ddp_files):

        if top_verbose:
            print("No new refined DDP files found. No need to consolidate.")
            if return_saved_data:
                if verbose: print("Returning existing file.")
                return False, data_io.load_parquet(cf=cf, storage_location="recoded", filename="donations_recoded.parquet")
        return False, None
    
 

    # --------------------------------------------------------------------------------------
    if top_verbose:
        print("Loading refined DDP logs...")
    many_ddp_logs = [data_io.load_parquet(cf=cf, storage_location="ddp_processed", filename=u) for u in refined_ddp_files]

    if top_verbose:
        print(f"Concatenating {len(refined_ddp_files)} refined DDP logs...")
    concatenated_ddp_logs = pd.concat(many_ddp_logs)
    if top_verbose:
        print(f"    ...done - initial shape of the concatenated dataframe: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")



    # --------------------------------------------------------------------------------------
    # naive drop_dupes based on these three columns
    concatenated_ddp_logs = concatenated_ddp_logs.drop_duplicates(subset=["D_donation_id","T_local_timestamp","item_id"], keep="first").copy()
    if top_verbose:
        print(f"Shape after naive duplication drop: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")




    # --------------------------------------------------------------------------------------
    # calculate the donation stats
    if top_verbose:
        print("Calculating donation metadata for the new donations...")
    donation_metadata = generate_donation_metadata(
        cf=cf, 
        ddp_events_df=concatenated_ddp_logs, 
        update_col=None,
        verbose=verbose
        )
    if top_verbose:
        print("    ...done updating donation metadata")

    
    # --------------------------------------------------------------------------------------
    # create list of donations to be dropped and drop donations which has a very small number of watched videos
    donations_to_drop = []
    donations_to_drop += list(donation_metadata["counts"][(donation_metadata["counts","watch"]<5)].index)
    concatenated_ddp_logs = concatenated_ddp_logs[~concatenated_ddp_logs.D_donation_id.isin(donations_to_drop)].copy()
    if top_verbose:
        print(f"Shape after dropping donations with fewer than 5 watch events: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")
    

    # --------------------------------------------------------------------------------------
    if top_verbose:
        print(f"Only keeping one of multiple overlapping (similar) donations...")

    # check for similarities between the new donations by looking for the same timestamps in the donations. 
    # The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates
    # first include all kinds of events, then exclude the watch events

    a1 = _identify_similar_donations(new_events=concatenated_ddp_logs, old_events=concatenated_ddp_logs, dont_check_these_cols=[])
    a2 = _identify_similar_donations(new_events=concatenated_ddp_logs, old_events=concatenated_ddp_logs, dont_check_these_cols=["watch"])
    new_donations_to_drop = (a1["new_drops"] | a2["new_drops"])
    old_donations_to_drop = (a1["old_drops"] | a2["old_drops"])
    donations_to_drop = new_donations_to_drop | old_donations_to_drop

    # drop the events in these donations
    concatenated_ddp_logs = concatenated_ddp_logs[~concatenated_ddp_logs["D_donation_id"].isin(donations_to_drop)].copy()
    if top_verbose:
        print(f"    ...done. Shape after dropping overlapping donations: {concatenated_ddp_logs.shape}. Unique donations: {concatenated_ddp_logs.D_donation_id.nunique()}")





    # --------------------------------------------------------------------------------------
    # update the donation metadata with a column to signify which donations are accepted
    # and included in the dataset. This is necessary since the donation metadata df contains
    # all donations, even those that are overlapping or are too small to be included in the dataset.  
    if top_verbose:
        print("Updating donation metadata with a column to signify which donations are accepted...")
    accepted_donation_ids = concatenated_ddp_logs["D_donation_id"].unique()
    accepted_col = pd.Series(donation_metadata.index.isin(accepted_donation_ids), index=donation_metadata.index, name=("other", "accepted"))
    donation_metadata = generate_donation_metadata(
        cf=cf, 
        ddp_events_df=None, 
        update_col=accepted_col,
        verbose=verbose
        )
    if top_verbose:
        print("    ...done updating donation metadata")

    

    # --------------------------------------------------------------------------------------
    # reset index
    concatenated_ddp_logs.reset_index(drop=True, inplace=True)

    if top_verbose:
        print("Recalculate session details to ensure that the session IDs are unique.")
    concatenated_ddp_logs = _add_session_info_to_ddp_log(concatenated_ddp_logs, verbose=verbose)
    if "session_id" in concatenated_ddp_logs.columns:
        concatenated_ddp_logs["session_id"] = concatenated_ddp_logs["session_id"].map(lambda x:f"SD{x:05}" if pd.notna(x) else pd.NA) # SD kind of indicates that this is a S-ession and D-onation
    
    # --------------------------------------------------------------------------------------
    # I will concatenate this df with anohter df where this column is missing. It makes
    # my life easier to turn it into str. I'm not using it for calculations anyway.  
    concatenated_ddp_logs["date_added_to_dataset"] = concatenated_ddp_logs["date_added_to_dataset"].dt.strftime('%Y-%m-%d')
    concatenated_ddp_logs = convert_dtypes_to_pyarrow(concatenated_ddp_logs)

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
    _ = data_io.save_json(cf, dataset_meta, "recoded", "dataset_meta.json")


    return True, concatenated_ddp_logs












# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------








def load_donation_data(
    cf = None, 
    study_name = None, 
    all_data = None,
    verbose=False):



    if study_name is None:
        raise ValueError("!!! [DDP] study_name must be specified")


    if cf is None:
        cf = initialize()

    the_selected_donations = cf["study_defs"][study_name].get("SELECTED_DONATIONS",[])


    print(f"    [DDP] Loading data for study...")
    

    START_DATE = cf["study_defs"][study_name].get("START_DATE","1970-01-01")
    if isinstance(START_DATE, str):
        START_DATE = _dt.datetime.strptime(START_DATE, "%Y-%m-%d").date()
    
    END_DATE = cf["study_defs"][study_name].get("END_DATE","2099-12-31")
    if isinstance(END_DATE, str):
        END_DATE = _dt.datetime.strptime(END_DATE, "%Y-%m-%d").date()

    sel = [("T_local_timestamp", ">=", START_DATE),("T_local_timestamp", "<=", END_DATE)]

    if len(the_selected_donations) > 0:
        sel.append(("D_donation_id", "in", the_selected_donations))

    if all_data is None:
        if verbose:
            print(f"    [DDP] Loading donation events from main storage")
        out_df = data_io.load_parquet(cf, "recoded", "donations_recoded.parquet", filters=sel,verbose=verbose)

    else:
        if verbose:
            print(f"    [DDP] Selecting date range from cached donation data")
        cached_ddp_events_df = all_data.copy()
        out_df = cached_ddp_events_df[(cached_ddp_events_df.T_local_timestamp>=START_DATE) & (cached_ddp_events_df.T_local_timestamp<=END_DATE)].copy()

        if len(the_selected_donations) > 0:
            out_df = out_df[out_df.D_donation_id.isin(the_selected_donations)].copy()


    #if verbose:
    #    print(f"    [DDP] Dataframe include data from {out_df.D_donation_id.nunique()} unique donations. Shape: {out_df.shape}")
    #    print(f"    [DDP] Date range: {out_df.T_local_timestamp.min():%Y-%m-%d} -- {out_df.T_local_timestamp.max():%Y-%m-%d}")

    print(f"    [DDP] ...done. | Shape: {out_df.shape} | Unique donations: {out_df.D_donation_id.nunique()} | Date range: {out_df.T_local_timestamp.min():%Y-%m-%d} -- {out_df.T_local_timestamp.max():%Y-%m-%d}")


    return out_df







def simple_sample_ddp_events(
    cf = None, 
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


    

    if cf is None:
        cf = initialize()
    
    if all_ddp_events_df is None:
        raise ValueError("[DD Sampling] all_ddp_events_df cannot be None")

    the_df = all_ddp_events_df.copy()

    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.

    group_factors = get_group_factors_from_var_schema(cf = cf, some_events_df = the_df, verbose=False)

    if len(group_factors) != 2:
        raise ValueError("!!! [DD Sampling] Group factors must be exactly 2")

    if not "D_donation_id" in group_factors:
        raise ValueError("!!! [DD Sampling] Group factors must include D_donation_id")

    # make sure D_donation_id is the first element 
    group_factors.remove("D_donation_id")
    group_factors = ["D_donation_id"] + group_factors

    if verbose:
        print(f"    [DD Sampling] Group factors: {group_factors}")

    MIN_EVENTS_REQUIRED = cf["study_defs"][study_name].get("MIN_EVENT_COUNT_REQUIRED_PER_AGG_GROUP",10)
    MAX_EVENTS_SELECTED = cf["study_defs"][study_name].get("MAX_EVENT_COUNT_SELECTED_PER_AGG_GROUP",100)
    MIN_GROUP_COUNT_REQUIRED_PER_DONATION = cf["study_defs"][study_name].get("MIN_GROUP_COUNT_REQUIRED_PER_DONATION",10)
    MAX_GROUP_COUNT_SELECTED_PER_DONATION = cf["study_defs"][study_name].get("MAX_GROUP_COUNT_SELECTED_PER_DONATION",100)

    # sorting the events by donation and event id in order to have a replicable sample
    donation_metadata_df = data_io.load_parquet(cf=cf, storage_location="ddp_main", filename="ddp_metadata.parquet")
    donation_to_d_dict = donation_metadata_df[("other","D_id")].to_dict()

    the_df["D_id"] = the_df["D_donation_id"].map(donation_to_d_dict)
    the_df = the_df.sort_values(by=["D_id","event_id"])


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
    ddp_watch_events_within_agg_group_size_limits = _filter_and_sample(all_watch_events_df, group_factors, MIN_EVENTS_REQUIRED, MAX_EVENTS_SELECTED)
    if verbose:
        sample_size = len(ddp_watch_events_within_agg_group_size_limits)
        print(f"    [DD Sampling] Watch events after sampling: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # build a df with unique pairs of the two group factors
    unique_group_factor_pairs = ddp_watch_events_within_agg_group_size_limits[group_factors].drop_duplicates()

    if verbose:
        print(f"    [DD Sampling] Dropping donations with less than {MIN_GROUP_COUNT_REQUIRED_PER_DONATION} groups within the limits")
        print(f"    [DD Sampling] Sampling at most {MAX_GROUP_COUNT_SELECTED_PER_DONATION} groups from each remaining donation. This might take a moment...")
    # select donations with a required number of groups
    donations_within_group_count_limits = _filter_and_sample(unique_group_factor_pairs, group_factors[:1], MIN_GROUP_COUNT_REQUIRED_PER_DONATION, MAX_GROUP_COUNT_SELECTED_PER_DONATION)
    if verbose:
        print(f"    [DD Sampling] Donations groups remaining after sampling: {len(donations_within_group_count_limits):,}")


    # ----------------------------------------------------------------------
    # find the watch events in the selected groups
    # 1. start with the events in the agg groups that meet the group size requirements and set the index to the group factors
    ddp_watch_events_in_candidate_groups = ddp_watch_events_within_agg_group_size_limits.set_index(group_factors)

    # 2. select the events in the groups that meet the group count requirements
    ddp_watch_events_in_selected_groups = ddp_watch_events_in_candidate_groups.loc[donations_within_group_count_limits.set_index(group_factors).index]
    ddp_watch_events_in_selected_groups = ddp_watch_events_in_selected_groups.reset_index()
    if verbose:
        sample_size = len(ddp_watch_events_in_selected_groups)
        print(f"    [DD Sampling] Watch events remaining in the sampled groups: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # ----------------------------------------------------------------------
    # find the non-watch events in the selected groups - note that since the non-watch events are not
    # sampled, there is a disproportional number of non-watch events in the sampled dataset compared 
    # to the number of watch events
    # 1. find all unique group factor pairs for the non-watch events
    unique_group_factor_pairs_for_nonwatch_events = all_nonwatch_events_df[group_factors].drop_duplicates()

    # 2. find the non-watch groups that are in the selected groups. This is necessary since there are some non-watch
    # groups that don't have any watch events, and I don't want these included in the sample
    nonwatch_groups = set(unique_group_factor_pairs_for_nonwatch_events.set_index(group_factors).index)
    selected_watch_groups = set(donations_within_group_count_limits.set_index(group_factors).index)
    selected_nonwatch_groups = list(nonwatch_groups & selected_watch_groups)

    selected_nonwatch_groups = pd.DataFrame(selected_nonwatch_groups, columns=group_factors)
    selected_nonwatch_groups = selected_nonwatch_groups.convert_dtypes(dtype_backend="pyarrow").set_index(group_factors).index

    mask = all_nonwatch_events_df.set_index(group_factors).index.isin(selected_nonwatch_groups)
    ddp_nonwatch_events_in_selected_groups = all_nonwatch_events_df[mask]
    if verbose:
        print(f"    [DD Sampling] Non-Watch events remaining in the selected groups: {len(ddp_nonwatch_events_in_selected_groups):,} (100% of original)")
    #print(ddp_nonwatch_events_in_selected_groups[:5])
    #ddp_nonwatch_events_in_selected_groups.reset_index(inplace=True)

    combined = pd.concat([ddp_watch_events_in_selected_groups, ddp_nonwatch_events_in_selected_groups])
    if verbose:
        print(f"    [DD Sampling] Combining the (not sampled) non-watch events with the sampled watch events with : {len(combined):,} in {len(combined[group_factors].drop_duplicates()):,} groups")
    combined.drop("D_id", axis=1, inplace=True)


    enrichment_status_df = data_io.load_parquet(
        cf=cf,
        storage_location="recoded",
        filename="enrichment_status.parquet")
    

    combined_deduped = combined.drop_duplicates(subset="item_id", keep="first")[["item_id"]]

    combined_deduped_enrichment_status = pd.merge(left=combined_deduped, right=enrichment_status_df, left_on='item_id', right_index=True, how='left')

    enrichment_summary = combined_deduped_enrichment_status.select_dtypes(include=["bool"]).fillna(False).sum().to_dict()

    mapper = cf['var_schema'][['variable_name','display_name']].dropna().set_index('variable_name').to_dict()['display_name']

    print(f"    [DD Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[group_factors].drop_duplicates()):,} groups")
    print(f"    [DD Sampling] - Unique videos: {len(combined_deduped_enrichment_status):,}")
    for k in enrichment_summary:
        print(f"    [DD Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} ({enrichment_summary[k]/len(combined_deduped_enrichment_status):.0%})")

    return combined














"""


def sample_ddp_events(
    cf = None, 
    study_name = None, 
    all_ddp_events_df = None, 
    verbose=False):

    from fyp.fyp_main import initialize
    from fyp.recode_variables import get_group_factors_from_var_schema

    if cf is None:
        cf = initialize()
    
    if all_ddp_events_df is None:
        raise ValueError("[DD Sampling] all_ddp_events_df cannot be None")

    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.

    print("Sampling DDP events...")

    group_factors = get_group_factors_from_var_schema(cf = cf, some_events_df = all_ddp_events_df, verbose=verbose)


    AGG_GROUP_SIZE_PERCENTILE_LIMITS = cf["study_defs"][study_name]["AGG_GROUP_SIZE_PERCENTILE_LIMITS"]
    MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION = cf["study_defs"][study_name]["MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION"]
    N_SAMPLED_DATES_FROM_EACH_DONATION = cf["study_defs"][study_name]["N_SAMPLED_DATES_FROM_EACH_DONATION"]
    N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP = cf["study_defs"][study_name]["N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP"]

    print("    [DD Sampling] Sampling DDP events...")

    # count the number of watch events in the donation-date groups. I don't want groups without any watch events
    donation_date_groups = all_ddp_events_df[all_ddp_events_df['D_feature_name']=="watch"].groupby(group_factors)["D_feature_name"].count()
    if verbose:
        print(f"    [DD Sampling] There {len(all_ddp_events_df):,} dd events in {len(donation_date_groups):,} groups before sampling")


    # this is transforming the donation-date group percentile limits to actual values
    donation_date_group_size_limits = donation_date_groups.describe(percentiles=AGG_GROUP_SIZE_PERCENTILE_LIMITS).loc[[f"{k:.0%}" for k in AGG_GROUP_SIZE_PERCENTILE_LIMITS]].values
    percentile_str = "-".join([f"{k:.0%}" for k in AGG_GROUP_SIZE_PERCENTILE_LIMITS])
    limits_str = "-".join([f"{k:,.0f}" for k in donation_date_group_size_limits])
    if verbose:
        print(f"    [DD Sampling] The percentile limits {percentile_str} for this study translate to {limits_str} in actual event counts")

    #return donation_date_groups
    # apply the size limits to the donation-date groups to get those that fit the criteria
    donation_date_groups_within_size_limits = donation_date_groups[(donation_date_groups>=donation_date_group_size_limits[0]) & (donation_date_groups<donation_date_group_size_limits[1])]
    if verbose:
        print(f"    [DD Sampling] There are {len(donation_date_groups_within_size_limits):,} donation-date groups with event counts within the limits")


    # for each donation, count how many dates have event counts within the limits
    n_tiktok_dates_within_limits_per_donation = (~donation_date_groups_within_size_limits.unstack(level=0).isna()).sum()


    # I want donations who have a considerable number of dates within this range.
    donations_with_many_dates_within_limits = n_tiktok_dates_within_limits_per_donation[n_tiktok_dates_within_limits_per_donation>=MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION]


    
    if verbose:
        print(f"    [DD Sampling] There are {len(donations_with_many_dates_within_limits):,} donations with at least {MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION} dates where the number of events is within the limits")


    # use these identified donations to identify the donation-date groups that meet the events per date criteria
    donation_date_groups_by_regulars = donation_date_groups_within_size_limits.unstack(1).loc[donations_with_many_dates_within_limits.index,:].stack()
    if verbose:
        print(f"    [DD Sampling] These donations yield {len(donation_date_groups_by_regulars):,} donation-date groups meeting the criteria")



    # Sample step 1: sample a certain number of dates from each donation

    # I'm first shuffling the dates for each donation (pseudo-randomly for replicability)
    ordered_groups = (
        donation_date_groups_by_regulars.groupby(level=0, group_keys=False)
          .apply(lambda g: g.sample(frac=1, replace=False, random_state=42))
    )

    # then I pick the top 'N_SAMPLED_DATES_FROM_EACH_DONATION' dates from each donation
    # this ensures that I keep the elements selected when 'N_SAMPLED_DATES_FROM_EACH_DONATION' is small,
    # also when I pick a higher 'N_SAMPLED_DATES_FROM_EACH_DONATION' value
    # It's $$$ to scrape and annotate videos, so I don't want to start from scratch
    # just because I increased the sample size
    sampled_donation_date_groups_by_regulars = ordered_groups.groupby(level=0).head(N_SAMPLED_DATES_FROM_EACH_DONATION)

    if verbose:
        print(f"    [DD Sampling] Sampling {N_SAMPLED_DATES_FROM_EACH_DONATION} dates from each donation, giving {len(sampled_donation_date_groups_by_regulars):,} donation-date groups")

    # get all events in these sampled donation-date groups (non-watch events as well)
    ddp_events_in_sampled_groups = all_ddp_events_df.set_index(group_factors)
    ddp_events_in_sampled_groups = ddp_events_in_sampled_groups.loc[sampled_donation_date_groups_by_regulars.index]
    ddp_events_in_sampled_groups = ddp_events_in_sampled_groups.reset_index()
    if verbose:
        print(f"    [DD Sampling] There are {len(ddp_events_in_sampled_groups):,} events in these {len(sampled_donation_date_groups_by_regulars):,} donation-date groups")



    # Sample step 2: sample a certain number of events from each donation-date group

    # I'm first shuffling the events for each donation-date group pseudo-randomly
    ordered_events_in_groups = (
        ddp_events_in_sampled_groups.groupby(group_factors)
          .apply(lambda g: g.sample(frac=1, replace=False, random_state=42), include_groups=False)
    )

    # then I pick the top 'N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP' events from each donation-date group
    # this ensures that I keep the elements selected when 'N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP' is small, 
    # also when I pick a higher 'N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP' value
    # It's $$$ to annotate videos, so I don't want to start from scratch
    # just because I increased the sample size
    sampled_ddp_events_in_sampled_donation_date_groups = ordered_events_in_groups.groupby(group_factors).head(N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP)

    # push the grouping variables back from index into columns
    sampled_ddp_events_in_sampled_donation_date_groups.reset_index(level=[0,1], inplace=True)

    print(f"    [DD Sampling] ...done. Sampled {N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP} events from each donation-date group, yielding {len(sampled_ddp_events_in_sampled_donation_date_groups):,} events")


    
    return sampled_ddp_events_in_sampled_donation_date_groups












def load_ddp_events(
    cf = None, 
    study_name = None, 
    all_data = None,
    verbose=False):
    # load DF with all donations previously ingested


    if study_name is None:
        raise ValueError("study_name must be specified")


    if cf is None:
        cf = initialize()


    if not cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() in ["sample","all"]:
        if verbose:
            print("Not loading DDP events")
        return None




    DDP_START_DATE = cf["study_defs"][study_name]["DDP_START_DATE"]
    if isinstance(DDP_START_DATE, str):
        DDP_START_DATE = _dt.datetime.strptime(DDP_START_DATE, "%Y-%m-%d").date()
    
    DDP_END_DATE = cf["study_defs"][study_name]["DDP_END_DATE"]
    if isinstance(DDP_END_DATE, str):
        DDP_END_DATE = _dt.datetime.strptime(DDP_END_DATE, "%Y-%m-%d").date()

    if all_data is None:
        print(f"    [DDP] Loading data...")
        sel = [("T_local_timestamp", ">=", DDP_START_DATE),("T_local_timestamp", "<=", DDP_END_DATE)]
        all_ddp_events_df = data_io.load_parquet(cf, "recoded", f"donations_recoded.parquet", filters=sel, verbose=verbose)
    else:
        print(f"    [DDP] Filtering cached data...")
        all_ddp_events_df = all_data.copy()
        all_ddp_events_df = all_ddp_events_df[(all_ddp_events_df.T_local_timestamp>=DDP_START_DATE) & (all_ddp_events_df.T_local_timestamp<=DDP_END_DATE)].copy()
        if verbose:
            print(f"    [DDP] Selected date range: {all_ddp_events_df.T_local_timestamp.min():%Y-%m-%d} -- {all_ddp_events_df.T_local_timestamp.max():%Y-%m-%d} Shape: {all_ddp_events_df.shape}")


    print(f"    [DDP] ...done. Dataframe ready. {all_ddp_events_df.D_donation_id.nunique()} unique donations. {all_ddp_events_df.shape[0]:,} events.")


    if cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() == "all":
        return all_ddp_events_df
    else:
        sampled_data_ddp_events = sample_ddp_events(
            cf = cf, 
            study_name = study_name, 
            all_ddp_events_df = all_ddp_events_df, 
            verbose=verbose)
        return sampled_data_ddp_events


"""











################################################################################
################################################################################
################################################################################
################################################################################
################################################################################


