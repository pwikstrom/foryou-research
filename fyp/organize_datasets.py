
from zoneinfo import ZoneInfo
import pandas as pd
from fyp.fyp_main import initialize
import fyp.data_io as data_io
from fyp.donations import consolidate_ddp_logs
from fyp.zeeschuimer import consolidate_zeeschuimer_logs
from fyp.machine_annotation import consolidate_and_save_refined_annotations
from fyp.donations import load_special_donations, load_ddp_events
from fyp.scrape import consolidate_and_save_scrape_data
from fyp.zeeschuimer import load_zeeschuimer_data
from fyp.fyp_main import initialize, connect_to_google
import fyp.data_io as data_io
from copy import deepcopy
from datetime import datetime



WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}










def load_study_datasets(
    cf = None,
    study_name = None,
    all_datasets = {},
    consolidate = False,
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
            if data_io.exists(cf, "cache", f"core_{k}.parquet"):

                parquet_study_name = data_io.find_key_value_in_pq_metadata(cf=cf, storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                if parquet_study_name == study_name or parquet_study_name == 'everything':
                    print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'. Loading...")
                    cached_core_datasets[k] = parquet_study_name
                    tutti_data[k] = data_io.load_parquet(cf=cf, storage_location="cache", filename=f"core_{k}.parquet")
                else:
                    pass
                    #print(f"    [Core datasets] Cached '{k}' core dataset for study '{parquet_study_name}' does not match requested study name '{study_name}'. Getting the data from the main storage instead.")
                
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")




    if cf["study_defs"][study_name]["INCLUDE_ZEESCHUIMER_DATA"]:
        if tutti_data.get("zeeschuimer") is None:
            tutti_data["zeeschuimer"] = load_zeeschuimer_data(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["zeeschuimer"] = load_zeeschuimer_data(cf = cf, study_name = study_name, all_data = tutti_data["zeeschuimer"], verbose=verbose)
        
    else:
        if "zeeschuimer" in tutti_data:
            del tutti_data["zeeschuimer"]


    if cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() in ['all','sample']:
        if tutti_data.get("donations") is None:
            tutti_data["donations"] = load_ddp_events(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["donations"] = load_ddp_events(cf = cf, study_name = study_name, all_data = tutti_data["donations"], verbose=verbose)

    elif cf["study_defs"][study_name]["INCLUDE_DONATIONS"].lower() == "special" and len(cf["study_defs"][study_name]["SPECIAL_DONATIONS"])>0:
        if tutti_data.get("donations") is None:
            tutti_data["donations"] = load_special_donations(cf = cf, study_name = study_name, verbose=verbose)
        else:
            tutti_data["donations"] = load_special_donations(cf = cf, study_name = study_name, all_data = tutti_data["donations"], verbose=verbose)

    else:
        if "donations" in tutti_data:
            del tutti_data["donations"]



    # I only want to download the videos that are needed for this particular study. So I check which videos are in the
    # ddp and baseline datasets, and use that to filter the scraped metadata. If the study is the special 
    # 'everything' study then I don't need to do this.
    unique_videos = set()
    if "zeeschuimer" in tutti_data:
        unique_videos = unique_videos | set(tutti_data["zeeschuimer"]["item_id"].dropna().values.tolist())
    if "donations" in tutti_data:
        unique_videos = unique_videos | set(tutti_data["donations"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in donation and zeeschuimer datasets")
    if study_name == 'everything':
        sel = None
    else:
        sel = [("item_id", "in", list(unique_videos))]

    if tutti_data.get("scrape") is None:
        print("    [Scrape] Loading scraped data from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data["scrape"] = data_io.load_parquet(cf=cf, storage_location="recoded", filename="scrape_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose:print(" ...done")
    else:
        print(f"    [Scrape] There are {len(tutti_data['scrape']):,} scraped data items in the cache", end="", flush=True)
        tutti_data["scrape"] = tutti_data["scrape"][tutti_data["scrape"]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data['scrape']):,} of those overlap with the activity datasets for this study.")    

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

    if save_to_cache and not cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        t1 = datetime.now()
        if verbose:
            print("    [Core datasets] Saving datasets to cache...")
        for k in tutti_data:
            if k in cached_core_datasets and cached_core_datasets[k] == 'everything':
                if verbose:
                    print(f"    [Core datasets] Cached 'everything' dataset for '{k}' already exists. No need to replace it with this dataset.")
                continue
            tutti_data[k].attrs["study_name"] = study_name
            data_io.save_parquet(cf=cf, df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")
        if verbose:
            print(f"    [Core datasets] ...done. (Took me {(datetime.now() - t1).total_seconds():.1f} seconds)")

    if verbose:
        print("    [Core datasets] Datasets:")
        dataset_info = "\n    [Core datasets] - ".join([f"'{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)" for k in tutti_data])
        print(f"    [Core datasets] - {dataset_info}")


    print(f"...done. Core datasets loaded for study '{study_name}'")


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

    from fyp.fyp_main import initialize
    from datetime import datetime
    import fyp.data_io as data_io
    #from os.path import exists as os_exists, join as os_join
    #from pandas import read_parquet as pd_read_parquet
    #from pyarrow.parquet import read_schema as pq_read_schema


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
        t1 = datetime.now()
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
            print(f"  ...done. Time taken to save datasets to cache: {(datetime.now() - t1).total_seconds():.1f} seconds")

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














def consolidate_and_save_activity_logs(cf = None, force_consolidation=False, verbose=False):

    if cf is None:
        cf = initialize()
    
    print("\n*** Zeeschuimer")
    new_z, z1 = consolidate_zeeschuimer_logs(cf = cf, force_consolidation=force_consolidation, verbose=verbose)
    print("\n*** Donations")
    new_d, d1 = consolidate_ddp_logs(cf = cf, force_consolidation=force_consolidation, verbose = verbose)

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
                    z1[c] = pd.Series("BASELINE", index=z1.index, dtype="string[pyarrow]")
            if not c in d1.columns:
                if pd.api.types.is_numeric_dtype(z1[c]):
                    if verbose:
                        print(f"    Adding {c} to d1 | numeric")
                    d1[c] = pd.Series(pd.NA, index=d1.index, dtype="int64[pyarrow]")
                else:
                    if verbose:
                        print(f"    Adding {c} to d1 | string")
                    d1[c] = pd.Series("DDP", index=d1.index, dtype="string[pyarrow]")
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

    consolidate_and_save_activity_logs(cf=cf, force_consolidation=force_consolidation, verbose=verbose)
    print("\n*** Annotations")
    consolidate_and_save_refined_annotations(cf=cf, force_consolidation=force_consolidation, verbose=verbose)
    print("\n*** Scrape")
    consolidate_and_save_scrape_data(cf=cf, force_consolidation=force_consolidation, verbose=verbose)









def new_merge(
    cf = None,
    study_name = None,
    all_datasets = {},
    verbose = False,
    save_to_cache = True,
    ):
    from pandas import merge, DataFrame, to_datetime, concat, NA as pd_NA
    from fyp.scrape import load_failed_scrapes
    from datetime import datetime
    import fyp.data_io as data_io
    #from os.path import join as os_join

    print(f"Merging all datasets...")

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    if not study_name in cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")

    #print(all_datasets.keys())

    if 'scrape' in all_datasets and 'machine_annotations' in all_datasets:
        enriched_data = merge(left=all_datasets['scrape'], right=all_datasets['machine_annotations'], on='item_id', how='left')
    elif 'scrape' in all_datasets and 'machine_annotations' not in all_datasets:
        enriched_data = all_datasets['scrape']
    elif 'machine_annotations' in all_datasets and 'scrape' not in all_datasets:
        enriched_data = all_datasets['machine_annotations']
    else:
        enriched_data = DataFrame()        
    
    if 'donations' in all_datasets and 'zeeschuimer' in all_datasets:
        activity_data = concat([all_datasets['donations'], all_datasets['zeeschuimer']], ignore_index=True)
    elif 'donations' in all_datasets and 'zeeschuimer' not in all_datasets:
        activity_data = all_datasets['donations']
    elif 'zeeschuimer' in all_datasets and 'donations' not in all_datasets:
        activity_data = all_datasets['zeeschuimer']
    else:
        activity_data = DataFrame()

    if len(activity_data) == 0:
        print("No activity data")
        return enriched_data
    if len(enriched_data) == 0:
        print("No enriched data")
        return activity_data
    
    shebang = merge(left=activity_data, right=enriched_data, on='item_id', how='left')

    shebang["T_days_since_created"] = shebang["T_local_timestamp"] - shebang["S_createTime"]
    shebang["T_days_since_created"] = shebang["T_days_since_created"].map(lambda x: x.days if x is not pd_NA else pd_NA).astype("int64[pyarrow]")

    if verbose:
        print(f"Adding 'days_since_created' column. Resulting output log DF shape {shebang.shape}")

    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose, consolidate = True))
    shebang["scraped_fail"] = shebang["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")
    
    def _safe_vector_divide(x, y):
        return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd_NA)
    shebang['plays_per_day'] = _safe_vector_divide(shebang['S_stats_playCount'],shebang['T_days_since_created'])


    if save_to_cache:
        t1 = datetime.now()
        if verbose:
            print("  Saving the '{study_name}' dataset to cache...")
        shebang.attrs['study_name'] = study_name
        data_io.save_parquet(
            cf=cf,
            df=shebang,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet")
        #shebang.to_parquet(os_join(cf['paths']['cache'], f"{study_name}_recoded.parquet"), engine='pyarrow')
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(datetime.now() - t1).total_seconds():.1f} seconds")


    print(f"...done. Merged all datasets. Shape: {shebang.shape}")


    return shebang









################################################################################################################################################
## Saturday morning, I need to build a cache refresh!
################################################################################################################################################



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












def create_study_recoded_dataset(
    cf = None,
    study_name = None,
    all_datasets = {},
    load_from_cache = True,
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

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None and not load_from_cache:
        cf = connect_to_google(cf)


    print(f"Generating unified dataset for study '{study_name}'")

    all_datasets = load_study_datasets(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        load_from_cache = True,
        save_to_cache = True,
        verbose = verbose)

    # with new merge, the datasets are already recoded
    study_recoded_dataset = new_merge(
        cf = cf,
        study_name = study_name,
        all_datasets = all_datasets,
        save_to_cache = True,
        verbose = verbose
    )


    memory_per_column = study_recoded_dataset.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {total_memory_mb:.2f} MB")


    return study_recoded_dataset







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
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_timestamp.min()} -- {outdata_filtered.T_local_timestamp.max()}")
            print(f"Now: {datetime.now()}")














"""def _process_scrape_metadata_for_merge_w_logs(
    all_datasets,
    combined_log,
    verbose=False
    ):

    #from pandas import isna as pd_isna, Timestamp, DataFrame, to_datetime, Series, NA as pd_NA
    #from datetime import datetime
    #from zoneinfo import ZoneInfo
    #from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow

    if len(combined_log) == 0:
        return pd.DataFrame()


    # polishing the scraped metadata dataset for merging with the log
    scrape_metadata_log = all_datasets["scraped"][all_datasets["scraped"].item_id.isin(combined_log.item_id.unique())].copy()

    if verbose:
        print(f"Processing scraped metadata {scrape_metadata_log.shape} for merge w logs. Combined log has shape:{combined_log.shape}...")


    object_cols = scrape_metadata_log.select_dtypes(exclude=['number']).columns
    scrape_metadata_log[object_cols] = scrape_metadata_log[object_cols].replace('nan', '').infer_objects(copy=False)


    scrape_metadata_log["createTime"] = pd.to_datetime(
        scrape_metadata_log["createTime"], 
        errors='coerce',
        utc=True
    ).fillna(pd_NA)#.fillna(Timestamp(year=2100, month=1, day=1, tz='UTC'))


    # it is not possible to have videos that are negative or zero duration. Replace with NA
    scrape_metadata_log['video_duration'] = scrape_metadata_log['video_duration'].fillna(pd_NA).replace(-1, pd.NA).replace(0, pd.NA)


    #scrape_metadata_log.drop(columns=[
    #    "image_list","video_url","video_downloaded","audio_extracted","cover_downloaded","do_not_modify","last_modified","video_cover"], inplace=True, errors="ignore")
    scrape_metadata_log.drop(columns=["audio_extracted","cover_downloaded","do_not_modify","last_modified","video_cover"], inplace=True, errors="ignore")


    scrape_metadata_log = scrape_metadata_log.rename(columns={c:"S_"+c if not c=="item_id" else c for c in scrape_metadata_log.columns}).copy()
    if verbose:
        print(f"...processed scraped metadata shape {scrape_metadata_log.shape}")



    scrape_metadata_log["scraped_ok"] = pd.Series(True, index=scrape_metadata_log.index, dtype="bool[pyarrow]")


    scrape_metadata_log = convert_dtypes_to_pyarrow(scrape_metadata_log, verbose=verbose)

    
    return scrape_metadata_log"""




"""
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

    machine_annotations_for_log["annotated_ok"] = ~machine_annotations_for_log["G_type_of_story"].isna()
    machine_annotations_for_log["annotated_fail"] = machine_annotations_for_log["G_type_of_story"].isna()


    machine_annotations_for_log = convert_dtypes_to_pyarrow(machine_annotations_for_log, verbose=verbose)

    return machine_annotations_for_log"""


"""


def _combine_all_logs(
    #cf = None,
    all_datasets=None,
    verbose=False
    ):
    

    from pandas import concat
    from os.path import exists, join
    from fyp.fyp_main import initialize, convert_dtypes_to_pyarrow
    import fyp.data_io as data_io
    from pandas import NA as pd_NA

    #if cf is None:
    #    cf = initialize()

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
        print(f"    [{__name__}] Combined all logs to shape {combined_log.shape}.")



    # this should never happen: Convert categorical columns to string to avoid fillna errors
    for col in combined_log.select_dtypes(include=['category']).columns:
        print(f" ----------------------- [{__name__}] Converting category column {col} to pyarrow string...")
        combined_log[col] = combined_log[col].astype("string[pyarrow]")



    # when combining logs from zeeschuimer with data donations, some columns that are only
    # relevant for one of the log types will not be present in the other one. These columns
    # are not really 'missing' in a data sense, so I need to fill them with something to keep the 
    # data consistent. 
    ddp_cols_isna = [c for c in combined_log.columns if c.startswith("D_") and combined_log[c].isna().any()]
    baseline_cols_isna = [c for c in combined_log.columns if c.startswith("B_") and combined_log[c].isna().any()]
    if verbose:
        print(f"    [{__name__}] DDP cols with missing values: {ddp_cols_isna}")
        print(f"    [{__name__}] Baseline cols with missing values: {baseline_cols_isna}")

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


    # TODO: This is a horrible patch. I've hopefully fixed the cause by now...
    combined_log['T_local_day_segment'] = combined_log['T_local_day_segment'].astype("string[pyarrow]")

    combined_log = convert_dtypes_to_pyarrow(combined_log, verbose=verbose)


    return combined_log"""





"""def _merge_all_study_datasets(
    cf = None,
    study_name = None,
    all_datasets = None,   
    verbose = False,
    save_to_cache = True,
    ):
    ### merge log with enriched metadata (scraped and annotated)

    from pandas import merge, to_datetime, Series
    from fyp.scrape import load_failed_scrapes
    #from os.path import join as os_join
    from datetime import datetime
    import fyp.data_io as data_io


    print(f"Merging all datasets...")

    if study_name is None:
        raise ValueError("study_name must be specified")

    if cf is None:
        cf = initialize()

    if not study_name in cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")

    # prepare datasets for merge
    combined_log = _combine_all_logs(all_datasets=all_datasets, verbose=verbose)
    scrape_metadata_log = _process_scrape_metadata_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)
    machine_annotations_for_log = _process_machine_annotations_for_merge_w_logs(all_datasets, combined_log, verbose=verbose)

    # load failed_scrapes as a set
    failed_scrapes = set(load_failed_scrapes(cf = cf, verbose=verbose, consolidate = True))

    # merge datasets
    outdata = merge(left=combined_log, right=rename_columns(scrape_metadata_log), on='item_id',how='left')
    outdata = merge(left=outdata, right=rename_columns(machine_annotations_for_log), on='item_id',how='left')

    # add flags to indicate success/failure of scrape and annotation
    outdata["scraped_ok"] = outdata["scraped_ok"].fillna(False)
    outdata["annotated_ok"] = outdata["annotated_ok"].fillna(False)
    outdata["annotated_fail"] = outdata["annotated_fail"].fillna(False)
    outdata["scraped_fail"] = outdata["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")


    # Create a new column by calculating the difference between 'T_local_timestamp' and 'S_createTime'.
    # Ensure both are proper datetime types  before subtraction
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

    if save_to_cache:
        t1 = datetime.now()
        if verbose:
            print("  Saving datasets to cache...")
        outdata.attrs['study_name'] = study_name
        outdata.to_parquet(os_join(cf['paths']['cache'], f"{study_name}_main.parquet"), engine='pyarrow')
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(datetime.now() - t1).total_seconds():.1f} seconds")


    print(f"...done. Merged all datasets. Shape: {outdata.shape}")

    return outdata"""


