"""Study/explorer data loading, caches and user-tag enrichment.

Pure moves from web_interface/data_service.py (Phase 7c). StudyCache and its
double-checked locking are verbatim; every cache singleton keeps its identity
via the data_service facade re-exports."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from cachetools import LRUCache

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL
from fyp.studies import init_study_defs

from .. import explorer_backend as explorer

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


# --- Per-user JSON cache -----------------------------------------------------
# The viewer/explorer item endpoints read user JSON files (own tags + every
# sharing user's annotations) on each request. On Cloud Run each read is a GCS
# round-trip; with ~60 sharing users that serialized into multi-second request
# latencies. Cache the parsed files in-process with a short TTL and invalidate
# a user's entry whenever their file is written (tag/vote/settings saves).

_USER_JSON_TTL = 60.0
_user_json_cache: dict[str, tuple[float, dict | None]] = {}
_user_json_lock = threading.Lock()




def invalidate_user_json_cache(username: str | None = None) -> None:
    """Drop the cached JSON for one user (or all users when ``None``).

    Call after writing a user's JSON file so their own next request sees the
    change immediately; other users pick it up within ``_USER_JSON_TTL``.
    """
    with _user_json_lock:
        if username is None:
            _user_json_cache.clear()
        else:
            _user_json_cache.pop(username, None)
            _user_json_cache.pop(username.lower(), None)




def _read_user_json_uncached(username: str) -> dict | None:
    """Load one user's JSON file (exact-case, then lowercase fallback)."""
    filename = f"{username}.json"
    if data_io.exists(storage_location="users", filename=filename):
        return data_io.load_json(storage_location="users", filename=filename) or None
    filename_lower = f"{username.lower()}.json"
    if filename_lower != filename and data_io.exists(storage_location="users", filename=filename_lower):
        return data_io.load_json(storage_location="users", filename=filename_lower) or None
    return None




def get_user_json_cached(username: str) -> dict | None:
    """Return one user's parsed JSON file through the TTL cache."""
    now = time.time()
    with _user_json_lock:
        entry = _user_json_cache.get(username)
        if entry is not None and entry[0] > now:
            return entry[1]
    try:
        blob = _read_user_json_uncached(username)
    except Exception as e:
        print(f"Error loading user file for {username}: {e}")
        return None
    with _user_json_lock:
        _user_json_cache[username] = (now + _USER_JSON_TTL, blob)
    return blob




def _prefetch_user_jsons(usernames: list[str]) -> None:
    """Warm the user-JSON cache for many users with parallel reads.

    A cold cache would otherwise fetch each file sequentially (one GCS
    round-trip per user); fetching in parallel bounds the cold-path cost at
    roughly one round-trip total.
    """
    now = time.time()
    with _user_json_lock:
        missing = [u for u in usernames
                   if (e := _user_json_cache.get(u)) is None or e[0] <= now]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=min(16, len(missing))) as pool:
        list(pool.map(get_user_json_cached, missing))


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
        # getmtime raises FileNotFoundError for a missing file/blob — no
        # separate exists() probe needed (saves a GCS round-trip per request).
        return data_io.getmtime(storage_location="cache", filename=f"{study}_recoded.parquet")
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
        return data_io.getmtime(storage_location="cache", filename=f"{study}_recoded.meta.json")
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
    user_data = get_user_json_cached(username) or {}
    user_tags = {}

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
    
    # 3. Machine Annotations — which model annotated each item. Annotated rows
    # get the annotating model's short name (resolved from the row's
    # annotation_version via the version registry); rows without per-row
    # provenance (pre-versioning history) fall back to the generic label.
    if 'annotated_ok' in df.columns:
        df['Machine Annotations'] = 'Not Attempted'
        df.loc[df['annotated_ok'] == True, 'Machine Annotations'] = 'Machine Annotated'
        df.loc[df['annotated_ok'] == False, 'Machine Annotations'] = 'Cannot Machine Annotate'

        if 'annotation_version' in df.columns:
            model_labels = _annotation_model_labels()
            if model_labels:
                ok_mask = (df['annotated_ok'] == True).to_numpy(dtype=bool)
                labels = df.loc[ok_mask, 'annotation_version'].astype(str).map(model_labels)
                df.loc[ok_mask, 'Machine Annotations'] = labels.fillna('Machine Annotated').to_numpy()

        col_types['Machine Annotations'] = 'category'

    return df, col_types




def _annotation_model_labels() -> dict:
    """Map each ``annotation_version`` to its model's short display label.

    Reads the annotation version registry; the label is the model id's last
    path segment (e.g. ``mlx-community/Qwen3-Omni-...`` → ``Qwen3-Omni-...``).
    Returns an empty dict on any failure so the caller falls back to the
    generic "Machine Annotated" label. Never raises.
    """
    try:
        from fyp import annotation_versioning
        versions = annotation_versioning.load_registry().get("versions", {})
    except Exception:
        return {}
    labels = {}
    for version, info in versions.items():
        model = str((info or {}).get("model") or "").strip()
        if model:
            labels[str(version)] = model.rsplit("/", 1)[-1]
    return labels


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

    # Warm the per-user cache with parallel reads; a cold cache would
    # otherwise serialize one GCS round-trip per sharing user.
    _prefetch_user_jsons(list(allowed_usernames))

    for user in allowed_usernames:
        try:
            user_blob = get_user_json_cached(user)
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

