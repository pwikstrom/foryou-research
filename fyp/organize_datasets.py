
import pandas as pd
import fyp.data_io as data_io
#from fyp.donations import consolidate_ddp_logs
from fyp.machine_annotation import consolidate_and_save_refined_annotations
from fyp.donations import load_donation_data, simple_sample_ddp_events
from fyp.scrape import consolidate_and_save_scrape_data, load_failed_scrapes
#from fyp.zeeschuimer import load_zeeschuimer_data, consolidate_zeeschuimer_logs
from fyp.studies import init_study_defs
import fyp.data_io as data_io
from copy import deepcopy
import datetime as _dt
from fyp.fyp_config import fyp_cf
from fyp.studies import init_study_defs, save_study_defs






def load_study_datasets(
    study_name = None,
    all_datasets = {},
    load_from_cache = True,
    verbose=False
    ):


    if study_name is None:
        raise ValueError("study_name must be specified")


    if not "study_defs" in fyp_cf:
        init_study_defs()

    if not study_name in fyp_cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")


    print(f"Loading core datasets for study '{study_name}'...")

    # load core datasets from cache. This makes sense if the storage is remote. Since a slow network connection makes loading of datasets 
    # take a long time. If this is not a problem, there is really no need to use this option.
    if load_from_cache and not fyp_cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        tutti_data = {}
        cached_core_datasets = {}
        for k in ['scrape','machine_annotations','donations']:
            tutti_data[k] = None

            # if a core dataset exists in cache - check what it is and in case it can be used for this study - load it
            if data_io.exists(storage_location="cache", filename=f"core_{k}.parquet"):
                parquet_study_name = data_io.find_key_value_in_pq_metadata(storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                if parquet_study_name == study_name or parquet_study_name == 'everything':
                    if verbose:
                        print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'. Loading...")
                    cached_core_datasets[k] = parquet_study_name
                    tutti_data[k] = data_io.load_parquet(storage_location="cache", filename=f"core_{k}.parquet")


            # if no dataset was loaded from cache and the cache and main storage are at different locations, then load everything from
            #  main storage and save to cache. It will save time later since this can be used for all studies
            if tutti_data[k] is None and fyp_cf['data_io']['use_gcs_for_data']==True and fyp_cf['data_io']['use_gcs_for_cache']==False:
                if verbose:
                    print(f"Loading core dataset '{k}' from main storage and saving to cache")
                tutti_data[k] = data_io.load_parquet(storage_location="recoded", filename=f"{k}_recoded.parquet")
                if verbose:
                    print(f"Saving core dataset '{k}' to cache")
                tutti_data[k].attrs["study_name"] = 'everything'
                data_io.save_parquet(df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")

                
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        if verbose:
            print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        if verbose:
            print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")



    # --------------------------------------------------------------------
    # load activity data
    # --------------------------------------------------------------------



    # if donation data is to be included in the analysis
    tutti_data["donations"] = load_donation_data(study_name = study_name, all_data = tutti_data.get("donations", None), verbose=verbose)


    for k in tutti_data.keys():
        if tutti_data.get(k, None) is None:
            tutti_data[k] = pd.DataFrame()


    if tutti_data.get("donations", pd.DataFrame()).empty:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None


    # --------------------------------------------------------------------
    # sample activity data
    # --------------------------------------------------------------------
    enrichment_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

    sample_frame_setting = fyp_cf["study_defs"][study_name].get("DONATION_SAMPLE_FRAME", "off")

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
            study_name = study_name, 
            all_ddp_events_df = sample_frame, 
            verbose = verbose)

    if tutti_data.get("donations", pd.DataFrame()).empty:
        print(f"!!! [Core datasets] Sampling resulted in empty datasets for study definition '{study_name}'. Returning None")
        return None



    # --------------------------------------------------------------------
    # load scraped and annotated data
    # --------------------------------------------------------------------

    # I only want to download the enrichment data that are needed for this particular study. So I check which videos are in the
    # activity datasets, and use that to filter the enrichment metadata. 
    unique_videos = set(tutti_data["donations"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in activity datasets")

    # If the study is the special 'everything' study then I don't need to do this.
    if study_name == 'everything':
        sel = None
    else:
        sel = [("item_id", "in", list(unique_videos))]


    # --------------------------------------------------------------------
    # load scraped data
    # --------------------------------------------------------------------
    if tutti_data.get("scrape") is None or tutti_data.get("scrape").empty:
        print("    [Scrape] Loading scraped data from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data["scrape"] = data_io.load_parquet(storage_location="recoded", filename="scrape_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose:print(" ...done")
    else:
        print(f"    [Scrape] There are {len(tutti_data['scrape']):,} scraped data items in the cache", end="", flush=True)
        tutti_data["scrape"] = tutti_data["scrape"][tutti_data["scrape"]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data['scrape']):,} of those overlap with the activity datasets for this study.")    

    # --------------------------------------------------------------------
    # load machine annotations
    # --------------------------------------------------------------------
    if tutti_data.get("machine_annotations") is None or tutti_data.get("machine_annotations").empty:
        print("    [Machine annotations] Loading machine annotations from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data["machine_annotations"] = data_io.load_parquet(storage_location="recoded", filename="machine_annotations_recoded.parquet", filters=sel, verbose=verbose)
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


    if verbose:
        if tutti_data is None:
            print("    [Core datasets] - None")
        else:
            print("    [Core datasets] Datasets:")
            for k in tutti_data:
                if tutti_data[k] is not None:
                    print(f"    [Core datasets] - '{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)")


    print(f"...done. Core datasets loaded for study '{study_name}'")


    return tutti_data

















def load_donation_datasets(
    donation_id = None,
    load_from_cache = True,
    verbose=False
    ):

    print(f"Loading core datasets for donation '{donation_id}'...")

    # load core datasets from cache. This makes sense if the storage is remote. Since a slow network connection makes loading of datasets 
    # take a long time. If this is not a problem, there is really no need to use this option.
    if load_from_cache and not fyp_cf['data_io']['use_gcs_for_cache']: # there is no point of caching these files to GCS since it is already available there
        tutti_data = {}
        cached_core_datasets = {}
        for k in ['scrape','machine_annotations','donations']:
            tutti_data[k] = None

            # if a core dataset exists in cache - check what it is and in case it can be used for this study - load it
            if data_io.exists(storage_location="cache", filename=f"core_{k}.parquet"):
                parquet_study_name = data_io.find_key_value_in_pq_metadata(storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'")
                if parquet_study_name == 'everything':
                    if verbose:
                        print(f"    [Core datasets] Found a cached version of '{k}' core dataset for study '{parquet_study_name}'. Loading...")
                    cached_core_datasets[k] = parquet_study_name
                    tutti_data[k] = data_io.load_parquet(storage_location="cache", filename=f"core_{k}.parquet")
            else:
                print(f"    [Core datasets] Loading core dataset '{k}' from main storage")
                tutti_data[k] = data_io.load_parquet(storage_location="recoded", filename=f"{k}_recoded.parquet")
                tutti_data[k].attrs["study_name"] = 'everything'

                # if the main storage is on gcs and cache is local, then save the core dataset to cache.
                # It will save time later since this can be used for all studies
                if fyp_cf['data_io']['use_gcs_for_data']==True and fyp_cf['data_io']['use_gcs_for_cache']==False:
                    print(f"    [Core datasets] Saving core dataset '{k}' to cache")
                    data_io.save_parquet(df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")

                
    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")



    # --------------------------------------------------------------------
    # load activity data
    # --------------------------------------------------------------------

    if "donations" in tutti_data and isinstance(tutti_data["donations"], pd.DataFrame):
        tutti_data["donations"] = tutti_data["donations"][tutti_data["donations"]["D_donation_id"] == donation_id]
        if len(tutti_data["donations"]) == 0:
            print(f"    [Core datasets] No donations found for donation_id '{donation_id}'")
            return None

    unique_videos = set(tutti_data["donations"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in activity datasets")

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
        if tutti_data is None:
            print("    [Core datasets] - None")
        else:
            print("    [Core datasets] Datasets:")
            for k in tutti_data:
                if tutti_data[k] is not None:
                    print(f"    [Core datasets] - '{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size(tutti_data[k]):.1f}MB)")


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
    study_dataset = None,
    query_string = "",
    verbose = False,
    notebook_mode = False
    ):


    if study_dataset is None:
        raise ValueError("study_dataset must be specified")


    # group by video URL and count the number of unique users
    agg_dict, confirmed_cols = _build_agg_dict_to_generate_basic_video_stats(study_dataset)

    video_stats = study_dataset[confirmed_cols].groupby('item_id').agg(**agg_dict)

    if "video_duration" in video_stats.columns:
        video_stats['duration_ok_to_annotate'] = (video_stats['video_duration'] <= fyp_cf["machine"]["max_duration_for_annotation"]).fillna(False)
        video_stats.drop(columns=["video_duration"], inplace=True)
    else:
        # If duration information is missing, default to False (safer not to annotate unknown duration)
        video_stats['duration_ok_to_annotate'] = False

    video_stats.fillna(False, inplace=True)

    video_stats.query(query_string, inplace=True)

    return video_stats








"""def generate_unique_videos_to_scrape_and_annotate(
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


    if load_from_cache and study_name is not None:
        if data_io.exists(storage_location="cache", filename=f"{study_name}_recoded.parquet"):
            if verbose:
                print(f"    Loading study recoded dataset from cache...", end=" ", flush=True)
            

            study_dataset = data_io.load_parquet(
                filename=f"{study_name}_recoded.parquet", 
                storage_location="cache")
            if verbose:
                print(f"Shape: {study_dataset.shape}")
        else:
            print("@@ No cached study dataset found. I must run the process to create it. Please wait a moment...")
            study_dataset = create_study_recoded_dataset(
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
        study_dataset = study_dataset_small,
        query_string = "scraped_ok & ~annotated_ok & ~annotated_fail & duration_ok_to_annotate",
        verbose = verbose,
        notebook_mode = False)

    selected_scrape_videos = select_videos_from_study_dataset(
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
            df=selected_annotate_videos,
            storage_location="cache",
            filename=f"{study_name}_unique_items_to_annotate.parquet")
        data_io.save_parquet(
            df=selected_scrape_videos,
            storage_location="cache",
            filename=f"{study_name}_unique_items_to_scrape.parquet")
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds")

    return {
        "annotate": selected_annotate_videos,
        "scrape": selected_scrape_videos
    }"""









"""def check_unique_videos_to_scrape_and_annotate(
    study_name = None,
    load_from_cache = True,
    save_to_cache = True,
    verbose = False
    ):


    print(f"Checking unique videos to scrape and annotate...")

    interesting_videos = generate_unique_videos_to_scrape_and_annotate(
        study_name = study_name,
        load_from_cache = load_from_cache,
        save_to_cache = save_to_cache,
        verbose = verbose)


    return {
        "annotate": interesting_videos["annotate"].shape,
        "scrape": interesting_videos["scrape"].shape
    }"""







def update_enrichment_status(
    all_datasets:dict = {},
    save_to_disk = True,
    verbose:bool = False):
    
    collection_id_column = "D_donation_id"

    """if "zeeschuimer_logs" in all_datasets and "ddp_logs" in all_datasets:
        combined_activity_data= pd.concat([
                all_datasets["zeeschuimer_logs"][['item_id', collection_id_column]],
                all_datasets["ddp_logs"][['item_id', collection_id_column]]
            ])
    elif "zeeschuimer_logs" in all_datasets:
        combined_activity_data = all_datasets["zeeschuimer_logs"][['item_id', collection_id_column]]
    elif "ddp_logs" in all_datasets:
        combined_activity_data = all_datasets["ddp_logs"][['item_id', collection_id_column]]
    else:
        raise ValueError("No activity data found")"""


    combined_activity_data = all_datasets["ddp_logs"][['item_id', collection_id_column]]

    enrichment_status_df = combined_activity_data.groupby("item_id").agg(
            nunique_donations=pd.NamedAgg(column=collection_id_column, aggfunc="nunique"),
            total_observations=pd.NamedAgg(column=collection_id_column, aggfunc="count")
        )



    enrichment_status_df["nunique_donations"] = enrichment_status_df["nunique_donations"].astype("int64[pyarrow]")

    enrichment_status_df.reset_index(inplace=True)

    most_common_item_id_length = enrichment_status_df.item_id.str.len().value_counts().index[0]
    enrichment_status_df = enrichment_status_df[enrichment_status_df.item_id.str.len()==most_common_item_id_length].copy()

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets['scrape_data'][['item_id','scraped_ok','S_video_downloaded']], on='item_id', how='left')

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets['annotations'][['item_id','annotated_ok','annotated_fail']], on='item_id', how='left')

    failed_scrapes = load_failed_scrapes()

    failed_scrapes = pd.DataFrame(failed_scrapes, columns=["item_id"])
    failed_scrapes["scrape_fail"] = True

    failed_scrapes = failed_scrapes.convert_dtypes(dtype_backend="pyarrow")

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=failed_scrapes, on="item_id", how="left").copy()

    enrichment_status_df.set_index("item_id", inplace=True)

    if save_to_disk:
        data_io.save_parquet(df=enrichment_status_df, storage_location="recoded", filename="enrichment_status.parquet", verbose=verbose)

    return enrichment_status_df










"""def OLD_consolidate_and_save_activity_logs(
    force_consolidation:bool = False, 
    verbose:bool = False):

    
    #print("\n*** Zeeschuimer")
    #new_z, z1 = consolidate_zeeschuimer_logs(
    #    force_consolidation=force_consolidation, 
    #    verbose=verbose)
    
    print("\n*** Donations")
    new_d, d1 = consolidate_ddp_logs(
        force_consolidation=force_consolidation, 
        consolidate_from_scratch=True, 
        verbose = verbose)

    # I need all columns in both datasets. When concatenating, the columns will be created with null values.
    # But sometimes the separate datasets will be used on its own. And in those situations I need to match
    # up the columns as I'm doing here. Previously I had some idea that the new columns should not be NA,
    # which is why the code is a bit complex. I've kept it since I might change my mind again.
    # Update Feb'26: I realised that I D_donation_id is crucial for most analyses to function and have to 
    # be treated separately. This is specifically for the case when data is generated from zeeschuimer and
    # not from ddp logs. I assume that this data is 'baseline' even though it can certainly be other things
    # as well.
    if new_z or new_d:
        print("\nMatching up columns between zeeschuimer and donation data...")
        for c in set(z1.columns) | set(d1.columns):
            if not c in z1.columns:
                if c=="D_donation_id": 
                    if verbose:
                        print(f"    Adding {c} to z1 | string")
                    z1[c] = pd.Series("BASELINE", index=z1.index, dtype="string[pyarrow]")
                elif c=="D_feature_name": 
                    if verbose:
                        print(f"    Adding {c} to z1 | string")
                    z1[c] = pd.Series("BASELINE", index=z1.index, dtype="string[pyarrow]")
                elif pd.api.types.is_numeric_dtype(d1[c]): 
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
        print(f"...done matching columns Zeeschuimer shape {z1.shape} and DDP shape {d1.shape}")

    if new_z:
        print(f"Saving Zeeschuimer dataset. Shape {z1.shape} to 'recoded' folder")
        _ = data_io.save_parquet(df=z1, storage_location="recoded", filename="zeeschuimer_recoded.parquet", verbose=verbose)

    if new_d:
        print(f"Saving DDP dataset. Shape {d1.shape} to 'recoded' folder")
        _ = data_io.save_parquet(df=d1, storage_location="recoded", filename="donations_recoded.parquet", verbose=verbose)
    
    if new_d:# or new_z:
        print("...done saving datasets")
    
    return (new_d, d1)
    #return (new_z, z1), (new_d, d1)"""









def consolidate_enrichment_data(force_consolidation=False, verbose=False):


    ddp_logs = data_io.load_parquet(filename="donations_recoded.parquet", storage_location="recoded")
    new_ddp_logs = False
    
    print("\n*** Annotations")
    (new_annotations, annotations) = consolidate_and_save_refined_annotations(force_consolidation=force_consolidation,
                                                                            verbose=verbose)
    print("\n*** Scrape")
    (new_scrape_data, scrape_data) = consolidate_and_save_scrape_data(force_consolidation=force_consolidation,
                                                                     verbose=verbose)

    fine_results = {
        "new_ddp_logs": new_ddp_logs,
        "ddp_logs": ddp_logs,
        "new_annotations": new_annotations,
        "annotations": annotations,
        "new_scrape_data": new_scrape_data,
        "scrape_data": scrape_data
        }

    print("\n*** Updating (and saving) data enrichment status...")
    update_enrichment_status(all_datasets=fine_results, verbose=verbose)
    print("...done.")

    return fine_results








"""def OLD_consolidate_fyp_core_data(force_consolidation=False, verbose=False):


    #(new_zeeschuimer_logs, zeeschuimer_logs), 
    (new_ddp_logs, ddp_logs) = _consolidate_and_save_activity_logs(force_consolidation=force_consolidation,
                                                                                                            verbose=verbose)
    print("\n*** Annotations")
    (new_annotations, annotations) = consolidate_and_save_refined_annotations(force_consolidation=force_consolidation,
                                                                            verbose=verbose)
    print("\n*** Scrape")
    (new_scrape_data, scrape_data) = consolidate_and_save_scrape_data(force_consolidation=force_consolidation,
                                                                     verbose=verbose)

    fine_results = {
        #"new_zeeschuimer_logs": new_zeeschuimer_logs,
        #"zeeschuimer_logs": zeeschuimer_logs,
        "new_ddp_logs": new_ddp_logs,
        "ddp_logs": ddp_logs,
        "new_annotations": new_annotations,
        "annotations": annotations,
        "new_scrape_data": new_scrape_data,
        "scrape_data": scrape_data
        }

    print("\n*** Updating (and saving) data enrichment status...")
    update_enrichment_status(all_datasets=fine_results, verbose=verbose)
    print("...done.")


    return fine_results"""











def new_merge(
    study_name = None,
    all_datasets = {},
    verbose = False,
    save_to_cache = True,
    ):

    print(f"Merging all datasets...")

    if study_name is None and save_to_cache == True:
        raise ValueError("study_name must be specified")


    if not "study_defs" in fyp_cf:
        init_study_defs()

    if not study_name in fyp_cf["study_defs"].keys() and save_to_cache == True:
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")
    


    #if 'zeeschuimer' in all_datasets.keys():
    #    del all_datasets['zeeschuimer']




    for k in all_datasets.keys():
        if all_datasets[k] is None:
            print(f"all_datasets['{k}'] is None")


    if all_datasets.get('scrape',None) is not None and all_datasets.get('machine_annotations',None) is not None:
        enriched_data = pd.merge(left=all_datasets['scrape'], right=all_datasets['machine_annotations'], on='item_id', how='left')
    elif all_datasets.get('scrape',None) is not None and all_datasets.get('machine_annotations',None) is None:
        enriched_data = all_datasets['scrape']
    elif all_datasets.get('machine_annotations',None) is not None and all_datasets.get('scrape',None) is None:
        enriched_data = all_datasets['machine_annotations']
    else:
        enriched_data = pd.DataFrame()        
    
    #if all_datasets.get('donations',None) is not None and all_datasets.get('zeeschuimer',None) is not None:
    #    activity_data = pd.concat([all_datasets['donations'], all_datasets['zeeschuimer']], ignore_index=True)
    if all_datasets.get('donations',None) is not None:# and all_datasets.get('zeeschuimer',None) is None:
        activity_data = all_datasets['donations']
    #elif all_datasets.get('zeeschuimer',None) is not None and all_datasets.get('donations',None) is None:
    #    activity_data = all_datasets['zeeschuimer']
    else:
        activity_data = pd.DataFrame()

    if len(activity_data) == 0:
        print("No activity data")
        return enriched_data
    if len(enriched_data) == 0:
        print("No enriched data")
        return activity_data
    
    shebang = pd.merge(left=activity_data, right=enriched_data, on='item_id', how='left')

    # --------------------------------------------------------------------------------------------------
    # adding some calculated columns to this merged dataset
    # --------------------------------------------------------------------------------------------------

    # 1. days since created
    calc_col = ["T_days_since_created"]
    shebang[calc_col[-1]] = shebang["T_local_timestamp"] - shebang["S_createTime"]
    shebang[calc_col[-1]] = shebang[calc_col[-1]].map(lambda x: x.days if x is not pd.NA else pd.NA).astype("int64[pyarrow]")
    shebang[calc_col[-1]] = shebang[calc_col[-1]].clip(lower=0)

    # 2. plays per day
    calc_col += ["plays_per_day"]
    def _safe_vector_divide(x, y):
        return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd.NA)
    shebang[calc_col[-1]] = _safe_vector_divide(shebang['S_stats_playCount'],shebang['T_days_since_created'])


    # 3. scraped fail
    failed_scrapes = set(load_failed_scrapes(verbose=verbose))  # load failed_scrapes as a set
    calc_col += ["scraped_fail"]
    shebang[calc_col[-1]] = shebang["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")

    
    # 4. completion rate
    calc_col += ["completion_rate"]
    shebang[calc_col[-1]] = shebang["D_watch_duration"] / shebang["S_video_duration"]
    shebang[calc_col[-1]] = shebang[calc_col[-1]].clip(lower=0,upper=1).astype("double[pyarrow]")

    if verbose:
        print(f"Adding columns: {calc_col}. Resulting output log DF shape {shebang.shape}")
    # --------------------------------------------------------------------------------------------------



    if save_to_cache:
        t1 = _dt.datetime.now()
        if verbose:
            print(f"  Saving the '{study_name}' dataset to cache...")
        shebang.attrs['study_name'] = study_name
        data_io.save_parquet(
            df=shebang,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            asyncronous=False,
            verbose=verbose)
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds")


    print(f"...done. Merged all datasets. Shape: {shebang.shape}")


    return shebang


















def create_study_recoded_dataset(
    study_name = None,
    all_datasets = {},
    save_to_cache = True,
    verbose = False
    ):


    if study_name is None:
        raise ValueError("study_name must be specified")


    if not study_name in fyp_cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")



    print(f"Generating unified dataset for study '{study_name}'")

    all_datasets = load_study_datasets(
        study_name = study_name,
        all_datasets = all_datasets,
        load_from_cache = True,
        verbose = verbose)

    if all_datasets == None:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None

    # with new merge, the datasets are already recoded
    study_recoded_dataset = new_merge(
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
    donation_id = None,
    verbose = False
    ):


    if donation_id is None:
        raise ValueError("donation_id must be specified")


    print(f"Generating unified dataset for donation '{donation_id}'")

    all_datasets = load_donation_datasets(
        donation_id = donation_id,
        load_from_cache = True,
        verbose = verbose)

    if all_datasets == None:
        print(f"!!! [Core datasets] No activity data matched the donation '{donation_id}'. Returning None")
        return None

    # with new merge, the datasets are already recoded
    donation_dataset = new_merge(
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
    study_name = None,
    outdata_filtered = None,
    file_label = "",
    verbose=False):



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
        outdata_for_csv_export.to_csv(join(fyp_cf['paths']['exports'],log_as_csv_filename), errors='replace')
        if verbose:
            print(f"Exported {len(outdata_for_csv_export):,} observations in {log_as_csv_filename}.")
            print(f"The date of the observations in the log range from {outdata_filtered.T_local_timestamp.min()} -- {outdata_filtered.T_local_timestamp.max()}")
            print(f"Now: {_dt.datetime.now()}")



