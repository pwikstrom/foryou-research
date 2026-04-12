
import re
import numpy as np
import pandas as pd
import datetime as _dt
from copy import deepcopy

import fyp.data_io as data_io
from fyp.machine_annotation import consolidate_and_save_refined_annotations
from fyp.scrape import consolidate_and_save_scrape_data, load_failed_scrapes
from fyp.studies import init_study_defs, save_study_defs
from fyp.recode_variables import get_grouping_factors_from_var_schema
from fyp.fyp_config import fyp_cf



collection_id_column = "collection_id"
timestamp_column = "local_timestamp"
event_type_column = "activity_type"

SCRAPES_LABEL = fyp_cf["labels"]["SCRAPES_LABEL"]
MACHINE_ANNOTATIONS_LABEL = fyp_cf["labels"]["MACHINE_ANNOTATIONS_LABEL"]
COLLECTIONS_LABEL = fyp_cf["labels"]["COLLECTIONS_LABEL"]




def _df_size_mb(df: pd.DataFrame) -> float:
    """Return DataFrame memory usage in megabytes."""
    return df.memory_usage(deep=True).sum() / (1024**2)




def _load_cached_core_datasets(verbose: bool = False) -> dict:
    """Load core datasets (scrape, annotations, collections) from cache or main storage.

    Tries the local cache first. If a dataset is not cached and main storage is on GCS,
    loads from GCS and saves a local cache copy for future use.

    Returns:
        Dict with keys SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL COLLECTIONS_LABEL from the config (values may be None).
    """
    tutti_data: dict = {}

    for k in [SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL, COLLECTIONS_LABEL]:
        tutti_data[k] = None

        # try loading from local cache
        if data_io.exists(storage_location="cache", filename=f"core_{k}.parquet"):
            parquet_study_name = data_io.find_key_value_in_pq_metadata(
                storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
            if parquet_study_name == 'everything':
                if verbose:
                    print(f"    [Core datasets] Loading '{k}' from cache (study: '{parquet_study_name}')...")
                tutti_data[k] = data_io.load_parquet(storage_location="cache", filename=f"core_{k}.parquet")
                continue

        # fallback: load from main storage
        if verbose:
            print(f"    [Core datasets] Loading '{k}' from main storage...")
        tutti_data[k] = data_io.load_parquet(storage_location="recoded", filename=f"{k}_recoded.parquet")
        tutti_data[k].attrs["study_name"] = 'everything'

        # if main storage is GCS and cache is local, persist to cache for next time
        if fyp_cf['data_io']['use_gcs_for_data'] and not fyp_cf['data_io']['use_gcs_for_cache']:
            if verbose:
                print(f"    [Core datasets] Saving '{k}' to local cache...")
            data_io.save_parquet(df=tutti_data[k], storage_location="cache", filename=f"core_{k}.parquet")

    return tutti_data





def _filter_enrichment_data(
    tutti_data: dict,
    unique_videos: set,
    study_name: str | None = None,
    verbose: bool = False
    ) -> None:
    """Load and filter scrape + annotation data to match the videos in the activity data.

    Modifies tutti_data in place: updates the 'scrape' and 'machine_annotations' entries.
    If the data is already present (from cache), it is filtered. Otherwise it is loaded from
    main storage with a parquet filter.
    """
    sel = None if study_name == 'everything' else [("item_id", "in", list(unique_videos))]

    # scrape data
    if tutti_data.get(SCRAPES_LABEL) is None or tutti_data[SCRAPES_LABEL].empty:
        print("    [Scrape] Loading scraped data from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data[SCRAPES_LABEL] = data_io.load_parquet(
            storage_location="recoded", filename=f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose: print(" ...done")
    else:
        print(f"    [Scrape] There are {len(tutti_data[SCRAPES_LABEL]):,} scraped data items in the cache", end="", flush=True)
        tutti_data[SCRAPES_LABEL] = tutti_data[SCRAPES_LABEL][tutti_data[SCRAPES_LABEL]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data[SCRAPES_LABEL]):,} of those overlap with the activity datasets.")

    # machine annotations
    if tutti_data.get(MACHINE_ANNOTATIONS_LABEL) is None or tutti_data[MACHINE_ANNOTATIONS_LABEL].empty:
        print("    [Machine annotations] Loading machine annotations from main storage...", end="", flush=True)
        if verbose: print()
        tutti_data[MACHINE_ANNOTATIONS_LABEL] = data_io.load_parquet(
            storage_location="recoded", filename=f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet", filters=sel, verbose=verbose)
        if not verbose: print(" ...done")
    else:
        print(f"    [Machine annotations] There are {len(tutti_data[MACHINE_ANNOTATIONS_LABEL]):,} annotations in the cache", end="", flush=True)
        tutti_data[MACHINE_ANNOTATIONS_LABEL] = tutti_data[MACHINE_ANNOTATIONS_LABEL][tutti_data[MACHINE_ANNOTATIONS_LABEL]["item_id"].isin(unique_videos)].copy()
        print(f" and {len(tutti_data[MACHINE_ANNOTATIONS_LABEL]):,} of those overlap with the activity datasets.")





def _print_dataset_summary(tutti_data: dict) -> None:
    """Print a summary of the datasets in tutti_data."""
    if tutti_data is None:
        print("    [Core datasets] - None")
        return
    print("    [Core datasets] Datasets:")
    for k in tutti_data:
        if tutti_data[k] is not None:
            print(f"    [Core datasets] - '{k}': {tutti_data[k].shape[0]:,}[R] x {tutti_data[k].shape[1]:,}[C] ({_df_size_mb(tutti_data[k]):.1f}MB)")




# ============================================================================
# Loading collection activity data
# ============================================================================


def load_collection_data(
    study_name: str = None,
    all_data: pd.DataFrame | None = None,
    verbose: bool = False
    ) -> pd.DataFrame | None:
    """Load and filter collection activity data for a study definition.

    If all_data is None, loads from main storage with parquet filters.
    If all_data is provided, filters the cached DataFrame in memory.
    """

    if study_name is None:
        raise ValueError("!!! [DDP] study_name must be specified")

    print(f"    [DDP] Loading data for study...")

    if "study_defs" not in fyp_cf:
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

    the_selected_collections = fyp_cf["study_defs"][study_name].get("SELECTED_COLLECTIONS",[])
    if len(the_selected_collections) > 0:
        the_selected_collections = [str(x) for x in the_selected_collections]
        the_selected_collections = [re.search(r'\[(.*?)\]', s).group(1) if re.search(r'\[(.*?)\]', s) else s for s in the_selected_collections]
        sel.append((collection_id_column, "in", the_selected_collections))

    if all_data is None:
        if verbose:
            print(f"    [DDP] Loading collection events from main storage")
        out_df = data_io.load_parquet("recoded", f"{COLLECTIONS_LABEL}_recoded.parquet", filters=sel, verbose=verbose)

    else:
        if verbose:
            print(f"    [DDP] Selecting date range from cached collection data")
        cached_collections_df = all_data.copy()
        out_df = cached_collections_df[(cached_collections_df[timestamp_column]>=START_DATE) & (cached_collections_df[timestamp_column]<=END_DATE)].copy()

        if collection_id_column not in out_df.columns or timestamp_column not in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] No events found in date range. Returning None.")
            return None

        if len(the_selected_collections) > 0:
            out_df = out_df[out_df[collection_id_column].isin(the_selected_collections)].copy()

        if collection_id_column not in out_df.columns or timestamp_column not in out_df.columns or len(out_df) == 0:
            print(f"!!! [DDP] The selected collections have no events in the date range. Returning None.")
            return None

    print(f"    [DDP] ...done. | Shape: {out_df.shape} | Unique collections: {out_df[collection_id_column].nunique()} | Date range: {out_df[timestamp_column].min():%Y-%m-%d} -- {out_df[timestamp_column].max():%Y-%m-%d}")

    return out_df




# ============================================================================
# Sampling
# ============================================================================


def simple_sample_collection_events(
    study_name: str = None,
    all_collections_df: pd.DataFrame = None,
    verbose: bool = False
    ) -> pd.DataFrame:
    """Sample activity events using study-defined grouping factors and thresholds.

    Separates play/non-play events, applies group-size and group-count filters with
    sampling, then recombines.
    """

    def _filter_and_sample(df: pd.DataFrame, group_cols: list[str],
                           x_threshold: int, y_samples: int,
                           rng: np.random.RandomState) -> pd.DataFrame:
        """Filters aggregation groups by size and samples rows."""
        group_sizes = df.groupby(group_cols)[group_cols[0]].transform('size')
        df_filtered = df[group_sizes >= x_threshold]

        sampled_indices = df_filtered.groupby(group_cols, group_keys=False).apply(
            lambda g: g.sample(n=min(len(g), y_samples), random_state=rng),
            include_groups=False
        )
        result = df_filtered.loc[sampled_indices.index]
        return result


    if all_collections_df is None:
        raise ValueError("[Sampling] all_collections_df cannot be None")

    rng = np.random.RandomState(42)
    the_df = all_collections_df.copy()

    # the grouping variables are defined in the study config with the prefixes used in the final version of the dataset
    # At this stage - the columns haven't been given these prefixes yet, so I need to drop them.

    grouping_factors = get_grouping_factors_from_var_schema(some_events_df = the_df, verbose=False)

    if len(grouping_factors) != 2:
        raise ValueError("!!! [Sampling] Group factors must be exactly 2")

    if collection_id_column not in grouping_factors:
        raise ValueError(f"!!! [Sampling] Group factors must include '{collection_id_column}'")

    # make sure collection_id_column is the first element
    grouping_factors.remove(collection_id_column)
    grouping_factors = [collection_id_column] + grouping_factors

    if verbose:
        print(f"    [Sampling] Grouping factors: {grouping_factors}")

    if "study_defs" not in fyp_cf:
        init_study_defs()

    MIN_EVENTS_REQUIRED = fyp_cf["study_defs"][study_name].get("MIN_ACTIVITY_COUNT_PER_GROUP",10)
    MAX_EVENTS_SELECTED = fyp_cf["study_defs"][study_name].get("MAX_ACTIVITY_COUNT_PER_GROUP",100)
    MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION = fyp_cf["study_defs"][study_name].get("MIN_GROUP_COUNT_PER_COLLECTION",10)
    MAX_GROUP_COUNT_SELECTED_PER_COLLECTION = fyp_cf["study_defs"][study_name].get("MAX_GROUP_COUNT_PER_COLLECTION",100)


    # Separate play and non-play events
    all_play_events_df = the_df[the_df[event_type_column]=="play"].copy()
    all_nonplay_events_df = the_df[the_df[event_type_column]!="play"].copy()
    sample_frame_size = len(all_play_events_df)

    if verbose:
        print(f"    [Sampling] Play events: {len(all_play_events_df):,}  |  Non-play events: {len(all_nonplay_events_df):,}")


    if verbose:
        print(f"    [Sampling] Dropping aggregation groups with less than {MIN_EVENTS_REQUIRED} events")
        print(f"    [Sampling] Sampling at most {MAX_EVENTS_SELECTED} events from each remaining group. This might take a moment...")
    # select agg groups with the required number of events
    play_events_within_agg_group_size_limits = _filter_and_sample(all_play_events_df, grouping_factors, MIN_EVENTS_REQUIRED, MAX_EVENTS_SELECTED, rng)
    if verbose:
        sample_size = len(play_events_within_agg_group_size_limits)
        if sample_frame_size > 0:
            print(f"    [Sampling] Play events after sampling: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # build a df with unique pairs of the two group factors
    unique_group_factor_pairs = play_events_within_agg_group_size_limits[grouping_factors].drop_duplicates()

    if verbose:
        print(f"    [Sampling] Dropping collections with less than {MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION} aggregation groups within the limits")
        print(f"    [Sampling] Sampling at most {MAX_GROUP_COUNT_SELECTED_PER_COLLECTION} aggregation groups from each remaining collection. This might take a moment...")
    # select collections with a required number of groups
    collections_within_group_count_limits = _filter_and_sample(unique_group_factor_pairs, grouping_factors[:1], MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION, MAX_GROUP_COUNT_SELECTED_PER_COLLECTION, rng)
    if verbose:
        print(f"    [Sampling] Aggregation groups remaining after sampling: {len(collections_within_group_count_limits):,}")

    selected_pairs_index = collections_within_group_count_limits.set_index(grouping_factors).index

    # ----------------------------------------------------------------------
    # find the play events in the selected groups
    play_events_in_candidate_groups = play_events_within_agg_group_size_limits.set_index(grouping_factors)

    # use isin() boolean mask instead of .loc[MultiIndex] to avoid potential reindexing
    play_events_in_selected_groups = play_events_in_candidate_groups[
        play_events_in_candidate_groups.index.isin(selected_pairs_index)
    ].reset_index()
    if verbose:
        sample_size = len(play_events_in_selected_groups)
        if sample_frame_size > 0:
            print(f"    [Sampling] Play events remaining in the sampled aggregation groups: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # ----------------------------------------------------------------------
    # find the non-play events in the selected groups - note that since the non-play events are not
    # sampled, there is a disproportional number of non-play events in the sampled dataset compared
    # to the number of play events.
    # only include non-play events from groups that also have play events in the sample
    nonplay_mask = all_nonplay_events_df.set_index(grouping_factors).index.isin(selected_pairs_index)
    nonplay_events_in_selected_groups = all_nonplay_events_df[nonplay_mask]
    if verbose:
        nonplay_total = len(all_nonplay_events_df)
        nonplay_selected = len(nonplay_events_in_selected_groups)
        pct = f"{nonplay_selected/nonplay_total:.0%}" if nonplay_total > 0 else "N/A"
        print(f"    [Sampling] Non-play events remaining in the selected aggregation groups: {nonplay_selected:,} ({pct} of original)")

    combined = pd.concat([play_events_in_selected_groups, nonplay_events_in_selected_groups])
    if verbose:
        print(f"    [Sampling] Combining the (not sampled) non-play events with the sampled play events: {len(combined):,} in {len(combined[grouping_factors].drop_duplicates()):,} groups")
    combined.drop("D_id", axis=1, inplace=True, errors='ignore')


    enrichment_status_df = data_io.load_parquet(
        storage_location="recoded",
        filename="enrichment_status.parquet")

    combined_deduped = combined.drop_duplicates(subset="item_id", keep="first")[["item_id"]]

    combined_deduped_enrichment_status = pd.merge(left=combined_deduped, right=enrichment_status_df, left_on='item_id', right_index=True, how='left')

    enrichment_summary = combined_deduped_enrichment_status.select_dtypes(include=["bool"]).fillna(False).sum().to_dict()

    mapper = fyp_cf['var_schema'][['variable_name','display_name']].dropna().set_index('variable_name').to_dict()['display_name']

    print(f"    [Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[grouping_factors].drop_duplicates()):,} groups")
    print(f"    [Sampling] - Unique items: {len(combined_deduped_enrichment_status):,}")
    for k in enrichment_summary:
        if len(combined_deduped_enrichment_status) > 0:
            print(f"    [Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} ({enrichment_summary[k]/len(combined_deduped_enrichment_status):.0%})")
        else:
            print(f"    [Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} (N/A)")

    return combined




# ============================================================================
# Loading core datasets (activity + scrape + annotations)
# ============================================================================


def load_study_datasets(
    study_name: str = None,
    all_datasets: dict = {},
    load_from_cache: bool = True,
    verbose: bool = False
    ) -> dict | None:
    """Load all core datasets for a study: collections, scrape data, and machine annotations.

    Handles caching, date-range filtering, and optional sampling based on the study definition.
    """

    if study_name is None:
        raise ValueError("study_name must be specified")

    if "study_defs" not in fyp_cf:
        init_study_defs()

    if study_name not in fyp_cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")


    print(f"Loading core datasets for study '{study_name}'...")

    # load core datasets from cache or main storage
    if load_from_cache and not fyp_cf['data_io']['use_gcs_for_cache']:
        tutti_data = _load_cached_core_datasets(verbose=verbose)

        # check if a study-specific cache exists (not just 'everything')
        for k in [SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL, COLLECTIONS_LABEL]:
            if data_io.exists(storage_location="cache", filename=f"core_{k}.parquet"):
                parquet_study_name = data_io.find_key_value_in_pq_metadata(
                    storage_location="cache", filename=f"core_{k}.parquet", the_key='study_name')
                if parquet_study_name == study_name:
                    if verbose:
                        print(f"    [Core datasets] Found study-specific cache for '{k}' (study: '{study_name}'). Loading...")
                    tutti_data[k] = data_io.load_parquet(storage_location="cache", filename=f"core_{k}.parquet")

    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        if verbose:
            print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        if verbose:
            print(f"    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")


    # --------------------------------------------------------------------
    # load and filter activity data
    # --------------------------------------------------------------------
    tutti_data["collections"] = load_collection_data(
        study_name=study_name, all_data=tutti_data.get("collections", None), verbose=verbose)

    for k in tutti_data.keys():
        if tutti_data.get(k, None) is None:
            tutti_data[k] = pd.DataFrame()

    if tutti_data.get("collections", pd.DataFrame()).empty:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None


    # --------------------------------------------------------------------
    # sample activity data
    # --------------------------------------------------------------------
    enrichment_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

    sample_frame_setting = fyp_cf["study_defs"][study_name].get("SAMPLE_FRAME", "off")

    if sample_frame_setting == "off":
        print(f"    [DD Sampling] Sample frame setting is 'off'. Not sampling collection data.")
        sample_frame = None

    elif sample_frame_setting == "events":
        sample_frame = tutti_data["collections"].copy()
        print(f"    [DD Sampling] Sample frame setting is 'events'. Using all {len(sample_frame):,} collection events as sample frame.")

    elif sample_frame_setting == "scraped":
        selected_videos = enrichment_status[enrichment_status["scraped_ok"]].index.tolist()
        sample_frame = tutti_data["collections"][tutti_data["collections"]["item_id"].isin(selected_videos)].copy()
        print(f"    [DD Sampling] Sample frame setting is 'scraped'. Using only {len(sample_frame):,} collection events that are scraped as sample frame.")

    elif sample_frame_setting == "annotated":
        selected_videos = enrichment_status[enrichment_status["annotated_ok"]].index.tolist()
        sample_frame = tutti_data["collections"][tutti_data["collections"]["item_id"].isin(selected_videos)].copy()
        print(f"    [DD Sampling] Sample frame setting is 'annotated'. Using only {len(sample_frame):,} collection events that are annotated as sample frame.")

    if sample_frame is not None:
        tutti_data["collections"] = simple_sample_collection_events(
            study_name=study_name, all_collections_df=sample_frame, verbose=verbose)

    if tutti_data.get("collections", pd.DataFrame()).empty:
        print(f"!!! [Core datasets] Sampling resulted in empty datasets for study definition '{study_name}'. Returning None")
        return None


    # --------------------------------------------------------------------
    # load scraped and annotated data
    # --------------------------------------------------------------------
    unique_videos = set(tutti_data["collections"]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos in activity datasets")

    _filter_enrichment_data(tutti_data, unique_videos, study_name=study_name, verbose=verbose)


    if verbose:
        _print_dataset_summary(tutti_data)

    print(f"...done. Core datasets loaded for study '{study_name}'")

    return tutti_data





def load_collection_datasets(
    collection_id: str = None,
    load_from_cache: bool = True,
    verbose: bool = False
    ) -> dict | None:
    """Load all core datasets for a single collection.

    Similar to load_study_datasets but filters by collection_id instead of a study definition.
    No sampling is performed.
    """

    print(f"Loading core datasets for collection '{collection_id}'...")

    if load_from_cache and not fyp_cf['data_io']['use_gcs_for_cache']:
        tutti_data = _load_cached_core_datasets(verbose=verbose)
    else:
        tutti_data = {}
        if verbose:
            print(f"    [Core datasets] Loading core datasets from main storage.")
        for k in [SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL, COLLECTIONS_LABEL]:
            tutti_data[k] = data_io.load_parquet(storage_location="recoded", filename=f"{k}_recoded.parquet")


    # --------------------------------------------------------------------
    # filter activity data to the requested collection
    # --------------------------------------------------------------------
    if COLLECTIONS_LABEL in tutti_data and isinstance(tutti_data[COLLECTIONS_LABEL], pd.DataFrame):
        tutti_data[COLLECTIONS_LABEL] = tutti_data[COLLECTIONS_LABEL][tutti_data[COLLECTIONS_LABEL]["collection_id"] == collection_id]
        if len(tutti_data[COLLECTIONS_LABEL]) == 0:
            print(f"    [Core datasets] No collections found for id '{collection_id}'")
            return None

    unique_videos = set(tutti_data[COLLECTIONS_LABEL]["item_id"].dropna().values.tolist())
    print(f"    [Core datasets] Found {len(unique_videos):,} unique videos")


    # --------------------------------------------------------------------
    # filter scraped and annotated data
    # --------------------------------------------------------------------
    _filter_enrichment_data(tutti_data, unique_videos, verbose=verbose)


    if verbose:
        _print_dataset_summary(tutti_data)

    print(f"...done. Core datasets loaded for collection '{collection_id}'")

    return tutti_data




# ============================================================================
# Video selection helpers
# ============================================================================


def _build_agg_dict_to_generate_basic_video_stats(study_dataset: pd.DataFrame = None):
    from pandas import NamedAgg

    agg_defs = {
        "nunique_collections": ("collection_id", "nunique"),
        "total_observations": ("collection_id", "count"),
        "scraped_ok": ("scraped_ok", "first"),
        "scraped_fail": ("scraped_fail", "first"),
        "annotated_ok": ("annotated_ok", "first"),
        "annotated_fail": ("annotated_fail", "first"),
        "video_duration": ("video_duration", "max"),
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
    study_dataset: pd.DataFrame = None,
    query_string: str = "",
    verbose: bool = False,
    notebook_mode: bool = False
    ) -> pd.DataFrame:
    """Select and aggregate video-level stats from a merged study dataset, then filter by query."""

    if study_dataset is None:
        raise ValueError("study_dataset must be specified")

    agg_dict, confirmed_cols = _build_agg_dict_to_generate_basic_video_stats(study_dataset)

    video_stats = study_dataset[confirmed_cols].groupby('item_id').agg(**agg_dict)

    if "video_duration" in video_stats.columns:
        video_stats['duration_ok_to_annotate'] = (video_stats['video_duration'] <= fyp_cf["machine"]["max_duration_for_annotation"]).fillna(False)
        video_stats.drop(columns=["video_duration"], inplace=True)
    else:
        video_stats['duration_ok_to_annotate'] = False

    video_stats.fillna(False, inplace=True)
    video_stats.query(query_string, inplace=True)

    return video_stats




# ============================================================================
# Enrichment status
# ============================================================================


def update_enrichment_status(
    all_datasets: dict = {},
    save_to_disk: bool = True,
    verbose: bool = False
    ) -> pd.DataFrame:
    """Rebuild enrichment_status.parquet from collections, scrapes, and annotations."""

    combined_activity_data = all_datasets[COLLECTIONS_LABEL][['item_id', collection_id_column]]

    enrichment_status_df = combined_activity_data.groupby("item_id").agg(
            nunique_collections=pd.NamedAgg(column=collection_id_column, aggfunc="nunique"),
            total_observations=pd.NamedAgg(column=collection_id_column, aggfunc="count")
        )

    annotation_votes = pd.DataFrame()
    if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
        existing = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet", verbose=verbose)
        if "annotation_votes" in existing.columns:
            annotation_votes = existing[["annotation_votes"]].copy()

    enrichment_status_df["nunique_collections"] = enrichment_status_df["nunique_collections"].astype("int64[pyarrow]")

    enrichment_status_df.reset_index(inplace=True)

    most_common_item_id_length = enrichment_status_df["item_id"].str.len().value_counts().index[0]
    enrichment_status_df = enrichment_status_df[enrichment_status_df["item_id"].str.len()==most_common_item_id_length].copy()

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets[SCRAPES_LABEL][['item_id','scraped_ok','video_downloaded']], on='item_id', how='left')

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=all_datasets[MACHINE_ANNOTATIONS_LABEL][['item_id','annotated_ok','annotated_fail']], on='item_id', how='left')

    failed_scrapes = load_failed_scrapes()
    failed_scrapes = pd.DataFrame(failed_scrapes, columns=["item_id"])
    failed_scrapes["scrape_fail"] = True
    failed_scrapes = failed_scrapes.convert_dtypes(dtype_backend="pyarrow")

    enrichment_status_df = pd.merge(left=enrichment_status_df, right=failed_scrapes, on="item_id", how="left").copy()

    enrichment_status_df.set_index("item_id", inplace=True)

    if not annotation_votes.empty:
        enrichment_status_df = pd.merge(left=enrichment_status_df, right=annotation_votes, left_index=True, right_index=True, how="left").copy()
    else:
        enrichment_status_df["annotation_votes"] = pd.Series(0, index=enrichment_status_df.index, dtype="int64[pyarrow]")

    if save_to_disk:
        data_io.save_parquet(df=enrichment_status_df, storage_location="recoded", filename="enrichment_status.parquet", verbose=verbose)

    return enrichment_status_df





def consolidate_enrichment_data(force_consolidation: bool = False, verbose: bool = False) -> dict:
    """Consolidate annotation and scrape data from raw sources, then rebuild enrichment status."""

    print("\n*** Annotations")
    (new_annotations, annotations, new_annotation_ids) = consolidate_and_save_refined_annotations(
        force_consolidation=force_consolidation, verbose=verbose)

    print("\n*** Scrape")
    (new_scrape_data, scrape_data, new_scrape_ids) = consolidate_and_save_scrape_data(
        force_consolidation=force_consolidation, verbose=verbose)

    had_new_data = new_annotations or new_scrape_data

    collections = data_io.load_parquet(filename=f"{COLLECTIONS_LABEL}_recoded.parquet", storage_location="recoded")

    fine_results = {
        COLLECTIONS_LABEL: collections,
        MACHINE_ANNOTATIONS_LABEL: annotations,
        SCRAPES_LABEL: scrape_data
        }

    print("\n*** Updating (and saving) data enrichment status...")
    update_enrichment_status(all_datasets=fine_results, verbose=verbose)
    print("...done.")

    fine_results["had_new_data"] = had_new_data

    # Compute consolidation impact: which collections and studies are affected by new data
    changed_item_ids = new_scrape_ids | new_annotation_ids
    impact = None

    if changed_item_ids and collections is not None and not collections.empty:
        print(f"\n*** Computing consolidation impact for {len(changed_item_ids):,} changed items...")

        affected_collection_ids = set(
            collections.loc[
                collections["item_id"].isin(changed_item_ids),
                collection_id_column
            ].unique()
        )

        if "study_defs" not in fyp_cf:
            init_study_defs()
        affected_studies = []
        for sname, sdef in fyp_cf.get("study_defs", {}).items():
            selected = sdef.get("SELECTED_COLLECTIONS", [])
            if not selected:
                affected_studies.append(sname)
            else:
                cleaned = [
                    re.search(r'\[(.*?)\]', str(s)).group(1) if re.search(r'\[(.*?)\]', str(s)) else str(s)
                    for s in selected
                ]
                if affected_collection_ids & set(cleaned):
                    affected_studies.append(sname)

        impact = {
            "changed_item_count": len(changed_item_ids),
            "new_scrape_item_count": len(new_scrape_ids),
            "new_annotation_item_count": len(new_annotation_ids),
            "affected_collection_ids": sorted(affected_collection_ids),
            "affected_study_names": sorted(affected_studies),
            "timestamp": _dt.datetime.now().isoformat(),
        }
        print(f"    {len(affected_collection_ids)} collection(s) and {len(affected_studies)} study/studies affected.")

    fine_results["impact"] = impact
    return fine_results




# ============================================================================
# Merging datasets
# ============================================================================


def new_merge(
    study_name: str = None,
    all_datasets: dict = {},
    verbose: bool = False,
    save_to_cache: bool = True,
    ) -> pd.DataFrame:
    """Merge activity data with scrape + annotation data, add calculated columns, and optionally cache."""

    print(f"Merging all datasets...")

    if study_name is None and save_to_cache:
        raise ValueError("study_name must be specified")

    if "study_defs" not in fyp_cf:
        init_study_defs()

    if study_name not in fyp_cf["study_defs"].keys() and save_to_cache:
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")

    for k in all_datasets.keys():
        if all_datasets[k] is None:
            print(f"all_datasets['{k}'] is None")


    # merge scrape + annotations into enrichment data
    if all_datasets.get(SCRAPES_LABEL) is not None and all_datasets.get(MACHINE_ANNOTATIONS_LABEL) is not None:
        enriched_data = pd.merge(left=all_datasets[SCRAPES_LABEL], right=all_datasets[MACHINE_ANNOTATIONS_LABEL], on='item_id', how='left')
    elif all_datasets.get(SCRAPES_LABEL) is not None:
        enriched_data = all_datasets[SCRAPES_LABEL]
    elif all_datasets.get(MACHINE_ANNOTATIONS_LABEL) is not None:
        enriched_data = all_datasets[MACHINE_ANNOTATIONS_LABEL]
    else:
        enriched_data = pd.DataFrame()

    if all_datasets.get(COLLECTIONS_LABEL) is not None:
        activity_data = all_datasets[COLLECTIONS_LABEL]
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
    calc_col = ["days_since_created"]
    shebang[calc_col[-1]] = shebang["local_timestamp"] - shebang["createTime"]
    shebang[calc_col[-1]] = shebang[calc_col[-1]].map(lambda x: x.days if x is not pd.NA else pd.NA).astype("int64[pyarrow]")
    shebang[calc_col[-1]] = shebang[calc_col[-1]].clip(lower=0)

    # 2. plays per day
    calc_col += ["plays_per_day"]
    def _safe_vector_divide(x, y):
        return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd.NA)
    shebang[calc_col[-1]] = _safe_vector_divide(shebang['stats_playCount'],shebang['days_since_created'])

    # 3. scraped fail
    failed_scrapes = set(load_failed_scrapes(verbose=verbose))
    calc_col += ["scraped_fail"]
    shebang[calc_col[-1]] = shebang["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")

    # 4. completion rate
    calc_col += ["completion_rate"]
    shebang[calc_col[-1]] = shebang["play_duration"] / shebang["video_duration"]
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




# ============================================================================
# Entry points — create unified datasets
# ============================================================================


def create_study_recoded_dataset(
    study_name: str = None,
    all_datasets: dict = {},
    save_to_cache: bool = True,
    verbose: bool = False
    ) -> pd.DataFrame | None:
    """Generate a unified, merged dataset for a study definition.

    Loads core datasets, applies sampling, merges activity + enrichment data, and caches the result.
    """

    if study_name is None:
        raise ValueError("study_name must be specified")

    if study_name not in fyp_cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    print(f"Generating unified dataset for study '{study_name}'")

    all_datasets = load_study_datasets(
        study_name=study_name,
        all_datasets=all_datasets,
        load_from_cache=True,
        verbose=verbose)

    if all_datasets is None:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None

    study_recoded_dataset = new_merge(
        study_name=study_name,
        all_datasets=all_datasets,
        save_to_cache=save_to_cache,
        verbose=verbose
    )

    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {_df_size_mb(study_recoded_dataset):.2f} MB")

    return study_recoded_dataset





def create_collection_unified_dataset(
    collection_id: str = None,
    verbose: bool = False
    ) -> pd.DataFrame | None:
    """Generate a unified, merged dataset for a single collection.

    Loads core datasets filtered to collection_id, merges activity + enrichment data.
    Not cached (single-collection datasets are typically one-off).
    """

    if collection_id is None:
        raise ValueError("collection_id must be specified")

    print(f"Generating unified dataset for collection '{collection_id}'")

    all_datasets = load_collection_datasets(
        collection_id=collection_id,
        load_from_cache=True,
        verbose=verbose)

    if all_datasets is None:
        print(f"!!! [Core datasets] No activity data matched the collection '{collection_id}'. Returning None")
        return None

    collection_dataset = new_merge(
        study_name=None,
        all_datasets=all_datasets,
        save_to_cache=False,
        verbose=verbose
    )

    print(f"...done. Unified dataset for collection '{collection_id}' generated. Total memory used: {_df_size_mb(collection_dataset):.2f} MB")

    return collection_dataset



