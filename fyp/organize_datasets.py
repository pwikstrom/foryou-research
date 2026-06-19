
import datetime as _dt
import hashlib
import json
import re
import resource as _resource
import sys as _sys
import time as _time
from copy import deepcopy

import numpy as np
import pandas as pd
import psutil as _psutil

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
import fyp.annotation_versioning as annotation_versioning
from fyp.machine_annotation import consolidate_and_save_refined_annotations
from fyp.polars_ops import fast_join
from fyp.recode_variables import compute_var_schema_hash, derive_australian_relevance, get_grouping_factors_from_var_schema
from fyp.scrape import consolidate_and_save_scrape_data, load_failed_scrapes
from fyp.studies import init_study_defs

collection_id_column = "collection_id"
timestamp_column = "local_timestamp"
event_type_column = "activity_type"


# ----------------------------------------------------------------------------
# Memory-profiling helpers for the merge hot path.
#
# Cloud Run enforces container memory via cgroups; process RSS is a good proxy
# for what the container reports against the 32 GB limit on fyp-task-runner.
# We use psutil for current RSS (cross-platform, bytes) and `getrusage` for
# the watermark since process start (KB on Linux, bytes on macOS).
# ----------------------------------------------------------------------------
_MEM_PROCESS = _psutil.Process()
_RU_MAXRSS_DIVISOR_TO_MB = (
    1024 * 1024 if _sys.platform == "darwin" else 1024
)





def _rss_mb() -> float:
    """Current process resident-set size in MB."""
    return _MEM_PROCESS.memory_info().rss / (1024 * 1024)





def _peak_rss_mb() -> float:
    """High-water-mark RSS of this process since startup, in MB."""
    return (
        _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        / _RU_MAXRSS_DIVISOR_TO_MB
    )

SCRAPES_LABEL = fyp_cf["labels"]["SCRAPES_LABEL"]
MACHINE_ANNOTATIONS_LABEL = fyp_cf["labels"]["MACHINE_ANNOTATIONS_LABEL"]
COLLECTIONS_LABEL = fyp_cf["labels"]["COLLECTIONS_LABEL"]

# Embeddings-derived niche map (see fyp.video_map). The niche columns are
# joined into each study's recoded dataset on item_id so they surface as
# ordinary analysis variables; the map is rebuilt out-of-band, so its
# fingerprint guards study-cache freshness.
_VIDEO_MAP_LOCATION = "recoded"
_VIDEO_MAP_FILE = "video_map.parquet"
_NICHE_COLUMNS = ("niche", "niche_name")
_NICHE_UNMAPPED = "unmapped"


# ============================================================================
# Refresh fingerprinting — sidecar metadata for incremental refresh
# ============================================================================
#
# Each `{study}_recoded.parquet` gets a sidecar `{study}_recoded.meta.json`
# that records fingerprints of every input whose change could invalidate the
# cached output. On refresh, the entry point compares current fingerprints to
# the sidecar to decide between full rebuild, incremental patch, and short-
# circuit (skip entirely). Missing/malformed sidecar -> full rebuild.


_FINGERPRINT_INPUT_FILES = {
    "collections_fp":  ("recoded", f"{COLLECTIONS_LABEL}_recoded.parquet"),
    "scrapes_fp":      ("recoded", f"{SCRAPES_LABEL}_recoded.parquet"),
    "annotations_fp":  ("recoded", f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"),
    "video_map_fp":    (_VIDEO_MAP_LOCATION, _VIDEO_MAP_FILE),
}




def _sidecar_filename(study_name: str) -> str:
    """Return the sidecar filename for a given study's recoded dataset."""
    return f"{study_name}_recoded.meta.json"




def compute_study_config_hash(study_name: str) -> str:
    """Return a deterministic SHA-256 hash of a study's configuration.

    Covers every study-definition field that can change the set of rows or the
    recoded column values: selected collections, date range, sampling mode and
    thresholds. Ordered JSON serialisation keeps the digest stable across
    Python-dict insertion order.
    """

    if "study_defs" not in fyp_cf:
        init_study_defs()
    cfg = fyp_cf["study_defs"].get(study_name, {}) or {}
    # Explicit key list: adding a new key should require a deliberate bump here,
    # and we don't want transient UI-only fields (stats, last_updated) to affect the hash.
    relevant_keys = [
        "SELECTED_COLLECTIONS",
        "START_DATE",
        "END_DATE",
        "SAMPLE_FRAME",
        "MIN_ACTIVITY_COUNT_PER_GROUP",
        "MAX_ACTIVITY_COUNT_PER_GROUP",
        "MIN_GROUP_COUNT_PER_COLLECTION",
        "MAX_GROUP_COUNT_PER_COLLECTION",
        "GROUPING_FACTORS",
    ]
    ordered = {k: cfg.get(k) for k in relevant_keys}
    # SELECTED_COLLECTIONS order shouldn't matter
    if isinstance(ordered.get("SELECTED_COLLECTIONS"), list):
        ordered["SELECTED_COLLECTIONS"] = sorted(str(x) for x in ordered["SELECTED_COLLECTIONS"])
    payload = json.dumps(ordered, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()




def compute_input_fingerprints() -> dict:
    """Stat each core input parquet and return a dict of fingerprint dicts.

    A missing file maps to None in the returned dict so callers can distinguish
    "file not present" from "file unchanged".
    """

    return {
        key: data_io.stat(storage_location=loc, filename=fn)
        for key, (loc, fn) in _FINGERPRINT_INPUT_FILES.items()
    }




def compute_failed_scrapes_fingerprint() -> dict:
    """Return a lightweight fingerprint of the failed-scrapes JSON set.

    `load_failed_scrapes` consolidates multiple JSON files into a single set of
    item_ids; fingerprint is (count, hash-of-sorted-ids). Cheap enough that we
    can compute it at refresh-planning time without paying the file read twice.
    """

    try:
        items = sorted(str(x) for x in load_failed_scrapes(verbose=False))
    except Exception as exc:
        print(f"    [FP] Could not load failed_scrapes for fingerprint: {exc}")
        return {"count": 0, "hash": ""}
    digest = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()
    return {"count": len(items), "hash": digest}




def _hash_item_ids(df: pd.DataFrame) -> str:
    """Return a stable hash of the unique item_ids in a recoded dataset."""
    if df is None or df.empty or "item_id" not in df.columns:
        return "empty"
    ids = sorted(set(df["item_id"].dropna().astype(str).tolist()))
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()




def _extract_selected_cells(recoded_df: pd.DataFrame) -> dict[str, list[str]]:
    """Return {collection_id: [local_date, ...]} for play events in the recoded df.

    The (collection_id, local_date) cells the recoded parquet contains are
    exactly the cells the study admitted post-sampling; the timeline endpoint
    uses this map to filter per-collection day series down to the study view.
    """
    if recoded_df is None or recoded_df.empty:
        return {}
    cols = {"collection_id", "local_date"}
    if not cols.issubset(recoded_df.columns):
        return {}

    df = recoded_df
    if event_type_column in df.columns:
        df = df[df[event_type_column].isin(("play", "observe"))]
    if df.empty:
        return {}

    pairs = df[["collection_id", "local_date"]].dropna().drop_duplicates()
    if pairs.empty:
        return {}

    pairs = pairs.assign(
        collection_id=pairs["collection_id"].astype(str),
        local_date=pd.to_datetime(pairs["local_date"]).dt.strftime("%Y-%m-%d"),
    )
    return {
        cid: sorted(group["local_date"].tolist())
        for cid, group in pairs.groupby("collection_id", sort=False)
    }




def build_sidecar(study_name: str, recoded_df: pd.DataFrame) -> dict:
    """Assemble the sidecar payload for a freshly (re)built recoded dataset."""

    cfg = fyp_cf.get("study_defs", {}).get(study_name, {}) or {}
    sampling_active = str(cfg.get("SAMPLE_FRAME", "off")) != "off"

    fps = compute_input_fingerprints()

    sidecar = {
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "sidecar_version": 3,
        "study_name": study_name,
        "study_config_hash": compute_study_config_hash(study_name),
        "var_schema_hash": compute_var_schema_hash(),
        "sampling_active": sampling_active,
        "collections_fp": fps.get("collections_fp"),
        "scrapes_fp": fps.get("scrapes_fp"),
        "annotations_fp": fps.get("annotations_fp"),
        "video_map_fp": fps.get("video_map_fp"),
        "failed_scrapes_fp": compute_failed_scrapes_fingerprint(),
        "item_ids_hash": _hash_item_ids(recoded_df),
        "row_count": len(recoded_df) if recoded_df is not None else 0,
    }

    if sampling_active:
        sidecar["selected_cells"] = _extract_selected_cells(recoded_df)

    return sidecar




def save_sidecar(study_name: str, recoded_df: pd.DataFrame, verbose: bool = False) -> dict:
    """Build and persist the sidecar for a study; return the payload written."""

    sidecar = build_sidecar(study_name, recoded_df)
    data_io.save_json(
        data=sidecar,
        storage_location="cache",
        filename=_sidecar_filename(study_name),
        verbose=verbose,
    )
    if verbose:
        print(f"    [Sidecar] Wrote {_sidecar_filename(study_name)} (rows={sidecar['row_count']})")
    return sidecar




def load_sidecar(study_name: str, verbose: bool = False) -> dict | None:
    """Load the sidecar for a study, or None if missing/malformed."""

    filename = _sidecar_filename(study_name)
    if not data_io.exists(storage_location="cache", filename=filename):
        return None
    try:
        payload = data_io.load_json(storage_location="cache", filename=filename, verbose=verbose)
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception as exc:
        print(f"    [Sidecar] Could not load '{filename}': {exc}")
        return None




def _fp_equal(a: dict | None, b: dict | None) -> bool:
    """Return True when two stat/fingerprint dicts compare as equal (both None is equal)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b




def plan_refresh(study_name: str, verbose: bool = False) -> dict:
    """Decide the cheapest correct refresh path for a study.

    Compares current input fingerprints against the sidecar and returns a plan:

    - `action`: "short_circuit" | "enrichment_patch" | "video_set_delta" | "full_rebuild"
    - `reasons`: list[str] — human-readable explanation for logging
    - `changed`: dict[str, bool] — which fingerprint categories drifted
    - `old_sidecar`, `current_fps`: raw inputs so callers can reuse them

    In Phase 2 only "short_circuit" vs "full_rebuild" are emitted; the patch
    actions will be decided by this function in Phase 3/4 once the patch paths
    land. Callers should treat unknown actions as "full_rebuild" for safety.
    """

    reasons: list[str] = []
    changed = {
        "cache_missing": False,
        "sidecar_missing": False,
        "var_schema": False,
        "study_config": False,
        "collections": False,
        "scrapes": False,
        "annotations": False,
        "failed_scrapes": False,
        "video_map": False,
    }

    cache_filename = f"{study_name}_recoded.parquet"
    cache_exists = data_io.exists(storage_location="cache", filename=cache_filename)
    sidecar = load_sidecar(study_name, verbose=verbose)

    # Compute current fingerprints once — reused by the caller when a patch path runs.
    current_fps = compute_input_fingerprints()
    current_failed_fp = compute_failed_scrapes_fingerprint()
    current_var_hash = compute_var_schema_hash()
    current_cfg_hash = compute_study_config_hash(study_name)
    cfg = fyp_cf.get("study_defs", {}).get(study_name, {}) or {}
    sampling_active = str(cfg.get("SAMPLE_FRAME", "off")) != "off"

    bundle = {
        "current_fps": current_fps,
        "current_failed_fp": current_failed_fp,
        "current_var_hash": current_var_hash,
        "current_cfg_hash": current_cfg_hash,
        "old_sidecar": sidecar,
        "sampling_active": sampling_active,
    }

    if not cache_exists:
        reasons.append("cache parquet missing")
        changed["cache_missing"] = True
        return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}

    if sidecar is None:
        reasons.append("sidecar missing")
        changed["sidecar_missing"] = True
        return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}

    if sidecar.get("var_schema_hash") != current_var_hash:
        reasons.append("var_schema changed")
        changed["var_schema"] = True

    if sidecar.get("study_config_hash") != current_cfg_hash:
        reasons.append("study_config changed")
        changed["study_config"] = True

    if not _fp_equal(sidecar.get("collections_fp"), current_fps.get("collections_fp")):
        reasons.append("collections parquet changed")
        changed["collections"] = True

    if not _fp_equal(sidecar.get("scrapes_fp"), current_fps.get("scrapes_fp")):
        reasons.append("scrapes parquet changed")
        changed["scrapes"] = True

    if not _fp_equal(sidecar.get("annotations_fp"), current_fps.get("annotations_fp")):
        reasons.append("annotations parquet changed")
        changed["annotations"] = True

    if not _fp_equal(sidecar.get("video_map_fp"), current_fps.get("video_map_fp")):
        reasons.append("video_map parquet changed")
        changed["video_map"] = True

    if not _fp_equal(sidecar.get("failed_scrapes_fp"), current_failed_fp):
        reasons.append("failed_scrapes list changed")
        changed["failed_scrapes"] = True

    if not any(changed.values()):
        reasons.append("all fingerprints match")
        return {"action": "short_circuit", "reasons": reasons, "changed": changed, **bundle}

    # Enrichment-only patch: scrapes / annotations / failed_scrapes changed, but
    # the activity side (collections parquet, study config, var schema) is
    # unchanged. Safe only when SAMPLE_FRAME does not depend on enrichment
    # state — "scraped" / "annotated" modes pick the sample frame from
    # enrichment_status, so any enrichment change can shift which activity rows
    # are kept, which breaks the assumption that we can reuse the cached rows.
    sample_frame = str(cfg.get("SAMPLE_FRAME", "off"))
    enrichment_driven_sampling = sample_frame in ("scraped", "annotated")

    enrichment_bits_changed = (
        changed["scrapes"] or changed["annotations"] or changed["failed_scrapes"]
    )
    activity_bits_changed = (
        changed["var_schema"] or changed["study_config"] or changed["collections"]
    )

    # The enrichment-only patch re-merges scrapes/annotations AND re-joins the
    # niche columns onto the cached activity rows, so it also refreshes a
    # video-map rebuild. A niche remap never shifts which activity rows are
    # sampled, so it stays patch-eligible even under enrichment-driven sampling;
    # only scrape/annotation changes can invalidate that sampling.
    patch_eligible = enrichment_bits_changed or changed["video_map"]

    if patch_eligible and not activity_bits_changed:
        if enrichment_bits_changed and enrichment_driven_sampling:
            reasons.append(
                f"sampling='{sample_frame}' depends on enrichment — forcing full rebuild"
            )
        else:
            return {"action": "enrichment_patch", "reasons": reasons, "changed": changed, **bundle}

    # Phase 4 will add the video-set delta patch here. For now, anything else => full rebuild.
    return {"action": "full_rebuild", "reasons": reasons, "changed": changed, **bundle}




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
        if not data_io.exists(storage_location="recoded", filename=f"{k}_recoded.parquet"):
            if verbose:
                print(f"    [Core datasets] '{k}_recoded.parquet' not present in main storage — treating as empty")
            tutti_data[k] = pd.DataFrame()
            tutti_data[k].attrs["study_name"] = 'everything'
            continue

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
    # Previously we pushed filters=[("item_id", "in", <27k ids>)] into pyarrow.
    # That forced pyarrow to decode every row group and evaluate a 27k-element
    # set-membership predicate per row — slower than just reading the full file.
    # We now load whole files and filter in memory. Parallel loads were tried
    # here but offered no speedup (GIL-serialised decode), so this stays serial.

    # scrape data
    _t_s = _time.perf_counter()
    if tutti_data.get(SCRAPES_LABEL) is None or tutti_data[SCRAPES_LABEL].empty:
        if not data_io.exists(storage_location="recoded", filename=f"{SCRAPES_LABEL}_recoded.parquet"):
            print(f"    [Scrape] '{SCRAPES_LABEL}_recoded.parquet' not present — treating as empty")
            tutti_data[SCRAPES_LABEL] = pd.DataFrame()
        else:
            print("    [Scrape] Loading scraped data from main storage...", flush=True)
            scrapes_df = data_io.load_parquet(
                storage_location="recoded", filename=f"{SCRAPES_LABEL}_recoded.parquet", verbose=verbose)
            if scrapes_df is not None and not scrapes_df.empty and study_name != 'everything':
                scrapes_df = scrapes_df[scrapes_df["item_id"].isin(unique_videos)].copy()
            tutti_data[SCRAPES_LABEL] = scrapes_df if scrapes_df is not None else pd.DataFrame()
            print(f"    [Scrape] ...done. Kept {len(tutti_data[SCRAPES_LABEL]):,} rows in {_time.perf_counter() - _t_s:.2f}s.")
    else:
        cached = tutti_data[SCRAPES_LABEL]
        tutti_data[SCRAPES_LABEL] = cached[cached["item_id"].isin(unique_videos)].copy()
        print(f"    [Scrape] Cache had {len(cached):,} items; {len(tutti_data[SCRAPES_LABEL]):,} overlap with activity datasets.")

    # machine annotations
    _t_a = _time.perf_counter()
    if tutti_data.get(MACHINE_ANNOTATIONS_LABEL) is None or tutti_data[MACHINE_ANNOTATIONS_LABEL].empty:
        if not data_io.exists(storage_location="recoded", filename=f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"):
            print(f"    [Machine annotations] '{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet' not present — treating as empty")
            tutti_data[MACHINE_ANNOTATIONS_LABEL] = pd.DataFrame()
        else:
            print("    [Machine annotations] Loading machine annotations from main storage...", flush=True)
            annotations_df = data_io.load_parquet(
                storage_location="recoded", filename=f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet", verbose=verbose)
            if annotations_df is not None and not annotations_df.empty and study_name != 'everything':
                annotations_df = annotations_df[annotations_df["item_id"].isin(unique_videos)].copy()
            tutti_data[MACHINE_ANNOTATIONS_LABEL] = annotations_df if annotations_df is not None else pd.DataFrame()
            print(f"    [Machine annotations] ...done. Kept {len(tutti_data[MACHINE_ANNOTATIONS_LABEL]):,} rows in {_time.perf_counter() - _t_a:.2f}s.")
    else:
        cached = tutti_data[MACHINE_ANNOTATIONS_LABEL]
        tutti_data[MACHINE_ANNOTATIONS_LABEL] = cached[cached["item_id"].isin(unique_videos)].copy()
        print(f"    [Machine annotations] Cache had {len(cached):,} items; {len(tutti_data[MACHINE_ANNOTATIONS_LABEL]):,} overlap with activity datasets.")





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

    print("    [DDP] Loading data for study...")

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

    # timestamp_column carries times-of-day; a date-only upper bound implicitly
    # means midnight, which excludes same-day events after 00:00:00. Treat the
    # user's END_DATE as "through the end of that day" by shifting the bound
    # to the start of the following day (exclusive).
    END_BOUND = _dt.datetime.combine(END_DATE + _dt.timedelta(days=1), _dt.time.min)

    sel = [(timestamp_column, ">=", START_DATE),(timestamp_column, "<", END_BOUND)]

    the_selected_collections = fyp_cf["study_defs"][study_name].get("SELECTED_COLLECTIONS",[])
    if len(the_selected_collections) > 0:
        the_selected_collections = [str(x) for x in the_selected_collections]
        the_selected_collections = [re.search(r'\[(.*?)\]', s).group(1) if re.search(r'\[(.*?)\]', s) else s for s in the_selected_collections]
        sel.append((collection_id_column, "in", the_selected_collections))

    if all_data is None:
        if verbose:
            print("    [DDP] Loading collection events from main storage")
        out_df = data_io.load_parquet("recoded", f"{COLLECTIONS_LABEL}_recoded.parquet", filters=sel, verbose=verbose)

    else:
        if verbose:
            print("    [DDP] Selecting date range from cached collection data")
        mask = (all_data[timestamp_column] >= START_DATE) & (all_data[timestamp_column] < END_BOUND)
        if len(the_selected_collections) > 0:
            mask = mask & all_data[collection_id_column].isin(the_selected_collections)
        out_df = all_data[mask].copy()

        if collection_id_column not in out_df.columns or timestamp_column not in out_df.columns or len(out_df) == 0:
            print("!!! [DDP] No events found matching the study filters. Returning None.")
            return None

    print(f"    [DDP] ...done. | Shape: {out_df.shape} | Unique collections: {out_df[collection_id_column].nunique()} | Date range: {out_df[timestamp_column].min():%Y-%m-%d} -- {out_df[timestamp_column].max():%Y-%m-%d}")

    return out_df




# ============================================================================
# Sampling
# ============================================================================


def simple_sample_collection_events(
    study_name: str = None,
    all_collections_df: pd.DataFrame = None,
    enrichment_status: pd.DataFrame | None = None,
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
    the_df = all_collections_df

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

    MIN_EVENTS_REQUIRED = fyp_cf["study_defs"][study_name].get("MIN_ACTIVITY_COUNT_PER_GROUP", 30)
    MAX_EVENTS_SELECTED = fyp_cf["study_defs"][study_name].get("MAX_ACTIVITY_COUNT_PER_GROUP", 50)
    MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION = fyp_cf["study_defs"][study_name].get("MIN_GROUP_COUNT_PER_COLLECTION", 20)
    MAX_GROUP_COUNT_SELECTED_PER_COLLECTION = fyp_cf["study_defs"][study_name].get("MAX_GROUP_COUNT_PER_COLLECTION", 200)


    # Filter to viewing events only (play + observe). Non-viewing activity types
    # are dropped — relevant signal from them is folded into adjacent play rows
    # during ingestion (see ingest.py:1335-1407).
    VIEWING_ACTIVITY_TYPES = ("play", "observe")
    all_viewing_events_df = the_df[the_df[event_type_column].isin(VIEWING_ACTIVITY_TYPES)].copy()
    sample_frame_size = len(all_viewing_events_df)

    if verbose:
        n_dropped = len(the_df) - len(all_viewing_events_df)
        print(f"    [Sampling] Viewing events (play+observe): {len(all_viewing_events_df):,}  |  Dropped non-viewing events: {n_dropped:,}")


    if verbose:
        print(f"    [Sampling] Dropping aggregation groups with less than {MIN_EVENTS_REQUIRED} events")
        print(f"    [Sampling] Sampling at most {MAX_EVENTS_SELECTED} events from each remaining group. This might take a moment...")
    # select agg groups with the required number of events
    viewing_events_within_agg_group_size_limits = _filter_and_sample(all_viewing_events_df, grouping_factors, MIN_EVENTS_REQUIRED, MAX_EVENTS_SELECTED, rng)
    if verbose:
        sample_size = len(viewing_events_within_agg_group_size_limits)
        if sample_frame_size > 0:
            print(f"    [Sampling] Viewing events after sampling: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    # build a df with unique pairs of the two group factors
    unique_group_factor_pairs = viewing_events_within_agg_group_size_limits[grouping_factors].drop_duplicates()

    # Track Stage 2 selection effects for pre-check reporting:
    #   - excluded: collections with fewer than MIN post-Stage-1 cells
    #   - downsampled: collections with more than MAX post-Stage-1 cells (capped to MAX)
    cells_per_collection = unique_group_factor_pairs.groupby(grouping_factors[0]).size()
    n_excluded_collections = int((cells_per_collection < MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION).sum())
    n_downsampled_collections = int((cells_per_collection > MAX_GROUP_COUNT_SELECTED_PER_COLLECTION).sum())

    if verbose:
        print(f"    [Sampling] Dropping collections with less than {MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION} aggregation groups within the limits")
        print(f"    [Sampling] Sampling at most {MAX_GROUP_COUNT_SELECTED_PER_COLLECTION} aggregation groups from each remaining collection. This might take a moment...")
    # select collections with a required number of groups
    collections_within_group_count_limits = _filter_and_sample(unique_group_factor_pairs, grouping_factors[:1], MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION, MAX_GROUP_COUNT_SELECTED_PER_COLLECTION, rng)
    if verbose:
        print(f"    [Sampling] Aggregation groups remaining after sampling: {len(collections_within_group_count_limits):,}")

    selected_pairs_index = collections_within_group_count_limits.set_index(grouping_factors).index

    # ----------------------------------------------------------------------
    # find the viewing events in the selected groups
    viewing_events_in_candidate_groups = viewing_events_within_agg_group_size_limits.set_index(grouping_factors)

    # use isin() boolean mask instead of .loc[MultiIndex] to avoid potential reindexing
    viewing_events_in_selected_groups = viewing_events_in_candidate_groups[
        viewing_events_in_candidate_groups.index.isin(selected_pairs_index)
    ].reset_index()
    if verbose:
        sample_size = len(viewing_events_in_selected_groups)
        if sample_frame_size > 0:
            print(f"    [Sampling] Viewing events remaining in the sampled aggregation groups: {sample_size:,} ({sample_size/sample_frame_size:.0%} of original)")

    combined = viewing_events_in_selected_groups
    if verbose:
        print(f"    [Sampling] Sampled viewing events: {len(combined):,} in {len(combined[grouping_factors].drop_duplicates()):,} groups")
    combined.drop("D_id", axis=1, inplace=True, errors='ignore')

    # Surface selection effects so callers (pre-check) can show them to the user.
    combined.attrs['sampling_report'] = {
        'n_excluded_collections': n_excluded_collections,
        'n_downsampled_collections': n_downsampled_collections,
        'min_cells_per_collection': MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION,
        'max_cells_per_collection': MAX_GROUP_COUNT_SELECTED_PER_COLLECTION,
    }


    # Caller is responsible for passing enrichment_status if the summary is wanted.
    # We deliberately do not reload from GCS here — an earlier version did, which
    # caused a duplicate read of enrichment_status.parquet per study refresh.
    enrichment_status_df = enrichment_status

    combined_deduped = combined.drop_duplicates(subset="item_id", keep="first")[["item_id"]]

    if enrichment_status_df is None:
        print("    [Sampling] No enrichment_status available — skipping enrichment summary")
        print(f"    [Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[grouping_factors].drop_duplicates()):,} groups")
        print(f"    [Sampling] - Unique items: {len(combined_deduped):,}")
        return combined

    # Ensure item_id is the index for the merge (callers may pass it as a column)
    if 'item_id' in enrichment_status_df.columns:
        enrichment_status_df = enrichment_status_df.set_index('item_id')

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
    enrichment_status: pd.DataFrame | None = None,
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

    elif len(all_datasets) > 0:
        tutti_data = deepcopy(all_datasets)
        if verbose:
            print(f"    [Core datasets] Using in-memory core datasets provided as argument: {len(tutti_data)} dataframes provided")
    else:
        tutti_data = {}
        if verbose:
            print("    [Core datasets] Starting without precomputed core datasets. Loading study core datasets from main storage.")


    # --------------------------------------------------------------------
    # load and filter activity data
    # --------------------------------------------------------------------
    tutti_data["collections"] = load_collection_data(
        study_name=study_name, all_data=tutti_data.get("collections"), verbose=verbose)

    for k in tutti_data.keys():
        if tutti_data.get(k) is None:
            tutti_data[k] = pd.DataFrame()

    if tutti_data.get("collections", pd.DataFrame()).empty:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        return None


    # --------------------------------------------------------------------
    # sample activity data
    # --------------------------------------------------------------------
    sample_frame_setting = fyp_cf["study_defs"][study_name].get("SAMPLE_FRAME", "off")

    if sample_frame_setting == "off":
        print("    [DD Sampling] Sample frame setting is 'off'. Not sampling collection data.")
        sample_frame = None

    elif sample_frame_setting in ("events", "activities"):
        # 'activities' (formerly 'events') doesn't need enrichment_status — use all collection events as the frame.
        sample_frame = tutti_data["collections"].copy()
        print(f"    [DD Sampling] Sample frame setting is '{sample_frame_setting}'. Using all {len(sample_frame):,} collection events as sample frame.")

    else:
        # 'scraped' and 'annotated' require enrichment_status to pick rows.
        if enrichment_status is None:
            if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
                enrichment_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")
            else:
                print("    [DD Sampling] 'enrichment_status.parquet' not present — no enrichment data available yet")

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
                print("!!! [DD Sampling] Sample frame setting is 'scraped' but no enrichment_status is available. Returning None")
                return None
            selected_videos = enrichment_status[enrichment_status["scraped_ok"]].index.tolist()
            sample_frame = tutti_data["collections"][tutti_data["collections"]["item_id"].isin(selected_videos)].copy()
            print(f"    [DD Sampling] Sample frame setting is 'scraped'. Using only {len(sample_frame):,} collection events that are scraped as sample frame.")

        elif sample_frame_setting == "annotated":
            if enrichment_status is None:
                print("!!! [DD Sampling] Sample frame setting is 'annotated' but no enrichment_status is available. Returning None")
                return None
            selected_videos = enrichment_status[enrichment_status["annotated_ok"]].index.tolist()
            sample_frame = tutti_data["collections"][tutti_data["collections"]["item_id"].isin(selected_videos)].copy()
            print(f"    [DD Sampling] Sample frame setting is 'annotated'. Using only {len(sample_frame):,} collection events that are annotated as sample frame.")

    if sample_frame is not None:
        tutti_data["collections"] = simple_sample_collection_events(
            study_name=study_name, all_collections_df=sample_frame,
            enrichment_status=enrichment_status, verbose=verbose)

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
            print("    [Core datasets] Loading core datasets from main storage.")
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

    scrapes_for_merge = all_datasets.get(SCRAPES_LABEL)
    if scrapes_for_merge is not None and not scrapes_for_merge.empty and {'item_id', 'scraped_ok', 'video_downloaded'}.issubset(scrapes_for_merge.columns):
        enrichment_status_df = pd.merge(left=enrichment_status_df, right=scrapes_for_merge[['item_id','scraped_ok','video_downloaded']], on='item_id', how='left')
    else:
        enrichment_status_df["scraped_ok"] = pd.Series(False, index=enrichment_status_df.index, dtype="bool[pyarrow]")
        enrichment_status_df["video_downloaded"] = pd.Series(False, index=enrichment_status_df.index, dtype="bool[pyarrow]")

    annotations_for_merge = all_datasets.get(MACHINE_ANNOTATIONS_LABEL)
    if annotations_for_merge is not None and not annotations_for_merge.empty and {'item_id', 'annotated_ok', 'annotated_fail'}.issubset(annotations_for_merge.columns):
        enrichment_status_df = pd.merge(left=enrichment_status_df, right=annotations_for_merge[['item_id','annotated_ok','annotated_fail']], on='item_id', how='left')
    else:
        enrichment_status_df["annotated_ok"] = pd.Series(False, index=enrichment_status_df.index, dtype="bool[pyarrow]")
        enrichment_status_df["annotated_fail"] = pd.Series(False, index=enrichment_status_df.index, dtype="bool[pyarrow]")

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

        # Drop NA collection_ids — legacy raw_files predating the manifest-based
        # ingest can leave orphan rows with no cid. They don't belong to any
        # collection or study so they shouldn't contribute to impact; including
        # them would also break the sorted() below (NA comparisons raise).
        affected_collection_ids = {
            cid for cid in collections.loc[
                collections["item_id"].isin(changed_item_ids),
                collection_id_column
            ].unique()
            if pd.notna(cid)
        }

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
            "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        }
        print(f"    {len(affected_collection_ids)} collection(s) and {len(affected_studies)} study/studies affected.")

    fine_results["impact"] = impact
    return fine_results




# ============================================================================
# Merging datasets
# ============================================================================


def _join_niche_columns(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """Left-join the embeddings-derived niche columns onto a study dataframe.

    Adds ``niche_name`` (readable categorical) and its integer ``niche`` id from
    ``video_map.parquet``, keyed on ``item_id``, so the niche surfaces as an
    ordinary analysis variable. Videos absent from the map (not yet
    embedded/clustered) get ``"unmapped"`` and a null id. Idempotent: existing
    niche columns are dropped first so a re-merge after a map rebuild refreshes
    them cleanly, and any niche column the map does not provide is backfilled as
    unmapped.

    Args:
        df: Merged study dataframe; a no-op when it lacks ``item_id``.
        verbose: Print join diagnostics.

    Returns:
        The dataframe with the niche columns present.
    """
    if "item_id" not in df.columns:
        return df

    df = df.drop(columns=[c for c in _NICHE_COLUMNS if c in df.columns], errors="ignore")

    available: set[str] = set()
    if data_io.exists(storage_location=_VIDEO_MAP_LOCATION, filename=_VIDEO_MAP_FILE):
        available = set(
            data_io.get_parquet_columns(
                storage_location=_VIDEO_MAP_LOCATION, filename=_VIDEO_MAP_FILE
            ) or []
        )

    join_cols = [c for c in _NICHE_COLUMNS if c in available]
    if "item_id" in available and join_cols:
        niche_map = data_io.load_parquet_selective(
            storage_location=_VIDEO_MAP_LOCATION,
            filename=_VIDEO_MAP_FILE,
            columns=["item_id", *join_cols],
        )
        niche_map["item_id"] = niche_map["item_id"].astype("string[pyarrow]")
        df["item_id"] = df["item_id"].astype("string[pyarrow]")
        df = fast_join(df, niche_map, on="item_id", how="left")

    n = len(df)
    for col in _NICHE_COLUMNS:
        is_name = col.endswith("name")
        if col not in df.columns:
            df[col] = pd.array(
                [_NICHE_UNMAPPED if is_name else pd.NA] * n,
                dtype="string[pyarrow]" if is_name else "int32[pyarrow]",
            )
        elif is_name:
            df[col] = df[col].astype("string[pyarrow]").fillna(_NICHE_UNMAPPED)

    if verbose:
        mapped = int((df["niche_name"] != _NICHE_UNMAPPED).sum())
        print(f"  Joined niche columns: {mapped:,}/{n:,} rows mapped to a niche")
    return df






def _annotations_for_study(study_name, annotations_df):
    """Return the annotations a study should merge against.

    If the study pins a specific ``annotation_version`` in its definition, the
    pinned version's rows are read from the version archive (strict, for
    reproducibility). Otherwise the supplied active annotations are used
    unchanged.

    Args:
        study_name: The study being merged, or ``None``.
        annotations_df: The active annotations frame loaded for the merge.

    Returns:
        The annotations frame to merge against.
    """
    if not study_name:
        return annotations_df
    study_def = fyp_cf.get("study_defs", {}).get(study_name, {}) or {}
    pin = study_def.get("annotation_version")
    if not pin:
        return annotations_df
    archive_fn = f"{MACHINE_ANNOTATIONS_LABEL}_all_versions.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        print(f"    [new_merge] study '{study_name}' pinned to {pin} but archive missing; using active annotations.")
        return annotations_df
    archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if archive is None or archive.empty:
        return annotations_df
    pinned = annotation_versioning.select_version_view(archive, pin)
    print(f"    [new_merge] study '{study_name}' pinned to annotation_version={pin}: {len(pinned):,} annotations.")
    return pinned




def new_merge(
    study_name: str = None,
    all_datasets: dict = {},
    verbose: bool = False,
    save_to_cache: bool = True,
    ) -> pd.DataFrame:
    """Merge activity data with scrape + annotation data, add calculated columns, and optionally cache."""

    print("Merging all datasets...")

    if study_name is None and save_to_cache:
        raise ValueError("study_name must be specified")

    if "study_defs" not in fyp_cf:
        init_study_defs()

    if study_name not in fyp_cf["study_defs"].keys() and save_to_cache:
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")

    for k in all_datasets:
        if all_datasets[k] is None:
            print(f"all_datasets['{k}'] is None")


    # merge scrape + annotations into enrichment data
    scrapes_df = all_datasets.get(SCRAPES_LABEL)
    annotations_df = _annotations_for_study(study_name, all_datasets.get(MACHINE_ANNOTATIONS_LABEL))
    has_scrapes = scrapes_df is not None and not scrapes_df.empty
    has_annotations = annotations_df is not None and not annotations_df.empty

    if has_scrapes and has_annotations:
        enriched_data = pd.merge(left=scrapes_df, right=annotations_df, on='item_id', how='left')
    elif has_scrapes:
        enriched_data = scrapes_df
    elif has_annotations:
        enriched_data = annotations_df
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
        print("No enriched data — caching activity-only dataset")
        shebang = activity_data.copy()
    else:
        # Biggest join in the pipeline: events × item-metadata, keyed on
        # item_id. Polars' parallel hash join is substantially faster and
        # more memory-efficient than pandas at events-scale (tens of
        # millions of rows). See fyp/polars_ops.py.
        shebang = fast_join(activity_data, enriched_data, on='item_id', how='left')

        # --------------------------------------------------------------------------------------------------
        # adding some calculated columns to this merged dataset
        # --------------------------------------------------------------------------------------------------

        # 1. days since created
        calc_col = ["days_since_created"]
        if "local_timestamp" in shebang.columns and "createTime" in shebang.columns:
            shebang[calc_col[-1]] = shebang["local_timestamp"] - shebang["createTime"]
            shebang[calc_col[-1]] = shebang[calc_col[-1]].map(lambda x: x.days if x is not pd.NA else pd.NA).astype("int64[pyarrow]")
            shebang[calc_col[-1]] = shebang[calc_col[-1]].clip(lower=0)
        else:
            shebang[calc_col[-1]] = pd.Series(pd.NA, index=shebang.index, dtype="int64[pyarrow]")

        # 2. plays per day
        calc_col += ["plays_per_day"]
        def _safe_vector_divide(x, y):
            return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd.NA)
        if "stats_playCount" in shebang.columns and "days_since_created" in shebang.columns and not shebang["days_since_created"].isna().all():
            shebang[calc_col[-1]] = _safe_vector_divide(shebang['stats_playCount'],shebang['days_since_created'])
        else:
            shebang[calc_col[-1]] = pd.Series(pd.NA, index=shebang.index, dtype="double[pyarrow]")

        # 3. scraped fail
        failed_scrapes = set(load_failed_scrapes(verbose=verbose))
        calc_col += ["scraped_fail"]
        shebang[calc_col[-1]] = shebang["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")

        # 4. completion rate
        calc_col += ["completion_rate"]
        if "play_duration" in shebang.columns and "video_duration" in shebang.columns:
            shebang[calc_col[-1]] = shebang["play_duration"] / shebang["video_duration"]
            shebang[calc_col[-1]] = shebang[calc_col[-1]].clip(lower=0,upper=1).astype("double[pyarrow]")
        else:
            shebang[calc_col[-1]] = pd.Series(pd.NA, index=shebang.index, dtype="double[pyarrow]")

        if verbose:
            print(f"Adding columns: {calc_col}. Resulting output log DF shape {shebang.shape}")
    # --------------------------------------------------------------------------------------------------

    # Backfill australian_relevance from primary_country for rows annotated under
    # the generalized contract (primary_country replaced it); older-version rows
    # keep their model-output value. No-op when primary_country is absent.
    shebang = derive_australian_relevance(shebang)

    # Join the embeddings-derived niche columns (item_id-keyed) so they surface
    # as ordinary analysis variables in the explore / timeline / correlation
    # tabs. Runs for both the merge and activity-only branches.
    shebang = _join_niche_columns(shebang, verbose=verbose)

    if save_to_cache:
        t1 = _dt.datetime.now()
        if verbose:
            print(f"  Saving the '{study_name}' dataset to cache...")
        shebang.attrs['study_name'] = study_name
        data_io.save_parquet(
            df=shebang,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            asyncronous=True,
            verbose=verbose)
        if verbose:
            print(f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds")

    print(f"...done. Merged all datasets. Shape: {shebang.shape}")

    return shebang




# ============================================================================
# Incremental refresh — enrichment-only patch
# ============================================================================


# Columns computed by new_merge() *after* the enrichment merge. Must be dropped
# from the cached recoded dataset before re-merging, otherwise new_merge would
# produce _x/_y suffixed duplicates.
_CALCULATED_ENRICHMENT_COLUMNS = {
    "days_since_created",
    "plays_per_day",
    "scraped_fail",
    "completion_rate",
    # Niche columns are re-joined by new_merge() via _join_niche_columns(), so
    # drop the cached copies before re-merging to avoid _x/_y suffixing.
    *_NICHE_COLUMNS,
}




def apply_enrichment_only_patch(
    study_name: str,
    verbose: bool = False,
) -> pd.DataFrame | None:
    """Re-merge fresh enrichment onto the cached activity rows of a study.

    Intended for the case where `plan_refresh` reports that only scrapes /
    annotations / failed_scrapes changed. Skips the (expensive) collections
    load and sampling entirely: reads the existing `{study}_recoded.parquet`,
    drops enrichment + calculated columns, re-loads scrapes + annotations
    filtered to the cached item_id set, then calls `new_merge` to rebuild the
    merged dataset. Writes both the new parquet and a fresh sidecar.

    Returns the new dataframe on success with ``attrs["refresh_action"] =
    "enrichment_patch"``. Returns None when the cached dataset is missing or
    unreadable so the caller can fall through to a full rebuild.
    """

    cache_filename = f"{study_name}_recoded.parquet"
    _t0 = _time.perf_counter()
    _rss_start = _rss_mb()
    _peak_start = _peak_rss_mb()
    cached_df = data_io.load_parquet(
        storage_location="cache", filename=cache_filename, verbose=verbose
    )
    if cached_df is None or cached_df.empty:
        print(f"    [EnrichPatch] Cached '{cache_filename}' missing/empty — aborting patch")
        return None

    if "item_id" not in cached_df.columns:
        print("    [EnrichPatch] Cached dataset missing 'item_id' column — aborting patch")
        return None

    scrape_filename = f"{SCRAPES_LABEL}_recoded.parquet"
    annot_filename = f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet"
    scrape_schema_cols = set(data_io.get_parquet_columns(storage_location="recoded", filename=scrape_filename) or [])
    annot_schema_cols = set(data_io.get_parquet_columns(storage_location="recoded", filename=annot_filename) or [])

    # Columns we will recompute in new_merge(): anything sourced from scrapes or
    # annotations, plus the four calculated columns. item_id stays — it is the
    # join key and also lives in the activity data.
    enrichment_and_calc = (scrape_schema_cols | annot_schema_cols | _CALCULATED_ENRICHMENT_COLUMNS) - {"item_id"}
    activity_cols = [c for c in cached_df.columns if c not in enrichment_and_calc]

    activity_df = cached_df[activity_cols].copy()
    unique_videos = set(activity_df["item_id"].dropna().astype(str).unique().tolist())
    print(
        f"    [EnrichPatch] Reusing {len(activity_df):,} activity rows / "
        f"{len(unique_videos):,} unique items; dropping "
        f"{len(cached_df.columns) - len(activity_cols)} enrichment/calc columns"
    )

    # Free the original cached dataframe before loading enrichment to cap peak memory.
    del cached_df

    tutti_data: dict = {
        COLLECTIONS_LABEL: activity_df,
        SCRAPES_LABEL: None,
        MACHINE_ANNOTATIONS_LABEL: None,
    }
    _t_enrich = _time.perf_counter()
    _filter_enrichment_data(tutti_data, unique_videos, study_name=study_name, verbose=verbose)
    _t_enrich = _time.perf_counter() - _t_enrich

    _t_merge = _time.perf_counter()
    result = new_merge(
        study_name=study_name,
        all_datasets=tutti_data,
        save_to_cache=True,
        verbose=verbose,
    )
    _t_merge = _time.perf_counter() - _t_merge

    if result is None or result.empty:
        print("    [EnrichPatch] Merge returned empty — aborting patch (caller should full-rebuild)")
        return None

    # Block until the async parquet write in new_merge finishes before writing
    # the sidecar — otherwise a concurrent refresh could load a stale sidecar
    # that points at a half-written parquet.
    try:
        with data_io.file_lock:
            pass
        save_sidecar(study_name=study_name, recoded_df=result, verbose=verbose)
    except Exception as exc:
        print(f"    [Sidecar] Non-fatal: failed to write sidecar after enrichment patch: {exc}")

    result.attrs["refresh_action"] = "enrichment_patch"
    result.attrs["study_name"] = study_name

    _t_total = _time.perf_counter() - _t0
    _rss_end = _rss_mb()
    _peak_end = _peak_rss_mb()
    print(
        f"[ENRICH PATCH][TIMING] study={study_name} "
        f"enrichment_load={_t_enrich:.2f}s merge={_t_merge:.2f}s "
        f"total={_t_total:.2f}s rows={len(result):,}"
    )
    print(
        f"[ENRICH PATCH][MEM] study={study_name} "
        f"rss_start={_rss_start:.0f}MB "
        f"rss_end={_rss_end:.0f}MB "
        f"peak_during={_peak_end:.0f}MB "
        f"peak_delta=+{(_peak_end - _peak_start):.0f}MB "
        f"df_size={_df_size_mb(result):.0f}MB"
    )
    return result




# ============================================================================
# Entry points — create unified datasets
# ============================================================================


def create_study_recoded_dataset(
    study_name: str = None,
    all_datasets: dict = {},
    save_to_cache: bool = True,
    load_from_cache: bool = True,
    enrichment_status: pd.DataFrame | None = None,
    force_full_rebuild: bool = False,
    verbose: bool = False
    ) -> pd.DataFrame | None:
    """Generate a unified, merged dataset for a study definition.

    Loads core datasets, applies sampling, merges activity + enrichment data, and caches the result.

    When `save_to_cache=True` and the refresh sidecar reports that no input has
    changed since the cached recoded parquet was written, this function returns
    the cached dataframe without rebuilding. Callers can inspect
    `df.attrs["refresh_action"]` to tell short-circuited loads ("short_circuit")
    from full rebuilds ("full_rebuild"). Pass `force_full_rebuild=True` to
    bypass the sidecar check.
    """

    if study_name is None:
        raise ValueError("study_name must be specified")

    if study_name not in fyp_cf["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    # Sidecar-guided refresh: fingerprint inputs and pick the cheapest correct
    # path. Saves both I/O and CPU for the "user clicked refresh but nothing
    # actually changed" case and for enrichment-only trickle-in updates.
    if save_to_cache and not force_full_rebuild:
        plan = plan_refresh(study_name, verbose=verbose)
        print(
            f"[REFRESH PLAN] study={study_name} action={plan['action']} "
            f"reasons={'; '.join(plan['reasons']) or 'no sidecar or changed inputs'}"
        )

        if plan["action"] == "short_circuit":
            cached_df = data_io.load_parquet(
                storage_location="cache",
                filename=f"{study_name}_recoded.parquet",
                verbose=verbose,
            )
            if cached_df is not None and not cached_df.empty:
                cached_df.attrs["refresh_action"] = "short_circuit"
                cached_df.attrs["refresh_plan"] = plan
                cached_df.attrs["study_name"] = study_name
                return cached_df
            # Cache surprisingly unreadable/empty — fall through to full rebuild.
            print(
                "    [Sidecar] Short-circuit aborted: cached parquet unreadable/empty. "
                "Falling through to full rebuild."
            )

        elif plan["action"] == "enrichment_patch":
            patched_df = apply_enrichment_only_patch(study_name=study_name, verbose=verbose)
            if patched_df is not None and not patched_df.empty:
                patched_df.attrs["refresh_plan"] = plan
                return patched_df
            # Patch refused (missing cache, empty merge, etc.) — fall through.
            print(
                "    [EnrichPatch] Patch path aborted — falling through to full rebuild."
            )

    print(f"Generating unified dataset for study '{study_name}'")

    # Memory baseline before any heavy lifting. We sample RSS at each phase
    # so the single [RECODE][MEM] log line gives enough resolution to tell
    # whether the load, the merge, or something in between dominates peak
    # memory — critical for sizing the Cloud Run task-runner container.
    _rss_start = _rss_mb()
    _peak_start = _peak_rss_mb()

    _t_phase = _time.perf_counter()
    all_datasets = load_study_datasets(
        study_name=study_name,
        all_datasets=all_datasets,
        load_from_cache=load_from_cache,
        enrichment_status=enrichment_status,
        verbose=verbose)
    _t_load = _time.perf_counter() - _t_phase
    _rss_after_load = _rss_mb()

    if all_datasets is None:
        print(f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None")
        print(f"[RECODE][TIMING] study={study_name} load={_t_load:.2f}s merge=0.00s total={_t_load:.2f}s")
        return None

    _t_phase = _time.perf_counter()
    study_recoded_dataset = new_merge(
        study_name=study_name,
        all_datasets=all_datasets,
        save_to_cache=save_to_cache,
        verbose=verbose
    )
    _t_merge = _time.perf_counter() - _t_phase
    _rss_after_merge = _rss_mb()
    _peak_end = _peak_rss_mb()

    # Preserve the sampling selection-effect report so pre-check UI can surface it.
    sampling_report = None
    if isinstance(all_datasets, dict):
        collections_df = all_datasets.get("collections")
        if collections_df is not None and hasattr(collections_df, 'attrs'):
            sampling_report = collections_df.attrs.get('sampling_report')
    if sampling_report and study_recoded_dataset is not None:
        study_recoded_dataset.attrs['sampling_report'] = sampling_report

    # Write the refresh sidecar alongside the recoded parquet so future refresh
    # calls can fingerprint inputs and skip redundant rebuilds. `new_merge`
    # submits the parquet save to a background thread guarded by
    # `data_io.file_lock`; acquire the same lock here to wait for the write to
    # finish before writing the sidecar. Otherwise a subsequent refresh could
    # load the sidecar, trust it, and try to read a half-written parquet.
    if save_to_cache and study_recoded_dataset is not None and not study_recoded_dataset.empty:
        try:
            with data_io.file_lock:
                pass
            save_sidecar(study_name=study_name, recoded_df=study_recoded_dataset, verbose=verbose)
        except Exception as exc:
            print(f"    [Sidecar] Non-fatal: failed to write sidecar for '{study_name}': {exc}")

    if study_recoded_dataset is not None:
        study_recoded_dataset.attrs["refresh_action"] = "full_rebuild"

    print(f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {_df_size_mb(study_recoded_dataset):.2f} MB")
    print(
        f"[RECODE][TIMING] study={study_name} "
        f"load={_t_load:.2f}s merge={_t_merge:.2f}s "
        f"total={(_t_load + _t_merge):.2f}s"
    )
    # Peak-delta is the max additional RSS claimed by the process during
    # this function relative to when we entered — the number that actually
    # dictates whether the 32 GB task-runner container is enough headroom
    # for this study.
    print(
        f"[RECODE][MEM] study={study_name} "
        f"rss_start={_rss_start:.0f}MB "
        f"rss_after_load={_rss_after_load:.0f}MB "
        f"rss_after_merge={_rss_after_merge:.0f}MB "
        f"peak_during={_peak_end:.0f}MB "
        f"peak_delta=+{(_peak_end - _peak_start):.0f}MB "
        f"df_size={_df_size_mb(study_recoded_dataset):.0f}MB"
    )

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



