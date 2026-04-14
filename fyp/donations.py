#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import json
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
from fyp.calc_collection_stats import generate_personas
from fyp.organize_datasets import COLLECTIONS_LABEL
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs



collection_id_column = "collection_id"
timestamp_column = "local_timestamp"
event_type_column = "activity_type"









def get_donation_metadata_from_aio_aws(
                        storage_location: str = "aio_participants",
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
    scan_args = shlex.split(scan_cmd)

    # Run it — write stdout directly to the temp file instead of using shell redirection
    try:
        with open(temp_file, 'w', encoding='utf-8') as outf:
            subprocess.run(scan_args, check=True, stdout=outf)
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
                    storage_location: str = "aio_raw",
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
    to the given storage location (local or GCS depending on config).

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
    # 3) Run DynamoDB scan to get donation IDs
    # ------------------------------------------------------------------
    scan_args = [
        "aws", "dynamodb", "scan",
        "--table-name", table_name,
        "--filter-expression", "consentProvided = :consent and #d >= :shareDate",
        "--expression-attribute-names", '{"#d": "date"}',
        "--expression-attribute-values",
        json.dumps({
            ":consent": {"BOOL": True},
            ":shareDate": {"S": share_date},
        }),
        "--query", "Items[*].id.S",
    ]

    print(f"Downloading recent donations to temporary storage: {dest}")
    scan_result = subprocess.run(scan_args, check=True, capture_output=True, text=True)
    donation_ids = json.loads(scan_result.stdout)

    # ------------------------------------------------------------------
    # 4) Download each donation file from S3
    # ------------------------------------------------------------------
    for donation_id in donation_ids:
        s3_uri = f"s3://{bucket}/donation/{donation_id}"
        subprocess.run(
            ["aws", "s3", "cp", s3_uri, str(dest) + os.sep],
            check=True,
        )

    # ------------------------------------------------------------------
    # 5) Move/Upload files to ddp_raw storage
    # ------------------------------------------------------------------
    downloaded_files = os.listdir(dest)
    print(f"Moving {len(downloaded_files)} files to {storage_location} storage...")
    
    count = 0
    for filename in downloaded_files:
        val_path = dest / filename
        # Read the content
        with open(val_path, 'r', encoding='utf-8') as f:
            try:
                # Assuming they are JSONs as per previous scripts?
                # ingest script treats them as JSONs
                data = json.load(f)
                
                # Use data_io to save (handles GCS upload + Local secondary)
                data_io.save_json(data, storage_location, filename)
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






def generate_collection_metadata(
    collections_df: pd.DataFrame | None = None,
    update_col: pd.Series | None = None,
    sort_by: str | None = None, 
    verbose: bool = False,
    save_to_disk_ok: bool = True,
    load_from_disk: bool = True,
    ) -> pd.DataFrame:
    """
    Generate or update collection metadata, either by calculating statistics from events 
    or by merging a specific column into existing metadata.

    Parameters
    ----------
    collections_df : pandas.DataFrame, optional
        Events DataFrame used to calculate metadata statistics.
    update_col : pandas.Series, optional
        A Series representing a single column to update or add to existing metadata.
        The index must be collection_id_column.
    sort_by : str, optional
        Column name to sort the resulting DataFrame by.
    verbose : bool, default False
        Whether to print progress and status messages.

    Returns
    -------
    pandas.DataFrame
        The resulting metadata DataFrame with collection IDs as index.
    """

    old_metadata_df = pd.DataFrame()
    if load_from_disk:
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            old_metadata_df = data_io.load_parquet(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
            if collection_id_column in old_metadata_df.columns:
                old_metadata_df.set_index(collection_id_column, inplace=True)
            if old_metadata_df.index.name != collection_id_column:
                old_metadata_df.index.name = collection_id_column
            if verbose:
                print(f"Loaded existing metadata from storage. Shape: {old_metadata_df.shape}")
    else:
        if verbose:
            print("No calculated metadata found in storage")


    # if no events df is provided, check if there is an update column
    if collections_df is None:
        if isinstance(update_col, pd.Series):
            print("Updating a single column | ", end="", flush=True)
            if update_col.index.name != collection_id_column:
                update_col.index.name = collection_id_column
            if set(update_col.index) != set(old_metadata_df.index):
                print("Error: Update column index don't match the index of the existing metadata DF. Exiting.")
                return old_metadata_df
            if update_col.name in old_metadata_df.columns:
                print(f"Dropping existing column: {update_col.name} | ", end="", flush=True)
                old_metadata_df = old_metadata_df.drop(columns=[update_col.name])

            new_metadata_df = pd.merge(old_metadata_df, update_col, left_index=True, right_index=True, how="left")
            if save_to_disk_ok:
                data_io.save_parquet(df=new_metadata_df, storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet", verbose=verbose)
                print(f"Saved updated metadata. Shape: {new_metadata_df.shape}")
            return new_metadata_df

        else:
            print("No new data provided or update column is not a matching pandas Series. Returning old metadata.")
            return old_metadata_df


    if collection_id_column not in collections_df.columns:
        print("Shape of the collection stats DF: (0,0)")
        return pd.DataFrame()
    
    collection_ids_in_the_incoming_df = set(collections_df[collection_id_column].unique())
    collection_ids_in_the_old_metadata_df = set(old_metadata_df.index)

    new_collections = collection_ids_in_the_incoming_df - collection_ids_in_the_old_metadata_df

    if len(new_collections) == 0:
        if verbose:
            print(f"No new collections to add to metadata. Returning the existing metadata. Shape: {old_metadata_df.shape}")
        return old_metadata_df


    if verbose:
        print(f"Calculating metadata for {len(new_collections)} new collections")

    collections_df_new = collections_df[collections_df[collection_id_column].isin(new_collections)].copy()


    df1 = collections_df_new.groupby(collection_id_column)[event_type_column].value_counts().unstack().fillna(0).astype(int)
    df1['total'] = df1.sum(axis=1)
    if sort_by is None:
        df1 = df1.sort_values("total").copy()
    else:
        df1 = df1.sort_values(sort_by).copy()
    if verbose:
        print(f"Shape of the collection stats DF: {df1.shape}")
    df1.columns = pd.MultiIndex.from_product([['counts'], df1.columns])


    a = collections_df_new[[collection_id_column,"ts_added_to_dataset"]].drop_duplicates()
    b = a.set_index(collection_id_column, inplace=False)
    these_collection_dates = b.to_dict()["ts_added_to_dataset"]
    df1["other","ts_added_to_dataset"] = df1.index.map(lambda x: these_collection_dates[x])


    df1.sort_values(by=[("other","ts_added_to_dataset")], inplace=True)



    collection_personas = generate_personas(collections_df_new)
    if not collection_personas.empty and collection_id_column in collection_personas.columns:
        collection_personas.set_index(collection_id_column, inplace=True)
        collection_personas.columns = pd.MultiIndex.from_product([['personas'], collection_personas.columns])


    if verbose:
        print("Checking DDP participant metadata files...")
    participant_metadata = {}
    for participant_data_file in data_io.listdir(storage_location="aio_participants"):
        if participant_data_file.endswith(".json"):
            participant_metadata_raw = data_io.load_json(storage_location="aio_participants", filename=participant_data_file)
            if verbose:
                print(f"    Found {len(participant_metadata_raw['Items']):,} items in the file {participant_data_file}")
            for item in participant_metadata_raw.get("Items", []):
                    py_item = {k: _deser(v) for k, v in item.items()}
                    participant_metadata[py_item['id']] = py_item

    participant_metadata_df = pd.DataFrame(participant_metadata).T
    participant_metadata_df.drop(["url","iat","pk","id","exp","profile","schemaChanged","appliedSchema"],axis=1, inplace=True)
    participant_metadata_df.columns = pd.MultiIndex.from_product([['participants'], participant_metadata_df.columns])

    combined_ddp_metadata = pd.merge(df1, participant_metadata_df, left_index=True, right_index=True, how="left")

    if not collection_personas.empty:
        combined_ddp_metadata = pd.merge(combined_ddp_metadata, collection_personas, left_index=True, right_index=True, how="left")


    if old_metadata_df is not None and not old_metadata_df.empty:
        # Only concat frames that have data - pandas 2.x FutureWarning about
        # empty/all-NA entries influencing result dtypes.
        frames = [f for f in (old_metadata_df, combined_ddp_metadata) if not f.empty]
        if len(frames) > 1:
            combined_ddp_metadata = pd.concat(frames, axis=0)
        elif frames:
            combined_ddp_metadata = frames[0]
        
    if save_to_disk_ok:
        if verbose:
            print(f"Saving updated metadata to disk. Shape: {combined_ddp_metadata.shape}")
        data_io.save_parquet(df=combined_ddp_metadata, storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet", verbose=verbose)

    if verbose:
        print(f"Shape of the combined metadata DF: {combined_ddp_metadata.shape}")

    return combined_ddp_metadata














# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------



