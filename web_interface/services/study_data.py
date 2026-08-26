"""Study/explorer data loading, caches and user-tag enrichment.

Pure moves from web_interface/data_service.py (Phase 7c). StudyCache and its
double-checked locking are verbatim; every cache singleton keeps its identity
via the data_service facade re-exports."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pa_compute
from cachetools import LRUCache

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL
from fyp.studies import init_study_defs, is_composed_study, participant_me_name

from .. import explorer_backend as explorer

# --- Explorer State ---


class StudyCache:
    def __init__(self, maxsize=2):
        self.cache = LRUCache(maxsize=maxsize)
        self.lock = threading.Lock()
        # ONE loading lock across all studies, created eagerly (the old lazy
        # hasattr-guarded creation let two first-ever requests each make their
        # own lock and load concurrently, doubling peak RAM). Deliberately
        # global rather than per-study: two multi-GB studies loading at once
        # is exactly the shape that OOM-killed the instance on 2026-08-03.
        self.loading_lock = threading.Lock()

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

    def clear_except(self, study_name):
        """Drop every cached study except ``study_name``."""
        with self.lock:
            for key in [k for k in self.cache if k != study_name]:
                del self.cache[key]




study_cache = StudyCache(maxsize=2)

# Studies at or above this raw row count evict every other cached study
# BEFORE their parquet is loaded (see _cached_study_frame). Below it, the
# two-slot LRU keeps a pair of small studies hot across switches.
_BIG_STUDY_ROW_THRESHOLD = 1_000_000


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




# Freshness probes (getmtime) ride a short TTL: on Cloud Run each is a GCS
# round-trip, and the explorer fires one per request just to validate its RAM
# caches. The TTL bounds staleness after a worker rewrites an artifact — the
# same trade the sessions blueprint's _STAT_CACHE already makes at 15s.
_MTIME_TTL_SECONDS = 15.0
_mtime_ttl_cache: dict[str, tuple[float, float | None]] = {}
_mtime_ttl_lock = threading.Lock()


def _ttl_mtime(filename):
    """mtime of a cache-location file through the 15s TTL, ``None`` if absent."""
    now = time.monotonic()
    with _mtime_ttl_lock:
        entry = _mtime_ttl_cache.get(filename)
        if entry is not None and now - entry[0] < _MTIME_TTL_SECONDS:
            return entry[1]
    try:
        # getmtime raises FileNotFoundError for a missing file/blob — no
        # separate exists() probe needed (saves a GCS round-trip per request).
        mtime = data_io.getmtime(storage_location="cache", filename=filename)
    except Exception:
        mtime = None
    with _mtime_ttl_lock:
        _mtime_ttl_cache[filename] = (now, mtime)
    return mtime


def _get_recoded_mtime(study):
    """Return the on-disk mtime of the study's recoded parquet, or ``None``
    if the file is missing / unreadable. Used to detect stale RAM cache
    entries when the parquet is refreshed by a worker subprocess. TTL-cached
    for 15s, so a refreshed parquet is picked up within that window."""
    return _ttl_mtime(f"{study}_recoded.parquet")


# --- Composed (participant "Everyone & Me") studies --------------------------
#
# A composed study stores NO artifacts of its own. Its frame is assembled at
# load time from the site default study (base) plus the owner's Just Me
# dataset (overlay); PCA / correlations / sequence / methods artifacts are
# served from the base. See web_interface/services/participant_studies.py for
# the lifecycle and fyp.analysis.studies for the def markers.


def resolve_compose(study):
    """Return ``(base_name, overlay_name)`` for a composed study, else None.

    The base is resolved to the CURRENT site default study at every call, so
    an admin repointing the default retargets every Everyone & Me study
    without touching their defs. Returns None when the study is not composed,
    the default is unset/missing, or the default is itself a composed study.
    """
    if "study_defs" not in fyp_cf:
        init_study_defs()
    defs = fyp_cf.get("study_defs") or {}
    cfg = defs.get(study)
    if not is_composed_study(cfg):
        return None
    from ..admin_settings import get_default_study
    base = get_default_study()
    owner = cfg.get("OWNER")
    if not base or not owner or base == study:
        return None
    base_cfg = defs.get(base)
    if not isinstance(base_cfg, dict) or is_composed_study(base_cfg):
        return None
    return base, participant_me_name(owner)


def resolve_artifact_study(study):
    """The study whose cached artifacts serve ``study``'s requests.

    Composed studies borrow the base (default) study's PCA / correlations /
    sequence / methods artifacts — the owner's extra videos are simply absent
    from those views. Every other study serves its own.
    """
    pair = resolve_compose(study)
    return pair[0] if pair else study


def _composed_mtime(base, overlay):
    """Joint staleness token for a composed frame: either side's rewrite
    changes the tuple, which the StudyCache treats as any other mtime value.
    Returns None when either parquet is missing (composed frame unavailable)."""
    base_mtime = _get_recoded_mtime(base)
    overlay_mtime = _get_recoded_mtime(overlay)
    if base_mtime is None or overlay_mtime is None:
        return None
    return (base_mtime, overlay_mtime)




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


# The explorer metadata JSON (multi-MB: top-200 values per categorical column)
# used to be exists()+downloaded from GCS on EVERY /api/explore/filter request
# — two round-trips plus a multi-MB parse per checkbox tick. Same mtime-keyed
# pattern as the sidecar cache above. Callers treat the payload as read-only;
# anything they graft onto it (e.g. total_stats reuse) must copy first.
_explorer_meta_cache = LRUCache(maxsize=8)
_explorer_meta_lock = threading.Lock()


def _merge_explorer_metadata(base_meta, overlay_meta):
    """Union two explorer-metadata payloads for a composed study.

    Generic recursive merge: dicts merge key-wise, lists union (base order
    first — so overlay-only categorical values, e.g. the owner's collection
    ids, become filterable), numeric ``min``/``max`` take the envelope, and
    any other conflict keeps the base value (counts therefore read as the
    base's — approximate, like the composed stats).
    """
    if isinstance(base_meta, dict) and isinstance(overlay_meta, dict):
        merged = dict(base_meta)
        for key, o_val in overlay_meta.items():
            if key not in merged:
                merged[key] = o_val
            else:
                merged[key] = _merge_explorer_metadata(merged[key], o_val)
        return merged
    if isinstance(base_meta, list) and isinstance(overlay_meta, list):
        seen = {v for v in base_meta if not isinstance(v, (dict, list))}
        extra = [v for v in overlay_meta
                 if isinstance(v, (dict, list)) or v not in seen]
        # Unhashable entries (dicts/lists) can't be deduped cheaply; keep base
        # only in that case to avoid duplicating structured rows.
        if any(isinstance(v, (dict, list)) for v in base_meta + overlay_meta):
            return base_meta
        return base_meta + extra
    if isinstance(base_meta, (int, float)) and isinstance(overlay_meta, (int, float)) \
            and not isinstance(base_meta, bool) and not isinstance(overlay_meta, bool):
        # Only meaningful for min/max-style bounds; for other numerics the
        # envelope is harmless (they are display hints, not counts the API
        # promises to be exact).
        return base_meta  # resolved by the min/max special case below
    return base_meta


def _merge_meta_bounds(merged, base_meta, overlay_meta):
    """Second pass: min/max envelopes wherever both sides carry them."""
    if not (isinstance(merged, dict) and isinstance(base_meta, dict)
            and isinstance(overlay_meta, dict)):
        return merged
    for key in ("min", "max"):
        b, o = base_meta.get(key), overlay_meta.get(key)
        if isinstance(b, (int, float)) and isinstance(o, (int, float)) \
                and not isinstance(b, bool) and not isinstance(o, bool):
            merged[key] = min(b, o) if key == "min" else max(b, o)
    for key, m_val in merged.items():
        if isinstance(m_val, dict):
            merged[key] = _merge_meta_bounds(
                m_val, base_meta.get(key), overlay_meta.get(key))
    return merged


def get_explorer_metadata_cached(study):
    """Parsed ``{study}_explorer_metadata.json``, or ``{}`` when absent.

    Cached in-process keyed by the file's mtime (probed through the 15s TTL),
    so a metadata rebuild is picked up within the TTL window. A composed
    study has no metadata file of its own: base and overlay payloads are
    merged on the fly and cached under the composed name, keyed by the pair
    of source mtimes.
    """
    compose = resolve_compose(study)
    if compose:
        base_name, overlay_name = compose
        token = (_ttl_mtime(f"{base_name}_explorer_metadata.json"),
                 _ttl_mtime(f"{overlay_name}_explorer_metadata.json"))
        with _explorer_meta_lock:
            entry = _explorer_meta_cache.get(study)
            if entry is not None and entry[0] == token:
                return entry[1]
        base_meta = get_explorer_metadata_cached(base_name)
        overlay_meta = get_explorer_metadata_cached(overlay_name)
        if not base_meta:
            return overlay_meta
        merged = _merge_explorer_metadata(base_meta, overlay_meta)
        merged = _merge_meta_bounds(merged, base_meta, overlay_meta)
        with _explorer_meta_lock:
            _explorer_meta_cache[study] = (token, merged)
        return merged

    filename = f"{study}_explorer_metadata.json"
    mtime = _ttl_mtime(filename)
    if mtime is None:
        return {}
    with _explorer_meta_lock:
        entry = _explorer_meta_cache.get(study)
        if entry is not None and entry[0] == mtime:
            return entry[1]
    try:
        payload = data_io.load_json(storage_location="cache", filename=filename)
    except Exception as e:
        print(f"    Warning: Could not load cached metadata: {e}")
        return {}
    if not isinstance(payload, dict):
        return {}
    with _explorer_meta_lock:
        _explorer_meta_cache[study] = (mtime, payload)
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


def _load_composed_raw(study, base, overlay, verbose=False):
    """Assemble a composed study's raw frame: base ∪ overlay, deduped.

    The overlay (the owner's Just Me dataset) is authoritative for the owner's
    collections: any base rows from those collections are dropped before the
    concat, so a collection that is both owned and in the default study
    contributes its full, unwindowed, unsampled Just Me rows exactly once.
    Base rows keep the base study's window and sampling. Column types come
    from the base — both parquets are written by the same recode pipeline.
    """
    raw_base, col_types = explorer.load_data(base, verbose=False)
    if raw_base is None:
        return None, None
    raw_overlay, _overlay_types = explorer.load_data(overlay, verbose=False)
    if raw_overlay is None or raw_overlay.empty:
        # A missing/empty overlay should not happen (the listing gates on its
        # parquet), but serving just the base beats a 500.
        return raw_base, col_types

    own_cids = set(raw_overlay['collection_id'].astype(str).unique())
    base_kept = raw_base[~raw_base['collection_id'].astype(str).isin(own_cids)]
    del raw_base
    combined = pd.concat([base_kept, raw_overlay], ignore_index=True, copy=False)
    if verbose:
        print(f"    Composed {study}: base {len(base_kept):,} rows "
              f"(after dropping {len(own_cids)} owned collection(s)) "
              f"+ overlay {len(raw_overlay):,} rows -> {len(combined):,}")
    return combined, col_types


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
    # next request. For a composed study the token covers both source parquets.
    compose = resolve_compose(study)
    if compose:
        current_mtime = _composed_mtime(*compose)
        if current_mtime is None:
            # Either side missing: the composed frame does not exist (and a
            # stale cache entry must not be served in its place).
            study_cache.invalidate(study)
            return None, None, None
    else:
        current_mtime = _get_recoded_mtime(study)

    # Check cache (First Check)
    cached = study_cache.get(study, current_mtime=current_mtime)
    if cached:
        if verbose:
            print(f"    Study {study} found in RAM cache. Accessing {len(cached['df']):,} rows")
        return cached['df'], cached['col_types'], cached['status']

    with study_cache.loading_lock:
        # Check cache again (Second Check)
        cached = study_cache.get(study, current_mtime=current_mtime)
        if cached:
            if verbose:
                print(f"    Study {study} found in RAM cache (after lock). Accessing {len(cached['df']):,} rows")
            return cached['df'], cached['col_types'], cached['status']

        if verbose:
            print(f"    Loading study {study} from disk (with lock)...")

        # Loading holds the raw frame AND the filtered result in memory at
        # once (the context mask reads the raw frame), so a very large study
        # cannot fit alongside previously cached frames — on 2026-08-03 the
        # all_collections load (7.25 GB raw + 5.52 GB filtered) OOM-killed
        # the 16 GiB instance because two other studies were still cached.
        # Evict everything else FIRST when the incoming study is big; eviction
        # at insert time (the LRU default) happens after the peak. A missing
        # sidecar reads as big — the cost of over-evicting is a re-load,
        # the cost of under-evicting is the instance.
        sidecar = get_study_sidecar(compose[0] if compose else study)
        row_count = (sidecar or {}).get('row_count')
        if row_count is None or row_count >= _BIG_STUDY_ROW_THRESHOLD:
            study_cache.clear_except(study)

        if compose:
            raw_df, col_types = _load_composed_raw(study, *compose, verbose=verbose)
        else:
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

        # The cached frame's index IS the row_idx contract: the viewer is handed
        # these labels with its id chunk and sends one back to name the exact row
        # behind the video on screen. Recoded parquets do not guarantee a usable
        # index — several carry a float index inherited from an upstream join,
        # mostly NaN with a few duplicated labels. Those labels are unusable as
        # row identity twice over: NaN != NaN, so the lookup never matches and
        # silently falls back to the first row with that item_id (the same video
        # watched twice then reports one timestamp for every occurrence), and a
        # NaN also serialises to a bare `NaN` token that no JSON parser accepts.
        # Normalising here repairs every already-written parquet without a
        # re-recode, and makes the labels unique, finite and stable for as long
        # as this cache entry lives — which is exactly the contract's lifetime,
        # since a rewritten parquet changes the mtime and invalidates the entry.
        # Assigning the index (rather than reset_index) rebinds the axis without
        # copying the columns; filtered_df is this function's own fresh object.
        # This also retires the hash-engine warm-up that used to stand here: a
        # RangeIndex resolves labels arithmetically and builds no shared lazy
        # engine, so the race that made a concurrent prefetch pair miss a valid
        # row_idx cannot happen on it.
        filtered_df.index = pd.RangeIndex(len(filtered_df))

        # Re-read the mtime *after* loading so the cache entry is
        # tagged with the version we actually have in memory.
        cache_item = {
            "df": filtered_df,
            "col_types": col_types,
            "status": status,
            "mtime": _composed_mtime(*compose) if compose else _get_recoded_mtime(study),
        }
        study_cache.put(study, cache_item)
        return filtered_df, col_types, status


def is_study_frame_cached(study):
    """True when the study's context-filtered frame is warm in the RAM cache."""
    return study_cache.get(study, current_mtime=_get_recoded_mtime(study)) is not None


# NOTE: an earlier cold-open design warmed the frame on a daemon thread here.
# Cloud Run throttles CPU to ~zero outside request processing, so that thread
# stalled indefinitely — the warm must ride a request (the client's
# wait_for_frame follow-up in explore.js).


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


def get_study_col_types(study):
    """Return the full column-type mapping for a study, or ``None`` if absent.

    Loads (and caches) the study frame when cold — the caller invariably needs
    the frame right after (e.g. to run a projected filter request), so the load
    is never wasted.
    """
    _df, col_types, _status = _cached_study_frame(study)
    return col_types






def get_search_column(study, column):
    """Return ``(series, dtype)`` for one column of a study's filtered frame.

    Warm path: a read-only column view of the ``StudyCache`` entry. Cold path:
    a selective single-column parquet read (plus the context-filter columns)
    that deliberately does NOT populate ``StudyCache`` — answering a value
    search must never trigger a multi-GB full load or evict a warm study.

    Returns ``(None, None)`` when the study or column doesn't exist.
    """
    current_mtime = _get_recoded_mtime(study)
    if current_mtime is None:
        return None, None

    cached = study_cache.get(study, current_mtime=current_mtime)
    if cached is not None:
        df = cached['df']
        if column not in df.columns:
            return None, None
        return df[column], cached['col_types'].get(column)

    try:
        frame = data_io.load_parquet_selective(
            storage_location="cache",
            filename=f"{study}_recoded.parquet",
            columns=list(dict.fromkeys([column, *_CONTEXT_FILTER_COLUMNS])),
        )
    except Exception:
        return None, None
    if frame is None or column not in frame.columns:
        return None, None
    filtered, _status = _apply_context_filter(frame)
    col_types = explorer.classify_columns(filtered[[column]])
    return filtered[column], col_types.get(column)






# Per-(study, column) full value-count cache backing the value-search
# endpoint. The counts pass over the column is the expensive step (one scan,
# or a Counter pass for list columns); it runs once per parquet version and
# every subsequent keystroke is a vectorized substring match over the cached
# unique values only.
_value_search_cache = LRUCache(maxsize=16)
_value_search_lock = threading.Lock()






def search_column_value_counts(study, column):
    """Return the cached full value counts for one categorical/list column.

    Returns a dict with ``type`` (``"category"``/``"list"``), ``values`` (all
    unique values, most frequent first, singletons included), ``counts``
    (parallel list of ints) and ``lowered`` (a lowercased pyarrow-string
    Series over ``values`` for case-insensitive matching) — or ``None`` when
    the study/column is missing or the column isn't value-searchable.

    Staleness is keyed on the recoded parquet's mtime, like ``StudyCache``.
    """
    mtime = _get_recoded_mtime(study)
    if mtime is None:
        return None
    key = (study, column)
    with _value_search_lock:
        entry = _value_search_cache.get(key)
        if entry is not None and entry['mtime'] == mtime:
            return entry

    series, dtype = get_search_column(study, column)
    if series is None or dtype not in ("category", "list"):
        return None
    pairs = explorer.column_value_counts(
        series, dtype, date_like="date" in column.lower())
    values = [k for k, _ in pairs]
    entry = {
        "mtime": mtime,
        "type": dtype,
        "values": values,
        "counts": [v for _, v in pairs],
        "lowered": pd.Series(values, dtype="string[pyarrow]").str.lower(),
    }
    with _value_search_lock:
        _value_search_cache[key] = entry
    return entry






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
    values are legitimate — the same video watched twice, which the item_id
    fallback cannot tell apart (it would answer every occurrence with the
    first one's row). ``_cached_study_frame`` guarantees those labels are a
    unique RangeIndex, so a match here is the exact row the viewer is showing.

    Returns (frame, col_types); the frame is empty when nothing matches, and
    (None, None) when the study has no recoded dataset.
    """
    df, col_types, status = _cached_study_frame(study, verbose=verbose)
    if df is None:
        return None, None

    id_col = 'item_id' if 'item_id' in df.columns else (
        'video_id' if 'video_id' in df.columns else None)

    # Row lookup via a plain array comparison rather than .loc, so a label the
    # frame no longer carries is an empty result rather than a KeyError.
    # Non-integral values are rejected outright: a client holding a chunk from
    # before the index was normalised can still send a float NaN, and NaN
    # compares False against everything — it would slip past this branch into
    # the item_id fallback and answer with the wrong occurrence.
    rows = None
    if row_index is not None:
        try:
            wanted = int(row_index)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None and wanted == row_index:
            positions = np.flatnonzero(np.asarray(df.index) == wanted)
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


# Column caches for the per-request enrichment. Computing the dynamic columns
# is O(rows) — an Arrow cast of item_id, two isin passes and two full-length
# object arrays — and used to run on EVERY filter/overlay/ids request (seconds
# per checkbox tick on a multi-million-row study). The columns only change
# when the frame changes (parquet mtime / row count) or the user's tag file is
# refetched, so they are cached and re-attached.
#
# The machine columns depend only on the frame, so they are shared across
# users and sized with the study cache (2 frames). The user columns are small
# per entry unless the user has tags. Tokens carry the parquet mtime, the row
# count and (for user columns) the identity of the cached user-JSON blob —
# the blob reference is pinned in the entry so id() stays valid; a TTL refetch
# or an invalidate_user_json_cache() write produces a new object and thereby a
# recompute.
_user_annot_cols_cache = LRUCache(maxsize=16)
_machine_annot_cols_cache = LRUCache(maxsize=2)
_annot_cols_lock = threading.Lock()


def _attach_columns(df, col_types, computed):
    """Attach precomputed (cols, ctypes) pairs to a shallow copy of ``df``."""
    # Shallow copy: only whole columns are added, so nothing shared with the
    # caller's frame is mutated. A deep copy would duplicate the whole study
    # frame (several GB on all_collections) to add three columns.
    df = df.copy(deep=False)
    col_types = col_types.copy()
    for cols, ctypes in computed:
        for name, values in cols.items():
            df[name] = values
        col_types.update(ctypes)
    return df, col_types


def enrich_with_user_tags(df, col_types, username, shared_users_tags=None,
                          study=None):
    """
    Injects a 'User Tags' column into the DataFrame based on the user's tag file.
    Returns (enriched_df, enriched_col_types).
    If no tags found, returns original.

    When ``study`` is given the computed columns are served from the
    mtime-keyed caches above; ``shared_users_tags`` bypasses the user-column
    cache (the shared map is assembled per request by the caller).
    """
    user_blob = get_user_json_cached(username)

    cache_token = None
    if study is not None:
        mtime = _get_recoded_mtime(study)
        cache_token = (mtime, len(df))

    # Machine columns: shared across users, keyed on the frame identity plus
    # which source columns this projection carries.
    machine_token = None
    if cache_token is not None:
        machine_token = (*cache_token,
                         'annotated_ok' in df.columns,
                         'annotation_version' in df.columns)
        with _annot_cols_lock:
            entry = _machine_annot_cols_cache.get(study)
        machine = entry[1] if entry is not None and entry[0] == machine_token else None
    else:
        machine = None
    if machine is None:
        machine = _compute_machine_annotation_columns(df)
        if machine_token is not None:
            with _annot_cols_lock:
                _machine_annot_cols_cache[study] = (machine_token, machine)

    # User columns: per (user, study), invalidated by frame change or a fresh
    # user-JSON blob. A request carrying shared_users_tags computes uncached.
    user_cols = None
    user_key = (username, study)
    if cache_token is not None and shared_users_tags is None:
        user_token = (*cache_token, id(user_blob))
        with _annot_cols_lock:
            entry = _user_annot_cols_cache.get(user_key)
        if entry is not None and entry[0] == user_token:
            user_cols = entry[1]
        if user_cols is None:
            user_cols = _compute_user_annotation_columns(df, user_blob, None)
            with _annot_cols_lock:
                _user_annot_cols_cache[user_key] = (user_token, user_cols, user_blob)
    else:
        user_cols = _compute_user_annotation_columns(df, user_blob, shared_users_tags)

    return _attach_columns(df, col_types, [user_cols, machine])


def _compute_user_annotation_columns(df, user_blob, shared_users_tags):
    """Build the 'User Tags' and 'Has Annotation' columns for ``df``.

    Returns ({column_name: values}, {column_name: col_type}).
    """
    user_data = user_blob or {}
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

    # Was: if not annotated_ids: return early.
    # We continue now to ensure "Has Annotation" is present even if empty.

    cols = {}
    ctypes = {}
    n = len(df)

    # Arrow-native string view of the ids: isin() on it stays vectorized, and
    # python-level per-row work below is confined to the annotated rows (tens
    # to thousands, not millions). The old implementation mapped and re-checked
    # every row through python callables, which alone cost tens of seconds per
    # request on a multi-million-row study.
    str_ids = df['item_id'].astype('string[pyarrow]')

    # 1. User Tags (List) — built as an Arrow list column, NOT a python-object
    # column. get_current_stats' list path has an Arrow fast path (value
    # counts in C); an object column falls into its per-row python loop, which
    # measured ~0.8s over 2.4M rows PER stats pass and ran on every filter
    # request and both slices. Python-level work here is confined to the
    # tagged rows (tens to thousands); the offsets array is vectorized.
    if id_to_tags:
        tag_mask = str_ids.isin(list(id_to_tags)).fillna(False).to_numpy(dtype=bool)
        if tag_mask.any():
            positions = np.flatnonzero(tag_mask)
            lengths = np.zeros(n, dtype=np.int64)
            values: list[str] = []
            # positions ascend, so appending keeps values in row order.
            for pos, iid in zip(positions, str_ids.iloc[positions]):
                row_tags = id_to_tags.get(str(iid)) or _NO_TAGS
                lengths[pos] = len(row_tags)
                values.extend(str(t) for t in row_tags)
            offsets = np.zeros(n + 1, dtype=np.int64)
            np.cumsum(lengths, out=offsets[1:])
            try:
                arr = pa.LargeListArray.from_arrays(
                    pa.array(offsets, type=pa.int64()),
                    pa.array(values, type=pa.large_string()),
                )
                cols['User Tags'] = pd.arrays.ArrowExtensionArray(arr)
            except Exception:
                # Fallback: the shared-empty-list object column (read-only by
                # contract — every untagged row shares ONE empty list).
                tags_col = np.empty(n, dtype=object)
                tags_col.fill(_NO_TAGS)
                for pos, iid in zip(positions, str_ids.iloc[positions]):
                    tags_col[pos] = id_to_tags.get(str(iid)) or _NO_TAGS
                cols['User Tags'] = tags_col
            ctypes['User Tags'] = 'list'

    # 2. Has Annotation (Boolean/Category)
    if shared_users_tags:
        annotated_ids.update(str(k) for k in shared_users_tags.keys())

    # numpy bools rather than Arrow: downstream astype(str) must keep yielding
    # 'True'/'False' (Arrow bools stringify lowercase), and the filter/metadata
    # payloads are built from these values.
    cols['Has Annotation'] = str_ids.isin(annotated_ids).fillna(False).to_numpy(dtype=bool)
    ctypes['Has Annotation'] = 'category'  # Treat as category to trigger checkbox UI

    return cols, ctypes


def _compute_machine_annotation_columns(df):
    """Build the 'Machine Annotations' column for ``df``.

    Which model annotated each item: annotated rows get the annotating model's
    short name (resolved from the row's annotation_version via the version
    registry); rows without per-row provenance (pre-versioning history) fall
    back to the generic label.

    Returns ({column_name: values}, {column_name: col_type}).
    """
    cols = {}
    ctypes = {}
    n = len(df)
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

        cols['Machine Annotations'] = machine
        ctypes['Machine Annotations'] = 'category'

    return cols, ctypes




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



# Collections a study's BUILT frame actually contains, keyed on that frame's
# mtime so a refresh worker rewriting the parquet invalidates the entry.
_frame_collections_cache = LRUCache(maxsize=8)
_frame_collections_lock = threading.Lock()




def get_study_frame_collections(study) -> set | None:
    """Collection ids present in the study's built dataset, or None if unbuilt.

    ``SELECTED_COLLECTIONS`` is what a study asked for; this is what it got. A
    selected collection whose activity is entirely removed by the study's date
    window or its group/activity-count thresholds contributes no rows, and is
    therefore absent from Explore, Timelines and the PCA. Anything rendering
    "the study's data" must scope to this set, not to the selection.

    Read from the sidecar's ``selected_cells`` when sampling is active (already
    cached, no extra I/O), else from the frame's ``collection_id`` column.

    Args:
        study: Study name.

    Returns:
        The set of collection ids in the study's frame, or ``None`` when that
        frame does not exist / cannot be read — which the caller must NOT
        confuse with a study that legitimately resolved to no collections.
    """
    if not study:
        return None

    compose = resolve_compose(study)
    if compose:
        # Union of what both source frames actually contain. Base unbuilt ⇒
        # the composed frame does not exist either; a missing overlay reads
        # as empty rather than hiding the base's data.
        base_cids = get_study_frame_collections(compose[0])
        if base_cids is None:
            return None
        overlay_cids = get_study_frame_collections(compose[1]) or set()
        return set(base_cids) | set(overlay_cids)

    mtime = _get_recoded_mtime(study)
    if mtime is None:
        return None

    with _frame_collections_lock:
        entry = _frame_collections_cache.get(study)
        if entry is not None and entry[0] == mtime:
            return entry[1]

    cids = None
    sidecar = get_study_sidecar(study)
    if sidecar and sidecar.get("sampling_active"):
        cells = sidecar.get("selected_cells")
        if isinstance(cells, dict):
            cids = {str(cid) for cid in cells}
    if cids is None:
        # One streamed pass over a dictionary-encoded column: bounded memory
        # even on a million-row study frame (same pattern as
        # session_explorer.discover_collections).
        try:
            cids = set()
            for batch in data_io.iter_parquet_batches(
                    storage_location="cache",
                    filename=f"{study}_recoded.parquet",
                    columns=["collection_id"]):
                if batch.num_columns == 0:
                    return None
                cids.update(str(c) for c in pa_compute.unique(batch.column(0)).to_pylist()
                            if c is not None)
        except Exception:
            return None

    with _frame_collections_lock:
        _frame_collections_cache[study] = (mtime, cids)
    return cids




def get_study_date_window(study) -> tuple[pd.Timestamp, pd.Timestamp]:
    """A study's activity date window as a half-open ``[start, end_bound)`` pair.

    The same convention the study builder applies in
    :func:`fyp.organize_datasets.load_collection_data`: ``START_DATE`` is
    inclusive, and the stored ``END_DATE`` means "through the end of that day",
    so the returned upper bound is the following midnight (exclusive). An
    absent or unparseable bound falls back to the builder's own wide defaults,
    which makes the window a no-op rather than an accidental cut.

    Anything rendering "the study's data" out of a GLOBAL artifact (one built
    over every collection's full history, e.g. the sessions index) must apply
    this alongside :func:`get_study_frame_collections` — the collection set
    alone does not carry the date window.

    Args:
        study: Study name.

    Returns:
        ``(start, end_bound)`` as pandas Timestamps, comparable against a
        wall-clock ``local_timestamp``-derived column.
    """
    if "study_defs" not in fyp_cf:
        init_study_defs()

    compose = resolve_compose(study)
    if compose:
        # Envelope of both sides. The composed def carries no dates (the
        # owner's rows span their full range) so this resolves to the wide
        # default window — base rows outside the base study's own window are
        # already absent from the composed frame, and the sessions index only
        # covers what some study's window spans, so the wide bound over-scopes
        # by at most a padding's worth of edge sessions.
        base_start, base_end = get_study_date_window(compose[0])
        cfg = (fyp_cf.get("study_defs", {}) or {}).get(study) or {}
    else:
        cfg = (fyp_cf.get("study_defs", {}) or {}).get(study) or {}

    def _bound(key: str, default: str) -> pd.Timestamp:
        raw = cfg.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return pd.Timestamp(datetime.strptime(raw.strip(), "%Y-%m-%d"))
            except ValueError:
                pass
        return pd.Timestamp(default)

    start = _bound("START_DATE", "1970-01-01")
    end_bound = _bound("END_DATE", "2099-12-31") + pd.Timedelta(days=1)
    if compose:
        return min(start, base_start), max(end_bound, base_end)
    return start, end_bound




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

    compose = resolve_compose(study)
    if compose:
        # A composed study's own list holds only the owner's collections; the
        # base (default) study contributes the rest at read time.
        base_selected = (fyp_cf["study_defs"].get(compose[0]) or {}).get(
            "SELECTED_COLLECTIONS", []) or []
        seen = {str(c).strip() for c in selected_collections}
        selected_collections = list(selected_collections) + [
            c for c in base_selected if str(c).strip() not in seen]

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

