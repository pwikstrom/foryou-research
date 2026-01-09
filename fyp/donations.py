#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


from .fyp_main import initialize
from . import data_io
import json
import os
import shutil
from numpy import int64 as np_int64






def _remove_link_events_with_corrupt_links(some_events_df):
    """Optimized: uses vectorized string length calculation instead of map(lambda)"""
    from pandas import concat

    non_video_ddp_events_df = some_events_df[some_events_df["primary_label"] != "link"].copy()
    video_ddp_events_df = some_events_df[some_events_df["primary_label"] == "link"].copy()
    
    # Vectorized string length calculation
    url_lengths = video_ddp_events_df.primary_value.str.len()
    most_common_url_length = int(url_lengths.value_counts().index[0])
    
    video_ddp_events_df = video_ddp_events_df[url_lengths == most_common_url_length].copy()
    some_events_df = concat([video_ddp_events_df, non_video_ddp_events_df])

    return some_events_df









def download_recent_metadata(hours_back: int,
                         output_dir: str,
                         *,
                         prefix: str = "metadata",
                         table_name: str = (
                             "data-donation-stack-"
                             "donationtablesmetadatatable1526CA1C-J3HP8RPY7RRW"
                         ),
                         campaign_name: str = "qut",
                         use_local_time: bool = False):


    """
    Scan *hours_back* into the past and save the raw DynamoDB JSON
    into ``output_dir/filename``.

    Returns
    -------
    Path
        The absolute path to the written JSON file.
    """

    import datetime as _dt
    from pathlib import Path
    import subprocess
    from shlex import quote as shlex_quote


    # ---------------------------------------------------------------
    # 1) Compute cut‑off time in ISO‑8601 (no microseconds)
    # ---------------------------------------------------------------
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())          # Brisbane local

    file_stamp = now.strftime("%Y%m%d%H%M%S") 



    # ---------------------------------------------------------------
    # 2) Prepare destination file
    # ---------------------------------------------------------------
    dest_dir = Path(output_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    outfile = dest_dir / f"{prefix}_{file_stamp}.json"

    # ---------------------------------------------------------------
    # 3) Assemble the AWS CLI command
    # ---------------------------------------------------------------
    scan_cmd = (
        "aws dynamodb scan "
        f"--table-name {shlex_quote(table_name)} "
        "--select ALL_ATTRIBUTES "
        "--page-size 500 "
        "--max-items 100000 "
        "--output json"
    )

    full_cmd = f"{scan_cmd} > {shlex_quote(str(outfile))}"

    # ---------------------------------------------------------------
    # 4) Run it
    # ---------------------------------------------------------------
    subprocess.run(full_cmd, shell=True, check=True)

    return outfile






def download_recent_donations(hours_back: int,
                              cf: dict = None,
                              *,
                              table_name: str = (
                                  "data-donation-stack-"
                                  "donationtablesmetadatatable1526CA1C-J3HP8RPY7RRW"
                              ),
                              bucket: str = (
                                  "data-donation-stack-"
                                  "donationbucket71125dbb-woyvcojrhlcw"
                              ),
                              campaign_name: str = "qut",
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

    import datetime as _dt
    from pathlib import Path
    import subprocess
    from shlex import quote as shlex_quote
    from shutil import rmtree


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
    temp_dir_path = os.path.join(cf["paths"]["temp"], "download_batch_" + now.strftime("%Y%m%d%H%M%S"))
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
    downloaded_files = os.listdir(dest)
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
        rmtree(dest)
    except Exception as e:
        print(f"Warning: Failed to clean up temp directory {dest}: {e}")








def identify_similar_donations(
    new_events=None,
    old_events=None,
    dont_check_these_cols=[],
    overlap_threshold=0.5):
    """
    Identify similar donations based on timestamp overlap.

    check for similarities in the donations by looking for the same timestamps in the donations. 
    The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates


    This function compares the timestamps of events in new donations against old donations (or within new donations themselves)
    to identify potential duplicates or highly similar donations.

    Parameters
    ----------
    new_events : pandas.DataFrame
        DataFrame containing the new donation events. Must contain 'donation_id', 'feature_name', and 'timestamp' columns.
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
    fine_events_df = new_events[~new_events.feature_name.isin(dont_check_these_cols)].copy()
    for d,i in fine_events_df.groupby('donation_id'):
        new_events_ts_dict[d] = set([int(j) for j in i['timestamp'].values])
    
    if old_events is not None:
        old_events_ts_dict = {}
        fine_events_df = old_events[~old_events.feature_name.isin(dont_check_these_cols)].copy()
        for d,i in fine_events_df.groupby('donation_id'):
            old_events_ts_dict[d] = set([int(j) for j in i['timestamp'].values])
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









# identify exact duplicates among the donated JSONs and remove these
# the filtered JSONs go inte the new variable 'no_duplicate_donations'

def drop_duplicates_donations(donation_data, no_duplicate_donations = {}):
    """
    Identify and remove exact duplicate donations from the raw data.

    Parameters
    ----------
    donation_data : dict
        Dictionary of raw donation data where keys are donation IDs and values are donation content.
    no_duplicate_donations : dict, optional
        Dictionary to store unique donations. If provided, checks against these as well.

    Returns
    -------
    dict
        A dictionary containing only the unique donations.
    """
    # iterate over all donation IDs
    print(f"Number of donations before dropping duplicates: {len(donation_data)}")
    for donation_id in donation_data.keys():
        already_donated = None
        for nd in no_duplicate_donations.keys():
            if donation_data[donation_id] == no_duplicate_donations[nd]:
                already_donated = nd
                break
        if already_donated:
            pass
        else:
            no_duplicate_donations[donation_id] = donation_data[donation_id].copy()
    
    return no_duplicate_donations.copy()







def transform_data_to_df(data_input, donation_item_id=0):
    """
    Transform raw donation dictionary into a structured pandas DataFrame.

    This function flattens the nested dictionary structure of donations, extracts relevant events,
    and performs initial cleaning and feature engineering.

    Parameters
    ----------
    data_input : dict
        Dictionary of raw donation data.
    donation_item_id : int, default 0
        Starting ID for donation items.

    Returns
    -------
    tuple
        - pandas.DataFrame: DataFrame containing the processed events.
        - dict: Unchanged donated variables (currently empty).
    """


    from collections import deque
    import pandas as pd
    import numpy as np


    donation_items = []

    # --- 1. recurse once per donation to recode & clean ----------
    for donation_id, donation_dict in data_input.items():
        #cleaned = _recode_recursive(donation_dict, recode_the_donation_keys)

        # --- 2. single pass: find *any* list of dicts -------------
        stack = deque([(None, donation_dict)])       # (feature_name, current_obj)
        while stack:
            feature, obj = stack.pop()
            if isinstance(obj, list):          # this is an event list
                for item in obj:
                    if isinstance(item, dict) and item:           # non-empty dict
                        donation_items.append({
                            "donation_id":       donation_id,
                            "donation_item_id":  donation_item_id,
                            "feature_name":      (feature or '').replace('xxx','').lower(),
                            "variable_list":     [k.lower() for k in item.keys()],
                            "value_list":        list(item.values())
                        })
                        donation_item_id += 1
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    stack.append((k, v))

    # --- 3. nothing found? bail out early ------------------------
    if not donation_items:
        return pd.DataFrame(), {}

    events = pd.DataFrame.from_records(donation_items)

    # --- 4. vectorised post-processing ---------------------------
    # keep rows that have at least one variable and contain 'date'
    mask_date = events['variable_list'].map(lambda lst: 'date' in lst)
    events = events[mask_date & (events['variable_list'].map(len) > 0)].copy()

    events['date']          = pd.to_datetime(events['value_list'].str[0])
    events['primary_label'] = events['variable_list'].str[1]
    events['primary_value'] = events['value_list'].str[1]

    # to ns → s int
    events['timestamp'] = (events['date'].astype('int64') // 1_000_000_000).astype(int)
    events['ts_jiggled'] = events['date'].astype('int64') + np.random.randint(-10_000, 10_000,
                                                                   size=len(events))

    events['secondary_label'] = pd.NA
    events['secondary_value'] = pd.NA


    # --- identify posts made by the donor
    post_events = [k for k in events.index if "whocanview" in events.loc[k,"variable_list"]]
    events.loc[post_events,"feature_name"] = "post"
    events.loc[post_events,"primary_label"] = "post_link"

    events["feature_name"] = events["feature_name"].map(
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
    ).copy()

    #return events.set_index('donation_item_id'), {}   # donated_variables unchanged

    # --- 5. watch-duration delta (vectorised, but with 2-step assignment)
    watch = (events.query("feature_name == 'watch'")
                .sort_values(['donation_id', 'ts_jiggled']))

    watch['delta'] = (watch.groupby('donation_id')['timestamp']
                            .shift(-1) - watch['timestamp'])

    short = watch.loc[watch['delta'].between(0, 15*60), ['donation_item_id', 'delta']]
    short = short.set_index('donation_item_id')
    #return short, events

    events = events.set_index('donation_item_id')

    events.loc[short.index, 'secondary_label'] = 'watch_duration'
    events.loc[short.index, 'secondary_value'] = short['delta']


    return events, {}   # donated_variables unchanged










def calc_donated_items_stats(edf, sort_by=None):
    """
    Calculate statistics for donated items, specifically counting feature occurrences per donation.

    Parameters
    ----------
    edf : pandas.DataFrame
        Events DataFrame containing 'donation_id' and 'feature_name' columns.
    sort_by : str, optional
        Column name to sort the resulting DataFrame by. If None, sorts by 'total'.

    Returns
    -------
    pandas.DataFrame
        A DataFrame with donation IDs as index and counts of each feature as columns.
        Includes a 'total' column and 'donation_date' (if available).
    """
    
    import pandas as pd

    if not isinstance(edf, pd.DataFrame):
        raise ValueError("edf must be a pandas DataFrame")
    if 'donation_id' not in edf.columns:
        print("Shape of the donation stats DF: (0,0)")
        return pd.DataFrame()
        
    df1 = edf.groupby('donation_id').feature_name.value_counts().unstack().fillna(0).astype(int)
    df1['total'] = df1.sum(axis=1)
    if sort_by is None:
        df1 = df1.sort_values("total").copy()
    else:
        df1 = df1.sort_values(sort_by).copy()
    print(f"Shape of the donation stats DF: {df1.shape}")

    df1.columns = pd.MultiIndex.from_product([['counts'], df1.columns])

    these_donation_dates = edf[["donation_id","donation_date"]].set_index("donation_id", inplace=False).to_dict()["donation_date"]

    df1["other","donation_date"] = df1.index.map(lambda x: these_donation_dates[x])

    return df1








def load_special_donations(
    cf = None, 
    study_name = None, 
    all_data = None,
    verbose=False):
    # sometimes it is useful to select events in a specific donation.


    import fyp.data_io as data_io
    from datetime import datetime
    from fyp.fyp_main import initialize

    if study_name is None:
        raise ValueError("study_name must be specified")


    if cf is None:
        cf = initialize()

    the_special_donations = cf["study_defs"][study_name]["SPECIAL_DONATIONS"]

    if len(the_special_donations) == 0:
        if verbose:
            print("Skipping special DDP events loading as the number of SPECIAL_DONATIONS is zero.")
            #print("--"*60)
        return {"data_special_ddps":DataFrame()}

    donations_str = '; '.join(the_special_donations)
    

    DDP_START_DATE = cf["study_defs"][study_name]["DDP_START_DATE"]
    if isinstance(DDP_START_DATE, str):
        DDP_START_DATE = datetime.strptime(DDP_START_DATE, "%Y-%m-%d").date()
    
    DDP_END_DATE = cf["study_defs"][study_name]["DDP_END_DATE"]
    if isinstance(DDP_END_DATE, str):
        DDP_END_DATE = datetime.strptime(DDP_END_DATE, "%Y-%m-%d").date()

    if verbose:
        print(f"Trying to load all events from {len(the_special_donations)} donations", end=" ", flush=True)

    if all_data is None:
        sel = [("D_donation_id", "in", the_special_donations),("T_local_date", ">=", DDP_START_DATE),("T_local_date", "<=", DDP_END_DATE)]
        special_ddp_events_df = data_io.load_parquet(cf, "recoded", "donations_recoded.parquet", filters=sel,verbose=verbose)
    else:
        special_ddp_events_df = all_data.copy()
        special_ddp_events_df = special_ddp_events_df[(special_ddp_events_df.D_donation_id.isin(the_special_donations)) & (special_ddp_events_df.T_local_date>=DDP_START_DATE) & (special_ddp_events_df.T_local_date<=DDP_END_DATE)].copy()
        if verbose:
            print(f"Special DDP events - selected date range: {special_ddp_events_df.T_local_date.min():%Y-%m-%d} ---- {special_ddp_events_df.T_local_date.max():%Y-%m-%d} Shape: {special_ddp_events_df.shape}")

    return special_ddp_events_df

    if verbose:
        print(f"Special DDP events dataframe loaded: {special_ddp_events_df.D_donation_id.nunique()} unique donations. Shape: {special_ddp_events_df.shape}")
        print(f"The special DDP events range from {special_ddp_events_df.T_local_date.min():%Y-%m-%d} -- {special_ddp_events_df.T_local_date.max():%Y-%m-%d}")


    return special_ddp_events_df









def sample_ddp_events(
    cf = None, 
    study_name = None, 
    all_ddp_events_df = None, 
    verbose=False):

    from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()
    
    if all_ddp_events_df is None:
        raise ValueError("[DD Sampling] all_ddp_events_df cannot be None")

    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.

    group_factors = cf["var_schema"][cf["var_schema"]["role"]=='group_factor'].variable_name.to_list()
    group_factors = sorted(group_factors) # in case they are entered in different order...

    """all_ddp_events_df = data_io.load_parquet(
        cf=cf,
        storage_location="recoded",
        filename="donations_recoded.parquet",
        #columns = ["D_feature_name","dd_event_id"] + group_factors,
        verbose=verbose)"""

    AGG_GROUP_SIZE_PERCENTILE_LIMITS = cf["study_defs"][study_name]["AGG_GROUP_SIZE_PERCENTILE_LIMITS"]
    MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION = cf["study_defs"][study_name]["MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION"]
    N_SAMPLED_DATES_FROM_EACH_DONATION = cf["study_defs"][study_name]["N_SAMPLED_DATES_FROM_EACH_DONATION"]
    N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP = cf["study_defs"][study_name]["N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP"]

    print("Sampling DDP events...")

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

    print(f"...done. Sampled {N_SAMPLED_EVENTS_FROM_EACH_AGG_GROUP} events from each donation-date group, yielding {len(sampled_ddp_events_in_sampled_donation_date_groups):,} events")

    sel = [
        ("dd_event_id", "in", sampled_ddp_events_in_sampled_donation_date_groups["dd_event_id"].unique()),
    ]

    
    return sampled_ddp_events_in_sampled_donation_date_groups





def add_session_stats_to_ddp_log(ddp_log_in, session_id_counter = np_int64(1_000_000), verbose=False):
    # attach session stats to donation events

    from pandas import isna as pd_isna, concat
    import numpy as np

    ddp_log = ddp_log_in.copy()

    all_sessions = []
    if len(ddp_log) and ("D_donation_id" in ddp_log.columns):

        ddp_log['session_id'] = -1
        ddp_log['event_order_in_session'] = -1
        ddp_log['event_pos_in_session'] = -1.0
        
        # Collect all updates, then apply in bulk at the end
        updates_list = []


        for one_donation_id,one_donation in ddp_log.groupby("D_donation_id"):

            watch = (one_donation.sort_values(['D_ts_jiggled'])).copy()

            watch['delta'] = watch['D_local_timestamp'].shift(-1) - watch['D_local_timestamp']
            # Vectorized timedelta conversion to seconds
            watch['delta'] = watch['delta'].dt.total_seconds()

            # VECTORIZED SESSION ID ASSIGNMENT (replaces slow for loop)
            # A new session starts when delta is >15 minutes or is NaN
            session_breaks = (watch['delta'].isna()) | (watch['delta'] > 15*60)
            # Cumsum creates incrementing session IDs at each break
            session_nums = session_breaks.astype(bool).cumsum()
            # Add the counter offset and assign
            watch['session_id'] = session_id_counter + session_nums
            
            # Update counter for next donation
            session_id_counter = watch['session_id'].max() + 1
            
            # VECTORIZED EVENT ORDER (replaces loop)
            # groupby().cumcount() gives sequential numbering within each session
            watch['event_order_in_session'] = watch.groupby('session_id').cumcount()
            # Adjust: events at session breaks get -1, others keep their count
            watch.loc[session_breaks, 'event_order_in_session'] = -1

            session_stats = watch.groupby('session_id').agg(
                session_duration=('delta', 'sum'),
                session_start_ts=('D_local_timestamp', 'min'),
                n_videos_in_session=('event_order_in_session', 'max'),
            )

            session_stats = session_stats.astype(int)
            session_stats["session_end_ts"] = session_stats["session_start_ts"] + session_stats["session_duration"]
            session_stats["donation_id"] = one_donation_id

            watch['n_videos_in_session'] = watch['session_id'].map(session_stats['n_videos_in_session'].to_dict())
            watch['event_pos_in_session'] = watch['event_order_in_session'] / watch['n_videos_in_session']
            watch['event_pos_in_session'] = watch['event_pos_in_session'].fillna(-1).astype(float)

            session_stats["n_videos_in_session"] = session_stats["n_videos_in_session"]+1

            short = watch.loc[watch['delta'].between(0, 15*60), ['delta', 'session_id', 'event_order_in_session', 'event_pos_in_session']]

            # OPTIMIZATION: Store updates instead of applying immediately
            if len(short) > 0:
                updates_list.append(short)

            all_sessions += [session_stats]

        # Apply all updates at once
        if updates_list:
            all_updates = concat(updates_list)
            ddp_log.loc[all_updates.index, 'session_id'] = all_updates['session_id']
            ddp_log.loc[all_updates.index, 'event_order_in_session'] = all_updates['event_order_in_session']
            ddp_log.loc[all_updates.index, 'event_pos_in_session'] = all_updates['event_pos_in_session'].astype(float)
            ddp_log.loc[all_updates.index, 'D_secondary_value'] = all_updates['delta']
        
        # Set first event in each session to -1
        ddp_log.loc[ddp_log[ddp_log["event_order_in_session"]==0].index, "D_secondary_value"] = -1

        if verbose:
            print("Adding session stats to DDP data",ddp_log.shape)
        

    else:
        if verbose:
            print("no ddp data")

    #if verbose:
        #print("--"*60)
    return ddp_log, session_id_counter












def process_ddp_log_for_core_dataset(
    cf = None, 
    all_ddp_events_df = None, 
    session_id_counter = np_int64(1_000_000), 
    verbose=False):
    # combine the special DDP events with the all DDP events

    from pandas import DataFrame, concat
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow
    from fyp.organize_datasets import rename_columns
    from numpy import int64 as np_int64
    from pandas import NA as pd_NA

    if all_ddp_events_df is None:
        raise ValueError("all_ddp_events_df must be specified")

    if cf is None:
        cf = initialize()



    ddp_log = all_ddp_events_df.copy()

    ddp_log.loc[ddp_log[ddp_log["primary_label"]=="ip"].index,"feature_name"] = "login_event"


    ddp_log["secondary_label"] = ddp_log["secondary_label"].fillna("")
    ddp_log["secondary_value"] = ddp_log["secondary_value"].fillna(np_int64(-1))

    #ddp_log = ddp_log.drop(columns=[
    #    "sample_id", "donation_date"], errors="ignore").copy()


    ddp_log = ddp_log.rename(columns={c:"D_"+c if not c in ["item_id"] else c for c in ddp_log.columns}).copy()

    if verbose:
        print(f"Shape of all DDP events DF: {ddp_log.shape} from {ddp_log.D_donation_id.nunique()} donations")
        print(f"The dates of the DDP events range from {ddp_log.D_date.min()} -- {ddp_log.D_date.max()}")


    if "var_schema" in cf and not cf["var_schema"].empty:
        vs = cf["var_schema"]
        # TODO: Keep an eye on this - I want it more dynamic. Structural columns
        structural_ddp_cols = [
            'item_id', 'D_sample_id',
            'T_local_timestamp', 'T_local_weekday', 'T_local_week',
            'T_local_hour', 'T_local_day_segment', 'T_local_date',
            'session_id', 'event_order_in_session',
            'event_pos_in_session',
            'D_donation_id',
            'D_feature_name','D_primary_label',
            'D_primary_value',
            'D_secondary_label', 'D_secondary_value',
        ]
                
        d_vars = vs[vs['variable_name'].str.startswith('D_', na=False)]['variable_name'].tolist()
        relevant_ddp_cols = structural_ddp_cols + d_vars
        relevant_ddp_cols = list(dict.fromkeys(relevant_ddp_cols))
    else:
        raise ValueError("var_schema not found in config")


    ddp_log, _ = add_session_stats_to_ddp_log(ddp_log, verbose=verbose)
    ddp_log = rename_columns(ddp_log)
    ddp_log = ddp_log[relevant_ddp_cols].copy()

    
    return ddp_log







def ingest_ddp_events(
    cf = None, 
    verbose=False):
    # load DF with all donations previously ingested

    from os import listdir, remove
    from os.path import join, exists
    from json import load as json_load
    from pandas import concat
    import fyp.data_io as data_io
    from datetime import datetime
    from fyp.fyp_main import initialize, connect_to_google, convert_dtypes_to_pyarrow
    from fyp.organize_datasets import extract_local_time_features

    if cf is None:
        cf = initialize()
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)


    print("Loading all DDP events...", end=" ", flush=True)
    all_ddp_events_df = data_io.load_parquet(cf, "ddp_main", f"all_participant_events.parquet", verbose=verbose)

    # drop two columns
    all_ddp_events_df = all_ddp_events_df.drop(["value_list","variable_list"], axis=1).copy()

    # Extract date
    all_ddp_events_df['simple_date'] = all_ddp_events_df['date'].dt.date
    
    # Extract sample_id
    all_ddp_events_df["sample_id"] = all_ddp_events_df.ts_jiggled.astype(str).str[-4:].astype(int)
    
    print(f"...DDP events dataframe loaded")
    print(f"The DF contains {all_ddp_events_df.donation_id.nunique()} unique donations and a total of {all_ddp_events_df.shape[0]:,} logged events.")

    if verbose:
        print(f"The DDP events range from {all_ddp_events_df.date.min()} -- {all_ddp_events_df.date.max()}")


    # dropping some corrupt URLs simply by calculating the most common length of the URLs and dropping those that doesn't match
    all_ddp_events_df = _remove_link_events_with_corrupt_links(all_ddp_events_df)
    if verbose:
        print(f"Dropping DDP events with corrupt TikTok URLs. New shape: {all_ddp_events_df.shape}")

    all_ddp_events_df = extract_local_time_features(
        cf = cf,
        some_events_df_in = all_ddp_events_df,
        kind_of_log = 'ddp',
        verbose = verbose)

    all_ddp_events_df = process_ddp_log_for_core_dataset(
        cf = cf, 
        all_ddp_events_df = all_ddp_events_df, 
        verbose=verbose)


    all_ddp_events_df = data_io.save_parquet(
        cf,
        all_ddp_events_df,
        "ddp_main", 
        f"all_participant_events_2.parquet", 
        verbose=verbose)


    return all_ddp_events_df







def load_ddp_events(
    cf = None, 
    study_name = None, 
    all_data = None,
    verbose=False):
    # load DF with all donations previously ingested

    import fyp.data_io as data_io
    from datetime import datetime
    from fyp.fyp_main import initialize

    if study_name is None:
        raise ValueError("study_name must be specified")


    if cf is None:
        cf = initialize()


    if not cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() in ["sample","all"]:
        if verbose:
            print("Not loading DDP events")
        return None


    print(f"Loading all DDP events...")


    DDP_START_DATE = cf["study_defs"][study_name]["DDP_START_DATE"]
    if isinstance(DDP_START_DATE, str):
        DDP_START_DATE = datetime.strptime(DDP_START_DATE, "%Y-%m-%d").date()
    
    DDP_END_DATE = cf["study_defs"][study_name]["DDP_END_DATE"]
    if isinstance(DDP_END_DATE, str):
        DDP_END_DATE = datetime.strptime(DDP_END_DATE, "%Y-%m-%d").date()

    if all_data is None:
        sel = [("T_local_date", ">=", DDP_START_DATE),("T_local_date", "<=", DDP_END_DATE)]
        all_ddp_events_df = data_io.load_parquet(cf, "recoded", f"donations_recoded.parquet", filters=sel, verbose=verbose)
    else:
        all_ddp_events_df = all_data.copy()
        all_ddp_events_df = all_ddp_events_df[(all_ddp_events_df.T_local_date>=DDP_START_DATE) & (all_ddp_events_df.T_local_date<=DDP_END_DATE)].copy()
        if verbose:
            print(f"DDP events - selected date range: {all_ddp_events_df.T_local_date.min():%Y-%m-%d} ---- {all_ddp_events_df.T_local_date.max():%Y-%m-%d} Shape: {all_ddp_events_df.shape}")


    print(f"...done. DDP events dataframe loaded. {all_ddp_events_df.D_donation_id.nunique()} unique donations. {all_ddp_events_df.shape[0]:,} events.")


    if cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() == "all":
        return all_ddp_events_df
    else:
        sampled_data_ddp_events = sample_ddp_events(
            cf = cf, 
            study_name = study_name, 
            all_ddp_events_df = all_ddp_events_df, 
            verbose=verbose)
        return sampled_data_ddp_events














################################################################################
################################################################################
################################################################################
################################################################################
################################################################################


