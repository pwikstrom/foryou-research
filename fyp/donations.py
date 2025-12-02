

from os import rename as os_rename
import pandas as pd
import numpy as np
from shlex import quote as shlex_quote
from pathlib import Path
from typing import Any, Dict, List
import re
import requests




def download_recent_metadata(hours_back: int,
                         output_dir: str,
                         *,
                         prefix: str = "metadata",
                         table_name: str = (
                             "data-donation-stack-"
                             "donationtablesmetadatatable1526CA1C-J3HP8RPY7RRW"
                         ),
                         campaign_name: str = "qut",
                         use_local_time: bool = False) -> Path:


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



    # ---------------------------------------------------------------
    # 1) Compute cut‑off time in ISO‑8601 (no microseconds)
    # ---------------------------------------------------------------
    now = (_dt.datetime.now(_dt.timezone.utc)
           if not use_local_time
           else _dt.datetime.now().astimezone())          # Brisbane local
    # cutoff = now - _dt.timedelta(hours=hours_back)
    # share_date = cutoff.replace(microsecond=0).isoformat()

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
        #"--filter-expression "
        #"\"campaign = :campaignName and consentProvided = :consent "
        #"and #d >= :shareDate\" "
        #"--expression-attribute-names "
        #"'{\"#d\": \"date\"}' "
        #"--expression-attribute-values "
        #f"'{{\":campaignName\": {{\"S\": \"{campaign_name}\"}}, "
        #f"\":consent\": {{\"BOOL\": true}}, "
        #f"\":shareDate\": {{\"S\": \"{share_date}\"}}}}' "
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
    #scan_cmd = (
    #    "aws dynamodb scan "
    #    f"--table-name {shlex_quote(table_name)} "
    #    "--filter-expression "
    #    "\"campaign = :campaignName and consentProvided = :consent and #d >= :shareDate\" "
    #    "--expression-attribute-names "
    #    "'{\"#d\": \"date\"}' "
    #    "--expression-attribute-values "
    #    f"'{{\":campaignName\": {{\"S\": \"{campaign_name}\"}}, "
    #    f"\":consent\": {{\"BOOL\": true}}, "
    #    f"\":shareDate\": {{\"S\": \"{share_date}\"}}}}' "
    #    "--query 'Items[*].id.S'"
    #)

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

#        f"\":consent\": {{\"BOOL\": true}}, "
#        f"'{{\":shareDate\": {{\"S\": \"{share_date}\"}}}}' "
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



# check for similarities in the donations by looking for the same timestamps in the donations. 
# The assumption is that if two donations have a lot of the same timestamps, they are likely to be duplicates


def identify_similar_donations(
    new_events=None,
    old_events=None,
    dont_check_these_cols=[],
    overlap_threshold=0.5):

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








def move_files(filenames_to_move, from_dir, to_dir):

    from os.path import join, exists

    my_little_counter = 0
    for filename in filenames_to_move:
        if exists(join(from_dir, filename)):

            # move the file to the archive folder
            os_rename(join(from_dir, filename), join(to_dir, filename))
            
            my_little_counter += 1
    if my_little_counter == 0:
        print("No files to move")
    else:
        print(f"Moved {my_little_counter} files from {from_dir} to {to_dir}")




# identify exact duplicates among the donated JSONs and remove these
# the filtered JSONs go inte the new variable 'no_duplicate_donations'

def drop_duplicates_donations(donation_data, no_duplicate_donations = {}):
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






OEURL   = "https://www.tiktok.com/oembed"
HEADERS = {"User-Agent": "Mozilla/5.0"}      # stops the occasional 403

def get_tiktok_meta(url: str, timeout: int = 10) -> dict:
    """
    Return {'title': ..., 'thumbnail': ...} for a TikTok video link.
    
    Works with full   https://www.tiktok.com/@user/video/<id>
            short    https://vm.tiktok.com/ZSe.../
            share    https://www.tiktokv.com/share/video/<id>/
    """
    url = _canonicalise(url)                          # ① make it oEmbed‑friendly
    if url is None:
        return None
    try:
        r   = requests.get(OEURL, params={"url": url},
                       headers=HEADERS, timeout=timeout)
        #r.raise_for_status()                              # raises on 4xx/5xx
        data = r.json()
        return data
    except Exception:
        return None

# ---------------------------------------------------------

def _canonicalise(link: str) -> str:
    """
    Convert any TikTok link to something the oEmbed API likes.
    Strategy:
      • If it's already a normal TikTok URL, keep it.
      • Else, pull the 19‑digit video ID and build
        https://m.tiktok.com/v/<id>.html   (accepted by oEmbed).
    """
    if "tiktok.com" in link and not link.startswith("https://www.tiktokv.com"):
        return link                                           # already fine
    
    # extract the numeric ID from /video/<id>/ or /v/<id>...
    m = re.search(r"/video/(\d{10,20})|/v/(\d{10,20})", link)
    if not m:
        return None   #   raise ValueError("Can’t find a video ID in that URL.")
    vid = next(group for group in m.groups() if group)        # first non‑None
    return f"https://m.tiktok.com/v/{vid}.html"               # oEmbed‑friendly









def calc_donated_items_stats(edf, sort_by=None):
    
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





def calc_persona_distrib(my_df):
    some_result = {}
    for c in my_df.columns:
        some_result[c] = {}
        if isinstance(my_df[c].iloc[0], list):
            lists_df = pd.DataFrame(my_df[c].tolist())

            some_result[c]['mean'] = lists_df.mean(axis=0).tolist()
            some_result[c]['q25'] = lists_df.quantile(0.25, axis=0).tolist()
            some_result[c]['q75'] = lists_df.quantile(0.75, axis=0).tolist()
            some_result[c]['median'] = lists_df.median(axis=0).tolist()
            some_result[c]['min'] = lists_df.min(axis=0).tolist()
            some_result[c]['max'] = lists_df.max(axis=0).tolist()
        else:
            try:
                some_result[c]['mean'] = my_df[c].mean()
                some_result[c]['q25'] = my_df[c].quantile(0.25)
                some_result[c]['q75'] = my_df[c].quantile(0.75)
                some_result[c]['median'] = my_df[c].median()
                some_result[c]['min'] = my_df[c].min()
                some_result[c]['max'] = my_df[c].max()
            except Exception:
                pass
                #print(f"{c} is not a number of a list of numbers")
    return pd.DataFrame(some_result).T








def transform_data_to_df(data_input, donation_item_id=0):


    from collections import deque
    import pandas as pd



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



def load_metadata_directory(
    dir_path: str | Path,
    *,
    include_filename: bool = True,
) -> pd.DataFrame:
    """
    Read every *.json file in *dir_path* and concatenate the DynamoDB
    `Items` sections into one pandas DataFrame.

    Parameters
    ----------
    dir_path : str | Path
        Folder that contains the metadata_YYYYMMDDHHMMSS.json files (or any
        other *.json files with the same structure).
    include_filename : bool, default True
        If True, add a column ``_source_file`` that records which JSON file
        each row came from.

    Returns
    -------
    pandas.DataFrame
        One row per donation‑record, with all attributes expanded into
        normal Python scalars/lists/dicts.
    """

    from pathlib import Path
    import pandas as pd
    import json


    dir_path = Path(dir_path).expanduser().resolve()
    if not dir_path.is_dir():
        raise NotADirectoryError(dir_path)

    rows: List[Dict[str, Any]] = []

    def _deser(value: Dict[str, Any]) -> Any:
        """Convert DynamoDB JSON value → native Python."""
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

    for path in sorted(dir_path.glob("*.json")):
        with path.open("r", encoding="utf‑8") as f:
            payload = json.load(f)

        for item in payload.get("Items", []):
            py_item = {k: _deser(v) for k, v in item.items()}
            if include_filename:
                py_item["_source_file"] = path.name
            rows.append(py_item)

    if not rows:
        raise ValueError(f"No DynamoDB‑style Items found in {dir_path}")

    df = pd.DataFrame(rows)
    # Parse ISO timestamps, if present, into pandas datetimes
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], utc=True)
    return df




def update_metadata_store(
    json_dir: str | Path,
    store_path: str | Path,
    *,
    dedupe_on: str = "id",
    output_format: str = "pickle",     # "parquet", "csv", or "pickle"
    **load_kwargs,                     # forwarded to load_metadata_directory
) -> pd.DataFrame:
    """
    Ingest new JSON files, merge with an existing on‑disk DataFrame,
    save the result, and return it.

    Parameters
    ----------
    json_dir : str | Path
        Folder containing the metadata_*.json files.
    store_path : str | Path, default 'metadata_store.parquet'
        Where the master DataFrame is (or will be) stored.
    dedupe_on : str, default 'id'
        Column used to drop duplicates when combining.
    output_format : {'parquet', 'csv', 'pickle'}, default 'parquet'
        Format used when writing back to disk.
    **load_kwargs
        Extra keyword args passed straight to `load_metadata_directory`
        (e.g. include_filename=False).

    Returns
    -------
    pandas.DataFrame
        The updated, de‑duplicated DataFrame.
    """

    from pathlib import Path
    import pandas as pd


    json_dir = Path(json_dir).expanduser().resolve()
    store_path = Path(store_path).expanduser().resolve()

    # ---- 1) Load fresh JSON records ------------------------------------------------
    new_df = load_metadata_directory(json_dir, **load_kwargs)

    # ---- 2) Read the existing store (if it exists) ---------------------------------
    if store_path.exists():
        if output_format == "parquet":
            existing_df = pd.read_parquet(store_path)
        elif output_format == "csv":
            existing_df = pd.read_csv(store_path)
        elif output_format == "pickle":
            existing_df = pd.read_pickle(store_path)
        else:
            raise ValueError("output_format must be 'parquet', 'csv', or 'pickle'")
        print(f"Found {len(existing_df)} existing records in the metadata store")
        combined = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined = new_df

    # ---- 3) De‑duplicate on the chosen key -----------------------------------------
    if dedupe_on in combined.columns:
        combined = combined.drop_duplicates(subset=dedupe_on, keep="last")

    print(f"Updated and deduped store contains {len(combined)} records")

    # ---- 4) Persist the updated table ----------------------------------------------
    store_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "parquet":
        combined.to_parquet(store_path, index=False)
    elif output_format == "csv":
        combined.to_csv(store_path, index=False)
    elif output_format == "pickle":
        combined.to_pickle(store_path)

    return len(combined)


