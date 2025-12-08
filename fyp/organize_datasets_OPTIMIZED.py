
from zoneinfo import ZoneInfo
import fyp.fyp_main as fyp
from numpy import int64 as np_int64
import pandas as pd

WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}





def _get_day_segment_from_hour_of_day(the_hour):
    if the_hour in [0,1,2,3,4,5]:
        return "night"
    elif the_hour in [6,7,8,9,10,11]:
        return "morning" 
    elif the_hour in [12,13,14,15,16,17]:
        return "afternoon"
    else:
        return "evening"






def extract_local_time_features(study_name, some_events_df_in, kind_of_log=None, verbose=False):
    """
    Optimized version - extracts local time features from timestamps using vectorized operations.
    
    This function was previously slow due to iterrows() and map(lambda) calls.
    Now uses vectorized pandas operations for much better performance.
    """
    from pandas import concat, to_datetime, Categorical
    from numpy import select as np_select

    TIME_ZONE = fyp.cf["study_defs"][study_name]["TIME_ZONE"]

    df = some_events_df_in.copy()

    if verbose:
        print(f"Processing timestamps in dataset to extract local time features (Timezone:{TIME_ZONE})")

    # ---------------------------------------------------------------------
    # 1. Build local_timestamp depending on log type
    # ---------------------------------------------------------------------
    if kind_of_log == "baseline":
        # assume timestamp_collected is naive datetime64[ns]
        tz_col = "source_url.tz_name"
        ts_col = "timestamp_collected"

        unique_tz = df[tz_col].dropna().unique()
        if len(unique_tz) == 1:
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            df["local_timestamp"] = df[ts_col].dt.tz_localize(tz)
        else:
            # Slower path: per-timezone blocks (still way faster than iterrows)
            local_parts = []
            for tz_name, block in df.groupby(tz_col, sort=False):
                tz = ZoneInfo(tz_name)
                part = block[ts_col].dt.tz_localize(tz)
                local_parts.append(part)
            df["local_timestamp"] = concat(local_parts).sort_index()

        df = df.drop(columns=[ts_col])

    elif kind_of_log == "ddp":

        # Build item_id if missing
        if "item_id" not in df.columns:
            # rsplit is cheaper than full split, only looks from the right
            extracted = (
                df["primary_value"]
                .astype("string")
                .str.rsplit("/", n=2)
                .str[-2]
            )

            # SAFE INTEGER PARSING: avoid float64 / to_numeric
            # keep only pure digit strings, everything else -> <NA>
            digits = extracted.str.fullmatch(r"\d+")
            ints = extracted.where(digits).astype("Int64")

            mask = (
                df["primary_label"].eq("link")
                & df["feature_name"].notna()
            )
            df["item_id"] = ints.where(mask)


        # normalise timestamp column name
        if "utc_timestamp" not in df.columns:
            df = df.rename(columns={"timestamp": "utc_timestamp"})


        # vectorised UTC -> local conversion
        utc_ts = to_datetime(
            df["utc_timestamp"].astype("int64"),
            unit="s",
            utc=True
        )
        df["local_timestamp"] = utc_ts.dt.tz_convert(TIME_ZONE)


    else:
        raise ValueError("kind_of_log can only be 'baseline' or 'ddp'")

    # ---------------------------------------------------------------------
    # 2. Derive local time features (fully vectorised)
    # ---------------------------------------------------------------------
    ts = df["local_timestamp"]
    
    # If stored as object dtype, reconstruct as proper datetime series
    if ts.dtype == 'object':
        # Get the first non-null value to determine timezone
        sample = ts.dropna().iloc[0] if len(ts.dropna()) > 0 else None
        if sample is not None and hasattr(sample, 'tz') and sample.tz is not None:
            # Extract timezone from sample
            tz = sample.tz
            # Convert each datetime to UTC timestamp, then reconstruct with timezone
            # This avoids the "tz-aware cannot be converted" error
            utc_timestamps = ts.apply(lambda x: x.timestamp() if pd.notna(x) and hasattr(x, 'timestamp') else pd.NaT)
            df["local_timestamp"] = pd.to_datetime(utc_timestamps, unit='s', utc=True).dt.tz_convert(tz)
        else:
            # No timezone info, simple conversion
            df["local_timestamp"] = pd.to_datetime(ts)
        ts = df["local_timestamp"]

    iso = ts.dt.isocalendar()  # DataFrame: year, week, day

    df["local_weekday"] = iso["day"].map(WEEKDAY_MAPPER).astype("category")

    # If you don't actually need the string, you can keep (year, week) numeric.
    df["local_week"] = (
        iso["year"].astype("uint16").astype("string")
        + "-"
        + iso["week"].astype("uint8").astype("string")
    )

    df["local_hour"] = ts.dt.hour.astype("uint8")

    # day segment via vectorised ranges, no Python helper needed
    hours = df["local_hour"].to_numpy()

    day_segment = np_select(
        [
            hours < 6,
            hours < 12,
            hours < 18,
        ],
        [
            "night",
            "morning",
            "afternoon",
        ],
        default="evening",
    )

    df["local_day_segment"] = Categorical(
        day_segment,
        categories=["night", "morning", "afternoon", "evening"],
        ordered=True,
    )

    # use normalized datetime instead of Python date objects (cheaper at 5M rows)
    df["local_date"] = ts.map(lambda x:x.date())
    df["local_date_str"] = df["local_date"].astype(str)

    return df










def remove_link_events_with_corrupt_links(some_events_df):
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









def load_scrape_metadata(consolidate=False, verbose=False):
    # load the scraped metadata dataframe

    import shutil
    from os import listdir
    from os.path import join, basename
    from pandas import concat, read_pickle
    from datetime import datetime


    # load the scrape_metadata dataframe
    print("Loading scraped metadata")

    scrape_metadata_filenames = [join(fyp.cf["paths"]["scrape"],gg) for gg in listdir(fyp.cf["paths"]["scrape"]) if gg.startswith("scrape_metadata")]

    scrape_metadata = pd.concat([pd.read_pickle(fn) for fn in scrape_metadata_filenames])
    if verbose:
        print(f"Shape of the scrape DF: {scrape_metadata.shape}")

    # deduplicate based on item_id but if there are both a true and a false video_downloaded status, keep both
    scrape_metadata = scrape_metadata.drop_duplicates(subset=["item_id","video_downloaded"]).copy()
    if verbose:
        print(f"Dropping duplicates based on items and whether the video is downloaded or not: {scrape_metadata.shape}")

    # identify items with inconsistent video_downloaded status
    items_w_inconsistent_video_download_status = scrape_metadata["item_id"].value_counts()
    items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status>1].index.tolist()

    # use the list generated above to separate items with consistent vs inconsistent video download status
    items_w_consistent_video_download_status = scrape_metadata[~scrape_metadata['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    items_w_inconsistent_video_download_status = scrape_metadata[scrape_metadata['item_id'].isin(items_w_inconsistent_video_download_status)].copy()
    if verbose:
        print(f"Identifying conflicting items in the dataset listed twice - once as video_downloaded and once as not")
        print(
            f"There are {len(items_w_inconsistent_video_download_status):,} items with such inconsistencies, "
            f"and {len(items_w_consistent_video_download_status):,} that look alright.")

    if len(items_w_inconsistent_video_download_status)>0:
        # for items with inconsistent video download status, only keep the ones where video_downloaded is True
        items_w_inconsistent_video_download_status = items_w_inconsistent_video_download_status[items_w_inconsistent_video_download_status['video_downloaded']].copy()
        if verbose:
            print(f"\nFixed the inconsistencies by keeping the one of the pairs with video_download=True")
            print(f"This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_metadata = pd.concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])
        if verbose:
            print(f"After this procedure, the shape of the scrape DF is: {scrape_metadata.shape}")

    if verbose:
        print(
            f"{scrape_metadata['video_downloaded'].value_counts().loc[True]:,} items have downloaded videos and "
            f"{scrape_metadata['video_downloaded'].value_counts().loc[False]:,} don't")
        print("--"*60)


    # fixing up some minor issues with the columns

    # set item_id as index
    scrape_metadata.set_index('item_id', inplace=True)

    # remove do_not_modify column if it exists
    scrape_metadata.drop(["do_not_modify"], axis=1, errors='ignore', inplace=True)

    # fill NaN values in image_list with empty strings
    scrape_metadata['image_list'] = scrape_metadata['image_list'].fillna("")

    # for items with non-empty image_list, set video_duration based on number of images * 2 seconds
    scrape_metadata.loc[scrape_metadata[scrape_metadata['image_list']!=""].index,'video_duration'] = scrape_metadata.loc[scrape_metadata[scrape_metadata['image_list']!=""].index,'image_list'].map(lambda x: len(x.split(' | ')) * 2)

    # video duration is never zero - set zero durations to -1
    scrape_metadata.loc[scrape_metadata[(scrape_metadata['video_duration']==0)].index,'video_duration'] = -1

    # move the item_id back from the index to a column
    scrape_metadata.reset_index(inplace=True)


    if consolidate and len(scrape_metadata_filenames) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"The scrape_metadata files will be consolidated into a single file: scrape_metadata_{fine_ts}.pkl.")

        scrape_metadata.to_pickle(join(fyp.cf['paths']['scrape'],f"scrape_metadata_{fine_ts}.pkl"))

        for fn in scrape_metadata_filenames:
            shutil.move(fn,join(fyp.cf['paths']['scrape'],'archive',basename(fn)))
            if verbose:
                print(f"Moved {basename(fn)} to archive")



    print(f"Loaded scraped metadata - shape {scrape_metadata.shape}")
    print("--"*60)
    
    return {"data_scraped":scrape_metadata}







def load_failed_scrapes(consolidate = False, verbose = False, super_verbose = False):
    # Load list of failed scraped attempts.

    from os import listdir
    from os.path import join, basename
    from json import load as json_load, dump
    from datetime import datetime
    from shutil import move

    failed_scrape_fn_core = "scrape_failed_items"

    failed_scrape_files = [join(fyp.cf["paths"]["scrape"],gg) for gg in listdir(fyp.cf["paths"]["scrape"]) if gg.startswith(failed_scrape_fn_core)]

    failed_scrapes = []
    for fn in failed_scrape_files:
        if super_verbose:
            print(fn)
        with open(fn, 'r') as file:
            failed_scrapes += json_load(file)
    failed_scrapes = set(map(lambda x:int(x), failed_scrapes))

    if consolidate and len(failed_scrape_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"{len(failed_scrapes):,} of these are unique and will be saved as a new consolidated file {failed_scrape_fn_core}_{fine_ts}.json.")

        with open(join(fyp.cf['paths']['scrape'],f"{failed_scrape_fn_core}_{fine_ts}.json"), "w") as jf:
            dump(list(failed_scrapes), jf)

        for fn in failed_scrape_files:
            move(fn,join(fyp.cf['paths']['scrape'],'archive',basename(fn)))
            if verbose:
                print(f"Moved {basename(fn)} to archive")
        if verbose:
            print("--"*60)


    if verbose:
        print(f"Loaded list of ALL failed scrapes: {len(failed_scrapes):,}")
        print("--"*60)

    return failed_scrapes









def load_zeeschuimer_data(study_name, use_half_baked = False, verbose=False):
    # load items from baseline logs

    from pandas import concat, read_pickle
    from os.path import exists, join
    from os import remove

    USE_HALF_BAKED_FILES = use_half_baked#fyp.cf["study_defs"][study_name]["USE_HALF_BAKED_FILES"]

    half_baked_baseline_path = join(fyp.cf['paths']['exports'],f"{study_name}_HALF_BAKED_BASELINE.pkl")

    if not USE_HALF_BAKED_FILES and exists(half_baked_baseline_path):
        remove(half_baked_baseline_path)
        if verbose:
            print("Deleted half-baked baseline events file.")


    if USE_HALF_BAKED_FILES and exists(half_baked_baseline_path):
        print("Loading half-baked baseline events from pickle...", end=" ", flush=True)
        baseline_log = read_pickle(half_baked_baseline_path)
        print(f"Shape: {baseline_log.shape}")
    else:

        BASELINE_START_DATE = fyp.cf["study_defs"][study_name]["BASELINE_START_DATE"]
        BASELINE_END_DATE = fyp.cf["study_defs"][study_name]["BASELINE_END_DATE"]

        print("Loading baseline logs...")

        from os import listdir
        from json import load as json_load
        from zoneinfo import ZoneInfo


        list_of_zeeschuimer_logs = []
        okay_test_cases = []
        for fn in listdir(fyp.cf["paths"]["zeeschuimer_refined"]):
            if fn.endswith(".pkl"):
                zeeschuimer_candidate = read_pickle(join(fyp.cf["paths"]["zeeschuimer_refined"],fn))
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

        baseline_log = baseline_log[(baseline_log.timestamp_collected>=BASELINE_START_DATE) & (baseline_log.timestamp_collected<=BASELINE_END_DATE)].copy()
        if verbose:
            print("Baseline log date range:",baseline_log.timestamp_collected.min(), " ---- ", baseline_log.timestamp_collected.max())
        
        baseline_log.reset_index(drop=True, inplace=True)

        baseline_log = extract_local_time_features(study_name, baseline_log, kind_of_log='baseline', verbose=verbose)

        if USE_HALF_BAKED_FILES:
            if verbose:
                print("Saving half-baked baseline events to pickle...")    
            baseline_log.to_pickle(half_baked_baseline_path)

    print(f"Baseline data contains {baseline_log.shape[0]:,} rows")
    print("--"*60)
    return {"data_baseline_log":baseline_log}









def sample_ddp_events(study_name, all_ddp_events_df, verbose=False):


    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.
    DONATION_DATE_GROUP_VARIABLES = fyp.cf["study_defs"][study_name]["DONATION_DATE_GROUP_VARIABLES"]
    t1 = []
    for c in DONATION_DATE_GROUP_VARIABLES:
        if c[:2] in ["D_","T_","S_","B_"]:
            t1 += [c[2:]]
        else:
            t1 += [c]
    DONATION_DATE_GROUP_VARIABLES = t1

    DONATION_DATE_GROUP_PERCENTILE_LIMITS = fyp.cf["study_defs"][study_name]["DONATION_DATE_GROUP_PERCENTILE_LIMITS"]
    MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION = fyp.cf["study_defs"][study_name]["MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION"]
    N_SAMPLED_DATES_FROM_EACH_DONATION = fyp.cf["study_defs"][study_name]["N_SAMPLED_DATES_FROM_EACH_DONATION"]
    N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP = fyp.cf["study_defs"][study_name]["N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP"]

    if verbose:
        print("Sampling events based on donation-date groups, which is the unit of analysis for the study")

    # count the number of events in the donation-date groups
    donation_date_groups = all_ddp_events_df[all_ddp_events_df['feature_name']=="watch"].groupby(DONATION_DATE_GROUP_VARIABLES)["sample_id"].count()


    # this is transforming the donation-date group percentile limits to actual values
    donation_date_group_size_limits = donation_date_groups.describe(percentiles=DONATION_DATE_GROUP_PERCENTILE_LIMITS).loc[[f"{k:.0%}" for k in DONATION_DATE_GROUP_PERCENTILE_LIMITS]].values
    percentile_str = "-".join([f"{k:.0%}" for k in DONATION_DATE_GROUP_PERCENTILE_LIMITS])
    limits_str = "-".join([f"{k:,.0f}" for k in donation_date_group_size_limits])
    if verbose:
        print(f"Percentile limits {percentile_str} translate to {limits_str} in actual event counts")


    # apply the size limits to the donation-date groups to get those that fit the criteria
    donation_date_groups_within_size_limits = donation_date_groups[(donation_date_groups>=donation_date_group_size_limits[0]) & (donation_date_groups<donation_date_group_size_limits[1])]
    if verbose:
        print(f"There are {len(donation_date_groups_within_size_limits):,} donation-date groups with event counts within the limits")


    # for each donation, count how many dates have event counts within the limits
    n_tiktok_dates_within_limits_per_donation = (~donation_date_groups_within_size_limits.unstack(level=0).isna()).sum()

    # I want donations who have a considerable number of dates within this range.
    donations_with_many_dates_within_limits = n_tiktok_dates_within_limits_per_donation[n_tiktok_dates_within_limits_per_donation>=MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION]
    if verbose:
        print(f"There are {len(donations_with_many_dates_within_limits):,} donations with at least {MIN_TIKTOK_DATES_WITHIN_LIMITS_PER_DONATION} dates where the number of events is within the limits")


    # use these identified donations to identify the donation-date groups that meet the events per date criteria
    donation_date_groups_by_regulars = donation_date_groups_within_size_limits.unstack(1).loc[donations_with_many_dates_within_limits.index,:].stack()
    if verbose:
        print(f"These donations yield {len(donation_date_groups_by_regulars):,} donation-date groups meeting the criteria")


    # Sample step 1: sample a certain number of dates from each donation

    # I'm first shuffling the dates for each donation (pseudo-randomly for replicability)
    ordered_groups = (
        donation_date_groups_by_regulars.groupby(level=0, group_keys=False)
          .apply(lambda g: g.sample(frac=1, replace=False, random_state=42))
    )

    # then I pick the top 'N_SAMPLED_DATES_FROM_EACH_DONATION' dates from each donation
    # this ensures that I keep the elements selected when 'N_SAMPLED_DATES_FROM_EACH_DONATION' is small,
    # also when I pick a higher 'N_SAMPLED_DATES_FROM_EACH_DONATION' value
    # It's expensive to scrape and annotate videos, so I don't want to start from scratch
    # just because I increased the sample size
    sampled_donation_date_groups_by_regulars = ordered_groups.groupby(level=0).head(N_SAMPLED_DATES_FROM_EACH_DONATION)
    del ordered_groups # clean up

    if verbose:
        print(f"Sample step 1: Sampled {N_SAMPLED_DATES_FROM_EACH_DONATION} dates from each donation, giving {len(sampled_donation_date_groups_by_regulars):,} donation-date groups")

    # get the watch events in these sampled donation-date groups (nonb-watch events are just cream on top)
    ddp_events_in_sampled_groups = all_ddp_events_df[all_ddp_events_df['feature_name']=="watch"].set_index(DONATION_DATE_GROUP_VARIABLES).loc[sampled_donation_date_groups_by_regulars.index].reset_index()
    if verbose:
        print(f"There are {len(ddp_events_in_sampled_groups):,} events in these {len(sampled_donation_date_groups_by_regulars):,} donation-date groups")


    # Sample step 2: sample a certain number of events from each donation-date group

    # I'm first shuffling the events for each donation-date group pseudo-randomly
    ordered_events_in_groups = (
        ddp_events_in_sampled_groups.groupby(DONATION_DATE_GROUP_VARIABLES)
          .apply(lambda g: g.sample(frac=1, replace=False, random_state=42), include_groups=False)
    )
    del ddp_events_in_sampled_groups # clean up

    # then I pick the top 'N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP' events from each donation-date group
    # this ensures that I keep the elements selected when 'N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP' is small, 
    # also when I pick a higher 'N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP' value
    # It's expensive to scrape and annotate videos, so I don't want to start from scratch
    # just because I increased the sample size
    sampled_ddp_events_in_sampled_donation_date_groups = ordered_events_in_groups.groupby(DONATION_DATE_GROUP_VARIABLES).head(N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP)
    del ordered_events_in_groups # clean up

    # push the grouping variables back from index into columns
    sampled_ddp_events_in_sampled_donation_date_groups.reset_index(level=[0,1], inplace=True)

    print(f"Sampled {N_SAMPLED_EVENTS_FROM_EACH_DONATION_DATE_GROUP} events from each donation-date group, yielding {len(sampled_ddp_events_in_sampled_donation_date_groups):,} events")


    # check some stats of the sampling procedure
    #print(sampled_ddp_events_in_sampled_donation_date_groups[sampled_ddp_events_in_sampled_donation_date_groups['feature_name']=="watch"].groupby(DONATION_DATE_GROUP_VARIABLES)["sample_id"].count().describe())
    
    return sampled_ddp_events_in_sampled_donation_date_groups






def load_ddp_events(study_name, use_half_baked = False, verbose=False):
    # load DF with all donations previously ingested

    from os import listdir, remove
    from os.path import join
    from os.path import exists
    from json import load as json_load
    from pandas import concat, read_pickle


    USE_HALF_BAKED_FILES = use_half_baked#fyp.cf["study_defs"][study_name]["USE_HALF_BAKED_FILES"]
    

    half_baked_ddp_events_path = join(fyp.cf['paths']['exports'],f"{study_name}_HALF_BAKED_ALL_DDP.pkl")
    half_baked_sampled_ddp_events_path = join(fyp.cf['paths']['exports'],f"{study_name}_HALF_BAKED_SAMPLED_DDP.pkl")


    if not USE_HALF_BAKED_FILES and exists(half_baked_ddp_events_path):
        remove(half_baked_ddp_events_path)
        remove(half_baked_sampled_ddp_events_path)
        if verbose:
            print("Deleted half-baked DDP events file and sampled DDP events file.")


    if USE_HALF_BAKED_FILES and exists(half_baked_ddp_events_path):
        print("Loading half-baked DDP events from pickle...", end=" ", flush=True)
        all_ddp_events_df = read_pickle(half_baked_ddp_events_path)
        print(f"New shape: {all_ddp_events_df.shape}")
    else:

        DDP_START_DATE = fyp.cf["study_defs"][study_name]["DDP_START_DATE"]
        DDP_END_DATE = fyp.cf["study_defs"][study_name]["DDP_END_DATE"]

        print("Loading all DDP events...", end=" ", flush=True)
        all_ddp_events_df = read_pickle(join(fyp.cf["paths"]["ddp_main"], "all_participant_events.pkl"))

        # drop two columns
        all_ddp_events_df = all_ddp_events_df.drop(["value_list","variable_list"], axis=1).copy()

        # Vectorized date extraction
        all_ddp_events_df['simple_date'] = all_ddp_events_df['date'].dt.date
        
        # Vectorized sample_id extraction using string operations
        all_ddp_events_df["sample_id"] = all_ddp_events_df.ts_jiggled.astype(str).str[-4:].astype(int)
        
        print(f"...DDP events dataframe loaded")
        print(f"The DF contains {all_ddp_events_df.donation_id.nunique()} unique donations and a total of {all_ddp_events_df.shape[0]:,} logged events.")

        if verbose:
            print(f"The DDP events range from {all_ddp_events_df.date.min()} -- {all_ddp_events_df.date.max()}")
        mask = (all_ddp_events_df["date"] >= DDP_START_DATE) & (all_ddp_events_df["date"] <= DDP_END_DATE)
        all_ddp_events_df = all_ddp_events_df.loc[mask].copy()
        if verbose:
            print(f"Keeping DDP events within date range {all_ddp_events_df.date.min()} -- {all_ddp_events_df.date.max()} yielding {len(all_ddp_events_df):,} events")

        # dropping some corrupt URLs simply by calculating the most common length of the URLs and dropping those that doesn't match
        all_ddp_events_df = remove_link_events_with_corrupt_links(all_ddp_events_df)
        if verbose:
            print(f"Dropping DDP events with corrupt TikTok URLs. New shape: {all_ddp_events_df.shape}")

        all_ddp_events_df = extract_local_time_features(study_name, all_ddp_events_df, kind_of_log='ddp', verbose=verbose)

        if USE_HALF_BAKED_FILES:
            if verbose:
                print("Saving half-baked DDP events to pickle...")    
            all_ddp_events_df.to_pickle(half_baked_ddp_events_path)

        
    if USE_HALF_BAKED_FILES and exists(half_baked_sampled_ddp_events_path):
        print("Loading half-baked sampled DDP events from pickle...", end=" ", flush=True)
        sampled_data_ddp_events = read_pickle(half_baked_sampled_ddp_events_path)
        print(f"Shape: {sampled_data_ddp_events.shape}")
    else:
        sampled_data_ddp_events = sample_ddp_events(study_name, all_ddp_events_df, verbose=verbose)

        if USE_HALF_BAKED_FILES:
            if verbose:
                print("Saving half-baked sampled DDP events to pickle...")    
            sampled_data_ddp_events.to_pickle(half_baked_sampled_ddp_events_path)


    print("--"*60)
    return {"sampled_data_ddp_events":sampled_data_ddp_events, "all_data_ddp_events":all_ddp_events_df }






def load_special_donations(study_name, verbose=False):
    # sometimes it is useful to select events in a specific donation.

    from pandas import concat, read_pickle, DataFrame
    from os.path import join

    DDP_START_DATE = fyp.cf["study_defs"][study_name]["DDP_START_DATE"]
    DDP_END_DATE = fyp.cf["study_defs"][study_name]["DDP_END_DATE"]
    the_special_donations = fyp.cf["study_defs"][study_name]["SPECIAL_DONATIONS"]

    if len(the_special_donations) == 0:
        if verbose:
            print("Skipping special DDP events loading as the number of SPECIAL_DONATIONS is zero.")
        return {"data_special_ddps":DataFrame()}
    
    donations_str = '\n - '.join(the_special_donations)
    print(f"Loading special DDP events from {donations_str}")

    # Loading all DDP events...
    all_ddp_events_df = read_pickle(join(fyp.cf["paths"]["ddp_main"], "all_participant_events.pkl"))

    # drop two columns
    all_ddp_events_df = all_ddp_events_df.drop(["value_list","variable_list"], axis=1).copy()

    # Vectorized date and sample_id extraction
    all_ddp_events_df['simple_date'] = all_ddp_events_df['date'].dt.date
    all_ddp_events_df["sample_id"] = all_ddp_events_df.ts_jiggled.astype(str).str[-4:].astype(int)

    special_ddp_events_df = all_ddp_events_df[all_ddp_events_df["donation_id"].isin(the_special_donations)].copy()
    if verbose:
        print(f"Special DDP events dataframe loaded: {special_ddp_events_df.donation_id.nunique()} unique donations. Shape: {special_ddp_events_df.shape}")
        print(f"The special DDP events range from {special_ddp_events_df.date.min()} -- {special_ddp_events_df.date.max()}")

    mask = (special_ddp_events_df["date"] >= DDP_START_DATE) & (special_ddp_events_df["date"] <= DDP_END_DATE)
    special_ddp_events_df = special_ddp_events_df.loc[mask].copy()
    if verbose:
        print(f"Keeping special DDP events within date range {special_ddp_events_df.date.min()} -- {special_ddp_events_df.date.max()}: {special_ddp_events_df.shape}")


    # dropping some corrupt URLs simply by calculating the most common length of the URLs and dropping those that doesn't match
    special_ddp_events_df = remove_link_events_with_corrupt_links(special_ddp_events_df)
    if verbose:
        print(f"Dropping special DDP events with corrupt TikTok URLs. New shape: {special_ddp_events_df.shape}")

    if verbose:
        print("Processing DDP events to extract local time features...")
    special_ddp_events_df = extract_local_time_features(study_name, special_ddp_events_df, kind_of_log='ddp', verbose=verbose)

    if verbose:
        print("--"*60)
    return {"data_special_ddps":special_ddp_events_df}






def load_datasets(
    study_name,
    use_half_baked = False,
    delete_all_half_baked_files = False,
    consolidate = False,
    verbose=False):

    from fyp.machine_annotation import load_machine_annotations

    from os import remove, listdir
    from os.path import join

    print("Loading all datasets:")
    tutti = {}

    if delete_all_half_baked_files:
        print(" - Deleting half-baked files")
        export_path = fyp.cf["paths"]["exports"]
        for half_baked_file in listdir(export_path):
            if "HALF_BAKED" in half_baked_file:
                path_to_it = join(export_path, half_baked_file)
                remove(path_to_it)
                if verbose:
                    print(f"   - Deleted half-baked file: .../{'/'.join(path_to_it.split('/')[-3:])}")

    if not use_half_baked:
        print(" - Generating fresh datasets - won't be saving half-baked files")
    elif delete_all_half_baked_files:
        print(" - Saving new half-baked files")
    else:
        print(" - Loading existing half-baked files")
    print("=="*60)


    tutti.update(load_zeeschuimer_data(study_name, use_half_baked = use_half_baked, verbose=verbose))
    tutti.update(load_ddp_events(study_name, use_half_baked = use_half_baked, verbose=verbose))
    tutti.update(load_special_donations(study_name, verbose=verbose))

    tutti.update(load_scrape_metadata(consolidate=consolidate, verbose=verbose))
    tutti["data_annotated"] = load_machine_annotations(include_failed_calls=False, consolidate=consolidate, verbose = verbose)


    #for k in sorted(list(tutti.keys())):
    #    print(k , type(tutti[k]), len(tutti[k]))

    return tutti





def identify_unique_videos(study_name, stuff, verbose = False):


    # combine the special DDP events with the sampled DDP events
    from pandas import concat, read_pickle, DataFrame, NamedAgg, NA

    MIN_NUNIQUE_USERS = fyp.cf["study_defs"][study_name]["MIN_NUNIQUE_USERS"]


    ddp_events_for_unique_videos_df = DataFrame(columns=stuff["sampled_data_ddp_events"].columns)

    if len(stuff["sampled_data_ddp_events"]) + len(stuff["data_special_ddps"]) > 0:

        dataframes_to_combine = [k for k in [stuff["sampled_data_ddp_events"], stuff["data_special_ddps"]] if len(k) > 0]
        ddp_events_for_unique_videos_df = concat(dataframes_to_combine, ignore_index=True).drop_duplicates()
        if verbose:
            print(f"Shape of the combined (sampled + special) DDP events DF for exporting list of unique videos: {ddp_events_for_unique_videos_df.shape}")
            print(f"The combined DDP events range from {ddp_events_for_unique_videos_df.date.min()} -- {ddp_events_for_unique_videos_df.date.max()}")
    else:
        if verbose:
            print("No DDP events to combine, creating an empty dataframe.")

    if verbose:
        print("--"*60)

    ### Generate a DF w unique videos from DDPs
    unique_ddp_videos = DataFrame()
    video_ddp_events_df = DataFrame()

    if len(ddp_events_for_unique_videos_df) > 0:

        video_ddp_events_df = ddp_events_for_unique_videos_df[ddp_events_for_unique_videos_df["primary_label"] == "link"].copy()

        if verbose:
            print(
                f"Selecting events that involves a video, dropping other event types. {video_ddp_events_df.donation_id.nunique()} "
                f"unique donations. Shape: {video_ddp_events_df.shape}"
            )

        # group by video URL and count the number of unique users
        ddp_video_stats = video_ddp_events_df.groupby('primary_value').agg(
            nunique_users = NamedAgg(column="donation_id", aggfunc="nunique"),
            total_views = NamedAgg(column="primary_value", aggfunc="count"),).sort_values("nunique_users", ascending=False)
        if verbose:
            print(f"Unique videos in the DDP logs: {len(ddp_video_stats):,}")

        unique_ddp_videos = ddp_video_stats[ddp_video_stats.nunique_users >= MIN_NUNIQUE_USERS].copy()
        if verbose:
            print(f"Keeping unique DDP videos that have been watched/liked/etc by at least {MIN_NUNIQUE_USERS} unique users. Shape: {unique_ddp_videos.shape}")

        # extracting item ids from the URLs (in the index) - vectorized
        unique_ddp_videos["item_id"] = unique_ddp_videos.index.str.split("/").str[-2].astype(int)
    else:
        if verbose:
            print("No events in the combined DDP dataframe")

    if verbose:
        print("--"*60)

    ### identify unique videos in baseline logs
    unique_baseline_videos = DataFrame(columns=["item_id", "nunique_users", "total_views", "primary_value"])

    if len(stuff["data_baseline_log"])>0:

        unique_item_id_list = list(int(k) for k in stuff["data_baseline_log"].item_id.unique())
        unique_baseline_videos = DataFrame()
        unique_baseline_videos['item_id'] = unique_item_id_list
        unique_baseline_videos['nunique_users'] = NA
        unique_baseline_videos['total_views'] = NA
        # Vectorized URL construction
        unique_baseline_videos['primary_value'] = "https://www.tiktokv.com/share/video/" + unique_baseline_videos['item_id'].astype(str) + "/"

    unique_baseline_videos.set_index('primary_value', inplace=True)

    if verbose:
        print(f"Unique videos identified in the baseline logs: {len(list(set(unique_baseline_videos.index.tolist()))):,}")
        print("--"*60)


    ### combine unique donation videos with unique baseline videos
    dataframes_to_combine = [k for k in [unique_baseline_videos, unique_ddp_videos] if len(k) > 0]
    video_observation_stats = concat(dataframes_to_combine, ignore_index=True).drop_duplicates(subset='item_id', keep='last')
    if verbose:
        print(f"Combining unique videos from data donation events with videos from baseline data into a DF with the shape: {video_observation_stats.shape}")
        print("--"*60)

    return video_observation_stats






def calculate_all_unique_video_subsets(study_name, stuff, verbose=False):
    ### Check the unique videos against scraped metadata, machine results and such things

    from fyp.machine_annotation import load_machine_annotations


    # load failed_scrapes as a set
    failed_scrapes = load_failed_scrapes(verbose=verbose, consolidate = True)

    # load 
    machine_annotated_videos = set([int(k) for k in stuff["data_annotated"].item_id.tolist()])

    failed_annotations = set(load_machine_annotations(
        include_failed_calls=True,
        verbose = verbose,
        completely_quiet=True
        ).item_id.tolist())
    failed_annotations = failed_annotations - machine_annotated_videos


    completed_downloads = set([int(k) for k in stuff["data_scraped"][stuff["data_scraped"]["video_downloaded"]].item_id.to_list()])
    missing_downloads = set([int(k) for k in stuff["data_scraped"][~stuff["data_scraped"]["video_downloaded"]].item_id.to_list()])

    unique_videos_with_stats = identify_unique_videos(study_name, stuff, verbose=verbose)
    all_unique_videos = set([int(k) for k in unique_videos_with_stats.item_id.to_list()])

    failed_annotations = failed_annotations & all_unique_videos

    unseen_videos = all_unique_videos - (completed_downloads | missing_downloads | failed_scrapes)
    completed_downloads = all_unique_videos & completed_downloads
    machine_annotated_videos = all_unique_videos & machine_annotated_videos
    downloaded_and_annotated = completed_downloads & machine_annotated_videos
    downloaded_not_annotated = completed_downloads - machine_annotated_videos - failed_annotations
    missing_downloads = all_unique_videos & missing_downloads
    failed_scrapes = all_unique_videos & failed_scrapes - completed_downloads - missing_downloads


    print(f"Videos in the selected logs: {len(all_unique_videos):,} videos")
    print(f"    Downloaded and annotated: {len(downloaded_and_annotated):,} videos")
    print(f"    Downloaded but not annotated: {len(downloaded_not_annotated):,} videos")
    print(f"    Failed annotations: {len(failed_annotations):,} videos")
    print(f"    Metadata found but not downloaded: {len(missing_downloads):,} videos")
    print(f"    Failed scrapes: {len(failed_scrapes):,} videos")
    print(f"    Unseen videos: {len(unseen_videos):,} videos")
    print(f"Sum of the set sizes: {len(unseen_videos) + len(downloaded_and_annotated) + len(downloaded_not_annotated) + len(missing_downloads) + len(failed_annotations) + len(failed_scrapes):,}")
    print("--"*60)

    return {
        'downloaded_and_annotated': downloaded_and_annotated,
        'downloaded_not_annotated': downloaded_not_annotated,
        'failed_annotations': failed_annotations,
        'missing_downloads': missing_downloads,
        'failed_scrapes': failed_scrapes,
        'unseen_videos': unseen_videos,
        'completed_downloads': completed_downloads,
        'machine_annotated_videos': machine_annotated_videos,
    }








def save_selected_unique_video_subsets(
    study_name,
    stuff,
    subsets,
    file_label = "",
    INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
    INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
    INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = False,
    INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
    INCLUDE_LONG_VIDEOS_IN_EXPORT = False,
    verbose=False
):

    from os.path import join
    from datetime import datetime
    from pandas import DataFrame

    if verbose:
        print("The user's selection of available subsets of the videos gives the following total set:")
    work_with_these_videos = set()
    if INCLUDE_UNSEEN_VIDEOS_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["unseen_videos"]
        if verbose:
            print(f"- UNSEEN_VIDEOS selected --> added {len(work_with_these_videos):,} videos")
    if INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["downloaded_and_annotated"]
        if verbose:
            print(f"- DOWNLOADED_AND_ANNOTATED selected --> added {len(work_with_these_videos):,} videos")
    if INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["downloaded_not_annotated"]
        if verbose:
            print(f"- DOWNLOADED_BUT_NOT_ANNOTATED selected --> added {len(work_with_these_videos):,} videos")
    if INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["missing_downloads"]
        if verbose:
            print(f"- SCRAPED_BUT_NOT_DOWNLOADED selected --> added {len(work_with_these_videos):,} videos")
    if INCLUDE_FAILED_SCRAPES_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["failed_scrapes"]
        if verbose:
            print(f"- FAILED_SCRAPES selected --> added {len(work_with_these_videos):,} videos")
    if INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["failed_annotations"]
        if verbose:
            print(f"- FAILED_ANNOTATIONS selected --> added {len(work_with_these_videos):,} videos")
    if verbose:
        print("- "*40)

    if len(work_with_these_videos) == 0:
        if verbose:
            print("No videos selected for export")
            print("--"*60)
        return work_with_these_videos

    if verbose:
        print(f"Unique videos selected (regardless of their duration): {len(work_with_these_videos):,}")

    if INCLUDE_LONG_VIDEOS_IN_EXPORT:
        if verbose:
            print("Keeping videos regardless of their duration")
    else:
        if verbose:
            print(f"Only keeping videos that are shorter than {fyp.cf['machine']['max_duration_for_annotation']} seconds")
        short_videos = set(stuff["data_scraped"][stuff["data_scraped"]["video_duration"]<fyp.cf["machine"]["max_duration_for_annotation"]].item_id.tolist())
        work_with_these_videos = work_with_these_videos & short_videos
        
    if verbose:
        print(f"This data selection policy yielded {len(work_with_these_videos):,} unique videos")
        print("--"*60)


    ### save the unique item_ids (videos) w basic stats to a file
    if len(work_with_these_videos) > 0:
        if len(file_label)>0 and file_label[-1] != "_":
            file_label += "_"
        unique_videos_filename = f"{study_name}_{file_label}UNIQUE.pkl"

        unique_videos_with_stats = identify_unique_videos(study_name, stuff, verbose=False)
        all_unique_videos_to_save = unique_videos_with_stats[unique_videos_with_stats.item_id.isin(work_with_these_videos)].copy()

        export_sub_folder_name = fyp.cf["paths"]["exports"].replace(fyp.cf["paths"]["main"],"")

        all_unique_videos_to_save.to_pickle(join(fyp.cf['paths']['exports'],unique_videos_filename))
        if verbose:
            print(f"Exported {len(all_unique_videos_to_save):,} unique videos to {join(export_sub_folder_name,unique_videos_filename)}")
            print(f"Now: {datetime.now()}")
            print("--"*60)
        return all_unique_videos_to_save
    else:
        if verbose:
            print("Not exporting unique videos as no videos were selected.")
        return DataFrame()











def _rename_columns(some_events):
    some_eventsC = some_events.copy()

    fixer_upper = [
        ("B_local_","T_local_"),
        ("D_local_","T_local_"),
        (".","_"),
        ("data_",""),
        ("source_url_","source_"),
        ("_collected",""),
        ("framing_analysis_","FA_"),
        ("cultural_representation_analysis_","CRA_"),
        ("ideological_analysis_","IA_"),

        ]

    for fu in fixer_upper:
        mapper = {c:c.replace(fu[0],fu[1]) for c in some_eventsC.columns if (c != c.replace(fu[0],fu[1])) and (not c.replace(fu[0],fu[1]) in some_eventsC.columns)}
        some_eventsC = some_eventsC.rename(columns=mapper).copy()
    
    return some_eventsC






def _check_for_null_values_in_df(some_df_C, verbose=False):
    some_df = some_df_C.copy()
    nullis = some_df.isna().sum()
    allok = True
    for n in nullis.index:
        if nullis[n] != 0:
            if allok:
                if verbose:
                    print("Making sure that there are no null values anywhere in the DF")
            if verbose:
                print(n, some_df[n].dtype)
            allok = False
    if not allok:
        if verbose:
            print("--"*60)
    
    return some_df





def process_baseline_for_log_export(stuff, session_id_counter = np_int64(0), verbose=False):

    from pandas import concat

    baseline_log = stuff["data_baseline_log"]
    
    if len(baseline_log) > 0:
        baseline_log_simple = baseline_log.rename(columns={c:"B_"+c if not c=="item_id" else c for c in baseline_log.columns}).copy()
        if verbose:
            print(f"The baseline log has shape: {baseline_log_simple.shape}")
    else:
        if verbose:
            print("No baseline log data available or log type is not 'baseline' --> skipping baseline log processing.")
    if verbose:
        print("--"*60)

    # attach session stats to baseline log

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
            print("no baseline data")

    if verbose:
        print("--"*60)


    relevant_baseline_cols = [
            'item_id',
            'T_local_timestamp', 'T_local_weekday', 'T_local_week',
            'T_local_hour', 'T_local_day_segment', 'T_local_date',
            'session_id', 'event_order_in_session',
            'event_pos_in_session',

            'B_challenges', 'B_anchors', 'B_effectStickers',
            'B_source_app_language', 'B_source_browser_language',
            'B_source_language', 'B_source_region',
            'B_source_tz_name', 


        ]


    baseline_log_simple = _rename_columns(baseline_log_simple)
    baseline_log_simple = baseline_log_simple[relevant_baseline_cols].copy()
    
    # Convert categorical columns to string to avoid fillna errors with categoricals
    for col in baseline_log_simple.select_dtypes(include=['category']).columns:
        baseline_log_simple[col] = baseline_log_simple[col].astype(str)
    
    baseline_log_simple = baseline_log_simple.fillna("").copy()
    baseline_log_simple = _check_for_null_values_in_df(baseline_log_simple, verbose=verbose)



    return baseline_log_simple, session_id_counter

  




def add_session_stats_to_ddp_log(ddp_log_in, session_id_counter = np_int64(0), verbose=False):
    # attach session stats to donation events
    ddp_log = ddp_log_in.copy()
    from pandas import isna as pd_isna
    import numpy as np


    all_sessions = []
    if len(ddp_log) and ("D_donation_id" in ddp_log.columns):
        ddp_log['session_id'] = -1
        ddp_log['event_order_in_session'] = -1
        ddp_log['event_pos_in_session'] = -1.0
        
        # OPTIMIZATION: Collect all updates, then apply in bulk at the end
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
            session_nums = session_breaks.cumsum()
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

        # OPTIMIZATION: Apply all updates at once instead of in the loop
        if updates_list:
            all_updates = pd.concat(updates_list)
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

    if verbose:
        print("--"*60)
    return ddp_log, session_id_counter







def process_ddp_log_for_log_export(stuff, session_id_counter = np_int64(0), verbose=False):
    # combine the special DDP events with the all DDP events

    from pandas import DataFrame, concat

    ddp_log = DataFrame(columns=stuff["all_data_ddp_events"].columns)

    if len(stuff["all_data_ddp_events"]) + len(stuff["data_special_ddps"]) > 0:

        dataframes_to_combine = [k for k in [stuff["all_data_ddp_events"], stuff["data_special_ddps"]] if len(k) > 0]
        ddp_log = concat(dataframes_to_combine, ignore_index=True).drop_duplicates()


    
        ddp_log.loc[ddp_log[ddp_log["primary_label"]=="ip"].index,"feature_name"] = "login_event"
        ddp_log.loc[ddp_log[ddp_log["item_id"].isna()].index,"item_id"] = -1
        #ddp_log["item_id"] = ddp_log["item_id"].fillna(np.int64(-1))

        ddp_log["secondary_label"] = ddp_log["secondary_label"].fillna("")
        ddp_log["secondary_value"] = ddp_log["secondary_value"].fillna("")

        ddp_log = ddp_log.drop(columns=[
            "sample_id", "donation_date"], errors="ignore").copy()


        ddp_log = ddp_log.rename(columns={c:"D_"+c if not c in ["item_id"] else c for c in ddp_log.columns}).copy()

        if verbose:
            print(f"Shape of all DDP events DF for exporting full logs: {ddp_log.shape}")
            print(f"The combined DDP events range from {ddp_log.D_date.min()} -- {ddp_log.D_date.max()}")
            print("--"*60)


    else:
        if verbose:
            print("No DDP events to combine, creating an empty dataframe.")
            print("--"*60)
        return ddp_log, session_id_counter


    relevant_ddp_cols = [
            'item_id',
            'T_local_timestamp', 'T_local_weekday', 'T_local_week',
            'T_local_hour', 'T_local_day_segment', 'T_local_date',
            'session_id', 'event_order_in_session',
            'event_pos_in_session',

            'D_donation_id',
            
            'D_feature_name','D_primary_label',
            'D_primary_value',
            'D_secondary_label', 'D_secondary_value',

        ]


    ddp_log, session_id_counter = add_session_stats_to_ddp_log(ddp_log, session_id_counter, verbose=verbose)
    ddp_log = _rename_columns(ddp_log)
    ddp_log = ddp_log[relevant_ddp_cols].copy()

    ddp_log = _check_for_null_values_in_df(ddp_log, verbose=verbose)

    
    return ddp_log, session_id_counter






def process_scrape_metadata_for_log_export(stuff, combined_log, verbose=False):

    from pandas import isna as pd_isna, Timestamp, DataFrame
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if len(combined_log) == 0:
        return DataFrame()

    # polishing the scraped metadata dataset for merging with the log
    if verbose:
        print("Processing scraped metadata for log export...")
    scrape_metadata_log = stuff["data_scraped"][stuff["data_scraped"].item_id.isin(combined_log.item_id.unique())].copy()

    # VECTORIZED: Replace 'nan' string with empty string (faster than map)
    # Only apply to object columns to avoid FutureWarning
    object_cols = scrape_metadata_log.select_dtypes(include=['object']).columns
    scrape_metadata_log[object_cols] = scrape_metadata_log[object_cols].replace('nan', '').infer_objects(copy=False)

    # VECTORIZED: Convert createTime without lambda
    # Use pd.to_datetime for the conversion instead of map(lambda)
    scrape_metadata_log["createTime"] = pd.to_datetime(
        scrape_metadata_log["createTime"], 
        errors='coerce',
        utc=True
    ).fillna(pd.Timestamp(year=2100, month=1, day=1, tz='UTC'))


    scrape_metadata_log.drop(columns=[
        "image_list","video_url","video_downloaded","audio_extracted","cover_downloaded","do_not_modify","last_modified","video_cover"], inplace=True, errors="ignore")


    scrape_metadata_log["video_duration"] = scrape_metadata_log["video_duration"].fillna(0)


    scrape_metadata_log = scrape_metadata_log.rename(columns={c:"S_"+c if not c=="item_id" else c for c in scrape_metadata_log.columns}).copy()
    if verbose:
        print(f"Resulting scraped metadata shape {scrape_metadata_log.shape}")

    if verbose:
        print("--"*60)

    scrape_metadata_log = _check_for_null_values_in_df(scrape_metadata_log, verbose=verbose)

    
    return scrape_metadata_log





def process_machine_annotations_for_log_export(stuff, combined_log, verbose=False):

    from pandas import DataFrame

    if len(combined_log) == 0:
        return DataFrame()

    # polishig the machine results data for merging with the log
    if verbose:
        print("Processing machine annotations for the log export...")
    machine_annotations_for_log = stuff["data_annotated"][stuff["data_annotated"].item_id.isin(combined_log.item_id.unique())].copy()

    machine_annotations_for_log.drop(columns=[
        "inference_ts","inference_duration","model","prompt_fn","error","finish_reason"], inplace=True, errors="ignore")


    machine_annotations_for_log = machine_annotations_for_log.fillna("").copy()

    machine_annotations_for_log = machine_annotations_for_log.rename(columns={c:"G_"+c if not c=="item_id" else c for c in machine_annotations_for_log.columns}).copy()

    if verbose:
        print(f"Resulting machine_annotations_for_log shape {machine_annotations_for_log.shape}")

    if verbose:
        print("--"*60)

    machine_annotations_for_log = _check_for_null_values_in_df(machine_annotations_for_log, verbose=verbose)


    return machine_annotations_for_log





def process_and_combine_logs_for_log_export(study_name, stuff=None, verbose=False):
    

    from pandas import concat, read_pickle
    from os.path import exists, join



    USE_HALF_BAKED_FILES = fyp.cf["study_defs"][study_name]["USE_HALF_BAKED_FILES"]
    half_baked_combined_path = join(fyp.cf['paths']['exports'],f"{study_name}_HALF_BAKED_COMBINED.pkl")


    if USE_HALF_BAKED_FILES and exists(half_baked_combined_path):
        if verbose:
            print("Loading half-baked combined log from pickle...", end=" ", flush=True)
        combined_log = read_pickle(half_baked_combined_path)
        if verbose:
            print(f"Shape: {combined_log.shape}")
    else:

        baseline_log_simple, sesh_counter = process_baseline_for_log_export(stuff, 100, verbose=verbose)
        ddp_log, sesh_counter = process_ddp_log_for_log_export(stuff, session_id_counter = sesh_counter, verbose=verbose)


        combined_log = concat([ddp_log,baseline_log_simple])
        if verbose:
            print(f"Combined log length: {len(combined_log)}")

        ddp_cols = [c for c in combined_log.columns if c.startswith("D_")]
        baseline_cols = [c for c in combined_log.columns if c.startswith("B_")]

        # Convert categorical columns to string to avoid fillna errors
        for col in combined_log.select_dtypes(include=['category']).columns:
            combined_log[col] = combined_log[col].astype(str)

        combined_log[ddp_cols] = combined_log[ddp_cols].fillna("BASELINE")
        combined_log[baseline_cols] = combined_log[baseline_cols].fillna("DDP")

        combined_log = _check_for_null_values_in_df(combined_log, verbose=verbose)

        if USE_HALF_BAKED_FILES:
            if verbose:
                print("Saving half-baked combined log to pickle...")    
            combined_log.to_pickle(half_baked_combined_path)


    return combined_log





def process_enrichment_data_and_merge_with_logs(
    stuff,
    combined_log,
    ONLY_EXPORT_LOG_EVENTS_THAT_ARE_SCRAPED_AND_ANNOTATED = True,
    verbose=False
):

    from pandas import merge

    scrape_metadata_log = process_scrape_metadata_for_log_export(stuff, combined_log, verbose=verbose)
    machine_annotations_for_log = process_machine_annotations_for_log_export(stuff, combined_log, verbose=verbose)

    ### merge log with enriched video metadata and annotations

    if verbose:
        print("Merging combined log with enriched metadata (scraped & annotated)")

    if ONLY_EXPORT_LOG_EVENTS_THAT_ARE_SCRAPED_AND_ANNOTATED:
        if verbose:
            print("Only keeping events in the merged log that have been both scraped and annotated")
        the_how = 'inner'
    else:
        if verbose:
            print("Adding enriched data and keeping log events even if enriched data is missing")
        the_how = 'left'

    outdata = merge(left=combined_log, right=_rename_columns(scrape_metadata_log), on='item_id',how=the_how)
    outdata = merge(left=outdata, right=_rename_columns(machine_annotations_for_log), on='item_id',how=the_how)


    # Create a new column by calculating the difference between 'T_local_timestamp' and 'S_createTime'.
    # Vectorized date difference calculation
    # Ensure both are proper datetime types (not object) before subtraction
    t_timestamp = outdata["T_local_timestamp"]
    s_createtime = outdata["S_createTime"]
    
    # Convert to datetime if they're object dtype
    if t_timestamp.dtype == 'object':
        t_timestamp = pd.to_datetime(t_timestamp, utc=True)
    if s_createtime.dtype == 'object':
        s_createtime = pd.to_datetime(s_createtime, utc=True)
    
    # Now we can subtract them
    outdata["T_days_since_created"] = (t_timestamp - s_createtime).dt.days

    if verbose:
        print(f"Adding 'days_since_created' column. Resulting output log DF shape {outdata.shape}")
        print("--"*60)


    return outdata







def filter_log_against_sampled_donation_groups(
    stuff,
    outdata,
    MAX_DAILY_MISSING_DATA_RATIO = 0.3,
    verbose=False
):

    from pandas import merge, concat

    outdata_filtered = outdata.copy()
    if verbose:
        print(f"Rows at this stage: {len(outdata_filtered):,}")

    # set up a filter to filter out only the DDP events that were in the DDP sample earlier in this notebook
    fine_filter = stuff["sampled_data_ddp_events"].copy()
    fine_filter.rename(columns={"donation_id":"D_donation_id","local_timestamp":"T_local_timestamp"}, inplace=True)
    fine_filter = fine_filter.drop_duplicates(subset=["D_donation_id","T_local_timestamp","item_id"])
    fine_filter = fine_filter.set_index(["D_donation_id","T_local_timestamp","item_id"])
    fine_filter = fine_filter[["local_date"]].fillna("-").copy()

    # use this filter to create a new version of outdata
    outdata_filtered = merge(
        left=fine_filter,
        right=outdata.set_index(["D_donation_id","T_local_timestamp","item_id"]),
        left_index=True, right_index=True, how="inner")
    outdata_filtered = outdata_filtered.reset_index().drop("local_date",axis=1).copy()


    if verbose:
        print(
            f"After matching the export ddp events against the sampled donation-date groups, we have {len(outdata_filtered):,} ddp events in the export log")


    # group the filter and the filtered_outdata to compare how many items were in the sample and how many
    # have been sampled and annotated. 
    check_missing_data = concat([
        fine_filter.reset_index().rename(columns={"local_date":"T_local_date","item_id":"target_count"}).groupby(["D_donation_id","T_local_date"])["target_count"].count(),
        outdata_filtered.rename(columns={"item_id":"real_count"}).groupby(["D_donation_id","T_local_date"])["real_count"].count()
    ], axis=1)

    # calculate a missing data ratio and an index based on a max daily missing data ratio
    check_missing_data["missing_ratio"] = 1 - check_missing_data["real_count"] / check_missing_data["target_count"]
    okay_dates_index = check_missing_data[check_missing_data["missing_ratio"]<MAX_DAILY_MISSING_DATA_RATIO].index

    # use the okay dates to get rid of dates with too much missing data
    outdata_filtered = outdata_filtered.set_index(["D_donation_id","T_local_date"]).loc[okay_dates_index,:].reset_index().copy()

    sampled_ddp_count = len(stuff["sampled_data_ddp_events"])
    if verbose:
        print(
            f"After dropping dates with too high missing data ratio, we have {len(outdata_filtered):,} ddp events in the export log,\n"
            f"which should be compared to {sampled_ddp_count:,} ddp events in the sampled donation-date groups")


    if verbose:
        print("Putting back the baseline data...")
    outdata_filtered = concat([outdata_filtered,outdata[outdata['D_donation_id']=='BASELINE']])

    if verbose:
        print(f"...making the total number of events (BASELINE and DDP) in the export data log to {len(outdata_filtered):,} events.")
        print("--"*60)
    
    return outdata_filtered






def save_logs_as_pkl(
    study_name,
    outdata_filtered,
    file_label = "",
    verbose=False):

    from datetime import datetime
    from os.path import join

    nullis = outdata_filtered.isna().sum()
    allok = True
    for n in nullis.index:
        if nullis[n] != 0:
            if allok:
                if verbose:
                    print("Making sure that there are no null values anywhere in the DF")
            allok = False
            if verbose:
                print(f"Found null values in {n} (Type: {outdata_filtered[n].dtype}). Fixing this ")
            if outdata_filtered[n].dtype==object:
                if n.startswith("S_"):
                    outdata_filtered[n] = outdata_filtered[n].fillna("not scraped")
                elif n.startswith("G_"):
                    outdata_filtered[n] = outdata_filtered[n].fillna("not annotated")
            elif outdata_filtered[n].dtype==float:
                outdata_filtered[n] = outdata_filtered[n].fillna(-1)
            elif is_datetime64_any_dtype(outdata_filtered[n]):
                outdata_filtered["S_createTime"] = outdata_filtered["S_createTime"].fillna(pd.Timestamp(year=2100,month=1,day=1))
    if not allok:
        if verbose:
            print("--"*60)

    if len(file_label)>0 and file_label[-1] != "_":
        file_label += "_"
    log_filename = f"{study_name}_{file_label}LOG.pkl"
    export_sub_folder_name = fyp.cf["paths"]["exports"].replace(fyp.cf["paths"]["main"],"")

    outdata_filtered.to_pickle(join(fyp.cf['paths']['exports'],log_filename))
    if verbose:
        print(f"Exported {len(outdata_filtered):,} observations in {join(export_sub_folder_name,log_filename)}.")
        print(f"The date of the observations in the log range from {outdata_filtered.T_local_date.min()} -- {outdata_filtered.T_local_date.max()}")
        print(f"Now: {datetime.now()}")
        print("=="*60)





def save_logs_as_csv(
    study_name,
    outdata_filtered,
    verbose=False):

    from datetime import datetime
    from os.path import join

    def _convert_num_to_string_and_then_some(a_number):
        bookend_char = "'"
        from copy import deepcopy

        a_number = deepcopy(str(a_number))

        if a_number[0] != bookend_char:
            a_number = bookend_char + a_number

        if a_number[-1] != bookend_char:
            a_number = a_number + bookend_char
        
        return a_number

    def _clean_surrogates(text):
        """Remove surrogate characters that can't be encoded in UTF-8"""
        if not isinstance(text, str):
            return text
        # Encode to UTF-8 with 'surrogatepass' then decode, replacing errors
        try:
            return text.encode('utf-8', 'ignore').decode('utf-8')
        except:
            # If that fails, use a more aggressive approach
            return ''.join(char for char in text if ord(char) < 0xD800 or ord(char) > 0xDFFF)


    if len(outdata_filtered) == 0:
        if verbose:
            print("A log file has not been generated so a CSV cannot be saved")
    else:
        if verbose:
            print("=="*60)
        log_as_csv_filename = study_name + "_" + "_LOG.csv"
        outdata_for_csv_export = outdata_filtered.copy()

        # Vectorized string cleaning - chain multiple replacements
        if verbose:
            print("Cleaning string data...")
        string_cols = outdata_for_csv_export.select_dtypes(include=['object']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = (
                outdata_for_csv_export[col]
                .astype(str)
                .str.replace("\n", " ", regex=False)
                .str.replace(";", " ", regex=False)
                .str.replace(", ", " ", regex=False)
                .str.replace(" ,", " ", regex=False)
                .str.replace("\t", " ", regex=False)
                .str.replace("|  ", " ", regex=False)
                .str.replace("،", " ", regex=False)  # arabic comma
            )

        # Clean surrogate characters from all string columns to prevent Unicode encoding errors
        # VECTORIZED: Only apply to string columns, not entire DataFrame
        if verbose:
            print("Cleaning surrogate characters from string data...")
        string_cols = outdata_for_csv_export.select_dtypes(include=['object']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = outdata_for_csv_export[col].apply(_clean_surrogates)

        # all numbers except for those related to session stats can be integers, so let's retype those
        some_float_cols = [c for c in outdata_for_csv_export.select_dtypes(include=[float]).columns if not "session" in c]
        outdata_for_csv_export[some_float_cols] = outdata_for_csv_export[some_float_cols].fillna(value=-1).astype(int)


        # VECTORIZED: Convert long numbers to strings for Excel
        # Use string operations instead of map
        for c in ["B_data_author_id","item_id","S_music_id","S_author_id","D_ts_jiggled"]:
            if c in outdata_for_csv_export.columns:
                # Faster: use str accessor to add quotes
                outdata_for_csv_export[c] = "'" + outdata_for_csv_export[c].astype(str) + "'"
            

        # VECTORIZED: Build TikTok URLs using string concatenation
        outdata_for_csv_export["tiktok_url"] = "https://www.tiktok.com/@/video/" + outdata_for_csv_export["item_id"] + "/"


        export_sub_folder_name = fyp.cf["paths"]["exports"].replace(fyp.cf["paths"]["main"],"")

        # Export with error handling for any remaining encoding issues
        outdata_for_csv_export.to_csv(join(fyp.cf['paths']['exports'],log_as_csv_filename), errors='replace')
        if verbose:
            print(f"Exported {len(outdata_for_csv_export):,} observations in {join(export_sub_folder_name,log_as_csv_filename)}.")
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_date.min()} -- {outdata_filtered.T_local_date.max()}")
            print(f"Now: {datetime.now()}")

        if verbose:
            print("=="*60)        