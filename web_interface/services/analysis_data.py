"""PCA and sequence-analysis data accessors.

Pure moves from web_interface/data_service.py (Phase 7c)."""

import threading

from cachetools import LRUCache

import fyp.data_io as data_io
from fyp.pca import calculate_scaled_pca_scores


# --- Explorer State ---


pca_df_cache = {}

def get_pca_df(study_name):


    global pca_df_cache
    if study_name in pca_df_cache:
        # Check freshness? Simple version: just return.
        return pca_df_cache[study_name]

    print("Loading PCA scores for study: ", study_name)

    # Load file
    if True:# try:
        
        pca_filename = f"{study_name}_PCA.parquet"
        comp_inter_filename = f"{study_name}_comp_interpretations.json"

        if data_io.exists(storage_location="cache", filename=pca_filename) and data_io.exists(storage_location="cache", filename=comp_inter_filename):         
            print("Loading PCA scores for study from cache: ", study_name)
            events_pca_scores_scaled = data_io.load_parquet(
                storage_location="cache",
                filename=pca_filename,
                )

        else:
            print("Calculating PCA scores for study: ", study_name)
            events_pca_scores_scaled, _ = calculate_scaled_pca_scores(
                study_name = study_name,
                study_recoded_dataset = None,
                minimum_group_size = 10,
                target_explained_variance = 0.8,
                drop_rare_globally_below = 0.01,
            )
            if events_pca_scores_scaled is None:
                return None
            data_io.save_parquet(
                df=events_pca_scores_scaled,
                storage_location="cache",
                filename=pca_filename,
            )

        pca_df_cache[study_name] = events_pca_scores_scaled
        return events_pca_scores_scaled




# In-process caches for sequence-analysis artifacts. Unlike pca_df_cache these
# are mtime-checked so a worker rewriting the artifact in another process
# invalidates the RAM copy automatically (same pattern as StudyCache).
_sequence_cache = LRUCache(maxsize=4)
_sequence_cache_lock = threading.Lock()


def _sequence_mtime(study_name, suffix):
    """Return the mtime of a study's sequence artifact, or None if missing."""
    filename = f"{study_name}_sequence{suffix}"
    try:
        if not data_io.exists(storage_location="cache", filename=filename):
            return None
        return data_io.getmtime(storage_location="cache", filename=filename)
    except Exception:
        return None


def get_sequence_summary(study_name):
    """Return the parsed ``{study}_sequence_summary.json`` dict, or None if absent.

    Cached in-process keyed by mtime; self-invalidates when a refresh worker
    rewrites the summary.
    """
    if not study_name:
        return None
    mtime = _sequence_mtime(study_name, "_summary.json")
    if mtime is None:
        return None
    key = ("summary", study_name)
    with _sequence_cache_lock:
        entry = _sequence_cache.get(key)
        if entry is not None and entry[0] == mtime:
            return entry[1]
    try:
        payload = data_io.load_json(
            storage_location="cache", filename=f"{study_name}_sequence_summary.json"
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    with _sequence_cache_lock:
        _sequence_cache[key] = (mtime, payload)
    return payload


def get_sequence_df(study_name):
    """Return the per-window ``{study}_sequence.parquet`` dataframe, or None if absent.

    Cached in-process keyed by mtime so different horizons/views can be derived
    on read without re-loading. Self-invalidates on worker rewrite.
    """
    if not study_name:
        return None
    mtime = _sequence_mtime(study_name, ".parquet")
    if mtime is None:
        return None
    key = ("df", study_name)
    with _sequence_cache_lock:
        entry = _sequence_cache.get(key)
        if entry is not None and entry[0] == mtime:
            return entry[1]
    try:
        df = data_io.load_parquet(
            storage_location="cache", filename=f"{study_name}_sequence.parquet"
        )
    except Exception:
        return None
    with _sequence_cache_lock:
        _sequence_cache[key] = (mtime, df)
    return df




