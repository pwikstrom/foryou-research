"""Study/explorer data loading, caches and user-tag enrichment.

Pure moves from web_interface/data_service.py (Phase 7c). StudyCache and its
double-checked locking are verbatim; every cache singleton keeps its identity
via the data_service facade re-exports."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
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




# Columns the context filter itself reads. A projected request keeps these on
# top of whatever the caller asked for, so callers that gate on the enrichment
# flags still find them.
_CONTEXT_FILTER_COLUMNS = ("annotated_ok", "scraped_ok", "activity_type", "item_id")


def _apply_context_filter(raw_df, verbose=False):
    """Reduce a freshly-loaded study frame to the rows the web layer exposes.

    Explore and Video Analysis show the same rows — enrichment-complete
    play/observe events with an item id — so the filter runs once at load time
    and the result is what gets cached. Returns (filtered frame, status dict).
    """
    # annotated_ok / scraped_ok only exist once the corresponding enrichment
    # data has been merged in. Without it (fresh app) the columns may be
    # missing — treat as all-False so nothing leaks through.
    # When require_annotated_items is False we still require scraped_ok so that
    # the Video Analysis viewer always has a media file to play and Explore has
    # populated scrape metadata. Items not yet scraped are excluded either way.
    require_annotated = fyp_cf.get("viz", {}).get("require_annotated_items", True)
    status = _enrichment_status(raw_df, require_annotated)
    flag = "annotated_ok" if require_annotated else "scraped_ok"

    if flag in raw_df.columns:
        enrichment_mask = raw_df[flag].fillna(False)
    else:
        enrichment_mask = pd.Series(False, index=raw_df.index)

    filtered = raw_df[
        enrichment_mask
        & (raw_df['activity_type'].isin(['play', 'observe']))
        & (raw_df['item_id'].notna())
    ]
    if verbose:
        print(f"    Context filter: {len(raw_df):,} -> {len(filtered):,} rows")
    return filtered, status


def _cached_study_frame(study, verbose=False):
    """Return the cached context-filtered frame for ``study``, loading it once.

    The cache holds the filtered frame rather than the raw one: the filter is
    identical for every context, so caching it post-filter means requests never
    have to materialise their own copy of it. The raw frame is released as soon
    as the filter has run.

    Returns (frame, col_types, status), or (None, None, None) when the study
    has no recoded dataset.
    """
    # Capture parquet mtime up front so a worker rewriting the file in another
    # process invalidates this Flask process's RAM cache automatically on the
    # next request.
    current_mtime = _get_recoded_mtime(study)

    # Check cache (First Check)
    cached = study_cache.get(study, current_mtime=current_mtime)
    if cached:
        if verbose:
            print(f"    Study {study} found in RAM cache. Accessing {len(cached['df']):,} rows")
        return cached['df'], cached['col_types'], cached['status']

    # Double-Checked Locking
    if not hasattr(study_cache, 'loading_lock'):
         study_cache.loading_lock = threading.Lock()

    with study_cache.loading_lock:
        # Check cache again (Second Check)
        cached = study_cache.get(study, current_mtime=current_mtime)
        if cached:
            if verbose:
                print(f"    Study {study} found in RAM cache (after lock). Accessing {len(cached['df']):,} rows")
            return cached['df'], cached['col_types'], cached['status']

        if verbose:
            print(f"    Loading study {study} from disk (with lock)...")
        # Resolve path
        raw_df, col_types = explorer.load_data(study, verbose=False)

        if raw_df is None:
            if verbose:
                print("The requested recoded study dataset was not found")
            return None, None, None

        filtered_df, status = _apply_context_filter(raw_df, verbose=verbose)
        # Drop the last reference to the unfiltered frame so it is freed before
        # this function returns — on a multi-million-row study it is the larger
        # of the two and nothing downstream needs it.
        del raw_df

        # Force the index's lazy hash engine to build NOW, while we still hold
        # the loading lock. The cached frame is shared across request threads,
        # and pandas builds this engine on the first lookup without locking —
        # two concurrent first lookups can each observe a partially built
        # table and miss keys that are present (seen in prod as a spurious
        # KeyError on a valid row_idx from the item endpoint's prefetch pair).
        _ = filtered_df.index.is_unique

        # Re-read the mtime *after* loading so the cache entry is
        # tagged with the version we actually have in memory.
        cache_item = {
            "df": filtered_df,
            "col_types": col_types,
            "status": status,
            "mtime": _get_recoded_mtime(study),
        }
        study_cache.put(study, cache_item)
        return filtered_df, col_types, status


def get_explorer_data(study, context=None, columns=None, verbose=False):
    """Return the rows the web layer exposes for ``study``, plus column types.

    The returned frame is a **read-only column view** of the cached frame, not
    a copy: selecting columns from a pyarrow-backed frame shares the underlying
    arrays. Callers must not write to it in place. The two that need to mutate
    (``enrich_with_user_tags``, ``explorer.filter_dataframe``) take their own
    shallow copy first, so adding, replacing or dropping columns is safe.

    Args:
        study: Study name.
        context: Retained for call-site clarity. Explore and Video Analysis see
            the same rows, so this no longer changes the result.
        columns: Optional column projection. ``_CONTEXT_FILTER_COLUMNS`` are
            added automatically, and the returned ``col_types`` is narrowed to
            match. ``None`` (the default) returns every column. The projection
            is free in itself; it pays off in the per-row work downstream
            (sorts, dedups, stats) that would otherwise span every column.
        verbose: When True, print cache hits and load progress.

    Returns:
        Tuple of (frame, column-type mapping), or (None, None) when the study
        has no recoded dataset.
    """
    df, col_types, status = _cached_study_frame(study, verbose=verbose)
    if df is None:
        return None, None

    if columns is not None:
        keep = [c for c in dict.fromkeys([*columns, *_CONTEXT_FILTER_COLUMNS])
                if c in df.columns]
        out_col_types = {k: v for k, v in col_types.items() if k in keep}
    else:
        keep = list(df.columns)
        out_col_types = col_types.copy()

    # Always go through the column selection, even for the full width: it costs
    # nothing and hands back a distinct DataFrame object, so the attrs stamp
    # below and any column add/drop downstream cannot reach the cache entry.
    view = df[keep]

    # Stash the dataset status on the DataFrame so routes can surface a
    # clear message when an empty result is caused by missing enrichment
    # columns (i.e. the recoded parquet predates the current pipeline).
    try:
        view.attrs['fyp_dataset_status'] = status
    except Exception:
        pass

    return view, out_col_types


def get_explorer_rows(study, item_id=None, row_index=None, verbose=False):
    """Return just the rows for one item, without touching the other millions.

    The Video Analysis detail panel needs one row and every column. Routing that
    through :func:`get_explorer_data` and narrowing afterwards used to be
    harmless, but on a multi-million-row study the intermediate frame is
    gigabytes — per opened video, twice over, since the viewer prefetches
    neighbours. Selecting the rows against the cached frame first keeps the cost
    proportional to what is actually returned.

    ``row_index`` (the frame index the client was handed with its id chunk)
    wins when it is present and still valid, because duplicate ``item_id``
    values are legitimate — the same video watched twice. Falls back to matching
    on ``item_id``.

    Returns (frame, col_types); the frame is empty when nothing matches, and
    (None, None) when the study has no recoded dataset.
    """
    df, col_types, status = _cached_study_frame(study, verbose=verbose)
    if df is None:
        return None, None

    id_col = 'item_id' if 'item_id' in df.columns else (
        'video_id' if 'video_id' in df.columns else None)

    # Row lookup via a plain array comparison rather than .loc: label lookups
    # go through the index's shared lazy hash engine, which is not safe to
    # build from two request threads at once (the engine is also pre-built at
    # cache time — this is defense in depth, and it handles duplicate labels
    # the same way .loc[[label]] would).
    rows = None
    if row_index is not None:
        positions = np.flatnonzero(np.asarray(df.index) == row_index)
        if positions.size:
            rows = df.iloc[positions]
    if rows is None:
        if id_col is not None:
            rows = df[df[id_col].astype(str) == str(item_id)]
        else:
            rows = df.iloc[0:0]

    rows = rows.copy()
    try:
        rows.attrs['fyp_dataset_status'] = status
    except Exception:
        pass

    return rows, col_types.copy()





# The single shared empty-list cell used for every untagged row of the
# 'User Tags' column. Read-only by contract — see enrich_with_user_tags.
_NO_TAGS: list = []


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
        

    # Shallow copy: this function only adds whole columns, so nothing shared
    # with the caller's frame is mutated. A deep copy would duplicate the whole
    # study frame (several GB on all_collections) to add three columns.
    df = df.copy(deep=False)
    col_types = col_types.copy()
    n = len(df)

    # Arrow-native string view of the ids: isin() on it stays vectorized, and
    # python-level per-row work below is confined to the annotated rows (tens
    # to thousands, not millions). The old implementation mapped and re-checked
    # every row through python callables, which alone cost tens of seconds per
    # request on a multi-million-row study.
    str_ids = df['item_id'].astype('string[pyarrow]')

    # 1. User Tags (List)
    if id_to_tags:
        tag_mask = str_ids.isin(list(id_to_tags)).fillna(False).to_numpy(dtype=bool)
        if tag_mask.any():
            tags_col = np.empty(n, dtype=object)
            # Every untagged row shares ONE empty list. The column is only ever
            # read (filters, stats, JSON serialization) — never mutated
            # per-cell — and materialising a fresh [] per row is seconds of
            # allocation at this scale. Do not append to these cells.
            tags_col.fill(_NO_TAGS)
            positions = np.flatnonzero(tag_mask)
            for pos, iid in zip(positions, str_ids.iloc[positions]):
                tags_col[pos] = id_to_tags.get(str(iid)) or _NO_TAGS
            df['User Tags'] = tags_col
            col_types['User Tags'] = 'list'

    # 2. Has Annotation (Boolean/Category)
    if shared_users_tags:
        annotated_ids.update(str(k) for k in shared_users_tags.keys())

    # numpy bools rather than Arrow: downstream astype(str) must keep yielding
    # 'True'/'False' (Arrow bools stringify lowercase), and the filter/metadata
    # payloads are built from these values.
    df['Has Annotation'] = str_ids.isin(annotated_ids).fillna(False).to_numpy(dtype=bool)

    # Only keep if there are any true values? Or always keep if explicit user request?
    # If no annotations exist at all, we returned early above.
    # So we have annotations.
    col_types['Has Annotation'] = 'category' # Treat as category to trigger checkbox UI

    # 3. Machine Annotations — which model annotated each item. Annotated rows
    # get the annotating model's short name (resolved from the row's
    # annotation_version via the version registry); rows without per-row
    # provenance (pre-versioning history) fall back to the generic label.
    if 'annotated_ok' in df.columns:
        # annotated_ok is bool[pyarrow] and can hold NA — fill before the numpy
        # coercion (NA rows count as neither annotated nor failed).
        ok = df['annotated_ok'].fillna(False).to_numpy(dtype=bool)
        fail = (df['annotated_ok'] == False).fillna(False).to_numpy(dtype=bool)  # noqa: E712 — pyarrow-NA-safe comparison

        machine = np.full(n, 'Not Attempted', dtype=object)
        machine[fail] = 'Cannot Machine Annotate'
        machine[ok] = 'Machine Annotated'

        if 'annotation_version' in df.columns:
            model_labels = _annotation_model_labels()
            if model_labels:
                # Factorize instead of mapping row-by-row: versions repeat, so
                # the python-level dict lookup runs once per distinct version.
                ok_positions = np.flatnonzero(ok)
                codes, uniques = pd.factorize(df['annotation_version'].iloc[ok_positions])
                mapped = np.array(
                    [model_labels.get(str(u), 'Machine Annotated') for u in uniques],
                    dtype=object,
                )
                labelled = np.where(codes >= 0, mapped[codes] if len(mapped) else 'Machine Annotated',
                                    'Machine Annotated')
                machine[ok_positions] = labelled

        df['Machine Annotations'] = machine
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

