#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""







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
                              output_dir: str,
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
    Scan the Donations metadata table for items whose *date* (\"shareDate\")
    is within the last ``hours_back`` hours and download the associated files
    to ``output_dir``.

    Parameters
    ----------
    hours_back : int
        How far back to look (in hours) from *now*.
    output_dir : str
        Directory where the files pulled from S3 will be written.
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


    # ------------------------------------------------------------------
    # 1) Figure out the time window and format it the way the table stores it
    # ------------------------------------------------------------------
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())     # Brisbane local
    cutoff = now - _dt.timedelta(hours=hours_back)
    share_date = cutoff.replace(microsecond=0).isoformat()

    # ------------------------------------------------------------------
    # 2) Make sure the destination directory exists
    # ------------------------------------------------------------------
    dest = Path(output_dir).expanduser().resolve()
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
    # 4) Run it
    # ------------------------------------------------------------------
    subprocess.run(full_cmd, shell=True, check=True)





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





################################################################################
################################################################################
################################################################################
################################################################################
################################################################################


