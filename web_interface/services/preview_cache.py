"""Tiered cache for the study-design preview frame.

Pure moves from ``web_interface/routes/management_routes.py`` (Phase 7b).
The "Check study design" button is pressed repeatedly while a user tweaks the
date range or sampling thresholds; those tweaks never change which collections
are read, nor the per-row preprocessing. So a preprocessed frame (play/observe
filtered, with the day key, scrape/annotation flags and event-window flag
precomputed) is cached keyed by the collection set.

Two tiers, both keyed by the collection set:
  - In-process (per web instance, TTL): serves the tweak-and-recheck loop in ~ms.
  - On disk (GCS/local, write-through, mtime-invalidated): lets the first check after
    a modal (re)open or on a freshly-scaled instance load a ~50 MB prepared parquet
    (~0.1 s) instead of rebuilding from the raw window (~2-3 s). The modal also calls
    the prewarm endpoint on open / collection-change so the build happens during the
    user's think-time rather than on the first button press.
"""

import hashlib
import threading
import time as _time

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

from .stats_service import (
    _filter_to_event_windows,
    _filter_to_play_observe,
    _load_collection_event_windows,
)

_PREVIEW_CACHE_TTL_S = 300
_PREVIEW_WINDOW_CACHE_MAXSIZE = 2
_PREVIEW_DISK_PREFIX = "study_precheck_frame__"
_PREVIEW_DISK_MAXFILES = 24
_preview_cache_lock = threading.Lock()
_preview_frame_cache: dict = {}    # frozenset(collection_ids) -> (monotonic_ts, df | None, src_mtime)
_preview_status_cache: dict = {}   # "status" -> (monotonic_ts, df | None, src_mtime)
_SOURCES_MTIME_MEMO_S = 5.0
_sources_mtime_memo: tuple[float, float] | None = None  # (monotonic_ts, sources_mtime)
_preview_warming: set = set()      # collection-set keys with a prewarm thread in flight
_preview_build_locks: dict = {}    # collection-set key -> Lock (one build at a time)




def _preview_frame_key(selected: list) -> frozenset:
    """Stable cache key for a collection set (order-independent)."""

    return frozenset(str(c) for c in selected)




def _preview_frame_filename(selected: list) -> str:
    """Disk filename for the prepared frame of a collection set (hash of sorted ids)."""

    digest = hashlib.sha1("\n".join(sorted(str(c) for c in selected)).encode("utf-8")).hexdigest()[:16]
    return f"{_PREVIEW_DISK_PREFIX}{digest}.parquet"




def _preview_sources_mtime() -> float:
    """Newest mtime among the inputs the prepared frame is derived from.

    A persisted frame older than this is stale and must be rebuilt. Covers every source
    the frame reads: collection activities (collections_recoded), the scrape/annotation
    flags (enrichment_status) and the event-window bounds (collections_metadata).
    """

    newest = 0.0
    sources = (
        ("recoded", f"{COLLECTIONS_LABEL}_recoded.parquet"),
        ("recoded", "enrichment_status.parquet"),
        ("recoded", f"{COLLECTIONS_LABEL}_metadata.parquet"),
    )
    for loc, fn in sources:
        try:
            if data_io.exists(storage_location=loc, filename=fn):
                newest = max(newest, float(data_io.getmtime(storage_location=loc, filename=fn)))
        except Exception:
            pass
    return newest




def _preview_sources_mtime_cached() -> float:
    """``_preview_sources_mtime`` behind a short memo.

    The in-memory cache tiers validate against the source mtimes on every read
    so a consolidation invalidates them immediately (not after the 5-min TTL);
    the memo bounds that to one stat sweep per few seconds, which also caps the
    worst-case staleness window.
    """

    global _sources_mtime_memo
    now = _time.monotonic()
    with _preview_cache_lock:
        memo = _sources_mtime_memo
        if memo is not None and (now - memo[0]) < _SOURCES_MTIME_MEMO_S:
            return memo[1]
    value = _preview_sources_mtime()
    with _preview_cache_lock:
        _sources_mtime_memo = (now, value)
    return value




def _load_prepared_from_disk(filename: str) -> pd.DataFrame | None:
    """Load a persisted prepared frame and restore the dtypes the estimator expects."""

    df = data_io.load_parquet(storage_location="cache", filename=filename)
    if df is None or df.empty:
        return None
    df["collection_id"] = df["collection_id"].astype("category")
    df["item_id"] = df["item_id"].astype("string[pyarrow]")
    df["_ts"] = pd.to_datetime(df["_ts"], errors="coerce")
    df["_ld"] = pd.to_datetime(df["_ld"], errors="coerce")
    for col in ("_scraped", "_annotated", "_in_window"):
        df[col] = df[col].astype(bool)
    return df




def _save_prepared_to_disk(frame: pd.DataFrame, filename: str) -> None:
    """Persist a prepared frame (write-through) and prune old frames. Best-effort."""

    out = frame.copy()
    # category metadata round-trips awkwardly; store as plain string.
    out["collection_id"] = out["collection_id"].astype("string[pyarrow]")
    data_io.save_parquet(out, storage_location="cache", filename=filename)
    _prune_disk_frames()




def _prune_disk_frames() -> None:
    """Keep only the newest _PREVIEW_DISK_MAXFILES persisted frames. Best-effort."""

    try:
        names = [f for f in data_io.listdir(storage_location="cache") if str(f).startswith(_PREVIEW_DISK_PREFIX)]
        if len(names) <= _PREVIEW_DISK_MAXFILES:
            return
        with_mtime = []
        for n in names:
            try:
                with_mtime.append((float(data_io.getmtime(storage_location="cache", filename=n)), n))
            except Exception:
                with_mtime.append((0.0, n))
        with_mtime.sort(reverse=True)
        for _, n in with_mtime[_PREVIEW_DISK_MAXFILES:]:
            try:
                data_io.remove(storage_location="cache", filename=n)
            except Exception:
                pass
    except Exception:
        pass




def _get_enrichment_status_cached() -> pd.DataFrame | None:
    """Return the projected enrichment_status, cached in-process with the preview TTL."""

    now = _time.monotonic()
    src_mtime = _preview_sources_mtime_cached()
    with _preview_cache_lock:
        hit = _preview_status_cache.get("status")
        if hit is not None and (now - hit[0]) < _PREVIEW_CACHE_TTL_S and hit[2] >= src_mtime:
            return hit[1]

    df = _load_enrichment_status_min()
    with _preview_cache_lock:
        _preview_status_cache["status"] = (now, df, src_mtime)
    return df




def _event_window_mask(cid: pd.Series, ts: pd.Series, windows: dict) -> np.ndarray:
    """Boolean mask: True where each row's day is within its collection's event window.

    Rows for a collection missing from `windows` are kept (no metadata, no filter).
    Mirrors _filter_to_event_windows but returns the mask so it can be precomputed once.
    """

    if not windows:
        return np.ones(len(cid), dtype=bool)
    ts_arr = ts.dt.normalize().to_numpy(dtype="datetime64[ns]")
    cid_s = cid.astype(str)
    # Map via dicts (C-level) rather than a python lambda per row.
    first_map = {k: v[0] for k, v in windows.items()}
    last_map = {k: v[1] for k, v in windows.items()}
    first_arr = pd.to_datetime(cid_s.map(first_map), errors="coerce").dt.normalize().to_numpy(dtype="datetime64[ns]")
    last_arr = pd.to_datetime(cid_s.map(last_map), errors="coerce").dt.normalize().to_numpy(dtype="datetime64[ns]")
    has_window = (~pd.isna(first_arr)) & (~pd.isna(last_arr))
    in_window = (ts_arr >= first_arr) & (ts_arr <= last_arr)
    return np.where(has_window, in_window, True)




def _prepare_preview_frame(selected: list, df_status: pd.DataFrame | None) -> pd.DataFrame | None:
    """Build the preprocessed preview frame for a collection set (the cacheable unit).

    Does all the per-row work once: filters to play/observe, parses the timestamp and
    day key, flags each row's scrape/annotation status and event-window membership. Every
    subsequent check (date / sampling tweak) operates on this frame with cheap masks.

    Args:
        selected: Collection ids to include.
        df_status: Projected enrichment_status (item_id / scraped_ok / annotated_ok), or None.

    Returns:
        A DataFrame with columns collection_id, item_id, _ts, _ld, _scraped, _annotated,
        _in_window, or None when nothing matches.
    """

    raw = _load_collections_window(selected)
    if raw is None or raw.empty:
        return None
    raw = raw[raw["activity_type"].isin(["play", "observe"])].copy()
    if raw.empty:
        return None

    # local_timestamp is a native timestamp; its normalized day equals the recoded
    # local_date group-factor, so derive the day key from it and skip the (slow)
    # date32 parse of local_date entirely.
    ts = pd.to_datetime(raw["local_timestamp"], errors="coerce")

    # Per-row scrape/annotation flags via one combined index lookup. Object keys are
    # needed only for the lookup; the frame keeps item_id arrow-backed (compact).
    iid_keys = raw["item_id"].astype(str).to_numpy()
    scraped_flag = np.zeros(len(raw), dtype=bool)
    annotated_flag = np.zeros(len(raw), dtype=bool)
    if df_status is not None and not df_status.empty:
        status = df_status
        if "item_id" not in status.columns and status.index.name == "item_id":
            status = status.reset_index()
        if "item_id" in status.columns:
            status = status.drop_duplicates("item_id").set_index(status["item_id"].astype(str))
            cols = [c for c in ("scraped_ok", "annotated_ok") if c in status.columns]
            if cols:
                flags = status[cols].reindex(iid_keys)
                if "scraped_ok" in cols:
                    scraped_flag = flags["scraped_ok"].fillna(False).to_numpy(dtype=bool)
                if "annotated_ok" in cols:
                    annotated_flag = flags["annotated_ok"].fillna(False).to_numpy(dtype=bool)

    windows = _load_collection_event_windows([str(c) for c in selected])

    # Assemble in place so item_id keeps its compact arrow dtype; collection_id becomes
    # a category (few uniques) for cheap groupbys and a small footprint.
    raw["_ts"] = ts.to_numpy()
    raw["_ld"] = ts.dt.normalize().to_numpy()
    raw["_scraped"] = scraped_flag
    raw["_annotated"] = annotated_flag
    raw["_in_window"] = _event_window_mask(raw["collection_id"], ts, windows)
    raw = raw[raw["_ld"].notna()]
    if raw.empty:
        return None

    frame = raw[["collection_id", "item_id", "_ts", "_ld", "_scraped", "_annotated", "_in_window"]].copy()
    frame["collection_id"] = frame["collection_id"].astype("category")
    return frame




def _cache_frame_in_memory(key: frozenset, frame: pd.DataFrame | None, now: float,
                           src_mtime: float) -> None:
    """Store a frame in the in-process cache, expiring stale entries and capping size.

    ``src_mtime`` is the sources mtime the frame was built against — a read
    whose current sources mtime exceeds it treats the entry as stale.
    """

    with _preview_cache_lock:
        _preview_frame_cache[key] = (now, frame, src_mtime)
        for stale in [k for k, v in _preview_frame_cache.items() if (now - v[0]) >= _PREVIEW_CACHE_TTL_S]:
            _preview_frame_cache.pop(stale, None)
        while len(_preview_frame_cache) > _PREVIEW_WINDOW_CACHE_MAXSIZE:
            oldest = min(_preview_frame_cache, key=lambda k: _preview_frame_cache[k][0])
            _preview_frame_cache.pop(oldest, None)




def _build_lock_for(key: frozenset) -> threading.Lock:
    """Return a per-collection-set lock so only one build runs per key at a time."""

    with _preview_cache_lock:
        lk = _preview_build_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _preview_build_locks[key] = lk
        return lk




def _read_cached_frame(key: frozenset, disk_fn: str) -> tuple[bool, pd.DataFrame | None]:
    """Try the in-memory then on-disk tiers. Returns (hit, frame)."""

    now = _time.monotonic()
    src_mtime = _preview_sources_mtime_cached()
    with _preview_cache_lock:
        hit = _preview_frame_cache.get(key)
        if hit is not None and (now - hit[0]) < _PREVIEW_CACHE_TTL_S and hit[2] >= src_mtime:
            return True, hit[1]

    try:
        if data_io.exists(storage_location="cache", filename=disk_fn):
            if float(data_io.getmtime(storage_location="cache", filename=disk_fn)) >= src_mtime:
                frame = _load_prepared_from_disk(disk_fn)
                if frame is not None:
                    _cache_frame_in_memory(key, frame, now, src_mtime)
                    return True, frame
    except Exception as e:
        print(f"[preview-cache] disk load failed for {disk_fn}: {e}")
    return False, None




def _get_prepared_frame_cached(selected: list, df_status: pd.DataFrame | None) -> pd.DataFrame | None:
    """Return the preprocessed preview frame for `selected`: memory → disk → build.

    1. In-process cache (TTL) — serves the tweak-and-recheck loop in ~ms.
    2. On-disk prepared frame, if present and newer than its source parquets — loaded in
       ~0.1 s instead of rebuilding (first check after a modal reopen / on a new instance).
    3. Otherwise build from the raw window, then write through to disk for next time.

    Builds are serialized per collection set, so a check that arrives while a prewarm
    build for the same set is in flight waits for it and reuses the result rather than
    starting a second (multi-second) build.
    """

    if not selected:
        return None
    key = _preview_frame_key(selected)
    disk_fn = _preview_frame_filename(selected)

    hit, frame = _read_cached_frame(key, disk_fn)
    if hit:
        return frame

    with _build_lock_for(key):
        # Re-check: another thread may have built it while we waited for the lock.
        hit, frame = _read_cached_frame(key, disk_fn)
        if hit:
            return frame

        # Probe the sources mtime BEFORE building: a consolidation write that
        # lands mid-build then correctly marks this entry stale on next read.
        src_mtime = _preview_sources_mtime_cached()
        frame = _prepare_preview_frame(selected, df_status)
        _cache_frame_in_memory(key, frame, _time.monotonic(), src_mtime)
        if frame is not None:
            try:
                _save_prepared_to_disk(frame, disk_fn)
            except Exception as e:
                print(f"[preview-cache] disk save failed for {disk_fn}: {e}")
        return frame




def _load_collections_window(selected: list) -> pd.DataFrame | None:
    """Read the selected collections' raw activities from collections_recoded once.

    Projects only the columns the study preview needs, filtered to the selected
    collections (no date / activity-type filter). Both the universe mosaic and the
    sampling estimate are derived from this single read in memory, so the modal's
    "Check study design" touches collections_recoded only once instead of twice.

    Args:
        selected: Collection ids to include.

    Returns:
        A DataFrame (collection_id, local_timestamp, local_date, activity_type,
        item_id), or None when nothing matches.
    """

    if not selected:
        return None
    if not data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_recoded.parquet"):
        return None
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["collection_id", "local_timestamp", "local_date", "activity_type", "item_id"],
        filters=[("collection_id", "in", [str(c) for c in selected])],
    )
    if df is None or df.empty:
        return None
    return df




def _load_enrichment_status_min() -> pd.DataFrame | None:
    """Load enrichment_status.parquet with only the columns the preview reads.

    The check only needs item_id / scraped_ok / annotated_ok; projecting to those
    three columns avoids materialising the full per-video status table.

    Returns:
        A DataFrame (item_id, scraped_ok, annotated_ok), or None when absent.
    """

    if not data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
        return None
    return data_io.load_parquet_selective(
        storage_location="recoded",
        filename="enrichment_status.parquet",
        columns=["item_id", "scraped_ok", "annotated_ok"],
    )




def _load_study_raw_window(selected: list, df_window: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """Load raw activities for the selected collections, within their event windows.

    Restricts to each collection's first/last event window and to play/observe rows —
    the same base set used for the modal's "potential" counts and universe mosaic.

    Args:
        selected: Collection ids to include.
        df_window: Pre-loaded collections window (from _load_collections_window) to
            filter in memory; avoids a redundant read when the caller already has it.

    Returns:
        A DataFrame with columns collection_id, local_timestamp, activity_type, item_id,
        or None when nothing is selected or no rows remain.
    """

    if not selected:
        return None
    if df_window is None:
        if not data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_recoded.parquet"):
            return None
        df_raw = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
            columns=["collection_id", "local_timestamp", "activity_type", "item_id"],
            filters=[("collection_id", "in", selected)],
        )
    else:
        df_raw = df_window
    if df_raw is None or df_raw.empty:
        return None
    windows = _load_collection_event_windows(selected)
    df_raw = _filter_to_event_windows(df_raw, windows)
    df_raw = _filter_to_play_observe(df_raw)
    return df_raw if not df_raw.empty else None




def _prewarm_preview_frame(selected: list) -> None:
    """Build + cache (memory + disk) the prepared frame in the background.

    Dedupes concurrent builds for the same collection set via _preview_warming.
    """

    key = _preview_frame_key(selected)
    with _preview_cache_lock:
        if key in _preview_warming:
            return
        _preview_warming.add(key)
    try:
        status = _get_enrichment_status_cached()
        _get_prepared_frame_cached(selected, status)
    except Exception as e:
        print(f"[prewarm] failed: {e}")
    finally:
        with _preview_cache_lock:
            _preview_warming.discard(key)




def _collections_hash(selected: list) -> str:
    """Return a short stable hash of a selected-collections list."""

    ids = sorted(str(x) for x in (selected or []))
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16]
