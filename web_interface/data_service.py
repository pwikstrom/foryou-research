import json
import threading
import time

import numpy as np
import pandas as pd
from cachetools import LRUCache
from sklearn.metrics import cohen_kappa_score

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL, create_collection_unified_dataset
from fyp.pca import calculate_scaled_pca_scores
from fyp.studies import init_study_defs
from fyp.utils import ACTIVITY_TYPE_MAP, ENGAGEMENT_TYPES

from . import explorer_backend as explorer

# --- Explorer State ---

class StudyCache:
    def __init__(self, maxsize=2):
        self.cache = LRUCache(maxsize=maxsize)
        self.lock = threading.Lock()

    def get(self, study_name, current_mtime=None):
        """Return the cached entry, evicting it first if the on-disk parquet
        has been rewritten since this entry was cached. ``current_mtime`` is
        the file mtime the caller just observed; pass ``None`` to skip the
        staleness check (e.g. when the file isn't accessible)."""
        with self.lock:
            entry = self.cache.get(study_name)
            if entry is None:
                return None
            cached_mtime = entry.get('mtime')
            if current_mtime is not None and cached_mtime is not None:
                # Treat any mtime change as stale — workers (potentially in
                # another process) may have rewritten the parquet.
                if current_mtime != cached_mtime:
                    del self.cache[study_name]
                    return None
            return entry

    def put(self, study_name, data):
        with self.lock:
            self.cache[study_name] = data

    def invalidate(self, study_name):
        with self.lock:
            if study_name in self.cache:
                del self.cache[study_name]




study_cache = StudyCache(maxsize=2)


# Hard-coded display order of UI sections. Variables in sections not listed here
# sort after these, alphabetically. Used by ``load_schema_metadata`` to order
# every web variable list and exposed to the frontend as ``section_order``.
SECTION_ORDER = ["Activity", "Item metadata", "Popularity", "AI Annotations"]

# ``scale`` values that count as categorical for the categorical-before-numerical
# ordering rule. Everything else (numeric/datetime/blank) is numerical.
_CAT_SCALES = {
    "categorical", "list", "text",
}




def _get_recoded_mtime(study):
    """Return the on-disk mtime of the study's recoded parquet, or ``None``
    if the file is missing / unreadable. Used to detect stale RAM cache
    entries when the parquet is refreshed by a worker subprocess."""
    try:
        filename = f"{study}_recoded.parquet"
        if not data_io.exists(storage_location="cache", filename=filename):
            return None
        return data_io.getmtime(storage_location="cache", filename=filename)
    except Exception:
        return None




# In-process cache for study sidecars. Sidecars carry the (cid, day) cell map
# the timeline endpoint uses to filter per-collection day series down to the
# study view; on Cloud Run each fetch crosses GCS, so caching matters even
# though parsing is cheap.
_sidecar_cache = LRUCache(maxsize=8)
_sidecar_cache_lock = threading.Lock()


def _get_sidecar_mtime(study):
    """Return the mtime of a study's sidecar JSON, or ``None`` if missing."""
    try:
        filename = f"{study}_recoded.meta.json"
        if not data_io.exists(storage_location="cache", filename=filename):
            return None
        return data_io.getmtime(storage_location="cache", filename=filename)
    except Exception:
        return None


def get_study_sidecar(study):
    """Return the parsed sidecar dict for a study, or ``None`` if absent.

    Cached in-process keyed by (study_name, sidecar mtime) so repeated timeline
    requests within a session don't re-fetch / re-parse the JSON. Self-
    invalidates when the sidecar is rewritten by a refresh worker.
    """
    if not study:
        return None
    mtime = _get_sidecar_mtime(study)
    if mtime is None:
        return None
    cache_key = study
    with _sidecar_cache_lock:
        entry = _sidecar_cache.get(cache_key)
        if entry is not None and entry[0] == mtime:
            return entry[1]
    try:
        payload = data_io.load_json(
            storage_location="cache",
            filename=f"{study}_recoded.meta.json",
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    with _sidecar_cache_lock:
        _sidecar_cache[cache_key] = (mtime, payload)
    return payload




def _enrichment_status(raw_df, require_annotated):
    """Describe whether the loaded recoded dataset has the column needed to
    satisfy the current ``require_annotated_items`` setting. Returned dict
    is attached to the filtered DataFrame via ``df.attrs`` so routes can
    surface a clear "stale data" message instead of a silent empty result.
    """
    needed = "annotated_ok" if require_annotated else "scraped_ok"
    if needed in raw_df.columns:
        return {
            "ok": True,
            "missing_column": None,
            "message": None,
        }
    if require_annotated:
        msg = (
            "This study's recoded dataset is missing the 'annotated_ok' column. "
            "Refresh the study to pick up the latest enrichment data."
        )
    else:
        msg = (
            "This study's recoded dataset is missing the 'scraped_ok' column. "
            "Refresh the study to pick up the latest enrichment data."
        )
    return {
        "ok": False,
        "missing_column": needed,
        "message": msg,
    }




def get_explorer_data(study, context=None, verbose=False):
    # Capture parquet mtime up front so a worker rewriting the file in another
    # process invalidates this Flask process's RAM cache automatically on the
    # next request.
    current_mtime = _get_recoded_mtime(study)

    # Check cache (First Check)
    cached = study_cache.get(study, current_mtime=current_mtime)

    # Store raw data in cache, filter on retrieval
    raw_df = None
    raw_col_types = None

    if cached:
        if verbose:
            print(f"    Study {study} found in RAM cache. Accessing {len(cached['df']):,} rows")
        raw_df = cached['df']
        raw_col_types = cached['col_types']
    else:
        # Double-Checked Locking
        if not hasattr(study_cache, 'loading_lock'):
             study_cache.loading_lock = threading.Lock()

        with study_cache.loading_lock:
            # Check cache again (Second Check)
            cached = study_cache.get(study, current_mtime=current_mtime)
            if cached:
                if verbose:
                    print(f"    Study {study} found in RAM cache (after lock). Accessing {len(cached['df']):,} rows")
                raw_df = cached['df']
                raw_col_types = cached['col_types']
            else:
                if verbose:
                    print(f"    Loading study {study} from disk (with lock)...")
                # Resolve path
                raw_df, raw_col_types = explorer.load_data(study, verbose=False)

                if raw_df is None:
                    if verbose:
                        print("The requested recoded study dataset was not found")
                    return None, None

                # Re-read the mtime *after* loading so the cache entry is
                # tagged with the version we actually have in memory.
                cache_item = {
                    "df": raw_df,
                    "col_types": raw_col_types,
                    "mtime": _get_recoded_mtime(study),
                }
                study_cache.put(study, cache_item)

    # Apply Context Filtering on a COPY
    if raw_df is not None:
        # annotated_ok / scraped_ok only exist once the corresponding enrichment
        # data has been merged in. Without it (fresh app) the columns may be
        # missing — treat as all-False so nothing leaks through.
        # When require_annotated_items is False we still require scraped_ok so that
        # the Video Analysis viewer always has a media file to play and Explore has
        # populated scrape metadata. Items not yet scraped are excluded either way.
        require_annotated = fyp_cf.get("viz", {}).get("require_annotated_items", True)
        status = _enrichment_status(raw_df, require_annotated)

        if require_annotated:
            if "annotated_ok" in raw_df.columns:
                enrichment_mask = raw_df["annotated_ok"].fillna(False)
            else:
                enrichment_mask = pd.Series(False, index=raw_df.index)
        else:
            if "scraped_ok" in raw_df.columns:
                enrichment_mask = raw_df["scraped_ok"].fillna(False)
            else:
                enrichment_mask = pd.Series(False, index=raw_df.index)

        if context in ("viewer", "explorer"):
            filtered_df = raw_df[
                enrichment_mask
                & (raw_df['activity_type'].isin(['play', 'observe']))
                & (raw_df['item_id'].notna())
            ].copy()
        else:
            # return raw copy to be safe. this should never happen though...
            filtered_df = raw_df.copy()

        # Stash the dataset status on the DataFrame so routes can surface a
        # clear message when an empty result is caused by missing enrichment
        # columns (i.e. the recoded parquet predates the current pipeline).
        try:
            filtered_df.attrs['fyp_dataset_status'] = status
        except Exception:
            pass

        return filtered_df, raw_col_types.copy()

    return None, None





def enrich_with_user_tags(df, col_types, username, shared_users_tags=None):
    """
    Injects a 'User Tags' column into the DataFrame based on the user's tag file.
    Returns (enriched_df, enriched_col_types).
    If no tags found, returns original.
    """
    tag_filename = f"{username}_tags.json"
    filename = f"{username}.json"
    user_data = {}
    user_tags = {}
    
    # Try loading exact match first
    if data_io.exists(storage_location="users", filename=filename):
        user_data = data_io.load_json(storage_location="users", filename=filename) or {}
    else:
        # Fallback to lowercase
        filename_lower = f"{username.lower()}.json"
        if data_io.exists(storage_location="users", filename=filename_lower):
             user_data = data_io.load_json(storage_location="users", filename=filename_lower) or {}

    if user_data:
        user_tags = user_data.get('annotations', {})
        
    # user_tags: { item_id: { var: [tags...] } }
    # We want a map: item_id -> unique list of tags (flattened across variables)
    
    # Pre-calculate map for Tags
    id_to_tags = {}
    
    # Set of IDs with ANY annotation (tags, notes, closed tags)
    annotated_ids = set()
    
    for item_id, var_map in user_tags.items():
        # If item is in user_tags, it has SOME annotation (due to cleanup logic on save)
        annotated_ids.add(str(item_id))
        
        # Collect explicit tags for the list column
        all_tags = set()
        for key, val in var_map.items():
            if isinstance(val, list): # It's a tag list
                all_tags.update(val)
                
        if all_tags:
            id_to_tags[str(item_id)] = list(all_tags)
            
    # Merge Shared Tags
    if shared_users_tags:
        for iid, tags in shared_users_tags.items():
            str_id = str(iid)
            annotated_ids.add(str_id) # Ensure ID is marked as annotated
            
            # Update id_to_tags
            if str_id in id_to_tags:
                existing = set(id_to_tags[str_id])
                existing.update(tags)
                id_to_tags[str_id] = list(existing)
            else:
                id_to_tags[str_id] = list(tags)

    # Was: if not annotated_ids: return df, col_types
    # We continue now to ensure "Has Annotation" and "Machine Annotations" are added even if empty.
        

    # Copy to avoid modifying cache
    df = df.copy()
    col_types = col_types.copy()
    
    str_ids = df['item_id'].astype(str) # just to be safe. item_id should always be a string
    
    # 1. User Tags (List)
    if id_to_tags:
        df['User Tags'] = str_ids.map(id_to_tags)
        
        # Fill NaNs with empty lists (crucial for type safety in filters)
        df['User Tags'] = df['User Tags'].apply(lambda x: x if isinstance(x, list) else [])
        
        if df['User Tags'].apply(len).sum() > 0: # Check if any tags exist
             col_types['User Tags'] = 'list'
        else:
             df.drop(columns=['User Tags'], inplace=True, errors='ignore')
    
    # 2. Has Annotation (Boolean/Category)
    if shared_users_tags:
        annotated_ids.update(str(k) for k in shared_users_tags.keys())

    
    df['Has Annotation'] = str_ids.isin(annotated_ids)
    
    # Only keep if there are any true values? Or always keep if explicit user request?
    # If no annotations exist at all, we returned early above.
    # So we have annotations.
    col_types['Has Annotation'] = 'category' # Treat as category to trigger checkbox UI
    
    # 3. Machine Annotations (New Request)
    # Check if annotated_ok exists
    if 'annotated_ok' in df.columns:
        # Map boolean to cleaner labels
        df['Machine Annotations'] = 'Not Attempted'
        df.loc[df['annotated_ok'] == True, 'Machine Annotations'] = 'Machine Annotated'
        df.loc[df['annotated_ok'] == False, 'Machine Annotations'] = 'Cannot Machine Annotate'
        
        col_types['Machine Annotations'] = 'category'
    
    return df, col_types


def load_shared_tags(allowed_usernames):
    """
    Loads tags from a list of usernames.
    Returns:
        simple_map: { item_id: set(tags) } (For DF Enrichment)
        detailed_map: { item_id: { variable: { user: { tags: [], notes: ... } } } } (For Viewer Details)
    """
    simple_map = {}
    detailed_map = {}
    
    if not allowed_usernames:
        return simple_map, detailed_map

    for user in allowed_usernames:
        try:
            filename = f"{user}.json"
            
            # Check exist
            user_blob = None
            if data_io.exists(storage_location="users", filename=filename):
                user_blob = data_io.load_json(storage_location="users", filename=filename)
            else:
                 # Check lowercase
                 filename_lower = f"{user.lower()}.json"
                 if data_io.exists(storage_location="users", filename=filename_lower):
                     user_blob = data_io.load_json(storage_location="users", filename=filename_lower)
            
            if not user_blob:
                continue
                
            user_data = user_blob.get('annotations', {})
            
            if not user_data:
                continue
            
            for item_id, item_vars in user_data.items():
                str_id = str(item_id)
                
                # --- Simple Map (All tags flattened) ---
                if str_id not in simple_map: simple_map[str_id] = set()
                
                # --- Detailed Map ---
                if str_id not in detailed_map: detailed_map[str_id] = {}
                
                for var, val in item_vars.items():
                    # Parse Special Keys
                    real_var = var
                    type_ = 'tags'
                    
                    if var.endswith('__NOTES'):
                        real_var = var[:-7]
                        type_ = 'notes'
                    elif var.endswith('__CLOSED_TAGGING'):
                        real_var = var[:-16]
                        type_ = 'closed'
                    
                    # Ensure struct
                    if real_var not in detailed_map[str_id]: detailed_map[str_id][real_var] = {}
                    if user not in detailed_map[str_id][real_var]: 
                        detailed_map[str_id][real_var][user] = {'tags': [], 'notes': None, 'closed': None}
                    
                    entry = detailed_map[str_id][real_var][user]
                    
                    if type_ == 'tags':
                        if isinstance(val, list):
                            simple_map[str_id].update(val)
                            entry['tags'] = val
                    elif type_ == 'notes':
                        entry['notes'] = val
                    elif type_ == 'closed':
                        entry['closed'] = val
                        
        except Exception as e:
            print(f"Error loading tokens for {user}: {e}")
            
    return simple_map, detailed_map





TIMELINE_SCHEMA_VERSION = 7
# Marker columns that prove a cached timeline parquet was written by the
# current schema.  Any one missing → cache is stale and gets regenerated.
# Bump TIMELINE_SCHEMA_VERSION and edit this set whenever the parquet
# schema or universe definition changes.
_TIMELINE_REQUIRED_COLUMNS: set[str] = {
    'machine_state_counts',         # original v1 marker
    'weighted_video_total',         # v2: per-period attention denominator
    'timeline_universe',            # v3: universe = scraped+annotated plays only
    'fave',                         # v4: engagement type breakdown columns
    'follow',                       # v5: engagement breakdown via activity_type
}


def check_and_update_timeline_cache(collection_id, viz_vars, verbose=False, preloaded_df=None):
    """Ensures that timeline aggregation for day exists in cache.

    If not, calculates it from the unified collection dataset.

    Returns:
        dict mapping interval name to aggregated DataFrame, or None on failure.
        Truthy when successful (backward-compatible with old bool return).
    """

    intervals = ['day']
    missing = []

    # Check if files exist and have the v2 marker columns.  Any cache from
    # a prior schema is regenerated; the dependent analysis JSON is also
    # invalidated so the two never go out of sync.
    for interval in intervals:
        filename = f"timeline_{collection_id}_{interval}.parquet"
        if not data_io.exists(storage_location="cache", filename=filename):
            missing.append(interval)
            continue

        try:
            sample_df = data_io.load_parquet(storage_location="cache", filename=filename)
            if not _TIMELINE_REQUIRED_COLUMNS.issubset(sample_df.columns):
                if verbose:
                    print(f"    [TIMELINE] Cache for {collection_id}/{interval} missing v{TIMELINE_SCHEMA_VERSION} columns; regenerating.")
                missing.append(interval)
        except Exception:
            missing.append(interval)

    # When regenerating a parquet, also drop the matching analysis JSON so
    # the two artefacts can't drift apart across schema versions.
    for interval in missing:
        analysis_fname = f"timeline_analysis_{collection_id}_{interval}.json"
        if data_io.exists(storage_location="cache", filename=analysis_fname):
            try:
                data_io.remove(storage_location="cache", filename=analysis_fname)
                if verbose:
                    print(f"    [TIMELINE] Removed stale analysis: {analysis_fname}")
            except Exception as e:
                print(f"    [TIMELINE] Failed to remove stale analysis {analysis_fname}: {e}")

    if not missing:
        if verbose:
            print(f"    [TIMELINE] Using cached timeline data for {collection_id}")
        return {"day": None}  # truthy — cache already exists, no agg_df available
            
    # Generate Data
    # 1. Load Unified Dataset
    if preloaded_df is not None:
        if verbose:
            print(f"    [TIMELINE] Using locally provided dataframe for {collection_id} (shape: {preloaded_df.shape})")
        df = preloaded_df
    else:
        df = create_collection_unified_dataset(collection_id=collection_id, verbose=False)
        
    if df is None or df.empty:
        print("ERROR: Could not load unified dataset for collection", collection_id)
        return None
        
    # Ensure date column
    date_col = 'local_date'
    if date_col not in df.columns:
         print(f"ERROR: {date_col} missing in unified dataset")
         return None
         
    df[date_col] = pd.to_datetime(df[date_col]).astype('datetime64[ns]')

    # Engagement-activity breakdown is now computed AFTER the universe
    # filter, from the folded `extra_data` on play/observe rows. This
    # keeps numerator (engagement counts) and denominator (filtered
    # plays) in the same universe, so per-play engagement rates are
    # well-defined. Engagement linked to a play but folded onto the
    # lead-play's `extra_data` is the only signal we count — standalone
    # engagement rows that aren't adjacent to a counted play are ignored
    # on purpose to avoid the "more faves than plays" mismatch.

    # Construct 'machine_state' before the universe filter so the synthetic
    # state value is set on every play (the filter strips non-play rows next).
    if 'scraped_ok' in df.columns and 'scraped_fail' in df.columns and 'annotated_ok' in df.columns:
        df['machine_state'] = '1: Activity data only'
        df.loc[df['scraped_fail'] == True, 'machine_state'] = '2: Scrape failed'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'].isna()), 'machine_state'] = '3: Scrape ok, not tried MA'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'] == False), 'machine_state'] = '4: Scrape ok, MA failed'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'] == True), 'machine_state'] = '5: Scrape ok, MA ok'

    # Universe filter: timelines describe only plays on videos that were both
    # successfully scraped and successfully machine-annotated, with recorded
    # watch time.  This keeps "untagged" semantically clean — it means
    # "annotation succeeded but returned no tag of this kind", a meaningful
    # zero.  Plays without scrape/annotation are excluded because their
    # emptiness reflects the pipeline (data gap), not user behaviour.
    if 'play_duration' not in df.columns:
        print(f"ERROR: play_duration missing in unified dataset for {collection_id}")
        return None

    # Universe filter — accept both 'play' (donor watched) and 'observe'
    # (baseline-collection scrapes with no donor watch-time). For 'observe'
    # rows there's no play_duration; duration acts as the implied
    # attention proxy so they can still participate in weighted aggregates.
    # Plays with play_duration == 0 are kept: zero watch time is still a real
    # exposure (rapid scroll-past), and contributes weight 0 to weighted
    # aggregates without distorting them. NA play_duration (run followers,
    # cap-overflow, last-in-log) is still excluded.
    valid_activity = (df['activity_type'].isin(['play', 'observe'])) if 'activity_type' in df.columns else pd.Series(True, index=df.index)
    play_dur_present = df['play_duration'].notna()
    vid_dur_present = (df['duration'].notna() & (df['duration'] > 0)) if 'duration' in df.columns else pd.Series(False, index=df.index)
    is_observe = (df['activity_type'] == 'observe') if 'activity_type' in df.columns else pd.Series(False, index=df.index)
    duration_mask = play_dur_present | (is_observe & vid_dur_present)
    scrape_mask = (df['scraped_ok'] == True) if 'scraped_ok' in df.columns else pd.Series(True, index=df.index)
    annot_mask = (df['annotated_ok'] == True) if 'annotated_ok' in df.columns else pd.Series(True, index=df.index)
    df = df[valid_activity & duration_mask & scrape_mask & annot_mask].copy()

    if df.empty:
        print(f"WARN: No annotated plays with recorded play_duration for {collection_id}; nothing to aggregate.")
        return None

    # Per-row attention weight: play rows = min(play_duration, duration);
    # observe rows = duration (full-video implied attention).
    play_dur = df['play_duration'].astype('float64')
    if 'duration' in df.columns:
        vid_dur = df['duration'].astype('float64')
        df['_w'] = np.minimum(play_dur, vid_dur.fillna(play_dur))
        if 'activity_type' in df.columns:
            observe_rows = df['activity_type'] == 'observe'
            df.loc[observe_rows, '_w'] = vid_dur[observe_rows].fillna(0.0)
    else:
        df['_w'] = play_dur.fillna(0.0)

    # ---------------------------------------------------------
    # 2. Iterate and Aggregate
    result_dfs: dict[str, pd.DataFrame] = {}
    
    for interval in intervals:

        # Grouping — assign() shares underlying column data, avoiding a full copy
        temp_df = df.assign(period=df[date_col].dt.date.astype(str))

        group_col = 'period'

        # --- Classify variables once upfront ---
        numeric_vars: list[str] = []
        list_vars: list[str] = []
        categorical_vars: list[str] = []

        def _safe_to_list(x):
            if isinstance(x, np.ndarray):
                return x.tolist()
            if isinstance(x, list):
                return x
            return x

        for var in viz_vars:
            if var not in temp_df.columns:
                continue

            col = temp_df[var]
            dt = col.dtype
            is_arrow_list = isinstance(dt, pd.ArrowDtype) and 'list' in str(dt)

            first_valid = None
            non_null = col.dropna()
            if not non_null.empty:
                first_valid = non_null.iloc[0]

            is_py_list = isinstance(first_valid, list)
            is_np_array = isinstance(first_valid, np.ndarray)
            is_list = is_arrow_list or is_py_list or is_np_array
            is_numeric = pd.api.types.is_numeric_dtype(dt) and not is_list

            if is_numeric:
                numeric_vars.append(var)
            elif is_list:
                # Pre-convert numpy arrays to lists once for the whole column
                temp_df[var] = col.apply(_safe_to_list)
                list_vars.append(var)
            else:
                categorical_vars.append(var)

        # --- Video counts per period (vectorized) ---
        # video_count is the unweighted count of plays-with-duration in the period;
        # weighted_video_total is the sum of attention weights (used as the denominator
        # for multi-label share calculations).
        agg_df = temp_df.groupby(group_col).size().reset_index(name='video_count')
        weighted_total = temp_df.groupby(group_col)['_w'].sum().reset_index(name='weighted_video_total')
        agg_df = agg_df.merge(weighted_total, on=group_col, how='left')
        agg_df['weighted_video_total'] = agg_df['weighted_video_total'].fillna(0.0).astype('float64')

        # --- Engagement activity breakdown per period ---
        # Parse the folded `extra_data` string on each play/observe row
        # ("fave", "fave,comment:hello", "follow:account_name") into
        # activity types and count occurrences per period. "following"
        # is normalised to "follow"; unknown types are ignored.
        if 'extra_data' in temp_df.columns:
            ed_mask = temp_df['extra_data'].notna()
            if ed_mask.any():
                ed_sub = temp_df.loc[ed_mask, [group_col, 'extra_data']]
                # Split each cell into the leading activity-type tokens.
                token_lists = ed_sub['extra_data'].astype('string').map(
                    lambda s: [p.split(':', 1)[0].strip().lower() for p in str(s).split(',')]
                )
                exploded = pd.DataFrame({
                    group_col: ed_sub[group_col].values.repeat(token_lists.map(len).values),
                    'atype': [ACTIVITY_TYPE_MAP.get(t) for lst in token_lists for t in lst]
                })
                exploded = exploded[exploded['atype'].notna()]
                if len(exploded) > 0:
                    breakdown = (exploded.groupby([group_col, 'atype'])
                                          .size()
                                          .unstack(fill_value=0)
                                          .reset_index())
                    agg_df = agg_df.merge(breakdown, on=group_col, how='left')
        for t in ENGAGEMENT_TYPES:
            if t not in agg_df.columns:
                agg_df[t] = 0
            agg_df[t] = agg_df[t].fillna(0).astype(int)
        agg_df['extra_data_count'] = agg_df[list(ENGAGEMENT_TYPES)].sum(axis=1).astype(int)

        # --- Accumulate all per-variable columns, single merge at end ---
        extra_cols: dict[str, pd.Series] = {}

        # --- Numeric variables: watch-time-weighted mean + non-null count ---
        # Mean is Σ(value · w) / Σ(w) over rows where the variable is non-null.
        # The unweighted count remains as the occurrence floor in downstream analysis;
        # weighted_valid is the matching attention-seconds total over the same rows.
        for v in numeric_vars:
            sub = temp_df[[group_col, v, '_w']].dropna(subset=[v])
            if len(sub):
                num = (sub[v] * sub['_w']).groupby(sub[group_col]).sum()
                den = sub.groupby(group_col)['_w'].sum()
                extra_cols[f"{v}_val"] = num / den.where(den > 0)
                extra_cols[f"{v}_weighted_valid"] = den
            else:
                extra_cols[f"{v}_val"] = pd.Series(dtype='float64')
                extra_cols[f"{v}_weighted_valid"] = pd.Series(dtype='float64')
            extra_cols[f"{v}_valid"] = temp_df.groupby(group_col)[v].count()

        # --- Categorical (non-list) variables: unweighted + weighted aggregates ---
        for var in categorical_vars:
            extra_cols[f"{var}_valid"] = temp_df.groupby(group_col)[var].count()

            vc = temp_df.groupby(group_col)[var].value_counts()
            unstacked = vc.unstack(fill_value=0)
            extra_cols[f"{var}_counts"] = unstacked.apply(
                lambda row: json.dumps({k: int(v) for k, v in row.items() if v > 0}), axis=1
            )

            # Weighted: Σw per (period, category) and Σw where var is non-null.
            wsub = temp_df[[group_col, var, '_w']].dropna(subset=[var])
            if len(wsub):
                wvc = wsub.groupby([group_col, var])['_w'].sum().unstack(fill_value=0.0)
                extra_cols[f"{var}_weighted_counts"] = wvc.apply(
                    lambda row: json.dumps({k: round(float(v), 2) for k, v in row.items() if v > 0}), axis=1
                )
                extra_cols[f"{var}_weighted_valid"] = wsub.groupby(group_col)['_w'].sum()
            else:
                extra_cols[f"{var}_weighted_counts"] = pd.Series(dtype='object')
                extra_cols[f"{var}_weighted_valid"] = pd.Series(dtype='float64')

        # --- List variables: explode (carrying weight) once per side ---
        for var in list_vars:
            is_valid_list = temp_df[var].apply(lambda x: isinstance(x, list) and len(x) > 0)
            extra_cols[f"{var}_valid"] = temp_df.assign(_is_valid=is_valid_list).groupby(group_col)['_is_valid'].sum().astype(int)
            extra_cols[f"{var}_weighted_valid"] = temp_df.loc[is_valid_list].groupby(group_col)['_w'].sum()

            # Unweighted exploded counts (kept for hover and occurrence-floor filtering).
            exploded = temp_df[[group_col, var]].explode(var)
            exploded = exploded[exploded[var].notna()]
            vc = exploded.groupby(group_col)[var].value_counts()
            if not vc.empty:
                unstacked = vc.unstack(fill_value=0)
                extra_cols[f"{var}_counts"] = unstacked.apply(
                    lambda row: json.dumps({k: int(v) for k, v in row.items() if v > 0}), axis=1
                )
            else:
                agg_df[f"{var}_counts"] = '{}'

            # Weighted exploded counts: each exploded tag inherits its play's weight.
            wexploded = temp_df[[group_col, var, '_w']].explode(var)
            wexploded = wexploded[wexploded[var].notna()]
            if not wexploded.empty:
                wvc = wexploded.groupby([group_col, var])['_w'].sum().unstack(fill_value=0.0)
                extra_cols[f"{var}_weighted_counts"] = wvc.apply(
                    lambda row: json.dumps({k: round(float(v), 2) for k, v in row.items() if v > 0}), axis=1
                )
            else:
                agg_df[f"{var}_weighted_counts"] = '{}'

        # Single merge for all accumulated columns
        if extra_cols:
            extras_df = pd.DataFrame(extra_cols)
            agg_df = agg_df.merge(extras_df, on=group_col, how='left')

        # Sort by period
        agg_df = agg_df.sort_values(group_col).reset_index(drop=True)

        # v3 universe marker — presence of this column (checked in
        # _TIMELINE_REQUIRED_COLUMNS) proves the parquet was written with
        # the "scraped + annotated plays only" universe definition.
        agg_df['timeline_universe'] = 'annotated_plays'

        # Save
        filename = f"timeline_{collection_id}_{interval}.parquet"
        data_io.save_parquet(df=agg_df, storage_location="cache", filename=filename)
        result_dfs[interval] = agg_df

    return result_dfs




def _remap_analysis_indices(
    analysis: dict,
    date_index_map: dict[int, int],
    n_new: int,
    new_date_labels: list[str],
) -> None:
    """Translate anomaly/break indices in ``analysis`` from the unfiltered
    timeline coordinate space to the post-filter space.

    Cached `timeline_analysis_<cid>_<interval>.json` is computed once against
    the full per-collection day series; when the timeline endpoint applies a
    study filter the returned `dates` list shrinks, so any anomaly whose
    `index` pointed past the filtered length would render as "Unknown Date"
    in the findings panel. Walks each variable's categories, drops anomalies/
    breaks that referenced filtered-out days, and rewrites the surviving
    indices to match `dates` after filtering. Also refreshes the per-variable
    `time_labels`/`n_periods` so any future consumer sees a self-consistent
    payload.
    """
    if not isinstance(analysis, dict):
        return

    for var_name, var_block in analysis.items():
        if not isinstance(var_block, dict):
            continue
        var_block["time_labels"] = list(new_date_labels)
        var_block["n_periods"] = n_new
        var_block["start_offset"] = 0

        cats = var_block.get("categories")
        if not isinstance(cats, list):
            continue

        for cat in cats:
            if not isinstance(cat, dict):
                continue

            anomalies = cat.get("anomalies")
            if isinstance(anomalies, list) and anomalies:
                kept = []
                for a in anomalies:
                    if not isinstance(a, dict):
                        continue
                    new_i = date_index_map.get(a.get("index"))
                    if new_i is None:
                        continue
                    a["index"] = new_i
                    # Span markers move along with the peak — drop ends
                    # that fell outside the filter, clamp to the surviving
                    # extreme so the cluster's reported span doesn't lie.
                    if "span_start_index" in a:
                        new_s = date_index_map.get(a["span_start_index"])
                        a["span_start_index"] = new_s if new_s is not None else new_i
                    if "span_end_index" in a:
                        new_e = date_index_map.get(a["span_end_index"])
                        a["span_end_index"] = new_e if new_e is not None else new_i
                    kept.append(a)
                cat["anomalies"] = kept

            brk = cat.get("break")
            if isinstance(brk, dict):
                new_i = date_index_map.get(brk.get("index"))
                if new_i is None:
                    cat["break"] = None
                else:
                    brk["index"] = new_i




def get_timeline_data(collection_id, interval='day', skip_cache_check: bool = False,
                      preloaded_agg_df: pd.DataFrame | None = None,
                      study: str | None = None):
    """Returns timeline data for plotting.

    - Numeric: Daily Mean (Raw values, invalid/missing ignored).
      Includes metadata if log scale is requested.
    - Categorical: Daily Counts per category + Daily Total Count (for % calc).

    Args:
        collection_id: The collection to load timeline data for.
        interval: Aggregation interval ('day', 'week', 'month').
        skip_cache_check: If True, skip the cache existence check. Use when
            the caller has already ensured the cache is fresh (e.g. batch refresh).
        preloaded_agg_df: Pre-computed aggregated DataFrame for this interval.
            When provided, skips loading from cache (avoids write-then-read I/O).
        study: When set and the study's sidecar advertises ``selected_cells``,
            restrict the returned series to the (collection, day) cells the
            study admitted post-sampling. Falls back to the unfiltered view
            when the sidecar is missing, pre-v2, or doesn't list this
            collection.
    """

    if 'var_schema' not in fyp_cf:
        print("ERROR: var_schema missing")
        return {}

    # Load Schema Metadata
    meta = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])
    schema_map = meta.get('schema_map', {})

    if 'machine_state' not in viz_vars:
        viz_vars = ['machine_state'] + viz_vars

    # Ensure Cache Exists (skip during batch refresh to avoid redundant I/O)
    if not skip_cache_check:
        try:
            if not check_and_update_timeline_cache(collection_id, viz_vars):
                print("ERROR: Failed to update timeline cache.")
                return {}
        except Exception as e:
            print(f"ERROR: Failed to update timeline cache: {e}")
            return {}
        
    # Get Counts Metadata (Load all 3 aggs to get lengths)

    period_counts = {}
    
    # Helper to load specific interval
    def load_interval_df(u_interval):
        fname = f"timeline_{collection_id}_{u_interval}.parquet"
        if data_io.exists(storage_location="cache", filename=fname):
            return data_io.load_parquet(storage_location="cache", filename=fname)
        return None

    # Load all to get counts
    aggs = {}
    for inv in ['day']:
        if preloaded_agg_df is not None and inv == interval:
            df_agg = preloaded_agg_df
        else:
            df_agg = load_interval_df(inv)
        if df_agg is not None:
             period_counts[inv] = len(df_agg)
             aggs[inv] = df_agg
        else:
             period_counts[inv] = 0
             
    # Use requested interval data
    df = aggs.get(interval)
    if df is None or df.empty:
         return {"dates": [], "variables": {}, "counts": period_counts}
         
    # Prepare Result
    # Dates
    # Sort by period just in case
    df = df.sort_values(by='period')

    # Study-aware filter: drop days outside the study's sampled (cid, day)
    # cells. Sidecar absence / pre-v2 / missing collection entry => no filter
    # (back-compat with timelines opened before the study has been refreshed).
    # When the filter shrinks the date list, capture old->new index map so the
    # cached analysis JSON (whose anomaly/break indices reference the
    # unfiltered series) can be remapped before being returned to the client.
    date_index_map: dict[int, int] | None = None
    if study:
        sidecar = get_study_sidecar(study)
        if sidecar and sidecar.get("sampling_active"):
            cells_map = sidecar.get("selected_cells")
            if isinstance(cells_map, dict):
                allowed_dates = cells_map.get(str(collection_id))
                if allowed_dates is not None:
                    original_periods = df['period'].astype(str).tolist()
                    allowed_set = set(allowed_dates)
                    df = df[df['period'].astype(str).isin(allowed_set)]
                    new_periods = df['period'].astype(str).tolist()
                    if len(new_periods) != len(original_periods):
                        new_index = {p: i for i, p in enumerate(new_periods)}
                        date_index_map = {
                            old_i: new_index[p]
                            for old_i, p in enumerate(original_periods)
                            if p in new_index
                        }

    if df.empty:
        return {"dates": [], "variables": {}, "counts": period_counts}

    dates = df['period'].tolist()
    
    # Formatted Labels
    date_labels = []
    for d_str in dates:
        try:
            dt = pd.to_datetime(d_str)
            lbl = dt.strftime('%d/%m/%y')
            date_labels.append(lbl)
        except (ValueError, TypeError):
            date_labels.append(str(d_str))
            
    variables = {}

    # Common per-period denominators read once.
    video_counts = df['video_count'].tolist()
    weighted_video_total = df.get('weighted_video_total', pd.Series([0.0] * len(df))).astype('float64').tolist()

    ignore_cats = {
        fyp_cf.get('labels', {}).get('OTHER_THINGS', 'Other things'),
        fyp_cf.get('labels', {}).get('UNABLE_TO_DETECT', 'Unable to detect'),
        fyp_cf.get('labels', {}).get('NOT_CODED', 'Not coded')
    }

    def _parse_counts_column(series, value_cast):
        """Parse a JSON-string-per-period column into a list of dicts.
        Drops the ignored category labels in one pass."""
        out = []
        for json_str in series:
            try:
                if json_str and isinstance(json_str, str):
                    d = json.loads(json_str)
                    for igc in ignore_cats:
                        d.pop(igc, None)
                    d = {k: value_cast(v) for k, v in d.items()}
                else:
                    d = {}
            except Exception:
                d = {}
            out.append(d)
        return out

    for var in viz_vars:
        has_val = f"{var}_val" in df.columns
        has_counts = f"{var}_counts" in df.columns

        if not has_val and not has_counts:
            continue

        # Display Name
        display_name = schema_map.get(var, {}).get('display_name', var)
        if var == 'machine_state':
            display_name = 'Scrape and Annotation States'

        # Multi-label flag drives the share denominator: list-scaled
        # variables (hashtags, content categories) can tag one video several
        # times, so their shares are taken over videos and may exceed 100%.
        # Everything else (and the synthetic 'machine_state') is single-label.
        is_multi_label = (schema_map.get(var, {}).get('scale') == 'list')
        share_denominator = 'videos' if is_multi_label else 'valid'

        # Per-period denominators consumed downstream.
        valid_counts = df.get(f"{var}_valid", pd.Series([0] * len(df))).tolist()
        weighted_valid = df.get(f"{var}_weighted_valid", pd.Series([0.0] * len(df))).astype('float64').tolist()

        if has_val:
            # Numeric: {var}_val is already the watch-time-weighted mean
            # (computed in check_and_update_timeline_cache).  Use list
            # comprehension to coerce NaN → None for JSON safety.
            vals = [None if pd.isna(x) else float(x) for x in df[f"{var}_val"]]
            # Log scale derived from the spread of the per-period means: bounded
            # scores stay linear, order-of-magnitude series go log.
            use_log = explorer.derive_log_scale(
                pd.Series([v for v in vals if v is not None], dtype="float64")
            )
            variables[var] = {
                "type": "numeric",
                "values": vals,
                "log": use_log,
                "daily_valid_counts": valid_counts,
                "daily_video_counts": video_counts,
                "daily_weighted_valid": weighted_valid,
                "daily_weighted_video_total": weighted_video_total,
                "display_name": display_name,
            }
            continue

        # Categorical
        counts_list = _parse_counts_column(df[f"{var}_counts"], int)
        weighted_counts_list = _parse_counts_column(
            df.get(f"{var}_weighted_counts", pd.Series([''] * len(df))),
            float,
        )

        # Pre-compute share series in the backend so the analysis layer and
        # the frontend share one source of truth.  Numerator is the weighted
        # count for the category; denominator is governed by the multi-label
        # flag (videos for sparse multi-label, valid-count for single-label).
        share_series = []
        for i, wcounts in enumerate(weighted_counts_list):
            if share_denominator == 'videos':
                denom = weighted_video_total[i] if i < len(weighted_video_total) else 0.0
            else:
                denom = weighted_valid[i] if i < len(weighted_valid) else 0.0
            if denom and denom > 0:
                share_series.append({
                    k: round((v / denom) * 100.0, 2)
                    for k, v in wcounts.items() if v > 0
                })
            else:
                share_series.append({})

        # Rank categories by total weighted attention across the window so
        # the default selection surfaces the most-watched, not just the
        # most-frequent.  Falls back to raw-count ranking if no weighted data.
        global_weighted = {}
        for d in weighted_counts_list:
            for k, v in d.items():
                global_weighted[k] = global_weighted.get(k, 0.0) + v
        if global_weighted:
            top_cats = sorted(global_weighted.keys(), key=lambda x: global_weighted[x], reverse=True)
        else:
            global_raw = {}
            for d in counts_list:
                for k, v in d.items():
                    global_raw[k] = global_raw.get(k, 0) + v
            top_cats = sorted(global_raw.keys(), key=lambda x: global_raw[x], reverse=True)

        variables[var] = {
            "type": "categorical",
            "counts": counts_list,
            "weighted_counts": weighted_counts_list,
            "share_series": share_series,
            "share_denominator": share_denominator,
            "daily_video_counts": video_counts,
            "daily_valid_counts": valid_counts,
            "daily_weighted_valid": weighted_valid,
            "daily_weighted_video_total": weighted_video_total,
            "top_categories": top_cats if var == 'machine_state' else top_cats[:3],
            "default_all": True if var == 'machine_state' else False,
            "display_name": display_name,
        }

    # Extra-data (engagement activity) counts per period, plus per-type breakdown
    extra_data_counts = df['extra_data_count'].tolist() if 'extra_data_count' in df.columns else None
    extra_data_breakdown = {t: df[t].tolist() for t in ENGAGEMENT_TYPES if t in df.columns}

    result = {"dates": dates, "date_labels": date_labels, "variables": variables, "counts": period_counts, "variables_order": viz_vars}

    if extra_data_counts is not None:
        result["extra_data_counts"] = extra_data_counts
    if extra_data_breakdown:
        result["extra_data_breakdown"] = extra_data_breakdown

    # Attach pre-computed analysis data if available, or generate if missing
    analysis_fname = f"timeline_analysis_{collection_id}_{interval}.json"
    try:
        if data_io.exists(storage_location="cache", filename=analysis_fname):
            analysis = data_io.load_json(storage_location="cache", filename=analysis_fname)
            if analysis:
                # Cached analysis was built against the unfiltered timeline;
                # remap its anomaly/break indices into the filtered series so
                # the findings panel and chart overlays align with the dates
                # we're actually returning.
                if date_index_map is not None:
                    _remap_analysis_indices(analysis, date_index_map, len(dates), date_labels)
                result["analysis"] = analysis
        else:
            # Analysis is missing, generate it on the fly
            from fyp.timeline_analysis import MIN_ACTIVE_DAYS_FOR_TIMELINE, analyse_timeline

            # Try to fetch first_activity_date and active_days from
            # {COLLECTIONS_LABEL}_metadata.parquet. Collections with
            # active_days below the timeline threshold are skipped entirely —
            # the stats aren't meaningful and caching them wastes disk.
            first_date = None
            active_days = None
            try:
                if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
                    # Project to just the columns we need; the metadata parquet
                    # stores MultiIndex columns as stringified tuples on disk.
                    ddp_meta = data_io.load_parquet_selective(
                        storage_location="recoded",
                        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
                        columns=["('personas', 'first_event_ts')", "first_event_ts",
                                 "('personas', 'active_days')", "active_days"],
                        set_index='collection_id',
                        verbose=False,
                    )
                    if ddp_meta is not None:
                        # Check index or column for collection_id
                        if ddp_meta.index.name == 'collection_id' or ddp_meta.index.name is None:
                            mask = ddp_meta.index.astype(str) == str(collection_id)
                        elif 'collection_id' in ddp_meta.columns:
                            mask = ddp_meta['collection_id'].astype(str) == str(collection_id)
                        else:
                            mask = ddp_meta.index.astype(str) == str(collection_id)

                        row = ddp_meta[mask]
                        if not row.empty:
                            if ('personas', 'first_event_ts') in row.columns:
                                ts = row[('personas', 'first_event_ts')].iloc[0]
                                if pd.notna(ts):
                                    first_date = str(ts)[:10]
                            elif 'first_event_ts' in row.columns:
                                ts = row['first_event_ts'].iloc[0]
                                if pd.notna(ts):
                                    first_date = str(ts)[:10]

                            if ('personas', 'active_days') in row.columns:
                                ad = row[('personas', 'active_days')].iloc[0]
                                if pd.notna(ad):
                                    active_days = int(ad)
                            elif 'active_days' in row.columns:
                                ad = row['active_days'].iloc[0]
                                if pd.notna(ad):
                                    active_days = int(ad)
            except Exception as e:
                print(f"Warning: Could not get metadata for analysis generation: {e}")

            if active_days is not None and active_days < MIN_ACTIVE_DAYS_FOR_TIMELINE:
                # Not enough data for meaningful timeline stats — skip the
                # compute (and the cache write) rather than emit misleading
                # output. The UI already disables these collections.
                print(f"Skipping timeline analysis for {collection_id}: "
                      f"active_days={active_days} < {MIN_ACTIVE_DAYS_FOR_TIMELINE}.")
            else:
                analysis = analyse_timeline(result, interval=interval, first_activity_date=first_date)
                if analysis:
                    data_io.save_json(analysis, storage_location="cache", filename=analysis_fname)
                    result["analysis"] = analysis

    except Exception as e:
        print(f"Warning: Could not load or generate analysis for {collection_id}/{interval}: {e}")

    # Inject the synthetic "Other" bucket into the per-day counts whenever
    # analyse_timeline rolled low-occurrence categories into one, so the
    # frontend sidebar can surface it and plot its per-day share.  Done
    # here (not in analyse_timeline) because analyse_timeline should not
    # mutate its input, and we want the injection to apply equally whether
    # the analysis was freshly computed or loaded from cache.
    _inject_other_bucket(result)

    return result


def _inject_other_bucket(result: dict) -> None:
    """Fold low-occurrence categories into a synthetic "Other" per-day bucket.

    analyse_timeline() returns an ``other_members`` list for each variable
    whose low-occurrence categories were folded into a synthetic "Other"
    bucket.  We mirror that aggregation into all per-day series the
    frontend consumes (raw counts, weighted counts, and pre-computed
    shares) so the sidebar, ribbon, and chart agree with the analysis:
    member categories are removed from each day's dict and their sum is
    stored under "Other".  ``top_categories`` is also updated so default
    selections don't reference cats that no longer exist in the per-day
    series.
    """
    analysis = result.get("analysis") or {}
    variables = result.get("variables") or {}
    other_label = "Other"

    def _fold_series(series_list, member_set, round_to: int | None):
        """Return total mass folded into Other across the series."""
        if not series_list:
            return 0.0
        running_total = 0.0
        for day in series_list:
            if not isinstance(day, dict):
                continue
            day_total = 0.0
            for m in list(day.keys()):
                if m in member_set:
                    val = day.pop(m) or 0
                    day_total += val
            if day_total:
                merged = (day.get(other_label) or 0) + day_total
                if round_to is not None:
                    merged = round(merged, round_to)
                day[other_label] = merged
                running_total += day_total
        return running_total

    for var_name, var_analysis in analysis.items():
        members = var_analysis.get("other_members") if isinstance(var_analysis, dict) else None
        if not members:
            continue
        var_data = variables.get(var_name)
        if not var_data or var_data.get("type") != "categorical":
            continue
        counts_list = var_data.get("counts")
        if not counts_list:
            continue
        member_set = set(members)

        other_total = _fold_series(counts_list, member_set, round_to=None)
        _fold_series(var_data.get("weighted_counts"), member_set, round_to=2)
        _fold_series(var_data.get("share_series"), member_set, round_to=2)

        # Rebuild top_categories so it doesn't point at cats we just removed.
        top_cats = var_data.get("top_categories") or []
        filtered_top = [c for c in top_cats if c not in member_set]
        if other_total and other_label not in filtered_top:
            filtered_top.append(other_label)
        var_data["top_categories"] = filtered_top


# --- Collection Tags Cache ---
# RAM cache for collections_tags.json to avoid repeated GCS round-trips.
# Explicit invalidation handles same-instance writes; TTL handles
# cross-instance staleness on Cloud Run (multiple container instances).

_collection_tags_cache: dict | None = None
_collection_tags_cache_time: float = 0.0
_COLLECTION_TAGS_TTL: float = 300.0  # 5 minutes


def get_collection_tags(force_reload: bool = False) -> dict:
    """Return parsed collections_tags.json, cached in RAM with TTL.

    On first call (or after TTL / invalidation) loads from storage.
    Subsequent calls within the TTL window return the cached dict.
    """
    global _collection_tags_cache, _collection_tags_cache_time
    now = time.monotonic()
    if _collection_tags_cache is None or force_reload or (now - _collection_tags_cache_time > _COLLECTION_TAGS_TTL):
        fn = f"{COLLECTIONS_LABEL}_tags.json"
        if data_io.exists(storage_location="recoded", filename=fn):
            _collection_tags_cache = data_io.load_json(storage_location="recoded", filename=fn) or {}
        else:
            _collection_tags_cache = {}
        _collection_tags_cache_time = now
    return _collection_tags_cache





def invalidate_collection_tags_cache() -> None:
    """Reset the RAM cache so the next get_collection_tags() call reloads from storage."""
    global _collection_tags_cache, _collection_tags_cache_time
    _collection_tags_cache = None
    _collection_tags_cache_time = 0.0





def load_display_id_map() -> dict[str, str]:
    """Return a map of { raw_collection_id: display_id } from the cached collection tags."""
    mapping = {}
    try:
        annotations = get_collection_tags()
        for raw_id, tag_data in annotations.items():
            disp = tag_data.get('display_collection_id')
            if disp and str(disp).strip():
                mapping[str(raw_id)] = str(disp).strip()
    except Exception as e:
        print(f"Error loading display id map: {e}")
    return mapping



def get_study_collections(study):
    """
    Returns a list of unique collections present in the study dataset.
    Returns: [{ 'collection_id': '...', }, ...]
    """

    if "study_defs" not in fyp_cf:
        init_study_defs()

    if study not in fyp_cf["study_defs"]:
        return []

    selected_collections = fyp_cf["study_defs"][study].get("SELECTED_COLLECTIONS", [])

    selected_collections = [{"collection_id": str(d).strip()} for d in selected_collections]

    return selected_collections



    """try:
        
        recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(storage_location="cache", filename=recoded_file):
             df = data_io.load_parquet(
                 storage_location="cache", 
                 filename=recoded_file, 
             )
        else:
             # Fallback to full load if cache missing (triggering creation)
             df, _ = get_explorer_data(study, context="explorer")
             
             
        if df is None:
            return []
        
        if 'collection_id' not in df.columns:
            print(f"ERROR: collection_id not found in df for {study}")
            return []

        # Unique collections
        collections = df[['collection_id']].drop_duplicates()
        
        # Format for frontend
        result = []
        for _, row in collections.iterrows():
            item = {'collection_id': row['collection_id']}
            result.append(item)
            
        return sorted(result, key=lambda x: str(x.get('collection_id', '')))
        
    except Exception as e:
        print(f"Error getting study collections: {e}")
        return []"""



# Alias from explorer to handle serialization issues
make_serializable = explorer.make_serializable


# --- PCA Visualization Endpoints ---

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




def get_accessible_studies(username: str, role: str, is_admin: bool,
                           include_stats: bool = False) -> list:
    """Return study names (or dicts with stats) that the user has access to.

    Args:
        username: Current user's username.
        role: Current user's role.
        is_admin: Whether the user is an admin.
        include_stats: When True, return ``[{"name": ..., "stats": {...}}]``
            instead of a flat list of names. The stats dict is augmented
            with ``has_pca`` and ``has_timelines`` booleans so the UI can
            gate the Correlations and Timelines tabs per study.
    """
    from fyp.studies import init_study_defs

    if 'study_defs' not in fyp_cf:
        init_study_defs()

    accessible_studies = []

    # When stats are requested we also need to know which studies have
    # PCA scores and which have timelines — both gate tab availability in
    # the UI. List the cache once so we can answer via set membership
    # instead of issuing one exists()/listdir() call per study/collection.
    # Timeline availability mirrors what the Timelines dropdown surfaces:
    # at least one collection in the study must have
    # active_days >= MIN_ACTIVE_DAYS_FOR_TIMELINE. Checking file presence
    # alone was insufficient — stray cache files from other studies can
    # falsely mark a study as timeline-capable even when every collection
    # in it falls below the analysable-length threshold.
    cache_files: set[str] = set()
    timeline_capable_cids: set[str] = set()
    if include_stats:
        try:
            cache_files = set(data_io.listdir(storage_location="cache"))
        except Exception:
            cache_files = set()
        try:
            from fyp.organize_datasets import COLLECTIONS_LABEL
            from fyp.timeline_analysis import MIN_ACTIVE_DAYS_FOR_TIMELINE
            meta_df = data_io.load_parquet_selective(
                storage_location="recoded",
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
                columns=["('personas', 'active_days')", "active_days"],
                set_index='collection_id',
            )
            if meta_df is not None and not meta_df.empty:
                active_days_col = None
                if ('personas', 'active_days') in meta_df.columns:
                    active_days_col = ('personas', 'active_days')
                elif 'active_days' in meta_df.columns:
                    active_days_col = 'active_days'
                if active_days_col is not None:
                    df_reset = meta_df.reset_index()
                    ad_series = pd.to_numeric(df_reset[active_days_col], errors='coerce')
                    capable_mask = ad_series >= MIN_ACTIVE_DAYS_FOR_TIMELINE
                    timeline_capable_cids = set(
                        df_reset.loc[capable_mask, 'collection_id']
                        .dropna().astype(str).str.strip().tolist()
                    )
        except Exception:
            timeline_capable_cids = set()

    if 'study_defs' in fyp_cf:
        for study_name, study_config in fyp_cf['study_defs'].items():
            # 1. Admin Override
            if is_admin:
                has_access = True
            else:
                user_access = study_config.get('USER_ACCESS')

                # 2. Missing or Empty => Default Allow
                if not user_access or not isinstance(user_access, list) or 'all' in user_access or role in user_access or username in user_access:
                    has_access = True
                else:
                    has_access = False

            if has_access:
                # Data Integrity Checks
                if not data_io.exists(storage_location="cache", filename=f"{study_name}_recoded.parquet"):
                    continue

                stats = study_config.get('stats', {})
                # Defensive: a bad client save could persist stats as a string
                # (e.g. "[object Object]"). Treat anything non-dict as empty
                # so the listing endpoint keeps working for other studies.
                if not isinstance(stats, dict):
                    stats = {}
                if stats.get('unique_videos', 0) <= 0:
                    continue

                if include_stats:
                    stats = dict(stats)
                    stats['has_pca'] = f"{study_name}_PCA.parquet" in cache_files
                    # A study "has timelines" only when at least one of its
                    # collections is long enough to analyse (active_days >=
                    # threshold) AND has an actual cached timeline parquet.
                    # Both gates matter: without the length check, a stale
                    # cache file would re-enable the tab for a study whose
                    # collections are all too short; without the file check,
                    # collections that qualify on paper but whose timelines
                    # have never been generated would appear available.
                    selected = study_config.get('SELECTED_COLLECTIONS', []) or []
                    stats['has_timelines'] = any(
                        (cid_clean := str(cid).strip()) in timeline_capable_cids
                        and f"timeline_{cid_clean}_day.parquet" in cache_files
                        for cid in selected
                    )
                    accessible_studies.append({"name": study_name, "stats": stats})
                else:
                    accessible_studies.append(study_name)

    if include_stats:
        return sorted(accessible_studies, key=lambda s: s["name"])
    return sorted(accessible_studies)




def load_schema_metadata(metadata):
    """Helper to load and inject schema metadata (priorities, descriptions, accepted_labels) from CSV."""
    try:
        #var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if "var_schema" in fyp_cf and not fyp_cf["var_schema"].empty:
            schema_df = fyp_cf["var_schema"].copy()

            # A variable's position in every web list is derived, not hand-ranked:
            # (1) hard-coded section order, (2) categorical before numerical (from
            # ``scale``), (3) alphabetical by display name. The four ``web_*_prio``
            # columns are read as on/off membership only — any non-blank value
            # includes the variable; the numeric value no longer affects order.
            if 'section' in schema_df.columns:
                _sections = schema_df['section'].astype('string').fillna('')
            else:
                _sections = pd.Series('', index=schema_df.index)
            if 'scale' in schema_df.columns:
                _scales = schema_df['scale'].astype('string').fillna('').str.strip().str.lower()
            else:
                _scales = pd.Series('', index=schema_df.index)
            if 'display_name' in schema_df.columns:
                _names = schema_df['display_name'].astype('string')
            else:
                _names = pd.Series(pd.NA, index=schema_df.index)
            _names = _names.fillna(schema_df['variable_name'].astype('string')).fillna('').str.strip().str.lower()

            schema_df['_sec_rank'] = _sections.map(
                lambda s: SECTION_ORDER.index(s) if s in SECTION_ORDER else len(SECTION_ORDER))
            schema_df['_section'] = _sections
            schema_df['_cat_num'] = _scales.map(lambda s: 0 if s in _CAT_SCALES else 1)
            schema_df['_sort_name'] = _names
            order_cols = ['_sec_rank', '_section', '_cat_num', '_sort_name']

            def _ordered(prio_col):
                """Return ON variables for ``prio_col`` in canonical sort order."""
                if prio_col not in schema_df.columns:
                    return []
                is_on = pd.to_numeric(schema_df[prio_col], errors='coerce').notna()
                return schema_df[is_on].sort_values(order_cols)['variable_name'].tolist()

            metadata['section_order'] = list(SECTION_ORDER)
            metadata['display_priority'] = _ordered('web_display_prio')
            metadata['viz_priority'] = _ordered('web_viz_prio')
            metadata['timeline_priority'] = _ordered('web_timeline_prio')
            metadata['filter_priority'] = _ordered('web_filter_prio')

            if 'section' not in schema_df.columns:
                schema_df['section'] = 'General'
            if 'description' not in schema_df.columns:
                schema_df['description'] = ''
            
            schema_df['section'] = schema_df['section'].fillna('General')
            schema_df['description'] = schema_df['description'].fillna('')
            
            schema_map = {}
            for _, row in schema_df.iterrows():
                var_name = row['variable_name']
                schema_map[var_name] = {
                    "section": str(row['section']),
                    "description": str(row['description'])
                }
                
                # Parse Accepted Labels for Closed Tags
                if 'accepted_labels' in row:
                    accepted = str(row['accepted_labels'])
                    if accepted and accepted.lower() != 'nan' and accepted.startswith('[') and accepted.endswith(']'):
                        content = accepted[1:-1]
                        if content.strip():
                            labels = [x.strip() for x in content.split(',')]
                            schema_map[var_name]['accepted_labels'] = labels
                
                # Add Display Name
                if 'display_name' in row:
                    dname = str(row['display_name'])
                    if dname and dname.lower() != 'nan' and dname.strip():
                        schema_map[var_name]['display_name'] = dname.strip()

                # On/off membership flag the viewer's metadata panel reads to
                # decide whether to render a variable (the value itself is no
                # longer used for ordering, so any non-blank entry counts as on).
                if 'web_display_prio' in row:
                    prio = pd.to_numeric(row['web_display_prio'], errors='coerce')
                    if pd.notna(prio):
                         schema_map[var_name]['web_display_prio'] = float(prio)

                # Scale drives the timeline multi-label share denominator
                # (collection => multi-label) now that web_viz_multi_label is
                # derived rather than stored.
                if 'scale' in row:
                    sval = row['scale']
                    if pd.notna(sval):
                        schema_map[var_name]['scale'] = str(sval).strip().lower()

            metadata['schema_map'] = schema_map
                
        else:
            # Only reset if keys missing? Or always reset? 
            # If CSV missing, we might want to keep existing if available?
            # But here we assume CSV is source of truth.
            metadata['display_priority'] = []
            metadata['filter_priority'] = []
            metadata['schema_map'] = {}
    except Exception as e:
        print(f"Error loading priority list: {e}")
        # Don't overwrite with empty if error?
    return metadata


def calculate_inter_coder_reliability():
    """
    Calculates inter-coder reliability (Agreement % and Cohen's Kappa) for closed tags.
    Returns a dictionary of stats.
    """
    
    # 1. Load Schema to identify accepted labels and closed variables
    meta = {}
    load_schema_metadata(meta)
    schema_map = meta.get('schema_map', {})
    
    # Identify Variables with accepted_labels (Closed Tags)
    closed_vars = {}
    for var, details in schema_map.items():
        if details.get('accepted_labels'):
            closed_vars[var] = details['accepted_labels']

    if not closed_vars:
        return {"error": "No closed tagging variables found in schema."}

    # 2. Load All User Annotations
    user_files = []
    try:
        # We assume 'users' storage location is set up in data_io
        # Listing files in users directory
        all_files = data_io.listdir(storage_location='users')
        user_files = [f for f in all_files if f.endswith('.json') and not f.endswith('_tags.json')]
    except Exception as e:
        print(f"Error listing users: {e}")
        return {"error": f"Error listing users: {e!s}"}

    if not user_files:
        return {"error": "No user files found."}

    # 3. Aggregate Data
    all_data = []

    for uf in user_files:
        username = uf.replace('.json', '')
        try:
            user_blob = data_io.load_json(storage_location='users', filename=uf)
            if not user_blob: continue
            
            annotations = user_blob.get('annotations', {})
            
            for item_id, item_vars in annotations.items():
                for var_key, val in item_vars.items():
                    # Handle variable naming conventions (e.g. VarName__CLOSED_TAGGING)
                    real_var = var_key
                    if var_key.endswith('__CLOSED_TAGGING'):
                         real_var = var_key[:-16]
                    
                    if real_var in closed_vars:
                         cleaned_val = None
                         if isinstance(val, list):
                             # Multi-label handling: For Kappa, we ideally need single labels.
                             # If specific requirement isn't set, we treat single-element lists as the value,
                             # and multi-element lists as a combined string to allow exact match agreement check.
                             if len(val) == 1:
                                 cleaned_val = val[0]
                             elif len(val) > 1:
                                 cleaned_val = ",".join(sorted(val))
                         else:
                             cleaned_val = str(val)
                             
                         if cleaned_val:
                             all_data.append({
                                 "item_id": str(item_id),
                                 "variable": real_var,
                                 "user": username,
                                 "value": cleaned_val
                             })
                             
        except Exception as e:
            print(f"Error loading {uf}: {e}")
            continue

    if not all_data:
         return {"error": "No closed tags found in user files."}

    df = pd.DataFrame(all_data)

    # 4. Compute Statistics Per Variable
    results = []
    
    # We define Consensus as the Mode (Most Common) tag for each item.
    
    unique_vars = sorted(df['variable'].unique())
    
    for var in unique_vars:
        var_df = df[df['variable'] == var]
        
        # Calculate Consensus (Mode) per Item
        item_groups = var_df.groupby('item_id')['value']
        consensus_map = {}
        
        for item, group in item_groups:
            modes = group.mode()
            if not modes.empty:
                consensus_val = sorted(modes.tolist())[0]
                consensus_map[item] = consensus_val
                
        # Calculate Stats Per User
        users = sorted(var_df['user'].unique())
        
        user_agreements = []
        user_kappas = []
        user_n_items = []
        
        for u in users:
            user_subset = var_df[var_df['user'] == u]
            
            y_true = [] # Consensus
            y_pred = [] # User
            
            common_items = 0
            
            for _, row in user_subset.iterrows():
                iid = row['item_id']
                val = row['value']
                
                if iid in consensus_map:
                    cons_val = consensus_map[iid]
                    y_true.append(cons_val)
                    y_pred.append(val)
                    common_items += 1
            
            if common_items == 0:
                continue
            
            user_n_items.append(common_items)
            
            # Percent Agreement
            agreement = np.mean(np.array(y_true) == np.array(y_pred))
            user_agreements.append(agreement)
            
            # Cohen's Kappa - simplified
            kappa = 0.0
            if common_items > 1 and len(set(y_true)) > 1: # Need variation for Kappa
                try:
                     kappa = cohen_kappa_score(y_true, y_pred)
                     if pd.isna(kappa): kappa = 0.0
                except Exception:
                    kappa = 0.0
            elif common_items > 0 and y_true == y_pred:
                 # Perfect agreement on single item or constant values
                 # Technically Kappa is undefined or 0, but Agreement is 1.0. 
                 # We'll treat Kappa as 1.0 for perfect match to not punish consistency?
                 # No, standard is 0 if expected==observed.
                 # Let's keep it 0.0 but rely on Agreement for interpretation.
                 kappa = 0.0 
                 # Wait, if I have 1 item and I match, Agreement is 100%. Kappa is undefined.

            user_kappas.append(kappa)
            
        if user_agreements:
            avg_agreement = np.mean(user_agreements)
            avg_kappa = np.mean(user_kappas)
            avg_n = np.mean(user_n_items)
            
            results.append({
                "variable": var,
                "avg_agreement": round(avg_agreement * 100, 1),
                "avg_kappa": round(avg_kappa, 3),
                "n_raters": len(users),
                "avg_items": round(avg_n, 1)
            })
            
    # Sort results
    results.sort(key=lambda x: x['variable'])

    return {"results": results}
