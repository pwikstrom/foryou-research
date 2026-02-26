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
from fyp.calc_donation_stats import generate_personas
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs



collection_id_column = "D_donation_id"
timestamp_column = "T_local_timestamp"
event_type_column = "D_feature_name"









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
        The index must be collection_id_column.
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
        if data_io.exists(storage_location="processed_activities", filename="ddp_metadata.parquet"):
            old_metadata_df = data_io.load_parquet(storage_location="processed_activities", filename="ddp_metadata.parquet")
            if verbose:
                print(f"Loaded existing metadata from storage. Shape: {old_metadata_df.shape}")
    else:
        if verbose:
            print("No calculated metadata found in storage")


    # if no events df is provided, check if there is an update column
    if ddp_events_df is None:
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
            #new_metadata_df = new_metadata_df.sort_index(axis='columns').sort_values(('other','D_id')).copy()
            if save_to_disk_ok:
                data_io.save_parquet(df=new_metadata_df, storage_location="processed_activities", filename="ddp_metadata.parquet", verbose=verbose)
                print(f"Saved updated metadata. Shape: {new_metadata_df.shape}")
            return new_metadata_df

        else:
            print("No new data provided or update column is not a matching pandas Series. Returning old metadata.")
            return old_metadata_df


    donation_ids_in_the_incoming_df = set(ddp_events_df[collection_id_column].unique())


    if collection_id_column not in ddp_events_df.columns:
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

    ddp_events_df_new = ddp_events_df[ddp_events_df[collection_id_column].isin(new_donations)].copy()


    df1 = ddp_events_df_new.groupby(collection_id_column)[event_type_column].value_counts().unstack().fillna(0).astype(int)
    df1['total'] = df1.sum(axis=1)
    if sort_by is None:
        df1 = df1.sort_values("total").copy()
    else:
        df1 = df1.sort_values(sort_by).copy()
    if verbose:
        print(f"Shape of the donation stats DF: {df1.shape}")
    df1.columns = pd.MultiIndex.from_product([['counts'], df1.columns])


    a = ddp_events_df_new[[collection_id_column,"ts_added_to_dataset"]].drop_duplicates()
    b = a.set_index(collection_id_column, inplace=False)
    these_donation_dates = b.to_dict()["ts_added_to_dataset"]
    df1["other","ts_added_to_dataset"] = df1.index.map(lambda x: these_donation_dates[x])


    df1.sort_values(by=[("other","ts_added_to_dataset")], inplace=True)
    #df1["other","D_id"] = list(range(len(df1)))



    donation_personas = generate_personas(ddp_events_df_new)
    if not donation_personas.empty and collection_id_column in donation_personas.columns:
        donation_personas.set_index(collection_id_column, inplace=True)
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
        data_io.save_parquet(df=combined_ddp_metadata, storage_location="processed_activities", filename="ddp_metadata.parquet", verbose=verbose)

    if verbose:
        print(f"Shape of the combined metadata DF: {combined_ddp_metadata.shape}")

    return combined_ddp_metadata














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

    sel = [(timestamp_column, ">=", START_DATE),(timestamp_column, "<=", END_DATE)]

    the_selected_donations = fyp_cf["study_defs"][study_name].get("SELECTED_DONATIONS",[])
    if len(the_selected_donations) > 0:
        the_selected_donations = [re.search(r'\[(.*?)\]', str(x)).group(1) if re.search(r'\[(.*?)\]', str(x)) else x for x in the_selected_donations]
        sel.append((collection_id_column, "in", the_selected_donations))

    if all_data is None:
        if verbose:
            print(f"    [DDP] Loading donation events from main storage")
        out_df = data_io.load_parquet("recoded", "donations_recoded.parquet", filters=sel,verbose=verbose)

    else:
        if verbose:
            print(f"    [DDP] Selecting date range from cached donation data")
        cached_ddp_events_df = all_data.copy()
        out_df = cached_ddp_events_df[(cached_ddp_events_df[timestamp_column]>=START_DATE) & (cached_ddp_events_df[timestamp_column]<=END_DATE)].copy()

        if not collection_id_column in out_df.columns or not timestamp_column in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] No events found in date range. Returning None.")
            return None

        if len(the_selected_donations) > 0:
            out_df = out_df[out_df[collection_id_column].isin(the_selected_donations)].copy()

        if not collection_id_column in out_df.columns or not timestamp_column in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] The selected donations have no events in the date range. Returning None.")
            return None

    print(f"    [DDP] ...done. | Shape: {out_df.shape} | Unique donations: {out_df[collection_id_column].nunique()} | Date range: {out_df[timestamp_column].min():%Y-%m-%d} -- {out_df[timestamp_column].max():%Y-%m-%d}")


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

    if not collection_id_column in grouping_factors:
        raise ValueError("!!! [DD Sampling] Group factors must include collection_id_column")

    # make sure collection_id_column is the first element 
    grouping_factors.remove(collection_id_column)
    grouping_factors = [collection_id_column] + grouping_factors

    if verbose:
        print(f"    [DD Sampling] Group factors: {grouping_factors}")

    if not "study_defs" in fyp_cf:
        init_study_defs()

    MIN_EVENTS_REQUIRED = fyp_cf["study_defs"][study_name].get("MIN_EVENT_COUNT_REQUIRED_PER_AGG_GROUP",10)
    MAX_EVENTS_SELECTED = fyp_cf["study_defs"][study_name].get("MAX_EVENT_COUNT_SELECTED_PER_AGG_GROUP",100)
    MIN_GROUP_COUNT_REQUIRED_PER_DONATION = fyp_cf["study_defs"][study_name].get("MIN_GROUP_COUNT_REQUIRED_PER_DONATION",10)
    MAX_GROUP_COUNT_SELECTED_PER_DONATION = fyp_cf["study_defs"][study_name].get("MAX_GROUP_COUNT_SELECTED_PER_DONATION",100)

    # sorting the events by donation and event id in order to have a replicable sample
    #donation_metadata_df = data_io.load_parquet(storage_location="processed_activities", filename="ddp_metadata.parquet")
    #donation_to_d_dict = donation_metadata_df[("other","D_id")].to_dict()

    #the_df["D_id"] = the_df[collection_id_column].map(donation_to_d_dict)
    #the_df = the_df.sort_values(by=["D_id","event_id"])


    # Separate watch and non-watch events 
    all_watch_events_df = the_df[the_df[event_type_column]=="watch"].copy()
    all_nonwatch_events_df = the_df[the_df[event_type_column]!="watch"].copy()
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


