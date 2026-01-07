
from zoneinfo import ZoneInfo
from numpy import int64 as np_int64







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





def extract_local_time_features(
    cf = None,
    some_events_df_in = None,
    kind_of_log = None,
    verbose = False):
    """
    Integrates per-donation timezone offsets from persona_stats_cache.
    """
    from pandas import concat, to_datetime, notna as pd_notna, NaT as pd_NaT, to_timedelta
    from numpy import select as np_select
    from fyp.fyp_main import initialize
    import fyp.data_io as data_io
    from os.path import join, exists
    from datetime import datetime

    if cf is None:
        cf = initialize()



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
        time_zone = cf["misc"]["TIME_ZONE"]
        try:
            now_local = datetime.now(ZoneInfo(time_zone))
            default_offset_hours = now_local.utcoffset().total_seconds() / 3600.0
        except Exception as e:
            if verbose: print(f"Warning: Could not determine offset for {time_zone}, defaulting to 0. {e}")
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


    df["local_day_segment"] = _day_segment_from_hour(df["local_hour"]).astype("string[pyarrow]")

    # Optimization: Use .dt.date directly (faster than map)
    df["local_date"] = ts.dt.date
    df["local_date_str"] = df["local_date"].astype("string[pyarrow]")


    print("...done")

    return df








def load_datasets(
    cf = None,
    study_name = None,
    all_datasets = {},
    consolidate = False,
    load_from_cache = False,
    save_to_cache = False,
    verbose=False
    ):


    from os.path import join as os_join, exists as os_exists
    from fyp.machine_annotation import load_machine_annotations
    from fyp.donations import load_special_donations, load_ddp_events
    from fyp.zeeschuimer import load_zeeschuimer_data
    from fyp.scrape import load_scrape_metadata
    from fyp.fyp_main import initialize
    import fyp.data_io as data_io
    from copy import deepcopy
    from datetime import datetime
    from pandas import read_parquet as pd_read_parquet


    if study_name is None:
        raise ValueError("study_name must be specified")
    if cf is None:
        cf = initialize()

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
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






def select_videos_from_study_dataset(
    cf = None,
    study_data = None,
    query_string = "",
    verbose = False,
    notebook_mode = False
    ):

    from fyp.fyp_main import initialize
    from pandas import NamedAgg

    if study_data is None:
        raise ValueError("study_data must be specified")
    if cf is None:
        cf = initialize()

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








def generate_and_check_unique_videos_to_scrape_and_annotate(
    cf = None, 
    study_data = None, 
    verbose = False
    ):

    from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()
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








def rename_columns(
    some_events
    ):
    
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










def _process_scrape_metadata_for_merge_w_logs(
    all_datasets,
    combined_log,
    verbose=False
    ):

    from pandas import isna as pd_isna, Timestamp, DataFrame, to_datetime, Series, NA as pd_NA
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow

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



    scrape_metadata_log["scraped_ok"] = Series(True, index=scrape_metadata_log.index, dtype="bool[pyarrow]")


    scrape_metadata_log = convert_dtypes_to_pyarrow(scrape_metadata_log, verbose=verbose)

    
    return scrape_metadata_log





def _process_machine_annotations_for_merge_w_logs(
    all_datasets,
    combined_log,
    verbose=False
    ):

    from pandas import DataFrame
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow

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
    all_datasets=None,
    verbose=False
    ):
    

    from pandas import concat
    from os.path import exists, join
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io
    from pandas import NA as pd_NA

    if cf is None:
        cf = initialize()

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
    all_datasets=None,   
    verbose=False
    ):
    ### merge log with enriched metadata (scraped and annotated)

    from pandas import merge, to_datetime, Series
    from fyp.scrape import load_failed_scrapes


    print(f"Merging all datasets...")


    combined_log = _combine_all_logs(cf = cf, all_datasets=all_datasets, verbose=verbose)

    scrape_metadata_log = _process_scrape_metadata_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)
    machine_annotations_for_log = _process_machine_annotations_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)

    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose, consolidate = True))


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









def create_study_main_dataset(
    cf = None,
    study_name = None,
    all_datasets = None,
    load_from_cache = False,
    save_to_cache = False,
    verbose = False
    ):

    from fyp.fyp_main import initialize

    if cf is None:
        cf = initialize()

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







def save_logs_as_csv(
    cf = None,
    study_name = None,
    outdata_filtered = None,
    file_label = "",
    verbose=False):

    from datetime import datetime
    from os.path import join
    from fyp.fyp_main import initialize
    from numpy import float64 as np_float64

    if cf is None:
        cf = initialize()

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
        if verbose:
            print("Cleaning surrogate characters from string data...")
        string_cols = outdata_for_csv_export.select_dtypes(exclude=['number']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = outdata_for_csv_export[col].apply(_clean_surrogates)

        # all numbers except for those related to session stats can be integers, so let's retype those
        some_float_cols = [c for c in outdata_for_csv_export.select_dtypes(include=[float, np_float64]).columns if not "session" in c]
        outdata_for_csv_export[some_float_cols] = outdata_for_csv_export[some_float_cols].fillna(value=-1).astype(int)


        # Convert long numbers to strings for Excel
        
        for c in ["B_data_author_id","item_id","S_music_id","S_author_id","D_ts_jiggled"]:
            if c in outdata_for_csv_export.columns:
                # Faster: use str accessor to add quotes
                outdata_for_csv_export[c] = "'" + outdata_for_csv_export[c].astype(str) + "'"
            

        # Build TikTok URLs
        outdata_for_csv_export["tiktok_url"] = "https://www.tiktok.com/@/video/" + outdata_for_csv_export["item_id"] + "/"


        # Export with error handling for any remaining encoding issues
        outdata_for_csv_export.to_csv(join(cf['paths']['exports'],log_as_csv_filename), errors='replace')
        if verbose:
            print(f"Exported {len(outdata_for_csv_export):,} observations in {log_as_csv_filename}.")
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_date.min()} -- {outdata_filtered.T_local_date.max()}")
            print(f"Now: {datetime.now()}")




