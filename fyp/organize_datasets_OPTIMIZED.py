
from zoneinfo import ZoneInfo
from numpy import int64 as np_int64



WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}




def _day_segment_from_hour(hour: int) -> str:
    if not (0 <= hour <= 23):
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if hour <= 5:
        return "night"
    if hour <= 11:
        return "morning"
    if hour <= 17:
        return "afternoon"
    return "evening"




"""def timestamp_to_local_parts(
    ts: int | float,
    input_tz: str,
    output_tz: str,
    *,
    ts_is_unix_utc: bool = True,
) -> dict:
    
    #Args:
    #    ts:
    #      - If ts_is_unix_utc=True (default): `ts` is a real Unix timestamp (absolute instant).
    #        In this mode, input_tz does NOT affect the final instant; it's included for metadata.
    #      - If ts_is_unix_utc=False: `ts` is treated as an epoch-like number that was "recorded"
    #        in the wall-clock of `input_tz`. In this mode, input_tz DOES affect output.

    #    input_tz: timezone in which the timestamp was recorded/assumed.
    #    output_tz: timezone that defines "local" outputs.
    #    ts_is_unix_utc: choose interpretation mode (see above).
    
    from datetime import datetime, timezone


    in_zone = ZoneInfo(input_tz)
    out_zone = ZoneInfo(output_tz)

    if ts_is_unix_utc:
        # Real Unix timestamp: absolute moment in time.
        dt_local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(out_zone)
    else:
        # Reinterpretation mode:
        # 1) Build the UTC wall-clock corresponding to ts (naive)
        # 2) Pretend that wall-clock was actually in input_tz
        # 3) Convert to output_tz
        dt_naive_utc = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        dt_in = dt_naive_utc.replace(tzinfo=in_zone)
        dt_local = dt_in.astimezone(out_zone)

    iso_year, iso_week, _ = dt_local.isocalendar()
    local_week = f"{iso_year:04d}-{iso_week:02d}"
    local_weekday = dt_local.strftime("%A").lower()
    local_hour = dt_local.hour

    return {
        "ts": ts,
        "input_tz": input_tz,
        "output_tz": output_tz,
        "ts_is_unix_utc": ts_is_unix_utc,
        "local_week": local_week,
        "local_weekday": local_weekday,
        "local_hour": local_hour,
        "local_day_segment": _day_segment_from_hour(local_hour),
        "local_date": dt_local.strftime("%Y-%m-%d"),
        "local_datetime": dt_local.strftime("%Y-%m-%d %H:%M:%S"),
    }"""







def extract_local_time_features(
    cf = None,
    some_events_df_in = None,
    kind_of_log = None,
    verbose = False):
    """
    Optimized version - extracts local time features from timestamps using vectorized operations.
    
    Now integrates per-donation timezone offsets from persona_stats_cache.
    """
    from pandas import concat, to_datetime, notna as pd_notna, NaT as pd_NaT, to_timedelta
    from numpy import select as np_select
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io
    from os.path import join, exists
    from datetime import datetime

    if cf is None:
        cf = init_config()



    df = some_events_df_in.copy()

    if verbose:
        print(f"Processing timestamps in dataset to extract local time features... ")

    # ---------------------------------------------------------------------
    # 1. Build local_timestamp depending on log type
    # ---------------------------------------------------------------------
    if kind_of_log == "baseline":
        # the 'baseline' timestamp is not utc - it is in the timezone of the device
        tz_col = "source_url.tz_name"
        ts_col = "timestamp_collected"

        unique_tz = df[tz_col].dropna().unique()
        if len(unique_tz) == 1:
            # Fast path: everything in same tz
            tz = ZoneInfo(unique_tz[0])
            df["local_timestamp"] = df[ts_col].dt.tz_localize(tz)
        else:
            # Slower path: per-timezone blocks
            local_parts = []
            for tz_name, block in df.groupby(tz_col, sort=False):
                tz = ZoneInfo(tz_name)
                part = block[ts_col].dt.tz_localize(tz)
                local_parts.append(part)
            df["local_timestamp"] = concat(local_parts).sort_index()

        df = df.drop(columns=[ts_col])
        
        # Convert baseline timestamps to naive local (remove timezone info but keep local wall clock)
        # This aligns with the new DDP strategy below
        df["local_timestamp"] = df["local_timestamp"].dt.tz_localize(None)

    elif kind_of_log == "ddp":
        # the 'ddp' timestamp is utc

        # Build item_id if missing
        if "item_id" not in df.columns:
            print("WARNING: item_id not found in ddp events df. Building it now...")
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
            ints = extracted.where(digits).astype("string[pyarrow]")

            mask = (
                df["primary_label"].eq("link")
                & df["feature_name"].notna()
            )
            df["item_id"] = ints.where(mask)
            # later we will convert it to string. One day I will make this more efficient.


        # normalise timestamp column name
        if "utc_timestamp" not in df.columns:
            print("WARNING: utc_timestamp not found in ddp events df. Renaming timestamp to utc_timestamp now...")
            df = df.rename(columns={"timestamp": "utc_timestamp"})


        # NEW LOGIC: Use per-donation timezone offsets
        
        # 1. Calculate Default Offset from static TIME_ZONE (as fallback)
        TIME_ZONE = cf["misc"]["TIME_ZONE"]
        try:
            now_local = datetime.now(ZoneInfo(TIME_ZONE))
            default_offset_hours = now_local.utcoffset().total_seconds() / 3600.0
        except Exception as e:
            if verbose: print(f"Warning: Could not determine offset for {TIME_ZONE}, defaulting to 0. {e}")
            default_offset_hours = 0.0
            
        # 2. Load Offsets from Cache
        stats_cache_path = "persona_stats_cache.parquet"
        
        df['tz_offset_hours'] = default_offset_hours # Initialize with default
        
        if data_io.exists(cf, "ddp_main", stats_cache_path):
            try:
                # Load only necessary columns
                stats_df = data_io.load_parquet(cf, "ddp_main", stats_cache_path, columns=['donation_id', 'inferred_tz_offset'])
                
                # Map offsets to main df
                # stats_df needs unique donation_ids. It should be unique per previous logic.
                if not stats_df['donation_id'].is_unique:
                    stats_df = stats_df.drop_duplicates(subset=['donation_id'])
                    
                offset_map = stats_df.set_index('donation_id')['inferred_tz_offset']
                
                # Map using donation_id column in df
                if 'donation_id' in df.columns:
                    mapped_offsets = df['donation_id'].map(offset_map)
                    # Update where not null
                    df.loc[mapped_offsets.notna(), 'tz_offset_hours'] = mapped_offsets[mapped_offsets.notna()]
                    if verbose:
                        print(f"Applied individual timezones to {mapped_offsets.notna().sum():,} events.")
                else:
                    if verbose: print("Warning: donation_id column missing, using default timezone.")
                    
            except Exception as e:
                print(f"Warning: Failed to load/apply timezone cache: {e}. Using default {TIME_ZONE}")
        else:
            if verbose: print("Timezone cache not found. Using default study timezone.")

        # 3. Calculate Local Timestamp (Naive Wall Clock)
        # local_ts = utc_ts + offset_hours
        # We work with timestamps (seconds) directly for speed then convert to datetime
        
        # Ensure utc_timestamp is numeric (seconds)
        utc_seconds = df["utc_timestamp"].astype("float64")
        
        # Add offset (hours * 3600)
        local_seconds = utc_seconds + (df['tz_offset_hours'] * 3600.0)
        
        # Convert to Naive Datetime
        df["local_timestamp"] = to_datetime(local_seconds, unit='s', utc=False)
        
        # Cleanup temp column
        df = df.drop(columns=['tz_offset_hours'], errors='ignore')
        
        # Pyarrow conversion
        df["local_timestamp"] = df["local_timestamp"].convert_dtypes(dtype_backend="pyarrow")


    else:
        raise ValueError("kind_of_log can only be 'baseline' or 'ddp'")

    # ---------------------------------------------------------------------
    # 2. Derive local time features
    # ---------------------------------------------------------------------
    ts = df["local_timestamp"]
    
    # Check if we still have timezone awareness (should be none for DDP, but maybe for baseline if not stripped)
    # The new logic strips it for baseline too.
    
    # If stored as object dtype, force conversion (should handle naive correctly)
    if ts.dtype == 'object':
        df["local_timestamp"] = to_datetime(ts)
        ts = df["local_timestamp"]

    iso = ts.dt.isocalendar()  # DataFrame: year, week, day
    iso["day"] = iso["day"].map(WEEKDAY_MAPPER)
    iso["year_week"] = iso["year"].astype("uint16").astype("string[pyarrow]") + "-" + iso["week"].astype("uint8").astype("string[pyarrow]")

    df["local_weekday"] = iso["day"].to_list()
    df["local_week"] = iso["year_week"].to_list()

    df["local_hour"] = ts.dt.hour.astype("uint8")

    """# day segment via vectorised ranges, no Python helper needed
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
    )"""

    df["local_day_segment"] = _day_segment_from_hour(df["local_hour"]).astype("string[pyarrow]")

    # Optimization: Use .dt.date directly (faster than map)
    df["local_date"] = ts.dt.date
    df["local_date_str"] = df["local_date"].astype("string[pyarrow]")


    print("...done")

    return df













def load_scrape_metadata(
    cf = None,
    consolidate=False,
    verbose=False):
    # load the scraped metadata dataframe


    #import shutil
    from os import listdir, rename
    from os.path import join, basename, exists
    from pandas import concat, NA as pd_NA
    import fyp.data_io as data_io
    from datetime import datetime
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow

    if cf is None:
        cf = init_config()
    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    # -------------------------------------------------
    # load the scrape_metadata dataframe
    # -------------------------------------------------
    print("Loading scraped metadata...")

    # if we are consolidating, load all columns (otherwise data is lost)
    if True:#consolidate:
        scrape_metadata = data_io.load_parquet(cf, "scrape", "*", verbose=verbose)
    # if we are not consolidating, load only the useful variables
    else:
        import re
        useful_variables = ["image_list", "video_downloaded", "createTime"]
        for k in cf['var_scheme'][cf['var_scheme']['role']!='skip'].variable_name:
            if re.match(r'^[A-Z]_', k):
                useful_variables.append(k[2:])
            useful_variables.append(k)

        scrape_metadata = data_io.load_parquet(cf, "scrape", "*", columns=useful_variables, verbose=verbose)


    # -------------------------------------------------
    # There may be some items listed twice - once as video_downloaded and once as not
    # This code addresses that issue
    # -------------------------------------------------

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
            print(f"Fixed the inconsistencies by keeping the one of the pairs with video_download=True")
            print(f"This reduces the number of inconsistent items to {len(items_w_inconsistent_video_download_status)}")

        # recombine the two dataframes
        scrape_metadata = concat([items_w_consistent_video_download_status,items_w_inconsistent_video_download_status])
        if verbose:
            print(f"After this procedure, the shape of the scrape DF is: {scrape_metadata.shape}")



    # -------------------------------------------------


    if verbose:
        print(
            f"{scrape_metadata['video_downloaded'].value_counts().loc[True]:,} items have downloaded videos and "
            f"{scrape_metadata['video_downloaded'].value_counts().loc[False]:,} don't")


    # fixing up some minor issues with the columns

    # first, set item_id as index
    scrape_metadata.set_index('item_id', inplace=True)

    # remove do_not_modify column if it exists
    scrape_metadata.drop(["do_not_modify"], axis=1, errors='ignore', inplace=True)

    # fill NA values in image_list with empty strings
    scrape_metadata['image_list'] = scrape_metadata['image_list'].fillna("")

    # for items with non-empty image_list, set video_duration based on number of images * 2 seconds
    scrape_metadata.loc[scrape_metadata[scrape_metadata['image_list']!=""].index,'video_duration'] = scrape_metadata.loc[scrape_metadata[scrape_metadata['image_list']!=""].index,'image_list'].map(lambda x: len(x.split(' | ')) * 2)

    # video duration is never zero - set zero durations to pd_NA
    scrape_metadata.loc[scrape_metadata[(scrape_metadata['video_duration']==0)].index,'video_duration'] = pd_NA

    # move the item_id back from the index to a column
    scrape_metadata.reset_index(inplace=True)



    # fix up the types for pyarrow
    scrape_metadata = convert_dtypes_to_pyarrow(scrape_metadata, verbose=verbose)
 

    if consolidate:
        scrape_metadata_filenames = [gg for gg in data_io.listdir(cf, "scrape", verbose=verbose) if gg.startswith("scrape_metadata") and gg.endswith(cf["misc"]["file_format"])]
        scrape_metadata_filenames = list(set(scrape_metadata_filenames))


        if len(scrape_metadata_filenames) > 1:

            # consolidating the files to a single file using the latest filename
            # the reason for using the old filename is to not kick off potential secondary processes that are monitoring the scrape folder
            # for new files. I want such processes to ignore files that are consolidations of other files

            latest_filename = sorted(scrape_metadata_filenames)[-1]
            if verbose:
                print(f"The scrape_metadata files will be consolidated into a single file: {basename(latest_filename)}.")

            data_io.save_parquet(cf, scrape_metadata, "scrape", latest_filename, verbose=verbose)

            for fn in scrape_metadata_filenames:
                if not fn == latest_filename:
                    data_io.move(cf, "scrape", "archive", fn, verbose=verbose)
        else:
            if verbose:
                print(f"Only a single scrape_metadata file was found. No need to consolidate.")
        

    print(f"...done. Loaded scraped metadata - shape {scrape_metadata.shape}")
    #print("--"*60)
    
    return scrape_metadata







def load_failed_scrapes(
    cf = None,
    consolidate = False,
    verbose = False,
    super_verbose = False):
    # Load list of failed scraped attempts.

    from datetime import datetime
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io

    if cf is None:
        cf = init_config()
    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    if verbose:
        print("Loading failed scrapes...")

    failed_scrape_fn_core = "scrape_failed_items"

    failed_scrape_files = [gg for gg in data_io.listdir(cf, "scrape", verbose=verbose) if gg.startswith(failed_scrape_fn_core)]

    failed_scrapes = []
    for fn in failed_scrape_files:
        if super_verbose:
            print(fn)
        some_dict = data_io.load_json(cf, "scrape", fn, verbose=verbose)
        if some_dict is not None:
            failed_scrapes += some_dict

    failed_scrapes = list(set(map(lambda one_item_id:str(one_item_id), failed_scrapes)))


    if consolidate and len(failed_scrape_files) > 1:
        fine_ts = "".join([k for k in str(datetime.now()) if k in "0123456789"])
        if verbose:
            print(f"{len(failed_scrapes):,} of these are unique and will be saved as a new consolidated file {failed_scrape_fn_core}_{fine_ts}.json.")

        data_io.save_json(cf, failed_scrapes, "scrape", f"{failed_scrape_fn_core}_{fine_ts}.json", verbose=verbose)

        for fn in failed_scrape_files:
            data_io.move(cf, "scrape", "archive", fn, verbose=verbose)
            if verbose:
                print(f"Moved {fn} to archive")


    if verbose:
        print(f"Loaded list of all failed scrapes: {len(failed_scrapes):,}")

    return failed_scrapes









"""def OLD_load_zeeschuimer_data(
    cf = None,
    study_name = None,
    use_half_baked = False,
    verbose=False):
    # load items from baseline logs

    from pandas import concat, DataFrame
    from os.path import exists, join
    from os import remove, listdir
    from datetime import datetime
    from json import load as json_load
    from zoneinfo import ZoneInfo
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = init_config()
    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    

    half_baked_baseline_path = f"{study_name}_HALF_BAKED_BASELINE{cf['misc']['file_format']}"

    if not use_half_baked and data_io.exists(cf, "exports", half_baked_baseline_path):
        data_io.remove(cf, "exports", half_baked_baseline_path, verbose=verbose)


    if use_half_baked and data_io.exists(cf, "exports", half_baked_baseline_path):
        #nice_time = datetime.fromtimestamp(getctime(half_baked_baseline_path)).strftime('%Y-%m-%d %H:%M:%S')
        nice_time = datetime.fromtimestamp(data_io.getctime(cf, "exports", half_baked_baseline_path)).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Loading half-baked baseline events file created at: {nice_time}", end=" ", flush=True)

        baseline_log = data_io.load_parquet(cf, "exports", half_baked_baseline_path, verbose=verbose)
        print(f"Shape: {baseline_log.shape}")
    else:

        BASELINE_START_DATE = cf["study_defs"][study_name]["BASELINE_START_DATE"]
        if isinstance(BASELINE_START_DATE, str):
            BASELINE_START_DATE = datetime.strptime(BASELINE_START_DATE, "%Y-%m-%d")
        
        BASELINE_END_DATE = cf["study_defs"][study_name]["BASELINE_END_DATE"]
        if isinstance(BASELINE_END_DATE, str):
            BASELINE_END_DATE = datetime.strptime(BASELINE_END_DATE, "%Y-%m-%d")
    
        print("Loading baseline logs...")


        list_of_zeeschuimer_logs = []
        okay_test_cases = []

        zeeschuimer_refined_files = [fn for fn in data_io.listdir(cf, "zeeschuimer_refined", verbose=verbose) if fn.endswith(cf['misc']['file_format'])]

        for fn in zeeschuimer_refined_files:
            
            zeeschuimer_candidate = data_io.load_parquet(cf, "zeeschuimer_refined", fn, verbose=verbose)

            empty_columns = []
            for c in zeeschuimer_candidate.columns:
                if zeeschuimer_candidate[c].isna().sum() / len(zeeschuimer_candidate) > 0.95:
                    empty_columns.append(c)

            zeeschuimer_candidate.drop(empty_columns, axis=1, inplace=True)

            zeeschuimer_candidate = zeeschuimer_candidate.convert_dtypes(dtype_backend="pyarrow") # to be sure
            

            test_cols = zeeschuimer_candidate[["item_id","timestamp_collected"]].reset_index(drop=True).sort_values("timestamp_collected").copy()
            duplicate_found = False
            for zl in okay_test_cases:
                if zl.shape == test_cols.shape:
                    if (zl.index == test_cols.index).all() and (zl.columns == test_cols.columns).all():
                        if (test_cols == zl).all().all():
                            duplicate_found = True
                            if verbose:
                                print("Warning: Found a duplicate zeeschuimer file. I'm not adding it to the collection...")
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


            empty_columns = []
            for c in baseline_log.columns:
                if baseline_log[c].isna().sum() / len(baseline_log) > 0.8:
                    empty_columns.append(c)

            baseline_log.drop(empty_columns, axis=1, inplace=True)


            baseline_log = extract_local_time_features(
                cf = cf,
                some_events_df_in = baseline_log,
                kind_of_log = 'baseline',
                verbose = verbose)

            baseline_log = convert_dtypes_to_pyarrow(baseline_log, verbose=verbose)
            if use_half_baked:
                if verbose:
                    print("Saving half-baked baseline events...")    
                data_io.save_parquet(cf, baseline_log, "exports", half_baked_baseline_path, verbose=verbose)
        
        else:
            baseline_log = DataFrame()

    return {"data_baseline_log":baseline_log}
"""












"""def OLD_load_ddp_events(
    cf = None, 
    study_name = None, 
    use_half_baked = False, 
    verbose=False):
    # load DF with all donations previously ingested

    from os import listdir, remove
    from os.path import join, exists
    from json import load as json_load
    from pandas import concat
    import fyp.data_io as data_io
    from datetime import datetime
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow

    if cf is None:
        cf = init_config()
    if study_name is None:
        raise ValueError("study_name must be specified")
    

    if not cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() in ["sample","all"]:
        if verbose:
            print("Not loading DDP events")
        return None


    half_baked_ddp_events_path = f"{study_name}_HALF_BAKED_ALL_DDP{cf['misc']['file_format']}"
    half_baked_sampled_ddp_events_path = f"{study_name}_HALF_BAKED_SAMPLED_DDP{cf['misc']['file_format']}"


    if not use_half_baked:
        data_io.remove(cf, "exports", half_baked_ddp_events_path, verbose=verbose)
        data_io.remove(cf, "exports", half_baked_sampled_ddp_events_path, verbose=verbose)


    if use_half_baked and data_io.exists(cf, "exports", half_baked_ddp_events_path): # use half-baked DDP events file if it exists
        print("Loading half-baked DDP events file...", end=" ", flush=True)
        all_ddp_events_df = data_io.load_parquet(cf, "exports", half_baked_ddp_events_path, verbose=verbose)
        print(f"Shape: {all_ddp_events_df.shape}")

    # otherwise load all DDP events
    else:

        DDP_START_DATE = cf["study_defs"][study_name]["DDP_START_DATE"]
        if isinstance(DDP_START_DATE, str):
            DDP_START_DATE = datetime.strptime(DDP_START_DATE, "%Y-%m-%d").date()
        
        DDP_END_DATE = cf["study_defs"][study_name]["DDP_END_DATE"]
        if isinstance(DDP_END_DATE, str):
            DDP_END_DATE = datetime.strptime(DDP_END_DATE, "%Y-%m-%d").date()

        print("Loading all DDP events...", end=" ", flush=True)
        all_ddp_events_df = data_io.load_parquet(cf, "ddp_main", f"all_participant_events{cf['misc']['file_format']}", verbose=verbose)

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
        mask = (all_ddp_events_df["date"] >= DDP_START_DATE) & (all_ddp_events_df["date"] <= DDP_END_DATE)
        all_ddp_events_df = all_ddp_events_df.loc[mask].copy()
        if verbose:
            print(f"Keeping DDP events within date range {all_ddp_events_df.date.min()} -- {all_ddp_events_df.date.max()} yielding {len(all_ddp_events_df):,} events")

        # dropping some corrupt URLs simply by calculating the most common length of the URLs and dropping those that doesn't match
        all_ddp_events_df = remove_link_events_with_corrupt_links(all_ddp_events_df)
        if verbose:
            print(f"Dropping DDP events with corrupt TikTok URLs. New shape: {all_ddp_events_df.shape}")

        all_ddp_events_df = extract_local_time_features(
            cf = cf,
            some_events_df_in = all_ddp_events_df,
            kind_of_log = 'ddp',
            verbose = verbose)

        all_ddp_events_df = convert_dtypes_to_pyarrow(all_ddp_events_df, verbose=verbose)

        if use_half_baked:
            if verbose:
                print("Saving half-baked DDP events...")    
            data_io.save_parquet(cf, all_ddp_events_df, "exports", half_baked_ddp_events_path, verbose=verbose)

    the_result = {"all_data_ddp_events":all_ddp_events_df}



    if cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() == "sample":
        if use_half_baked and data_io.exists(cf, "exports", half_baked_sampled_ddp_events_path):
            print("Loading half-baked sampled DDP events file...", end=" ", flush=True)
            sampled_data_ddp_events = data_io.load_parquet(cf, "exports", half_baked_sampled_ddp_events_path, verbose=verbose)
            print(f"Shape: {sampled_data_ddp_events.shape}")
        else:
            sampled_data_ddp_events = sample_ddp_events(
                cf = cf, 
                study_name = study_name, 
                all_ddp_events_df = all_ddp_events_df, 
                verbose=verbose)

            sampled_data_ddp_events = convert_dtypes_to_pyarrow(sampled_data_ddp_events, verbose=verbose)

            if use_half_baked:
                if verbose:
                    print("Saving half-baked sampled DDP events...")    
                data_io.save_parquet(cf, sampled_data_ddp_events, "exports", half_baked_sampled_ddp_events_path, verbose=verbose)
        
        the_result["sampled_data_ddp_events"] = sampled_data_ddp_events

    return the_result"""












def load_datasets(
    cf = None,
    study_name = None,
    all_datasets = {},
    consolidate = False,
    load_from_cache = False,
    save_to_cache = False,
    verbose=False):


    from os.path import join as os_join, exists as os_exists
    from fyp.machine_annotation import load_machine_annotations
    from fyp.donations import load_special_donations, load_ddp_events
    from fyp.zeeschuimer import load_zeeschuimer_data
    from fyp.fyp_main import init_config
    import fyp.data_io as data_io
    from copy import deepcopy
    from datetime import datetime
    from pandas import read_parquet as pd_read_parquet


    if study_name is None:
        raise ValueError("study_name must be specified")
    if cf is None:
        cf = init_config()

    if cf['misc']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    print(f"Loading datasets for study '{study_name}'...")

    if load_from_cache:
        tutti_data = {}
        for k in ['scraped','annotated','ddp','baseline']:
            if os_exists(os_join(cf["paths"]["temp"], f"CACHE_complete_{k}.parquet")):
                tutti_data[k] = pd_read_parquet(os_join(cf["paths"]["temp"], f"CACHE_complete_{k}.parquet"), engine='pyarrow', dtype_backend="pyarrow", use_threads=True)
        print(f"  Using complete datasets from temp folder: {len(tutti_data)}")
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        print(f"  Using complete datasets provided as argument: {len(tutti_data)}")
    else:
        tutti_data = {}
        print(f"  Starting without complete dataset. Building from scratch: {len(tutti_data)}")

    #all_datasets = {}

    #if delete_all_half_baked_files:
    #    print(f" - Deleting half-baked files - study: {study_name}")
    #    for half_baked_file in data_io.listdir(cf, "exports", verbose=verbose):
    #        if half_baked_file.startswith(f"{study_name}_HALF_BAKED"):
    #            data_io.remove(cf, "exports", half_baked_file, verbose=verbose)


    #if not use_half_baked:
    #    print(f" - Generating fresh datasets - WON'T be saving half-baked files - {study_name}")
    #elif delete_all_half_baked_files:
    #    print(f" - Generating fresh datasets - WILL SAVE new half-baked files - {study_name}")
    #else:
    #    print(f" - Loading existing half-baked files - {study_name}")


    if cf["study_defs"][study_name]["INCLUDE_ZEESCHUIMER_DATA"]:
        if tutti_data.get("baseline") is None:
            tutti_data["baseline"] = load_zeeschuimer_data(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["baseline"] = load_zeeschuimer_data(cf = cf, study_name = study_name, all_data = tutti_data["baseline"], verbose=verbose)
    else:
        if "baseline" in tutti_data:
            del tutti_data["baseline"]

    if cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() in ['all','sample']:
        if tutti_data.get("ddp") is None:
            tutti_data["ddp"] = load_ddp_events(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["ddp"] = load_ddp_events(cf = cf, study_name = study_name, all_data = tutti_data["ddp"], verbose=verbose)

    elif cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() == "special" and len(cf["study_defs"][study_name]["SPECIAL_DONATIONS"])>0:
        if tutti_data.get("ddp") is None:
            tutti_data["ddp"] = load_special_donations(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["ddp"] = load_special_donations(cf = cf, study_name = study_name, all_data = tutti_data["ddp"], verbose=verbose)

    else:
        if "ddp" in tutti_data:
            del tutti_data["ddp"]

    if tutti_data.get("scraped") is None:
        tutti_data["scraped"] = load_scrape_metadata(cf = cf, consolidate=consolidate, verbose=verbose)
    else:
        print("  Scraped metadata already loaded")
    
    if tutti_data.get("annotated") is None:
        tutti_data["annotated"] = load_machine_annotations(cf = cf, consolidate=consolidate, verbose = verbose)
    else:
        print("  Video annotations already loaded")


    def _df_size(df):
        memory_per_column = df.memory_usage(deep=True) 
        total_memory_bytes = memory_per_column.sum()
        total_memory_mb = total_memory_bytes / (1024**2)
        return total_memory_mb

    if save_to_cache:
        t1 = datetime.now()
        if verbose:
            print("  Saving datasets to temp folder...")
        for k in tutti_data:
            tutti_data[k].to_parquet(os_join(cf["paths"]["temp"], f"CACHE_complete_{k}.parquet"), engine='pyarrow')
        if verbose:
            print(f"  ...done. Time taken to save datasets to temp folder: {(datetime.now() - t1).total_seconds():.1f} seconds")

    print(f"...done. Datasets loaded for study '{study_name}'")
    if verbose:
        print(f"- {"\n - ".join([f"'{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)" for k in tutti_data])}")




    return tutti_data





"""def identify_unique_videos(cf = None, study_name = None, all_datasets = None, verbose = False):
    # combine the special DDP events with the sampled DDP events

    from pandas import concat, DataFrame, NamedAgg, NA
    from fyp.fyp_main import init_config

    if study_name is None:
        raise ValueError("study_name must be specified")
    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    if cf is None:
        cf = init_config()

    MIN_NUNIQUE_USERS = cf["study_defs"][study_name]["MIN_NUNIQUE_USERS"]


    dataframes_to_combine = []
    # use the sampled events if available, otherwise use the all events
    if "sampled_data_ddp_events" in all_datasets:
        dataframes_to_combine.append(all_datasets["sampled_data_ddp_events"])
    elif "all_data_ddp_events" in all_datasets:
        dataframes_to_combine.append(all_datasets["all_data_ddp_events"])
    
    if "data_special_ddps" in all_datasets:
        dataframes_to_combine.append(all_datasets["data_special_ddps"])


    if len(dataframes_to_combine) > 0:
        ddp_events_for_unique_videos_df = concat(dataframes_to_combine, ignore_index=True).drop_duplicates()
        if verbose:
            print(f"Shape of the combined (sampled + special) DDP events DF for exporting list of unique videos: {ddp_events_for_unique_videos_df.shape}")
            print(f"The combined DDP events range from {ddp_events_for_unique_videos_df.date.min()} -- {ddp_events_for_unique_videos_df.date.max()}")
    else:
        ddp_events_for_unique_videos_df = DataFrame()
        if verbose:
            print("No DDP events to combine, creating an empty dataframe.")

    #if verbose:
        #print("--"*60)

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

        # extracting item ids from the URLs (in the index)
        unique_ddp_videos["item_id"] = [parts[-2] if len(parts := s.rsplit("/", 2)) > 1 else None for s in unique_ddp_videos.index]
        unique_ddp_videos['item_id'] = unique_ddp_videos['item_id'].astype("string[pyarrow]")

    else:
        if verbose:
            print("No events in the combined DDP dataframe")


    ### identify unique videos in baseline logs
    unique_baseline_videos = DataFrame(columns=["item_id", "nunique_users", "total_views", "primary_value"])

    if len(all_datasets["data_baseline_log"])>0:

        unique_item_id_list = list(str(k) for k in all_datasets["data_baseline_log"].item_id.unique())
        unique_baseline_videos = DataFrame()
        unique_baseline_videos['item_id'] = unique_item_id_list
        unique_baseline_videos['item_id'] = unique_baseline_videos['item_id'].astype("string[pyarrow]")
        unique_baseline_videos['nunique_users'] = NA
        unique_baseline_videos['total_views'] = NA
        # construct URL
        unique_baseline_videos['primary_value'] = "https://www.tiktokv.com/share/video/" + unique_baseline_videos['item_id'].astype(str) + "/"

    unique_baseline_videos.set_index('primary_value', inplace=True)

    if verbose:
        print(f"Unique videos identified in the baseline logs: {len(list(set(unique_baseline_videos.index.tolist()))):,}")

    ### combine unique donation videos with unique baseline videos.Drop columns with all null values
    dataframes_to_combine = [k.dropna(axis="columns", how="all") for k in [unique_baseline_videos, unique_ddp_videos] if len(k) > 0]
    
    video_observation_stats = concat(dataframes_to_combine, ignore_index=True).drop_duplicates(subset='item_id', keep='last')
    if verbose:
        print(f"Combining unique videos from data donation events with videos from baseline data into a DF with the shape: {video_observation_stats.shape}")

    return video_observation_stats






def calculate_all_unique_video_subsets(cf = None, study_name = None, all_datasets = None, verbose=False):
    ### Check the unique videos against scraped metadata, machine results and such things

    from fyp.machine_annotation import load_machine_annotations
    from fyp.fyp_main import init_config


    if study_name is None:
        raise ValueError("study_name must be specified")
    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    if cf is None:
        cf = init_config()



    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose, consolidate = True))

    # load 
    machine_annotated_videos = set([str(k) for k in all_datasets["data_annotated"].item_id.tolist()])

    failed_annotations = set(load_machine_annotations(
        cf = cf,
        include_failed_calls=True,
        verbose = False,
        ).item_id.tolist())
    failed_annotations = failed_annotations - machine_annotated_videos

    too_long_videos = set(all_datasets["data_scraped"][all_datasets["data_scraped"]["video_duration"]>cf["machine"]["max_duration_for_annotation"]].item_id.to_list())
    if verbose:
        print(f"Too long videos: {len(too_long_videos):,} of {len(all_datasets['data_scraped']):,}")

    completed_downloads = set([str(k) for k in all_datasets["data_scraped"][all_datasets["data_scraped"]["video_downloaded"]].item_id.to_list()]) - too_long_videos
    missing_downloads = set([str(k) for k in all_datasets["data_scraped"][~all_datasets["data_scraped"]["video_downloaded"]].item_id.to_list()]) | too_long_videos

    unique_videos_with_stats = identify_unique_videos(cf = cf, study_name = study_name, all_datasets = all_datasets, verbose=verbose)
    all_unique_videos = set([str(k) for k in unique_videos_with_stats.item_id.to_list()])

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
    print(f"    Metadata found but not downloaded: {len(missing_downloads):,} videos (including videos too long to process)")
    print(f"    Failed scrapes: {len(failed_scrapes):,} videos")
    print(f"    Unseen videos: {len(unseen_videos):,} videos")
    print(f"Sum of the set sizes: {len(unseen_videos) + len(downloaded_and_annotated) + len(downloaded_not_annotated) + len(missing_downloads) + len(failed_annotations) + len(failed_scrapes):,}")
    #print("--"*60)

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
    cf = None,
    study_name = None,
    all_datasets = None,
    subsets = None,
    file_label = "",
    INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
    INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
    INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = False,
    INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
    verbose=False
):

    from os.path import join
    from datetime import datetime
    from pandas import DataFrame
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io

    if study_name is None:
        raise ValueError("study_name must be specified")
    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    if cf is None:
        cf = init_config()
    


    if verbose:
        print("The user's selection of available subsets of the videos gives the following total set:")
    work_with_these_videos = set()
    if INCLUDE_UNSEEN_VIDEOS_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["unseen_videos"]
        print(f"- UNSEEN_VIDEOS selected --> gives {len(work_with_these_videos):,} videos")
    if INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["downloaded_and_annotated"]
        print(f"- DOWNLOADED_AND_ANNOTATED selected --> gives {len(work_with_these_videos):,} videos")
    if INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["downloaded_not_annotated"]
        print(f"- DOWNLOADED_BUT_NOT_ANNOTATED selected --> gives {len(work_with_these_videos):,} videos")
    if INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["missing_downloads"]
        print(f"- SCRAPED_BUT_NOT_DOWNLOADED selected --> gives {len(work_with_these_videos):,} videos")
    if INCLUDE_FAILED_SCRAPES_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["failed_scrapes"]
        print(f"- FAILED_SCRAPES selected --> gives {len(work_with_these_videos):,} videos")
    if INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT:
        work_with_these_videos = work_with_these_videos | subsets["failed_annotations"]
        print(f"- FAILED_ANNOTATIONS selected --> gives {len(work_with_these_videos):,} videos")


    if len(work_with_these_videos) == 0:
        if verbose:
            print("This data selection did not yield any videos")
        return DataFrame()

    if verbose:
        print(f"This data selection yielded {len(work_with_these_videos):,} unique videos")




    unique_videos_with_stats = identify_unique_videos(cf = cf, study_name = study_name, all_datasets = all_datasets, verbose=False)

    if verbose:
        print(f"Unique videos (DF with some stats) shape: {unique_videos_with_stats.shape}")
        #print(f"Type of first item in work_with_these_videos: {type(list(work_with_these_videos)[0])}")
        #print(f"Type of item_id in unique_videos_with_stats: {unique_videos_with_stats.item_id.dtype}")

    all_unique_videos_to_save = unique_videos_with_stats[unique_videos_with_stats.item_id.isin(work_with_these_videos)].copy()

    all_unique_videos_to_save = convert_dtypes_to_pyarrow(all_unique_videos_to_save, verbose=verbose)

    if verbose:
        print(f"All unique videos to save shape: {all_unique_videos_to_save.shape}")


    ### save the unique item_ids (videos) w basic stats to a file
    if len(file_label)>0 and file_label[-1] != "_":
        file_label += "_"
    unique_videos_filename = f"{study_name}_{file_label}UNIQUE{cf['misc']['file_format']}"

    data_io.save_parquet(cf, all_unique_videos_to_save, "exports", unique_videos_filename, verbose=verbose)

    if verbose:
        print(f"Exported {len(all_unique_videos_to_save):,} unique videos to '{unique_videos_filename}'")
        print(f"Now: {datetime.now()}")
    return all_unique_videos_to_save
"""





def select_videos_from_study_dataset(
    cf = None,
    study_data = None,
    query_string = "",
    verbose = False, notebook_mode = False):

    from fyp.fyp_main import init_config
    from pandas import NamedAgg

    if study_data is None:
        raise ValueError("study_data must be specified")
    if cf is None:
        cf = init_config()

    # group by video URL and count the number of unique users
    video_stats = study_data.groupby('item_id').agg(
        nunique_donations = NamedAgg(column="D_donation_id", aggfunc="nunique"),
        total_observations = NamedAgg(column="D_donation_id", aggfunc="count"),
        scraped_ok = NamedAgg(column="scraped_ok", aggfunc="first"),
        scraped_fail = NamedAgg(column="scraped_fail", aggfunc="first"),
        annotated_ok = NamedAgg(column="annotated_ok", aggfunc="first"),
        annotated_fail = NamedAgg(column="annotated_fail", aggfunc="first"),
        video_duration = NamedAgg(column="S_video_duration", aggfunc="max"),
        )

    video_stats['duration_ok_to_annotate'] = (video_stats['video_duration'] <= cf["machine"]["max_duration_for_annotation"]).fillna(False)
    video_stats.drop(columns=["video_duration"], inplace=True)

    video_stats.query(query_string, inplace=True)

    return video_stats






"""def select_videos_from_half_baked(
    cf = None,
    study_name = None,
    file_label = "",
    INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
    INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
    INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = False,
    INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
    INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
    verbose = False, notebook_mode = False):

    from fyp.fyp_main import init_config

    if study_name is None:
        raise ValueError("study_name must be specified")
    if cf is None:
        cf = init_config()


    all_datasets = load_datasets(
        cf = cf,
        study_name = study_name,
        use_half_baked = True,
        delete_all_half_baked_files = False,
        consolidate = False,
        verbose = verbose)

    video_subsets = calculate_all_unique_video_subsets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        verbose = verbose)

    selected_videos = save_selected_unique_video_subsets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        subsets = video_subsets,
        file_label = file_label,
        INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = INCLUDE_UNSEEN_VIDEOS_IN_EXPORT,
        INCLUDE_FAILED_SCRAPES_IN_EXPORT = INCLUDE_FAILED_SCRAPES_IN_EXPORT,
        INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT,
        INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT,
        INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT,
        INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT,
        verbose = verbose
    )

    return selected_videos"""





def generate_and_check_unique_videos_for_scrape_and_annotate(cf = None, study_data = None, verbose = False):

    from fyp.fyp_main import init_config

    if cf is None:
        cf = init_config()
    if study_data is None:
        raise ValueError("study_data must be specified")

    selected_annotate_videos = select_videos_from_study_dataset(
        cf = cf,
        study_data = study_data,
        query_string = "scraped_ok & ~annotated_ok & ~annotated_fail & duration_ok_to_annotate",
        verbose = verbose,
        notebook_mode = False)

    selected_scrape_videos = select_videos_from_study_dataset(
        cf = cf,
        study_data = study_data,
        query_string = "~scraped_ok & ~scraped_fail",
        verbose = verbose,
        notebook_mode = False)

    return {
        "annotate": selected_annotate_videos.shape,
        "scrape": selected_scrape_videos.shape
    }







"""def OLD_generate_and_check_unique_videos_for_scrape_and_annotate(cf = None, study_name = None, verbose = False):

    from fyp.fyp_main import init_config

    if cf is None:
        cf = init_config()
    if study_name is None:
        raise ValueError("study_name must be specified")

    all_datasets = load_datasets(
        cf = cf,
        study_name = study_name,
        use_half_baked = True,
        delete_all_half_baked_files = False,
        consolidate = False,
        verbose = verbose)

    video_subsets = calculate_all_unique_video_subsets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        verbose = verbose)


    selected_annotate_videos = save_selected_unique_video_subsets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        subsets = video_subsets,
        file_label = "ANNOTATE",
        INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = False,
        INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
        INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
        INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = True,
        INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
        INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
        verbose = verbose
    )

    selected_scrape_videos = save_selected_unique_video_subsets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        subsets = video_subsets,
        file_label = "SCRAPE",
        INCLUDE_UNSEEN_VIDEOS_IN_EXPORT = True,
        INCLUDE_FAILED_SCRAPES_IN_EXPORT = False,
        INCLUDE_SCRAPED_BUT_NOT_DOWNLOADED_IN_EXPORT = False,
        INCLUDE_DOWNLOADED_BUT_NOT_ANNOTATED_IN_EXPORT = False,
        INCLUDE_FAILED_ANNOTATIONS_IN_EXPORT = False,
        INCLUDE_DOWNLOADED_AND_ANNOTATED_IN_EXPORT = False,
        verbose = verbose
    )

    return {
        "annotate": selected_annotate_videos.shape,
        "scrape": selected_scrape_videos.shape
    }"""






def rename_columns(some_events):
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

    from pandas import set_option
    set_option('future.no_silent_downcasting', True)

    for fu in fixer_upper:
        mapper = {c:c.replace(fu[0],fu[1]) for c in some_eventsC.columns if (c != c.replace(fu[0],fu[1])) and (not c.replace(fu[0],fu[1]) in some_eventsC.columns)}
        some_eventsC = some_eventsC.rename(columns=mapper).copy()
    
    return some_eventsC














def _process_scrape_metadata_for_merge_w_logs(all_datasets, combined_log, verbose=False):

    from pandas import isna as pd_isna, Timestamp, DataFrame, to_datetime, Series, NA as pd_NA
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow

    if len(combined_log) == 0:
        return DataFrame()


    # polishing the scraped metadata dataset for merging with the log
    scrape_metadata_log = all_datasets["scraped"][all_datasets["scraped"].item_id.isin(combined_log.item_id.unique())].copy()

    if verbose:
        print(f"Processing scraped metadata {scrape_metadata_log.shape} for merge w logs. Combined log has shape:{combined_log.shape}...")


    object_cols = scrape_metadata_log.select_dtypes(exclude=['number']).columns
    scrape_metadata_log[object_cols] = scrape_metadata_log[object_cols].replace('nan', '').infer_objects(copy=False)


    scrape_metadata_log["createTime"] = to_datetime(
        scrape_metadata_log["createTime"], 
        errors='coerce',
        utc=True
    ).fillna(pd_NA)#.fillna(Timestamp(year=2100, month=1, day=1, tz='UTC'))


    # it is not possible to have videos that are negative or zero duration. Replace with NA
    scrape_metadata_log['video_duration'] = scrape_metadata_log['video_duration'].fillna(pd_NA).replace(-1, pd_NA).replace(0, pd_NA)


    scrape_metadata_log.drop(columns=[
        "image_list","video_url","video_downloaded","audio_extracted","cover_downloaded","do_not_modify","last_modified","video_cover"], inplace=True, errors="ignore")


    scrape_metadata_log = scrape_metadata_log.rename(columns={c:"S_"+c if not c=="item_id" else c for c in scrape_metadata_log.columns}).copy()
    if verbose:
        print(f"...processed scraped metadata shape {scrape_metadata_log.shape}")

    #if verbose:
        #print("--"*60)

    #scrape_metadata_log = _check_for_null_values_in_df(scrape_metadata_log, verbose=verbose)


    scrape_metadata_log["scraped_ok"] = Series(True, index=scrape_metadata_log.index, dtype="bool[pyarrow]")


    scrape_metadata_log = convert_dtypes_to_pyarrow(scrape_metadata_log, verbose=verbose)

    
    return scrape_metadata_log





def _process_machine_annotations_for_merge_w_logs(all_datasets, combined_log, verbose=False):

    from pandas import DataFrame
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow

    if len(combined_log) == 0:
        return DataFrame()


    # polishing the machine results data for merging with the log
    machine_annotations_for_log = all_datasets["annotated"][all_datasets["annotated"].item_id.isin(combined_log.item_id.unique())].copy()
    if verbose:
        print(f"Processing machine annotations {machine_annotations_for_log.shape} for log export. Log shape: {combined_log.shape}")

    machine_annotations_for_log.drop(columns=[
        "inference_ts","inference_duration","model","prompt_fn","error","finish_reason"], inplace=True, errors="ignore")

    #machine_annotations_for_log = machine_annotations_for_log.fillna("").copy()

    machine_annotations_for_log = machine_annotations_for_log.rename(columns={c:"G_"+c if not c=="item_id" else c for c in machine_annotations_for_log.columns}).copy()

    if verbose:
        print(f"Resulting machine_annotations_for_log shape {machine_annotations_for_log.shape}")


    #machine_annotations_for_log = _check_for_null_values_in_df(machine_annotations_for_log, verbose=verbose)

    machine_annotations_for_log["annotated_ok"] = ~machine_annotations_for_log["G_transcript_no_repetitions"].isna()
    machine_annotations_for_log["annotated_fail"] = machine_annotations_for_log["G_transcript_no_repetitions"].isna()


    machine_annotations_for_log = convert_dtypes_to_pyarrow(machine_annotations_for_log, verbose=verbose)

    return machine_annotations_for_log





def _combine_all_logs(
    cf = None,
    #study_name = None,
    all_datasets=None,
    #use_half_baked=False,
    verbose=False):
    

    from pandas import concat
    from os.path import exists, join
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io
    from pandas import NA as pd_NA

    if cf is None:
        cf = init_config()

    #if study_name is None:
    #    raise ValueError("study_name must be specified")
    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    

    baseline_log_simple = all_datasets.get("baseline")
    ddp_log = all_datasets.get("ddp")

    if not ddp_log is None and not baseline_log_simple is None:
        combined_log = concat([ddp_log,baseline_log_simple])
    elif not ddp_log is None:
        combined_log = ddp_log
    elif not baseline_log_simple is None:
        combined_log = baseline_log_simple
    else:
        raise ValueError("No DDP or baseline log found")

    if verbose:
        print(f" [{__name__}] Combined all logs to shape {combined_log.shape}.")




    # when combining logs from zeeschuimer with data donations, some columns that are only
    # relevant for one of the log types will obviously not be available for the other one. These columns
    # are not really 'missing' in a data sense, but we need to fill them with something to keep the 
    # data consistent. 
    ddp_cols_isna = [c for c in combined_log.columns if c.startswith("D_") and combined_log[c].isna().any()]

    baseline_cols_isna = [c for c in combined_log.columns if c.startswith("B_") and combined_log[c].isna().any()]
    if verbose:
        print(f" [{__name__}] DDP cols with missing values: {ddp_cols_isna}")
        print(f" [{__name__}] Baseline cols with missing values: {baseline_cols_isna}")


    # Convert categorical columns to string to avoid fillna errors
    for col in combined_log.select_dtypes(include=['category']).columns:
        print(f" [{__name__}] Converting category column {col} to pyarrow string...")
        combined_log[col] = combined_log[col].astype("string[pyarrow]")

    for one_ddp_col in ddp_cols_isna:
        if combined_log[one_ddp_col].dtype == "string[pyarrow]":
            combined_log[one_ddp_col] = combined_log[one_ddp_col].fillna("BASELINE")
        elif combined_log[one_ddp_col].dtype in ["double[pyarrow]","int64[pyarrow]"]:
            combined_log[one_ddp_col] = combined_log[one_ddp_col].fillna(-1)
        else:
            combined_log[one_ddp_col] = combined_log[one_ddp_col].fillna(pd_NA)

    for one_baseline_col in baseline_cols_isna:
        if combined_log[one_baseline_col].dtype == "string[pyarrow]":
            combined_log[one_baseline_col] = combined_log[one_baseline_col].fillna("DDP")
        elif combined_log[one_baseline_col].dtype in ["double[pyarrow]","int64[pyarrow]"]:
            combined_log[one_baseline_col] = combined_log[one_baseline_col].fillna(-1)
        else:
            combined_log[one_baseline_col] = combined_log[one_baseline_col].fillna(pd_NA)


    # TODO: This is a horrible patch. I've probably fixed the cause by now...
    combined_log['T_local_day_segment'] = combined_log['T_local_day_segment'].astype("string[pyarrow]")

    combined_log = convert_dtypes_to_pyarrow(combined_log, verbose=verbose)


    return combined_log





def merge_all_study_datasets(
    cf = None,
    #study_name = None,
    all_datasets=None,   
    #ONLY_MERGE_LOG_EVENTS_THAT_ARE_SCRAPED_AND_ANNOTATED = True,
    verbose=False
):
    ### merge log with enriched metadata (scraped and annotated)

    from pandas import merge, to_datetime, Series


    print(f"Merging all datasets...")


    combined_log = _combine_all_logs(cf = cf, all_datasets=all_datasets, verbose=verbose)

    scrape_metadata_log = _process_scrape_metadata_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)
    machine_annotations_for_log = _process_machine_annotations_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)

    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose, consolidate = True))


    #if ONLY_MERGE_LOG_EVENTS_THAT_ARE_SCRAPED_AND_ANNOTATED:
    #    if verbose:
    #        print("Only keeping events in the merged log that have been both scraped and annotated")
    #    the_how = 'inner'
    #else:
    #    if verbose:
    #        print("Adding enriched data and keeping log events even if enriched data is missing")
    #    the_how = 'left'
    the_how = 'left'

    outdata = merge(left=combined_log, right=rename_columns(scrape_metadata_log), on='item_id',how=the_how)
    outdata = merge(left=outdata, right=rename_columns(machine_annotations_for_log), on='item_id',how=the_how)

    outdata["scraped_ok"] = outdata["scraped_ok"].fillna(False)
    outdata["annotated_ok"] = outdata["annotated_ok"].fillna(False)
    outdata["annotated_fail"] = outdata["annotated_fail"].fillna(False)
    outdata["scraped_fail"] = outdata["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")


    # Create a new column by calculating the difference between 'T_local_timestamp' and 'S_createTime'.
    # Ensure both are proper datetime types (not object) before subtraction
    # TODO: This needs to be more dynamic and not make direct references to variable name
    t_timestamp = outdata["T_local_timestamp"]
    s_createtime = outdata["S_createTime"]
    
    t_timestamp = to_datetime(t_timestamp, utc=True).convert_dtypes(dtype_backend="pyarrow")
    s_createtime = to_datetime(s_createtime, utc=True).convert_dtypes(dtype_backend="pyarrow")
    
    # Now we can subtract them
    # TODO: This needs to be more dynamic and not make direct references to variable name
    outdata["T_days_since_created"] = (t_timestamp - s_createtime).dt.days
    outdata["T_days_since_created"] = outdata["T_days_since_created"].convert_dtypes(dtype_backend="pyarrow")

    if verbose:
        print(f"Adding 'days_since_created' column. Resulting output log DF shape {outdata.shape}")
        #print("--"*60)

    print(f"...done. Merged all datasets. Shape: {outdata.shape}")

    return outdata







"""def filter_log_against_sampled_donation_groups(
    cf = None,
    all_datasets = None,
    outdata = None,
    MAX_DAILY_MISSING_DATA_RATIO = 0.3,
    verbose=False
):

    from pandas import merge, concat
    from fyp.recode_variables import get_factors_and_features_from_var_scheme
    from fyp.fyp_main import init_config

    if cf is None:
        cf = init_config()
    

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    if outdata is None:
        raise ValueError("outdata must be specified")

    if not "sampled_data_ddp_events" in all_datasets:
        if verbose:
            print("Not filtering donation events against sampled donation groups")
        return outdata



    fyp_factors, _ = get_factors_and_features_from_var_scheme(cf = cf, some_events_df = outdata, verbose=verbose)

    outdata_filtered = outdata.copy()
    if verbose:
        print(f"Rows at this stage: {len(outdata_filtered):,}")

    # set up a filter to filter out only the DDP events that are in the DDP sample
    # TODO: This needs to be more dynamic and not make direct references to variable name
    fine_filter = all_datasets["sampled_data_ddp_events"].copy()
    
    #fine_filter.rename(columns={"donation_id":"D_donation_id","local_timestamp":"T_local_timestamp"}, inplace=True)
    fine_filter = fine_filter.drop_duplicates(subset=["D_donation_id","T_local_timestamp","item_id"])
    fine_filter = fine_filter.set_index(["D_donation_id","T_local_timestamp","item_id"]).copy()

    fine_filter = fine_filter[["D_sample_id"]].rename(columns={"D_sample_id":"whatever"}).copy()
    #return fine_filter




    # use this filter to create a new version of outdata
    outdata_filtered = merge(
        left=fine_filter,
        right=outdata.set_index(["D_donation_id","T_local_timestamp","item_id"]), #TODO: avoid direct references to variable names
        left_index=True, right_index=True, how="inner")
    outdata_filtered = outdata_filtered.reset_index().drop("whatever",axis=1).copy() #TODO: avoid direct references to variable names


    if verbose:
        print(f"After matching the export ddp events against the sampled donation-date groups, we have {len(outdata_filtered):,} ddp events in the export log")

    return outdata_filtered

    # group the filter and the filtered_outdata to compare how many items were in the sample and how many
    # have been sampled and annotated. 
    check_missing_data = concat([
        fine_filter.reset_index().rename(columns={"item_id":"target_count"}).groupby(["D_donation_id","whatever"])["target_count"].count(),
        outdata_filtered.rename(columns={"item_id":"real_count"}).groupby(["D_donation_id","T_local_date"])["real_count"].count()
    ], axis=1)

    # calculate a missing data ratio and an index based on a max daily missing data ratio
    check_missing_data["missing_ratio"] = 1 - check_missing_data["real_count"] / check_missing_data["target_count"]
    okay_dates_index = check_missing_data[check_missing_data["missing_ratio"]<MAX_DAILY_MISSING_DATA_RATIO].index

    # use the okay dates to get rid of dates with too much missing data
    outdata_filtered = outdata_filtered.set_index(["D_donation_id","T_local_date"]).loc[okay_dates_index,:].reset_index().copy() #TODO: avoid direct references to variable names

    sampled_ddp_count = len(all_datasets["sampled_data_ddp_events"])
    if verbose:
        print(
            f"After dropping dates with too high missing data ratio, we have {len(outdata_filtered):,} ddp events in the export log,\n"
            f"which should be compared to {sampled_ddp_count:,} ddp events in the sampled donation-date groups")


    if verbose:
        print("Putting back the baseline data...")
    outdata_filtered = concat([outdata_filtered,outdata[outdata['D_donation_id']=='BASELINE']]) #TODO: avoid direct references to variable names

    if verbose:
        print(f"...making the total number of events (BASELINE and DDP) in the export data log to {len(outdata_filtered):,} events.")
        #print("--"*60)
    
    return outdata_filtered"""








"""def save_logs(
    cf = None,
    study_name = None,
    outdata_filtered = None,
    file_label = "",
    verbose=False):

    from datetime import datetime
    from os.path import join
    from pandas import Timestamp
    from fyp.fyp_main import init_config, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io
    from numpy import float64 as np_float64

    if cf is None:
        cf = init_config()

    if study_name is None:
        raise ValueError("study_name must be specified")
    
    file_format = cf['misc']['file_format']


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
            if outdata_filtered[n].dtype in [object,"string[pyarrow]"]:
                if n.startswith("S_"):
                    outdata_filtered[n] = outdata_filtered[n].fillna("not scraped")
                elif n.startswith("G_"):
                    outdata_filtered[n] = outdata_filtered[n].fillna("not annotated")
            elif outdata_filtered[n].dtype in [float,np_float64]:
                outdata_filtered[n] = outdata_filtered[n].fillna(-1)
            #elif is_datetime64_any_dtype(outdata_filtered[n]):
            #    outdata_filtered["S_createTime"] = outdata_filtered["S_createTime"].fillna(Timestamp(year=2100,month=1,day=1))

    if len(file_label)>0 and file_label[-1] != "_":
        file_label += "_"

    log_filename = f"{study_name}_{file_label}LOG{cf['misc']['file_format']}"

    outdata_filtered = data_io.save_parquet(cf, outdata_filtered, "exports", log_filename, verbose=verbose)
    if verbose:
        print(f"Exported {len(outdata_filtered):,} events to '{log_filename}'.")
        print(f"Date range: {outdata_filtered.T_local_date.min()} -- {outdata_filtered.T_local_date.max()}")
        print(f"Now: {datetime.now()}")
        #print("--"*60)"""







def create_study_main_dataset(
    cf = None,
    study_name = None,
    all_datasets = None,
    load_from_cache = False,
    save_to_cache = False,
    verbose = False
    ):

    from fyp.fyp_main import init_config

    if cf is None:
        cf = init_config()

    if study_name is None:
        raise ValueError("study_name must be specified")

    #print("--"*60)
    print(f"Generating unified dataset for study '{study_name}'")
    #print("--"*60)

    all_datasets = load_datasets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        load_from_cache = load_from_cache,
        save_to_cache = save_to_cache,
        verbose = verbose)


    """combined_log = process_and_combine_logs_for_merge_w_logs(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        #use_half_baked = False,
        verbose=verbose
        )"""


    study_main_dataset = merge_all_study_datasets(
        cf = cf,
        all_datasets = all_datasets,
        verbose=verbose
    )


    memory_per_column = study_main_dataset.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {total_memory_mb:.2f} MB")



    return study_main_dataset


    """if False and cf['study_defs'][study_name]['INCLUDE_DONATIONS'].lower() == "sample":
        outdata_filtered = filter_log_against_sampled_donation_groups(
            cf = cf,
            all_datasets = all_datasets,
            outdata = outdata,
            MAX_DAILY_MISSING_DATA_RATIO = 0.3,
        verbose=verbose
        )
    else:
        outdata_filtered = outdata

    save_logs(
        cf = cf,
        study_name = study_name,
        outdata_filtered = study_event_log_df,
        file_label = "",
        verbose=verbose)"""







def save_logs_as_csv(
    cf = None,
    study_name = None,
    outdata_filtered = None,
    file_label = "",
    verbose=False):

    from datetime import datetime
    from os.path import join
    from fyp.fyp_main import init_config
    from numpy import float64 as np_float64

    if cf is None:
        cf = init_config()

    if study_name is None:
        raise ValueError("study_name must be specified")
    if outdata_filtered is None:
        raise ValueError("outdata_filtered must be specified")

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
        #if verbose:
            #print("--"*60)
        log_as_csv_filename = study_name + "_" + "_LOG.csv"
        outdata_for_csv_export = outdata_filtered.copy()

        # Vectorized string cleaning - chain multiple replacements
        if verbose:
            print("Cleaning string data...")
        string_cols = outdata_for_csv_export.select_dtypes(exclude=['number']).columns
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
        string_cols = outdata_for_csv_export.select_dtypes(exclude=['number']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = outdata_for_csv_export[col].apply(_clean_surrogates)

        # all numbers except for those related to session stats can be integers, so let's retype those
        some_float_cols = [c for c in outdata_for_csv_export.select_dtypes(include=[float, np_float64]).columns if not "session" in c]
        outdata_for_csv_export[some_float_cols] = outdata_for_csv_export[some_float_cols].fillna(value=-1).astype(int)


        # VECTORIZED: Convert long numbers to strings for Excel
        # Use string operations instead of map
        for c in ["B_data_author_id","item_id","S_music_id","S_author_id","D_ts_jiggled"]:
            if c in outdata_for_csv_export.columns:
                # Faster: use str accessor to add quotes
                outdata_for_csv_export[c] = "'" + outdata_for_csv_export[c].astype(str) + "'"
            

        # VECTORIZED: Build TikTok URLs using string concatenation
        outdata_for_csv_export["tiktok_url"] = "https://www.tiktok.com/@/video/" + outdata_for_csv_export["item_id"] + "/"



        # Export with error handling for any remaining encoding issues
        outdata_for_csv_export.to_csv(join(cf['paths']['exports'],log_as_csv_filename), errors='replace')
        if verbose:
            print(f"Exported {len(outdata_for_csv_export):,} observations in {log_as_csv_filename}.")
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_date.min()} -- {outdata_filtered.T_local_date.max()}")
            print(f"Now: {datetime.now()}")

        #if verbose:
            #print("--"*60)



