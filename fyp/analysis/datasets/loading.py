"""Loading the core datasets: collection activity, scrapes and annotations for a study or a collection."""

import datetime as _dt
import re
import time as _time
from copy import deepcopy

import pandas as pd

import fyp.core.data_io as data_io
from fyp.analysis.datasets import common, sampling
from fyp.analysis.studies import init_study_defs
from fyp.core.artifacts import ENRICHMENT_STATUS_FILE
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.memory import df_size_mb as _df_size_mb
from fyp.core.runtime import cf as _cf

logger = get_logger(__name__)


def _load_cached_core_datasets(verbose: bool = False) -> dict:
    """Load core datasets (scrape, annotations, collections) from cache or main storage.

    Tries the local cache first. If a dataset is not cached and main storage is on GCS,
    loads from GCS and saves a local cache copy for future use.

    Returns:
        Dict with keys SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL COLLECTIONS_LABEL from the config (values may be None).
    """
    core_datasets: dict = {}

    for k in [
        common._scrapes_label(),
        common._machine_annotations_label(),
        common._collections_label(),
    ]:
        core_datasets[k] = None

        # try loading from local cache
        if data_io.exists(storage_location="cache", filename=f"core_{k}.parquet"):
            parquet_study_name = data_io.find_key_value_in_pq_metadata(
                storage_location="cache", filename=f"core_{k}.parquet", the_key="study_name"
            )
            if parquet_study_name == "everything":
                if verbose:
                    logger.info(
                        f"    [Core datasets] Loading '{k}' from cache (study: '{parquet_study_name}')..."
                    )
                core_datasets[k] = data_io.load_parquet(
                    storage_location="cache", filename=f"core_{k}.parquet"
                )
                continue

        # fallback: load from main storage
        if not data_io.exists(storage_location="recoded", filename=f"{k}_recoded.parquet"):
            if verbose:
                logger.info(
                    f"    [Core datasets] '{k}_recoded.parquet' not present in main storage — treating as empty"
                )
            core_datasets[k] = pd.DataFrame()
            core_datasets[k].attrs["study_name"] = "everything"
            continue

        if verbose:
            logger.info(f"    [Core datasets] Loading '{k}' from main storage...")
        core_datasets[k] = data_io.load_parquet(
            storage_location="recoded", filename=f"{k}_recoded.parquet"
        )
        core_datasets[k].attrs["study_name"] = "everything"

        # if main storage is GCS and cache is local, persist to cache for next time
        if _cf()["data_io"]["use_gcs_for_data"] and not _cf()["data_io"]["use_gcs_for_cache"]:
            if verbose:
                logger.info(f"    [Core datasets] Saving '{k}' to local cache...")
            data_io.save_parquet(
                df=core_datasets[k], storage_location="cache", filename=f"core_{k}.parquet"
            )

    return core_datasets


def _filter_enrichment_data(
    core_datasets: dict, unique_videos: set, study_name: str | None = None, verbose: bool = False
) -> None:
    """Load and filter scrape + annotation data to match the videos in the activity data.

    Modifies core_datasets in place: updates the 'scrape' and 'machine_annotations' entries.
    If the data is already present (from cache), it is filtered. Otherwise it is loaded from
    main storage with a parquet filter.
    """
    # Previously we pushed filters=[("item_id", "in", <27k ids>)] into pyarrow.
    # That forced pyarrow to decode every row group and evaluate a 27k-element
    # set-membership predicate per row — slower than just reading the full file.
    # We now load whole files and filter in memory. Parallel loads were tried
    # here but offered no speedup (GIL-serialised decode), so this stays serial.

    # scrape data
    if (
        core_datasets.get(common._scrapes_label()) is None
        or core_datasets[common._scrapes_label()].empty
    ):
        core_datasets[common._scrapes_label()] = _load_enrichment_frame(
            common._scrapes_label(), "Scrape", "scraped data", unique_videos, study_name, verbose
        )
    else:
        cached = core_datasets[common._scrapes_label()]
        core_datasets[common._scrapes_label()] = cached[
            cached["item_id"].isin(unique_videos)
        ].copy()
        logger.info(
            f"    [Scrape] Cache had {len(cached):,} items; {len(core_datasets[common._scrapes_label()]):,} overlap with activity datasets."
        )

    # machine annotations
    if (
        core_datasets.get(common._machine_annotations_label()) is None
        or core_datasets[common._machine_annotations_label()].empty
    ):
        core_datasets[common._machine_annotations_label()] = _load_enrichment_frame(
            common._machine_annotations_label(),
            "Machine annotations",
            "machine annotations",
            unique_videos,
            study_name,
            verbose,
        )
    else:
        cached = core_datasets[common._machine_annotations_label()]
        core_datasets[common._machine_annotations_label()] = cached[
            cached["item_id"].isin(unique_videos)
        ].copy()
        logger.info(
            f"    [Machine annotations] Cache had {len(cached):,} items; {len(core_datasets[common._machine_annotations_label()]):,} overlap with activity datasets."
        )


# Run-scoped stash of the full enrichment frames, or None when no run holds one.
# See enrichment_preload.
_ENRICHMENT_PRELOAD: dict[str, pd.DataFrame] | None = None


class enrichment_preload:
    """Hold each enrichment blob in memory for the duration of a multi-study run.

    Every study refresh ends in :func:`_filter_enrichment_data`, which reads
    ``scrapes_recoded.parquet`` (327 MB) and ``machine_annotations_recoded
    .parquet`` (479 MB) from storage and keeps the study's rows. Refreshing
    five studies in one process therefore downloaded the same two blobs five
    times — measured at 3.5 GB and 54 s of a 179 s run. Inside this
    context the first study to need a blob loads it and parks the full frame
    here; later studies filter the parked copy. Nothing is loaded up front, so
    a study that short-circuits costs nothing, and the enrichment-patch path
    benefits as much as a full rebuild.

    The price is memory: both frames stay resident for the run (~3 GB on top
    of a peak that was ~9 GB with per-study load/free, on a 32 GB runner).
    ``__exit__`` drops them and collects, so nothing outlives the run.
    Re-entrant: an inner block defers to the outer owner.
    """

    def __enter__(self):
        global _ENRICHMENT_PRELOAD
        self._owner = _ENRICHMENT_PRELOAD is None
        if self._owner:
            _ENRICHMENT_PRELOAD = {}
        return self

    def __exit__(self, *exc):
        global _ENRICHMENT_PRELOAD
        if not self._owner:
            return False
        held, _ENRICHMENT_PRELOAD = _ENRICHMENT_PRELOAD, None
        if held:
            logger.info(
                f"    [Enrichment preload] Released {len(held)} frame(s) held for this run."
            )
            held.clear()
            import gc

            gc.collect()
        return False


def _load_enrichment_frame(
    label: str,
    tag: str,
    what: str,
    unique_videos: set,
    study_name: str | None,
    verbose: bool,
) -> pd.DataFrame:
    """The ``<label>_recoded.parquet`` rows for ``unique_videos``.

    Serves from the run-scoped preload when one is active and already holds
    the label, loads (and, inside a preload, parks) the full frame otherwise.
    ``study_name == 'everything'`` means no filtering.
    """
    fn = f"{label}_recoded.parquet"
    t0 = _time.perf_counter()
    stash = _ENRICHMENT_PRELOAD
    if stash is not None and label in stash:
        full = stash[label]
        source = "run preload"
    else:
        if not data_io.exists(storage_location="recoded", filename=fn):
            logger.info(f"    [{tag}] '{fn}' not present — treating as empty")
            return pd.DataFrame()
        logger.info(f"    [{tag}] Loading {what} from main storage...")
        full = data_io.load_parquet(storage_location="recoded", filename=fn, verbose=verbose)
        if full is None:
            full = pd.DataFrame()
        source = "main storage"
        if stash is not None:
            stash[label] = full
            logger.info(
                f"    [{tag}] Holding the full frame for the rest of this run "
                f"({len(full):,} rows, {_df_size_mb(full):.0f} MB)."
            )
    if not full.empty and study_name != "everything":
        out = full[full["item_id"].isin(unique_videos)].copy()
    else:
        # A parked frame must never be handed out by reference.
        out = full.copy() if stash is not None else full
    logger.info(
        f"    [{tag}] ...done. Kept {len(out):,} rows in "
        f"{_time.perf_counter() - t0:.2f}s ({source})."
    )
    return out


def _print_dataset_summary(core_datasets: dict) -> None:
    """Print a summary of the datasets in core_datasets."""
    if core_datasets is None:
        logger.info("    [Core datasets] - None")
        return
    logger.info("    [Core datasets] Datasets:")
    for k in core_datasets:
        if core_datasets[k] is not None:
            logger.info(
                f"    [Core datasets] - '{k}': {core_datasets[k].shape[0]:,}[R] x {core_datasets[k].shape[1]:,}[C] ({_df_size_mb(core_datasets[k]):.1f}MB)"
            )


# ============================================================================
# Loading collection activity data
# ============================================================================


def load_collection_data(
    study_name: str = None, all_data: pd.DataFrame | None = None, verbose: bool = False
) -> pd.DataFrame | None:
    """Load and filter collection activity data for a study definition.

    If all_data is None, loads from main storage with parquet filters.
    If all_data is provided, filters the cached DataFrame in memory.
    """

    if study_name is None:
        raise ValueError("!!! [DDP] study_name must be specified")

    logger.info("    [DDP] Loading data for study...")

    if "study_defs" not in _cf():
        init_study_defs()

    START_DATE = _cf()["study_defs"][study_name].get("START_DATE", "1970-01-01")
    if isinstance(START_DATE, str):
        try:
            START_DATE = _dt.datetime.strptime(START_DATE, "%Y-%m-%d").date()
        except ValueError:
            START_DATE = _dt.datetime(1970, 1, 1).date()

    END_DATE = _cf()["study_defs"][study_name].get("END_DATE", "2099-12-31")
    if isinstance(END_DATE, str):
        try:
            END_DATE = _dt.datetime.strptime(END_DATE, "%Y-%m-%d").date()
        except ValueError:
            END_DATE = _dt.datetime(2099, 12, 31).date()

    # timestamp_column carries times-of-day; a date-only upper bound implicitly
    # means midnight, which excludes same-day events after 00:00:00. Treat the
    # user's END_DATE as "through the end of that day" by shifting the bound
    # to the start of the following day (exclusive).
    END_BOUND = _dt.datetime.combine(END_DATE + _dt.timedelta(days=1), _dt.time.min)

    sel = [(common.timestamp_column, ">=", START_DATE), (common.timestamp_column, "<", END_BOUND)]

    the_selected_collections = _cf()["study_defs"][study_name].get("SELECTED_COLLECTIONS", [])
    if len(the_selected_collections) > 0:
        the_selected_collections = [str(x) for x in the_selected_collections]
        the_selected_collections = [
            re.search(r"\[(.*?)\]", s).group(1) if re.search(r"\[(.*?)\]", s) else s
            for s in the_selected_collections
        ]
        sel.append((common.collection_id_column, "in", the_selected_collections))

    if all_data is None:
        if verbose:
            logger.info("    [DDP] Loading collection events from main storage")
        out_df = data_io.load_parquet(
            "recoded",
            f"{common._collections_label()}_recoded.parquet",
            filters=sel,
            verbose=verbose,
        )

    else:
        if verbose:
            logger.info("    [DDP] Selecting date range from cached collection data")
        mask = (all_data[common.timestamp_column] >= START_DATE) & (
            all_data[common.timestamp_column] < END_BOUND
        )
        if len(the_selected_collections) > 0:
            mask = mask & all_data[common.collection_id_column].isin(the_selected_collections)
        out_df = all_data[mask].copy()

        if (
            common.collection_id_column not in out_df.columns
            or common.timestamp_column not in out_df.columns
            or len(out_df) == 0
        ):
            logger.warning("!!! [DDP] No events found matching the study filters. Returning None.")
            return None

    logger.info(
        f"    [DDP] ...done. | Shape: {out_df.shape} | Unique collections: {out_df[common.collection_id_column].nunique()} | Date range: {out_df[common.timestamp_column].min():%Y-%m-%d} -- {out_df[common.timestamp_column].max():%Y-%m-%d}"
    )

    return out_df


# ============================================================================
# Loading core datasets (activity + scrape + annotations)
# ============================================================================


def load_study_datasets(
    study_name: str = None,
    all_datasets: dict = {},
    load_from_cache: bool = True,
    enrichment_status: pd.DataFrame | None = None,
    verbose: bool = False,
) -> dict | None:
    """Load all core datasets for a study: collections, scrape data, and machine annotations.

    Handles caching, date-range filtering, and optional sampling based on the study definition.
    """

    if study_name is None:
        raise ValueError("study_name must be specified")

    if "study_defs" not in _cf():
        init_study_defs()

    if study_name not in _cf()["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    logger.info(f"Loading core datasets for study '{study_name}'...")

    # load core datasets from cache or main storage
    if load_from_cache and not _cf()["data_io"]["use_gcs_for_cache"]:
        core_datasets = _load_cached_core_datasets(verbose=verbose)

    elif len(all_datasets) > 0:
        core_datasets = deepcopy(all_datasets)
        if verbose:
            logger.info(
                f"    [Core datasets] Using in-memory core datasets provided as argument: {len(core_datasets)} dataframes provided"
            )
    else:
        core_datasets = {}
        if verbose:
            logger.info(
                "    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage."
            )

    # --------------------------------------------------------------------
    # load and filter activity data
    # --------------------------------------------------------------------
    core_datasets["collections"] = load_collection_data(
        study_name=study_name, all_data=core_datasets.get("collections"), verbose=verbose
    )

    for k in core_datasets.keys():
        if core_datasets.get(k) is None:
            core_datasets[k] = pd.DataFrame()

    if core_datasets.get("collections", pd.DataFrame()).empty:
        logger.warning(
            f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None"
        )
        return None

    # --------------------------------------------------------------------
    # sample activity data
    # --------------------------------------------------------------------
    sample_frame_setting = _cf()["study_defs"][study_name].get("SAMPLE_FRAME", "off")

    if sample_frame_setting == "off":
        logger.info(
            "    [DD Sampling] Sample frame setting is 'off'. Not sampling collection data."
        )
        sample_frame = None

    elif sample_frame_setting in ("events", "activities"):
        # 'activities' (formerly 'events') doesn't need enrichment_status — use all collection events as the frame.
        sample_frame = core_datasets["collections"].copy()
        logger.info(
            f"    [DD Sampling] Sample frame setting is '{sample_frame_setting}'. Using all {len(sample_frame):,} collection events as sample frame."
        )

    else:
        # 'scraped' and 'annotated' require enrichment_status to pick rows.
        if enrichment_status is None:
            if data_io.exists(storage_location="recoded", filename=ENRICHMENT_STATUS_FILE):
                enrichment_status = data_io.load_parquet(
                    storage_location="recoded", filename=ENRICHMENT_STATUS_FILE
                )
            else:
                logger.info(
                    "    [DD Sampling] 'enrichment_status.parquet' not present — no enrichment data available yet"
                )

        # Callers may pass enrichment_status with item_id as either the index or
        # a column (run_recode_refresh_studies resets it to a column so the same
        # df can be reused for downstream column-based matching). Normalise to
        # item_id-as-index here so `.index.tolist()` below returns string ids, not
        # integer row positions — which would surface as a PyArrow type mismatch
        # when the resulting list is passed to `isin` on a string[pyarrow] column.
        if enrichment_status is not None and "item_id" in enrichment_status.columns:
            enrichment_status = enrichment_status.set_index("item_id")

        if sample_frame_setting == "scraped":
            if enrichment_status is None:
                logger.warning(
                    "!!! [DD Sampling] Sample frame setting is 'scraped' but no enrichment_status is available. Returning None"
                )
                return None
            selected_videos = enrichment_status[enrichment_status["scraped_ok"]].index.tolist()
            sample_frame = core_datasets["collections"][
                core_datasets["collections"]["item_id"].isin(selected_videos)
            ].copy()
            logger.info(
                f"    [DD Sampling] Sample frame setting is 'scraped'. Using only {len(sample_frame):,} collection events that are scraped as sample frame."
            )

        elif sample_frame_setting == "annotated":
            if enrichment_status is None:
                logger.warning(
                    "!!! [DD Sampling] Sample frame setting is 'annotated' but no enrichment_status is available. Returning None"
                )
                return None
            selected_videos = enrichment_status[enrichment_status["annotated_ok"]].index.tolist()
            sample_frame = core_datasets["collections"][
                core_datasets["collections"]["item_id"].isin(selected_videos)
            ].copy()
            logger.info(
                f"    [DD Sampling] Sample frame setting is 'annotated'. Using only {len(sample_frame):,} collection events that are annotated as sample frame."
            )

    if sample_frame is not None:
        core_datasets["collections"] = sampling.simple_sample_collection_events(
            study_name=study_name,
            all_collections_df=sample_frame,
            enrichment_status=enrichment_status,
            verbose=verbose,
        )

    if core_datasets.get("collections", pd.DataFrame()).empty:
        logger.warning(
            f"!!! [Core datasets] Sampling resulted in empty datasets for study definition '{study_name}'. Returning None"
        )
        return None

    # --------------------------------------------------------------------
    # load scraped and annotated data
    # --------------------------------------------------------------------
    unique_videos = set(core_datasets["collections"]["item_id"].dropna().values.tolist())
    logger.info(
        f"    [Core datasets] Found {len(unique_videos):,} unique videos in activity datasets"
    )

    _filter_enrichment_data(core_datasets, unique_videos, study_name=study_name, verbose=verbose)

    if verbose:
        _print_dataset_summary(core_datasets)

    logger.info(f"...done. Core datasets loaded for study '{study_name}'")

    return core_datasets


def load_collection_datasets(
    collection_id: str = None, load_from_cache: bool = True, verbose: bool = False
) -> dict | None:
    """Load all core datasets for a single collection.

    Similar to load_study_datasets but filters by collection_id instead of a study definition.
    No sampling is performed.
    """

    logger.info(f"Loading core datasets for collection '{collection_id}'...")

    if load_from_cache and not _cf()["data_io"]["use_gcs_for_cache"]:
        core_datasets = _load_cached_core_datasets(verbose=verbose)
    else:
        core_datasets = {}
        if verbose:
            logger.info("    [Core datasets] Loading core datasets from main storage.")
        for k in [
            common._scrapes_label(),
            common._machine_annotations_label(),
            common._collections_label(),
        ]:
            core_datasets[k] = data_io.load_parquet(
                storage_location="recoded", filename=f"{k}_recoded.parquet"
            )

    # --------------------------------------------------------------------
    # filter activity data to the requested collection
    # --------------------------------------------------------------------
    if common._collections_label() in core_datasets and isinstance(
        core_datasets[common._collections_label()], pd.DataFrame
    ):
        core_datasets[common._collections_label()] = core_datasets[common._collections_label()][
            core_datasets[common._collections_label()]["collection_id"] == collection_id
        ]
        if len(core_datasets[common._collections_label()]) == 0:
            logger.info(f"    [Core datasets] No collections found for id '{collection_id}'")
            return None

    unique_videos = set(
        core_datasets[common._collections_label()]["item_id"].dropna().values.tolist()
    )
    logger.info(f"    [Core datasets] Found {len(unique_videos):,} unique videos")

    # --------------------------------------------------------------------
    # filter scraped and annotated data
    # --------------------------------------------------------------------
    _filter_enrichment_data(core_datasets, unique_videos, verbose=verbose)

    if verbose:
        _print_dataset_summary(core_datasets)

    logger.info(f"...done. Core datasets loaded for collection '{collection_id}'")

    return core_datasets
