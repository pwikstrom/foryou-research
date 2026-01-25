
from zoneinfo import ZoneInfo
import pandas as pd
from fyp.fyp_main import initialize
import fyp.data_io as data_io
from fyp.donations import consolidate_ddp_logs
from fyp.zeeschuimer import consolidate_zeeschuimer_logs
from fyp.machine_annotation import consolidate_and_save_refined_annotations
from fyp.donations import load_donation_data, simple_sample_ddp_events
from fyp.scrape import consolidate_and_save_scrape_data, load_failed_scrapes
from fyp.zeeschuimer import load_zeeschuimer_data
from fyp.fyp_main import initialize, connect_to_google
import fyp.data_io as data_io
from copy import deepcopy
import datetime as _dt


#WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}










def load_study_datasets(
    cf = None,
    study_name = None,
    all_datasets = {},
    load_from_cache = True,
    save_to_cache = True,
    verbose=False
    ):

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    if not study_name in cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None and not load_from_cache:
        cf = connect_to_google(cf)

    print(f"Loading core datasets for study '{study_name}'...")

    # load core datasets from cache. This makes sense if the storage is remote. Since a slow network connection makes loading of datasets 
    # take a long time. If this is not a problem, there is really no need to use this option.
    if load_from_cache and not cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        tutti_data = {}
        cached_core_datasets = {}
        for k in ['scrape','machine_annotations','donations','zeeschuimer']:
            tutti_data[k] = None

            # if a core dataset exists in cache - check what it is and in case it can be used for this study - load it
            if data_io.exists(cf, "cache", f"core_{k}.parquet"):
                parquet_study_name = data_io.find_key_value_in_pq_metadata(cf=cf, storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                print(f"Found a cached version of '{k}' core dataset for study '{parquet_study_name}'")
                if parquet_study_name == study_name or parquet_study_name == 'everything':
                    if verbose:
                        print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'. Loading...")
                    cached_core_datasets[k] = parquet_study_name
                    tutti_data[k] = data_io.load_parquet(cf=cf, storage_location="cache", filename=f"core_{k}.parquet")


            # if no dataset was loaded from cache and the cache and main storage are at different locations, then load everything from
            #  main storage and save to cache. It will save time later since this can be used for all studies
            if tutti_data[k] is None and cf['data_io']['use_gcs_for_data']==True and cf['data_io']['use_gcs_for_cache']==False:
                print(f"Loading core dataset '{k}' from main storage and saving to cache")
                tutti_data[k] = data_io.load_parquet(cf=cf, storage_location="recoded", filename=f"{k}_recoded.parquet")
                print(f"Saving core dataset '{k}' to cache")
                tutti_data[k].attrs["study_name"] = 'everything'
                data_io.save_parquet(cf=cf, df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")

                
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")



    # --------------------------------------------------------------------
    # load activity data
    # --------------------------------------------------------------------

    # if zeeschuimer data is to be included in the analysis
    if cf["study_defs"][study_name].get("INCLUDE_ZEESCHUIMER_DATA",True):
        tutti_data["zeeschuimer"] = load_zeeschuimer_data(cf = cf, study_name = study_name, all_data = tutti_data.get("zeeschuimer", None), verbose=verbose)
    # if it should not be included, remove it from the dictionary if it exists
    elif "zeeschuimer" in tutti_data:
        del tutti_data["zeeschuimer"]


    # if donation data is to be included in the analysis
    if cf["study_defs"][study_name].get("INCLUDE_DONATION_DATA",True):
        tutti_data["donations"] = load_donation_data(cf = cf, study_name = study_name, all_data = tutti_data.get("donations", None), verbose=verbose)
    # if it should not be included, remove it from the dictionary if it exists
    elif "donations" in tutti_data:
        del tutti_data["donations"]

    if tutti_data.get("donations", None) is None and tutti_data.get("zeeschuimer", None) is None:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None

    # --------------------------------------------------------------------
    # sample donation data
    # --------------------------------------------------------------------
    enrichment_status = data_io.load_parquet(cf=cf, storage_location="recoded", filename="enrichment_status.parquet")

    sample_frame_setting = cf["study_defs"][study_name].get("DONATION_SAMPLE_FRAME", "off")

    # no sampling is performed if the sample frame setting is "off"
    if sample_frame_setting == "off":
        print(f"    [DD Sampling] Sample frame setting is 'off'. Not sampling donation data.")
        sample_frame = None
    
    # if the sample frame setting is "events", then the sample frame is the donation events, regardless if they are enriched or not
    elif sample_frame_setting == "events":
        sample_frame = tutti_data["donations"].copy()
        print(f"    [DD Sampling] Sample frame setting is 'events'. Using all {len(sample_frame):,} donation events as sample frame.")
    
    # if the sample frame setting is "scraped", then the sample frame is the donation events that are scraped
    elif sample_frame_setting == "scraped":
        selected_videos = enrichment_status[enrichment_status["scraped_ok"]].index.tolist()
        sample_frame = tutti_data["donations"][tutti_data["donations"]["item_id"].isin(selected_videos)].copy()
        print(f"    [DD Sampling] Sample frame setting is 'scraped'. Using only {len(sample_frame):,} donation events that are scraped as sample frame.")
    
    # if the sample frame setting is "annotated", then the sample frame is the donation events that are annotated
    elif sample_frame_setting == "annotated":
        selected_videos = enrichment_status[enrichment_status["annotated_ok"]].index.tolist()
        sample_frame = tutti_data["donations"][tutti_data["donations"]["item_id"].isin(selected_videos)].copy()
        print(f"    [DD Sampling] Sample frame setting is 'annotated'. Using only {len(sample_frame):,} donation events that are annotated as sample frame.")

    # perform the sampling if a sample frame was defined
    if sample_frame is not None:
        tutti_data["donations"] = simple_sample_ddp_events(
            cf = cf, 
            study_name = study_name, 
            all_ddp_events_df = sample_frame, 
            verbose = verbose)




    # --------------------------------------------------------------------
    # load scraped and annotated data
    # --------------------------------------------------------------------

    # I only want to download the enrichment data that are needed for this particular study. So I check which videos are in the
    # activity datasets, and use that to filter the enrichment metadata. 
    unique_videos = set()
    if "zeeschuimer" in tutti_data:
        unique_videos = unique_videos | set(tutti_data["zeeschuimer"]["item_id"].dropna().values.tolist())
    if "donations" in tutti_data:
        unique_videos = unique_videos | set(tutti_data["donations"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in donation and zeeschuimer datasets")

    # If the study is the special 'everything' study then I don't need to do this.
    if study_name == 'everything':
        sel = None
    else:
        sel = [("item_id", "in", list(unique_videos))]



    # --------------------------------------------------------------------
    # load scraped data
    # --------------------------------------------------------------------
    if tutti_data.get("scrape") is None:
        print("    [Scrape] Loading scraped data from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data["scrape"] = data_io.load_parquet(cf=cf, storage_location="recoded", filename="scrape_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose:print(" ...done")
    else:
        print(f"    [Scrape] There are {len(tutti_data['scrape']):,} scraped data items in the cache", end="", flush=True)
        tutti_data["scrape"] = tutti_data["scrape"][tutti_data["scrape"]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data['scrape']):,} of those overlap with the activity datasets for this study.")    

    # --------------------------------------------------------------------
    # load machine annotations
    # --------------------------------------------------------------------
    if tutti_data.get("machine_annotations") is None:
        print("    [Machine annotations] Loading machine annotations from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data["machine_annotations"] = data_io.load_parquet(cf=cf, storage_location="recoded", filename="machine_annotations_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose: print(" ...done")

    else:
        print(f"    [Machine annotations] There are {len(tutti_data['machine_annotations']):,} annotations in the cache", end="", flush=True)
        tutti_data["machine_annotations"] = tutti_data["machine_annotations"][tutti_data["machine_annotations"]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data['machine_annotations']):,} of those overlap with the activity datasets for this study.")


    def _df_size(df):
        memory_per_column = df.memory_usage(deep=True) 
        total_memory_bytes = memory_per_column.sum()
        total_memory_mb = total_memory_bytes / (1024**2)
        return total_memory_mb

    # save the core datasets to cache if requested
    """if save_to_cache and not cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        t1 = _dt.datetime.now()
        if verbose:
            print("    [Core datasets] Saving datasets to cache...")
        for k in tutti_data:
            if k in cached_core_datasets and cached_core_datasets[k] in ['everything', study_name]:
                if verbose:
                    print(f"    [Core datasets] Cached 'everything' dataset for '{k}' already exists. No need to replace it with this dataset.")
                continue
            tutti_data[k].attrs["study_name"] = study_name
            data_io.save_parquet(cf=cf, df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")
        if verbose:
            print(f"    [Core datasets] ...done. (Took me {(_dt.datetime.now() - t1).total_seconds():.1f} seconds)")"""

    if verbose:
        print("    [Core datasets] Datasets:")
        dataset_info = "\n    [Core datasets] - ".join([f"'{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)" for k in tutti_data])
        print(f"    [Core datasets] - {dataset_info}")


    print(f"...done. Core datasets loaded for study '{study_name}'")


    return tutti_data

















def load_donation_datasets(
    cf = None,
    donation_id = None,
    load_from_cache = True,
    verbose=False
    ):


    if cf is None:
        cf = initialize()


    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None and not load_from_cache:
        cf = connect_to_google(cf)

    print(f"Loading core datasets for donation '{donation_id}'...")

    # load core datasets from cache. This makes sense if the storage is remote. Since a slow network connection makes loading of datasets 
    # take a long time. If this is not a problem, there is really no need to use this option.
    if load_from_cache and not cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        tutti_data = {}
        cached_core_datasets = {}
        for k in ['scrape','machine_annotations','donations']:
            tutti_data[k] = None

            # if a core dataset exists in cache - check what it is and in case it can be used for this study - load it
            if data_io.exists(cf, "cache", f"core_{k}.parquet"):
                parquet_study_name = data_io.find_key_value_in_pq_metadata(cf=cf, storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                print(f"Found a cached version of '{k}' core dataset for study '{parquet_study_name}'")
                if parquet_study_name == 'everything':
                    if verbose:
                        print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'. Loading...")
                    cached_core_datasets[k] = parquet_study_name
                    tutti_data[k] = data_io.load_parquet(cf=cf, storage_location="cache", filename=f"core_{k}.parquet")


            # if no dataset was loaded from cache and the cache and main storage are at different locations, then load everything from
            #  main storage and save to cache. It will save time later since this can be used for all studies
            if tutti_data[k] is None and cf['data_io']['use_gcs_for_data']==True and cf['data_io']['use_gcs_for_cache']==False:
                print(f"Loading core dataset '{k}' from main storage and saving to cache")
                tutti_data[k] = data_io.load_parquet(cf=cf, storage_location="recoded", filename=f"{k}_recoded.parquet")
                print(f"Saving core dataset '{k}' to cache")
                tutti_data[k].attrs["study_name"] = 'everything'
                data_io.save_parquet(cf=cf, df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")

                
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")



    # --------------------------------------------------------------------
    # load activity data
    # --------------------------------------------------------------------


    tutti_data["donations"] = tutti_data["donations"][tutti_data["donations"]["D_donation_id"] == donation_id]

    unique_videos = set(tutti_data["donations"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in donation and zeeschuimer datasets")

    # If the study is the special 'everything' study then I don't need to do this.
    sel = [("item_id", "in", list(unique_videos))]



    # --------------------------------------------------------------------
    # load scraped data
    # --------------------------------------------------------------------
    print(f"    [Scrape] There are {len(tutti_data['scrape']):,} scraped data items in the cache", end="", flush=True)
    tutti_data["scrape"] = tutti_data["scrape"][tutti_data["scrape"]["item_id"].isin(unique_videos)].copy()
    print(f" and {len(tutti_data['scrape']):,} of those overlap with the activity datasets for this study.")    

    # --------------------------------------------------------------------
    # load machine annotations
    # --------------------------------------------------------------------
    print(f"    [Machine annotations] There are {len(tutti_data['machine_annotations']):,} annotations in the cache", end="", flush=True)
    tutti_data["machine_annotations"] = tutti_data["machine_annotations"][tutti_data["machine_annotations"]["item_id"].isin(unique_videos)].copy()
    print(f" and {len(tutti_data['machine_annotations']):,} of those overlap with the activity datasets for this study.")


    def _df_size(df):
        memory_per_column = df.memory_usage(deep=True) 
        total_memory_bytes = memory_per_column.sum()
        total_memory_mb = total_memory_bytes / (1024**2)
        return total_memory_mb


    if verbose:
        print("    [Core datasets] Datasets:")
        dataset_info = "\n    [Core datasets] - ".join([f"'{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)" for k in tutti_data])
        print(f"    [Core datasets] - {dataset_info}")


    print(f"...done. Core datasets loaded for donation '{donation_id}'")


    return tutti_data













def _build_agg_dict_to_generate_basic_video_stats(study_dataset = None):
    from pandas import NamedAgg

    # Check that each columns exists and gradually build the aggregation based on what columns are available
    agg_defs = {
        "nunique_donations": ("D_donation_id", "nunique"),
        "total_observations": ("D_donation_id", "count"),
        "scraped_ok": ("scraped_ok", "first"),
        "scraped_fail": ("scraped_fail", "first"),
        "annotated_ok": ("annotated_ok", "first"),
        "annotated_fail": ("annotated_fail", "first"),
        "video_duration": ("S_video_duration", "max"),
    }

    if study_dataset is None:
        source_cols = list(set(["item_id"] + [source_col for _, (source_col, _) in agg_defs.items()]))
        return None, list(set(source_cols))

    agg_dict = {}
    confirmed_cols = ["item_id"]
    for target_col, (source_col, agg_func) in agg_defs.items():
        if source_col in study_dataset.columns:
            agg_dict[target_col] = NamedAgg(column=source_col, aggfunc=agg_func)
            confirmed_cols.append(source_col)
    return agg_dict, list(set(confirmed_cols))




def select_videos_from_study_dataset(
    cf = None,
    study_dataset = None,
    query_string = "",
    verbose = False,
    notebook_mode = False
    ):

    from fyp.fyp_main import initialize

    if study_dataset is None:
        raise ValueError("study_dataset must be specified")
    if cf is None:
        cf = initialize()


    # group by video URL and count the number of unique users
    agg_dict, confirmed_cols = _build_agg_dict_to_generate_basic_video_stats(study_dataset)

    video_stats = study_dataset[confirmed_cols].groupby('item_id').agg(**agg_dict)

    if "video_duration" in video_stats.columns:
        video_stats['duration_ok_to_annotate'] = (video_stats['video_duration'] <= cf["machine"]["max_duration_for_annotation"]).fillna(False)
        video_stats.drop(columns=["video_duration"], inplace=True)
    else:
        # If duration information is missing, default to False (safer not to annotate unknown duration)
        video_stats['duration_ok_to_annotate'] = False

    video_stats.fillna(False, inplace=True)

    video_stats.query(query_string, inplace=True)

    return video_stats








def generate_unique_videos_to_scrape_and_annotate(
    cf = None,
    study_name = None,
    study_dataset = None,
    load_from_cache = True,
    save_to_cache = True,
    verbose = False
    ):



    print(f"Generating unique videos to scrape and annotate...")

    if study_name is None and study_dataset is None:
        print("  This process cannot run without a study name or a study dataset as input. Process failed.")
        return None

    if cf is None:
        cf = initialize()

    if load_from_cache and study_name is not None:
        #study_dataset_cache_path = os_join(cf['paths']['cache'], f"{study_name}_recoded.parquet")
        if data_io.exists(cf=cf, storage_location="cache", filename=f"{study_name}_recoded.parquet"):
            if verbose:
                print(f"    Loading study recoded dataset from cache...", end=" ", flush=True)
            
            #schema = pq_read_schema(study_dataset_cache_path)
            #confirmed_cols = list(set(schema.names) & set(_build_agg_dict_to_generate_basic_video_stats()[1]))
            #print(study_dataset_cache_path)

            study_dataset = data_io.load_parquet(
                cf=cf, 
                filename=f"{study_name}_recoded.parquet", 
                storage_location="cache")
            if verbose:
                print(f"Shape: {study_dataset.shape}")
        else:
            print("@@ No cached study dataset found. I must run the process to create it. Please wait a moment...")
            study_dataset = create_study_recoded_dataset(
                cf = cf,
                study_name = study_name,
                load_from_cache = True,
                save_to_cache = True,
                verbose = verbose
            )
            if study_dataset is None:
                raise ValueError("No study dataset found for study '{study_name}'")
            confirmed_cols = list(set(study_dataset.columns) & set(_build_agg_dict_to_generate_basic_video_stats()[1]))
            study_dataset = study_dataset[confirmed_cols].copy()
            print("@@ I'm back after having created the unified study dataset. I will now resume generating unique videos to scrape and annotate.")


    if study_dataset is None:
        print("    This process cannot run without a study dataset. Process failed.")
        return None

    study_dataset_small = study_dataset[["item_id","S_video_duration","annotated_ok","annotated_fail","scraped_ok","scraped_fail"]].copy()

    selected_annotate_videos = select_videos_from_study_dataset(
        cf = cf,
        study_dataset = study_dataset_small,
        query_string = "scraped_ok & ~annotated_ok & ~annotated_fail & duration_ok_to_annotate",
        verbose = verbose,
        notebook_mode = False)

    selected_scrape_videos = select_videos_from_study_dataset(
        cf = cf,
        study_dataset = study_dataset_small,
        query_string = "~scraped_ok & ~scraped_fail",
        verbose = verbose,
        notebook_mode = False)
    
    if save_to_cache:
        t1 = _dt.datetime.now()
        if verbose:
            print("  Saving datasets to cache...")
        selected_annotate_videos.attrs['study_name'] = study_name
        selected_scrape_videos.attrs['study_name'] = study_name
        data_io.save_parquet(
            cf=cf,
            df=selected_annotate_videos,
            storage_location="cache",
            filename=f"{study_name}_unique_items_to_annotate.parquet")
        data_io.save_parquet(
            cf=cf,
            df=selected_scrape_videos,
            storage_location="cache",
            filename=f"{study_name}_unique_items_to_scrape.parquet")
        #selected_annotate_videos.to_parquet(os_join(cf['paths']['cache'], f"{study_name}_unique_items_to_annotate.parquet"), engine='pyarrow')
        #selected_scrape_videos.to_parquet(os_join(cf['paths']['cache'], f"{study_name}_unique_items_to_scrape.parquet"), engine='pyarrow')
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds")

    return {
        "annotate": selected_annotate_videos,
        "scrape": selected_scrape_videos
    }









def check_unique_videos_to_scrape_and_annotate(
    cf = None,
    study_name = None,
    load_from_cache = True,
    save_to_cache = True,
    verbose = False
    ):

    from fyp.fyp_main import initialize
    #from os.path import exists as os_exists, join as os_join


    print(f"Checking unique videos to scrape and annotate...")

    interesting_videos = generate_unique_videos_to_scrape_and_annotate(
        cf = cf,
        study_name = study_name,
        load_from_cache = load_from_cache,
        save_to_cache = save_to_cache,
        verbose = verbose)


    return {
        "annotate": interesting_videos["annotate"].shape,
        "scrape": interesting_videos["scrape"].shape
    }







def update_enrichment_status(
    cf:dict | None = None,
    all_datasets:dict = {},
    verbose:bool = False):
    
    if cf is None:
        cf = initialize()


    enrichment_status_df = pd.concat([
            all_datasets["zeeschuimer_logs"][['item_id','D_donation_id']],
            all_datasets["ddp_logs"][['item_id','D_donation_id']]
        ]).groupby("item_id").agg(
            nunique_donations=pd.NamedAgg(column="D_donation_id", aggfunc="nunique"),
            total_observations=pd.NamedAgg(column="D_donation_id", aggfunc="count")
        )
        
    enrichment_status_df["nunique_donations"] = enrichment_status_df["nunique_donations"].astype("int64[pyarrow]")

    enrichment_status_df.reset_index(inplace=True)

    most_common_item_id_length = enrichment_status_df.item_id.str.len().value_counts().index[0]
    enrichment_status_df = enrichment_status_df[enrichment_status_df.item_id.str.len()==most_common_item_id_length].copy()

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets['scrape_data'][['item_id','scraped_ok','S_video_downloaded']], on='item_id', how='left')

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets['annotations'][['item_id','annotated_ok','annotated_fail']], on='item_id', how='left')

    failed_scrapes = load_failed_scrapes(cf)

    failed_scrapes = pd.DataFrame(failed_scrapes, columns=["item_id"])
    failed_scrapes["scrape_fail"] = True

    failed_scrapes = failed_scrapes.convert_dtypes(dtype_backend="pyarrow")

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=failed_scrapes, on="item_id", how="left").copy()

    enrichment_status_df.set_index("item_id", inplace=True)

    data_io.save_parquet(cf=cf, df=enrichment_status_df, storage_location="recoded", filename="enrichment_status.parquet", verbose=verbose)

    return enrichment_status_df










def _consolidate_and_save_activity_logs(cf = None, force_consolidation=False, verbose=False):

    if cf is None:
        cf = initialize()
    
    print("\n*** Zeeschuimer")
    new_z, z1 = consolidate_zeeschuimer_logs(cf = cf, force_consolidation=False)#force_consolidation, verbose=verbose)
    print("\n*** Donations")
    new_d, d1 = consolidate_ddp_logs(cf = cf, force_consolidation=force_consolidation, consolidate_from_scratch=True, verbose = verbose)

    # I need all columns in both datasets. When concatenating, the columns will be created with null values.
    # But sometimes the separate datasets will be used on its own. And in those situations I need to match
    # up the columns as I'm doing here. Previously I had some idea that the new columns should not be NA,
    # which is why the code is a bit complex. I've kept it since I might change my mind again.  
    if new_z or new_d:
        print("\nMatching up columns between zeeschuimer and donation data...")
        for c in set(z1.columns) | set(d1.columns):
            if not c in z1.columns:
                if pd.api.types.is_numeric_dtype(d1[c]):
                    if verbose:
                        print(f"    Adding {c} to z1 | numeric")
                    z1[c] = pd.Series(pd.NA, index=z1.index, dtype="int64[pyarrow]")
                else:
                    if verbose:
                        print(f"    Adding {c} to z1 | string")
                    z1[c] = pd.Series(pd.NA, index=z1.index, dtype="string[pyarrow]")
            if not c in d1.columns:
                if pd.api.types.is_numeric_dtype(z1[c]):
                    if verbose:
                        print(f"    Adding {c} to d1 | numeric")
                    d1[c] = pd.Series(pd.NA, index=d1.index, dtype="int64[pyarrow]")
                else:
                    if verbose:
                        print(f"    Adding {c} to d1 | string")
                    d1[c] = pd.Series(pd.NA, index=d1.index, dtype="string[pyarrow]")
        print(f"...done matching columns Zeeshuimer shape {z1.shape} and DDP shape {d1.shape}")

    if new_z:
        print(f"Saving Zeeschuimer dataset. Shape {z1.shape} to 'recoded' folder")
        _ = data_io.save_parquet(cf, z1, "recoded", "zeeschuimer_recoded.parquet", verbose=verbose)

    if new_d:
        print(f"Saving DDP dataset. Shape {d1.shape} to 'recoded' folder")
        _ = data_io.save_parquet(cf, d1, "recoded", "donations_recoded.parquet", verbose=verbose)
    
    if new_z or new_d:
        print("...done saving datasets")
    
    return (new_z, z1), (new_d, d1)










def consolidate_fyp_core_data(cf = None, force_consolidation=False, verbose=False):

    if cf is None:
        cf = initialize()

    (new_zeeschuimer_logs, zeeschuimer_logs), (new_ddp_logs, ddp_logs) = _consolidate_and_save_activity_logs(cf=cf, 
                                                                                                            force_consolidation=force_consolidation,
                                                                                                            verbose=verbose)
    print("\n*** Annotations")
    (new_annotations, annotations) = consolidate_and_save_refined_annotations(cf=cf, 
                                                                            force_consolidation=force_consolidation,
                                                                            verbose=verbose)
    print("\n*** Scrape")
    (new_scrape_data, scrape_data) = consolidate_and_save_scrape_data(cf=cf, 
                                                                     force_consolidation=force_consolidation,
                                                                     verbose=verbose)

    fine_results = {
        "new_zeeschuimer_logs": new_zeeschuimer_logs,
        "zeeschuimer_logs": zeeschuimer_logs,
        "new_ddp_logs": new_ddp_logs,
        "ddp_logs": ddp_logs,
        "new_annotations": new_annotations,
        "annotations": annotations,
        "new_scrape_data": new_scrape_data,
        "scrape_data": scrape_data
        }

    print("\n*** Updating (and saving) data enrichment status...")
    update_enrichment_status(cf=cf, all_datasets=fine_results, verbose=verbose)
    print("...done.")


    return fine_results











def new_merge(
    cf = None,
    study_name = None,
    all_datasets = {},
    verbose = False,
    save_to_cache = True,
    ):

    print(f"Merging all datasets...")

    if study_name is None and save_to_cache == True:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    if not study_name in cf["study_defs"].keys() and save_to_cache == True:
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")


    if 'scrape' in all_datasets and 'machine_annotations' in all_datasets:
        enriched_data = pd.merge(left=all_datasets['scrape'], right=all_datasets['machine_annotations'], on='item_id', how='left')
    elif 'scrape' in all_datasets and 'machine_annotations' not in all_datasets:
        enriched_data = all_datasets['scrape']
    elif 'machine_annotations' in all_datasets and 'scrape' not in all_datasets:
        enriched_data = all_datasets['machine_annotations']
    else:
        enriched_data = pd.DataFrame()        
    
    if 'donations' in all_datasets and 'zeeschuimer' in all_datasets:
        activity_data = pd.concat([all_datasets['donations'], all_datasets['zeeschuimer']], ignore_index=True)
    elif 'donations' in all_datasets and 'zeeschuimer' not in all_datasets:
        activity_data = all_datasets['donations']
    elif 'zeeschuimer' in all_datasets and 'donations' not in all_datasets:
        activity_data = all_datasets['zeeschuimer']
    else:
        activity_data = pd.DataFrame()

    if len(activity_data) == 0:
        print("No activity data")
        return enriched_data
    if len(enriched_data) == 0:
        print("No enriched data")
        return activity_data
    
    shebang = pd.merge(left=activity_data, right=enriched_data, on='item_id', how='left')

    shebang["T_days_since_created"] = shebang["T_local_timestamp"] - shebang["S_createTime"]
    shebang["T_days_since_created"] = shebang["T_days_since_created"].map(lambda x: x.days if x is not pd.NA else pd.NA).astype("int64[pyarrow]")

    if verbose:
        print(f"Adding 'days_since_created' column. Resulting output log DF shape {shebang.shape}")

    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose))
    shebang["scraped_fail"] = shebang["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")
    
    def _safe_vector_divide(x, y):
        return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd.NA)
    shebang['plays_per_day'] = _safe_vector_divide(shebang['S_stats_playCount'],shebang['T_days_since_created'])


    if save_to_cache:
        t1 = _dt.datetime.now()
        if verbose:
            print(f"  Saving the '{study_name}' dataset to cache...")
        shebang.attrs['study_name'] = study_name
        data_io.save_parquet(
            cf=cf,
            df=shebang,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            asyncronous=False,
            verbose=verbose)
        #shebang.to_parquet(os_join(cf['paths']['cache'], f"{study_name}_recoded.parquet"), engine='pyarrow')
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds")


    print(f"...done. Merged all datasets. Shape: {shebang.shape}")


    return shebang









################################################################################################################################################
## Saturday morning, I need to build a cache refresh!
################################################################################################################################################


"""
def refresh_cache(cf = None, study_name = "chenglong"):
    if cf is None:
        cf = initialize()
    
    fyp_core = {}
    for fn in data_io.listdir(cf=cf, storage_location="recoded"):
        if fn.endswith("_recoded.parquet"):
            dataset_name = fn.replace("_recoded.parquet","")
            print(f"Found one core dataset: {dataset_name}")
            fyp_core[dataset_name] = data_io.load_parquet(
                cf=cf,
                storage_location="recoded",
                filename=fn)
    

        
    # with new merge, the datasets are already recoded
    study_recoded_dataset = new_merge(
        cf = cf,
        study_name = study_name,
        all_datasets = fyp_core,
        save_to_cache = True,
        verbose = True
    )


    memory_per_column = study_recoded_dataset.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {total_memory_mb:.2f} MB")


"""









def create_study_recoded_dataset(
    cf = None,
    study_name = None,
    all_datasets = {},
    save_to_cache = True,
    verbose = False
    ):

    from fyp.fyp_main import initialize, connect_to_google

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    if not study_name in cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)


    print(f"Generating unified dataset for study '{study_name}'")

    all_datasets = load_study_datasets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        load_from_cache = True,
        save_to_cache = save_to_cache,
        verbose = verbose)

    if all_datasets == None:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None

    # with new merge, the datasets are already recoded
    study_recoded_dataset = new_merge(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        save_to_cache = save_to_cache,
        verbose = verbose
    )


    memory_per_column = study_recoded_dataset.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {total_memory_mb:.2f} MB")


    return study_recoded_dataset







def create_donation_unified_dataset(
    cf = None,
    donation_id = None,
    verbose = False
    ):

    from fyp.fyp_main import initialize, connect_to_google

    if donation_id is None:
        raise ValueError("donation_id must be specified")

    if cf is None:
        cf = initialize()


    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    print(f"Generating unified dataset for donation '{donation_id}'")

    all_datasets = load_donation_datasets(
        cf = cf,
        donation_id = donation_id,
        load_from_cache = True,
        verbose = verbose)

    if all_datasets == None:
        print(f"!!! [Core datasets] No activity data matched the donation '{donation_id}'. Returning None")
        return None

    # with new merge, the datasets are already recoded
    donation_dataset = new_merge(
        cf = cf,
        study_name = None,
        all_datasets = all_datasets,
        save_to_cache = False,
        verbose = verbose
    )


    memory_per_column = donation_dataset.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    print(f"...done. Unified dataset for donation '{donation_id}' generated. Total memory used: {total_memory_mb:.2f} MB")


    return donation_dataset










def save_logs_as_csv(
    cf = None,
    study_name = None,
    outdata_filtered = None,
    file_label = "",
    verbose=False):


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
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_timestamp.min()} -- {outdata_filtered.T_local_timestamp.max()}")
            print(f"Now: {_dt.datetime.now()}")



