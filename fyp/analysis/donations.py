#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import datetime as _dt
import json
import os
import shutil
from pathlib import Path

import boto3
import pandas as pd

import fyp.data_io as data_io
from fyp.calc_collection_stats import generate_personas
from fyp.logging_setup import get_logger
from fyp.recode_variables import *

logger = get_logger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf




def _collections_label() -> str:
    """Lazy accessor for the config-derived collections label."""
    from fyp.organize_datasets import COLLECTIONS_LABEL

    return COLLECTIONS_LABEL

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
    then move to the ddp_participants' storage location (local or GCS depending on config).
    Authenticates via the boto3 default credential chain (env vars on Cloud Run,
    ~/.aws/credentials locally).
    """

    # Compute cut‑off time
    now = (_dt.datetime.now(_dt.UTC)
           if not use_local_time
           else _dt.datetime.now().astimezone())
    file_stamp = now.strftime("%Y%m%d%H%M%S")

    # Prepare destination
    filename = f"ddp_metadata_{file_stamp}.json"
    temp_file = os.path.join(_cf()["paths"]["temp"], filename)
    os.makedirs(_cf()["paths"]["temp"], exist_ok=True)

    # Scan the metadata table. The previous AWS-CLI implementation produced
    # a JSON object of the shape {"Items": [...], "Count": N, "ScannedCount": N};
    # we mirror that here so downstream readers don't change.
    try:
        ddb = boto3.client("dynamodb")
        paginator = ddb.get_paginator("scan")
        items: list = []
        scanned = 0
        for page in paginator.paginate(
            TableName=table_name,
            Select="ALL_ATTRIBUTES",
            PaginationConfig={"PageSize": 500, "MaxItems": 100000},
        ):
            items.extend(page.get("Items", []))
            scanned += page.get("ScannedCount", 0)
    except Exception as e:
        logger.error(f"Error scanning participant metadata table via boto3: {e}")
        return None

    payload = {"Items": items, "Count": len(items), "ScannedCount": scanned}
    with open(temp_file, 'w', encoding='utf-8') as outf:
        json.dump(payload, outf)

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
    now = (_dt.datetime.now(_dt.UTC)
           if not use_local_time
           else _dt.datetime.now().astimezone())     # Brisbane local
    cutoff = now - _dt.timedelta(hours=hours_back)
    share_date = cutoff.replace(microsecond=0).isoformat()

    # ------------------------------------------------------------------
    # 2) Prepare temporary destination
    # ------------------------------------------------------------------
    # Use a specific temp folder for this batch

    temp_dir_path = os.path.join(_cf()["paths"]["temp"], f"download_batch_{now.strftime('%Y%m%d%H%M%S')}")
    dest = Path(temp_dir_path).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    ddb = boto3.client("dynamodb")
    s3 = boto3.client("s3")

    # ------------------------------------------------------------------
    # 3) Scan DynamoDB for donation IDs in the window
    # ------------------------------------------------------------------
    paginator = ddb.get_paginator("scan")
    donation_ids: list = []
    for page in paginator.paginate(
        TableName=table_name,
        FilterExpression="consentProvided = :consent and #d >= :shareDate",
        ExpressionAttributeNames={"#d": "date"},
        ExpressionAttributeValues={
            ":consent": {"BOOL": True},
            ":shareDate": {"S": share_date},
        },
    ):
        for item in page.get("Items", []):
            id_val = item.get("id", {}).get("S")
            if id_val:
                donation_ids.append(id_val)

    logger.info(f"Downloading {len(donation_ids)} donations to temporary storage: {dest}")

    # ------------------------------------------------------------------
    # 4) Download each donation file from S3
    # ------------------------------------------------------------------
    for donation_id in donation_ids:
        target_path = dest / donation_id
        s3.download_file(bucket, f"donation/{donation_id}", str(target_path))

    # ------------------------------------------------------------------
    # 5) Move/Upload files to ddp_raw storage
    # ------------------------------------------------------------------
    downloaded_files = os.listdir(dest)
    logger.info(f"Moving {len(downloaded_files)} files to {storage_location} storage...")

    count = 0
    for filename in downloaded_files:
        val_path = dest / filename
        # Read the content
        with open(val_path, encoding='utf-8') as f:
            try:
                # Assuming they are JSONs as per previous scripts?
                # ingest script treats them as JSONs
                data = json.load(f)

                # Use data_io to save (handles GCS upload + Local secondary)
                data_io.save_json(data, storage_location, filename)
                count += 1
            except Exception as e:
                logger.error(f"Failed to process/upload {filename}: {e}")

    logger.info(f"Successfully processed {count} files.")

    # ------------------------------------------------------------------
    # 6) Cleanup Temp
    # ------------------------------------------------------------------
    try:
        shutil.rmtree(dest)
    except Exception as e:
        logger.warning(f"Warning: Failed to clean up temp directory {dest}: {e}")

    return {"donation_ids": donation_ids, "uploaded_count": count}











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
        if data_io.exists(storage_location="recoded", filename=f"{_collections_label()}_metadata.parquet"):
            old_metadata_df = data_io.load_parquet(storage_location="recoded", filename=f"{_collections_label()}_metadata.parquet")
            if collection_id_column in old_metadata_df.columns:
                old_metadata_df.set_index(collection_id_column, inplace=True)
            if old_metadata_df.index.name != collection_id_column:
                old_metadata_df.index.name = collection_id_column
            if verbose:
                logger.info(f"Loaded existing metadata from storage. Shape: {old_metadata_df.shape}")
    else:
        if verbose:
            logger.info("No calculated metadata found in storage")


    # if no events df is provided, check if there is an update column
    if collections_df is None:
        if isinstance(update_col, pd.Series):
            print("Updating a single column | ", end="", flush=True)
            if update_col.index.name != collection_id_column:
                update_col.index.name = collection_id_column
            if set(update_col.index) != set(old_metadata_df.index):
                logger.error("Error: Update column index don't match the index of the existing metadata DF. Exiting.")
                return old_metadata_df
            if update_col.name in old_metadata_df.columns:
                print(f"Dropping existing column: {update_col.name} | ", end="", flush=True)
                old_metadata_df = old_metadata_df.drop(columns=[update_col.name])

            new_metadata_df = pd.merge(old_metadata_df, update_col, left_index=True, right_index=True, how="left")
            if save_to_disk_ok:
                data_io.save_parquet(df=new_metadata_df, storage_location="recoded", filename=f"{_collections_label()}_metadata.parquet", verbose=verbose)
                logger.info(f"Saved updated metadata. Shape: {new_metadata_df.shape}")
            return new_metadata_df

        else:
            logger.info("No new data provided or update column is not a matching pandas Series. Returning old metadata.")
            return old_metadata_df


    if collection_id_column not in collections_df.columns:
        logger.info("Shape of the collection stats DF: (0,0)")
        return pd.DataFrame()
    
    collection_ids_in_the_incoming_df = set(collections_df[collection_id_column].unique())
    collection_ids_in_the_old_metadata_df = set(old_metadata_df.index)

    new_collections = collection_ids_in_the_incoming_df - collection_ids_in_the_old_metadata_df

    if len(new_collections) == 0:
        if verbose:
            logger.info(f"No new collections to add to metadata. Returning the existing metadata. Shape: {old_metadata_df.shape}")
        return old_metadata_df


    if verbose:
        logger.info(f"Calculating metadata for {len(new_collections)} new collections")

    collections_df_new = collections_df[collections_df[collection_id_column].isin(new_collections)].copy()


    df1 = collections_df_new.groupby(collection_id_column)[event_type_column].value_counts().unstack().fillna(0).astype(int)
    df1['total'] = df1.sum(axis=1)
    if sort_by is None:
        df1 = df1.sort_values("total").copy()
    else:
        df1 = df1.sort_values(sort_by).copy()
    if verbose:
        logger.info(f"Shape of the collection stats DF: {df1.shape}")
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
        logger.info("Checking DDP participant metadata files...")
    participant_metadata = {}
    for participant_data_file in data_io.listdir(storage_location="aio_participants"):
        if participant_data_file.endswith(".json"):
            participant_metadata_raw = data_io.load_json(storage_location="aio_participants", filename=participant_data_file)
            if verbose:
                logger.info(f"    Found {len(participant_metadata_raw['Items']):,} items in the file {participant_data_file}")
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
            logger.info(f"Saving updated metadata to disk. Shape: {combined_ddp_metadata.shape}")
        data_io.save_parquet(df=combined_ddp_metadata, storage_location="recoded", filename=f"{_collections_label()}_metadata.parquet", verbose=verbose)

    if verbose:
        logger.info(f"Shape of the combined metadata DF: {combined_ddp_metadata.shape}")

    return combined_ddp_metadata














# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------



