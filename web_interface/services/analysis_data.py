"""PCA and sequence-analysis data accessors.

Pure moves from web_interface/data_service.py (Phase 7c)."""

import threading

from cachetools import LRUCache

import fyp.data_io as data_io
from fyp.pca import calculate_scaled_pca_scores


# --- Explorer State ---


# In-process cache for the per-study PCA score tables. Mtime-keyed so a
# pca_refresh worker rewriting the parquet in another process invalidates
# the RAM copy automatically (same pattern as the sequence caches below).
_pca_cache = LRUCache(maxsize=8)
_pca_cache_lock = threading.Lock()


def _pca_mtime(study_name):
    """Return the mtime of a study's PCA parquet, or None if missing."""
    try:
        filename = f"{study_name}_PCA.parquet"
        if not data_io.exists(storage_location="cache", filename=filename):
            return None
        return data_io.getmtime(storage_location="cache", filename=filename)
    except Exception:
        return None


def get_pca_df(study_name):
    """Return the ``{study}_PCA.parquet`` dataframe, computing it if absent.

    Cached in-process keyed by the parquet's mtime, so a refresh worker
    rewriting the artifact invalidates the RAM copy on the next request.
    When the artifacts are missing entirely they are computed lazily (and
    saved by ``calculate_scaled_pca_scores`` itself).
    """
    if not study_name:
        return None

    pca_filename = f"{study_name}_PCA.parquet"
    comp_inter_filename = f"{study_name}_comp_interpretations.json"

    mtime = _pca_mtime(study_name)
    has_interpretations = data_io.exists(storage_location="cache", filename=comp_inter_filename)

    if mtime is not None and has_interpretations:
        with _pca_cache_lock:
            entry = _pca_cache.get(study_name)
            if entry is not None and entry[0] == mtime:
                return entry[1]
        df = data_io.load_parquet(storage_location="cache", filename=pca_filename)
        with _pca_cache_lock:
            _pca_cache[study_name] = (mtime, df)
        return df

    print("Calculating PCA scores for study: ", study_name)
    result = calculate_scaled_pca_scores(
        study_name=study_name,
        study_recoded_dataset=None,
        minimum_group_size=10,
        target_explained_variance=0.8,
        drop_rare_globally_below=0.01,
        save_to_cache=True,
    )
    df = result[0] if isinstance(result, tuple) else result
    if df is None:
        return None

    mtime = _pca_mtime(study_name)
    if mtime is not None:
        with _pca_cache_lock:
            _pca_cache[study_name] = (mtime, df)
    return df




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




