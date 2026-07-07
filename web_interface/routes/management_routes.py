import hashlib
import json
import os
import threading
import time as _time
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

import fyp.data_io as data_io
import fyp.scrape_queues as scrape_queues
from fyp.platform_scraper import get_scraper
from fyp.fyp_config import (
    fyp_cf,
    load_var_schema,
)
from fyp.recode_variables import (
    SEMANTIC_COLUMNS,
    VAR_SCHEMA_ROLES,
    VAR_SCHEMA_SCALES,
    compute_var_schema_hash,
)
import fyp.annotation_versioning as annotation_versioning
from fyp.ingest import get_main_collection, parse_donor_timezone, registered_raw_locations
from fyp.machine_annotation import rebuild_active_annotations_from_archive
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    SAMPLE_NO_CAP,
    create_study_recoded_dataset,
    parse_sample_threshold,
)
from fyp.studies import init_study_defs, save_study_defs

from .. import activity_log
from ..data_service import (
    calculate_inter_coder_reliability,
    invalidate_collection_tags_cache,
    load_display_id_map,
    study_cache,
)
from ..process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
)
from ..permissions import permission_required
from ..task_status import is_cloud_run, read_task_status


def _actor() -> str:
    """Return the username of the acting user, or empty string if unauthenticated."""
    try:
        return current_user.username if current_user.is_authenticated else ""
    except Exception:
        return ""

management_bp = Blueprint('management_bp', __name__)


# Downstream refresh steps considered by the auto-pipeline, in the order they
# are dispatched. Keep in sync with _PIPELINE_STEPS_ORDER in
# run_consolidate_enrichment.py. Ordering matters: embeddings feed video_map
# (the niches), video_map feeds recode, and recode produces the recoded datasets
# that meta_refresh_groups / pca_refresh consume. This list is used only to
# check whether any pipeline step is currently running, so membership matters
# more than order, but the two lists are kept identical to avoid drift.
PIPELINE_STEPS_ORDER = [
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
]


def _is_worker_running(name: str) -> bool:
    """True if a worker (subprocess or Cloud Task) is currently running.

    Consults the in-memory subprocess state *and* the GCS status file, with
    the same stale-heartbeat detection used by /api/status. Safe to call from
    any endpoint that needs to gate behaviour on worker activity.
    """
    proc_state = processes.get(name, {})
    proc = proc_state.get("proc")
    if proc is not None and proc.poll() is None:
        return True

    if is_cloud_run():
        gcs_status = read_task_status(name)
        if gcs_status and gcs_status.get("state") == "running":
            updated_str = gcs_status.get("updated_at") or ""
            try:
                updated_at = datetime.fromisoformat(updated_str)
                age = (datetime.now(UTC) - updated_at).total_seconds()
                if age <= 600:
                    return True
            except (ValueError, TypeError):
                # No / malformed heartbeat — treat as running to be safe.
                return True

    return False


def _workers_blocking_consolidate() -> list[str]:
    """Return the names of scraper/annotator workers currently running."""
    blocking = []
    for name in ("queue_scraper", "queue_annotator"):
        if _is_worker_running(name):
            blocking.append(name)
    return blocking


def _build_pipeline_step_view(pipeline_active: bool) -> list[dict]:
    """Build an ordered per-step view of the last/active consolidate pipeline.

    Returns one dict per step (``consolidate_enrichment`` plus every step in the
    persisted ``pipeline_plan``) with keys ``step``, ``label``, ``state``,
    ``percent``, ``message`` and ``ran_at``. ``state`` is one of ``running``,
    ``queued``, ``success``, ``failed``, ``skipped`` or ``pending``. Live state
    comes from each step's GCS status file (Cloud Run); terminal outcomes fall
    back to ``process_stats``. Returns ``[]`` when no plan has been recorded so
    the UI hides the list.

    Args:
        pipeline_active: Whether a consolidate pipeline is currently in flight
            (``pipeline_in_flight`` or any step running). Drives the
            pending-vs-skipped distinction for steps that have not run.
    """
    from web_interface.run_consolidate_enrichment import _PIPELINE_STAGE_LABELS

    # Merge the in-memory ::DATA:: copy: in local/subprocess mode the consolidate
    # worker's pipeline_plan lives in processes[...]["data"] until the process
    # completes, so reading process_stats alone would miss it mid-run.
    entry = {
        **process_stats.get("consolidate_enrichment", {}),
        **(processes.get("consolidate_enrichment", {}).get("data", {}) or {}),
    }
    plan = entry.get("pipeline_plan") or {}
    steps = plan.get("steps") or []
    if not steps:
        return []

    started_dt = None
    started_ts = plan.get("started_ts")
    if started_ts:
        try:
            started_dt = datetime.fromisoformat(started_ts)
        except (ValueError, TypeError):
            started_dt = None

    cloud = is_cloud_run()
    view: list[dict] = []
    for step in ["consolidate_enrichment"] + steps:
        ps = process_stats.get(step, {})
        label = _PIPELINE_STAGE_LABELS.get(step, step)

        # Live status: a fresh running/queued state wins. On Cloud Run this comes
        # from the per-step GCS status file; locally from the in-memory process
        # entry (the web service runs the local pipeline thread in-process).
        live_state = None
        percent = None
        message = None
        if cloud:
            st = read_task_status(step) or {}
            raw = (st.get("state") or "").lower()
            fresh = True
            updated = st.get("updated_at")
            if started_dt and updated:
                try:
                    fresh = datetime.fromisoformat(updated) >= started_dt
                except (ValueError, TypeError):
                    fresh = True
            if fresh and raw in ("running", "queued"):
                live_state = raw
                prog = st.get("progress") or {}
                percent = prog.get("percent")
                message = prog.get("message")
        else:
            if (processes.get(step, {}) or {}).get("status") == "running":
                live_state = "running"
                prog = (processes.get(step, {}) or {}).get("progress") or {}
                percent = prog.get("percent")
                message = prog.get("message")

        # Did this step reach a terminal state as part of THIS pipeline run?
        end = ps.get("last_run_end_time")
        end_dt = None
        if end:
            try:
                end_dt = datetime.fromisoformat(end)
            except (ValueError, TypeError):
                end_dt = None
        ran_this_run = end_dt is not None and (started_dt is None or end_dt >= started_dt)

        if live_state:
            state = live_state
        elif ran_this_run:
            state = "success" if ps.get("last_run_outcome") == "Success" else "failed"
        else:
            # Never ran this round: pending while the pipeline is still active,
            # otherwise skipped (aborted before reaching it / dropped by a 429).
            state = "pending" if pipeline_active else "skipped"

        view.append({
            "step": step,
            "label": label,
            "state": state,
            "percent": percent if state == "running" else None,
            "message": message if state == "running" else None,
            "ran_at": end if ran_this_run else None,
        })

    return view






LARGE_STUDY_THRESHOLD = 500_000
SPARSE_CELL_MIN_ACTIVITIES = 10




def _daily_counts(df: pd.DataFrame, timestamp_col: str = 'local_timestamp') -> list[dict]:
    """Return a sorted list of {date: 'YYYY-MM-DD', count: int} from a DataFrame."""

    if df is None or df.empty or timestamp_col not in df.columns:
        return []

    ts = pd.to_datetime(df[timestamp_col], errors='coerce').dropna()
    if ts.empty:
        return []

    grouped = ts.dt.date.value_counts().sort_index()
    return [{"date": d.isoformat(), "count": int(c)} for d, c in grouped.items()]




def _load_collection_event_windows(collection_ids: list) -> dict:
    """Return {collection_id: (first_date, last_date)} from collections_metadata.parquet.

    Dates are pandas.Timestamp (date-only, no timezone) so they can be compared
    directly to `local_timestamp.dt.normalize()`. Collections without metadata
    are simply absent from the returned dict — the caller should decide whether
    to include or exclude them.
    """

    filename = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return {}

    try:
        df_meta = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=[
                "('personas', 'first_event_ts')", "first_event_ts",
                "('personas', 'last_event_ts')", "last_event_ts",
            ],
            set_index='collection_id',
        )
    except Exception as e:
        print(f"[daily_activities] failed to load collections_metadata: {e}")
        return {}

    if df_meta is None or df_meta.empty:
        return {}

    first_col = ('personas', 'first_event_ts') if ('personas', 'first_event_ts') in df_meta.columns else ('first_event_ts' if 'first_event_ts' in df_meta.columns else None)
    last_col = ('personas', 'last_event_ts') if ('personas', 'last_event_ts') in df_meta.columns else ('last_event_ts' if 'last_event_ts' in df_meta.columns else None)
    if first_col is None or last_col is None:
        return {}

    ids = set(collection_ids) if collection_ids else None
    out: dict = {}
    for cid, row in df_meta.iterrows():
        cid_str = str(cid)
        if ids is not None and cid_str not in ids:
            continue
        first_raw = row[first_col]
        last_raw = row[last_col]
        if pd.isna(first_raw) or pd.isna(last_raw):
            continue
        first_ts = pd.to_datetime(first_raw, errors='coerce')
        last_ts = pd.to_datetime(last_raw, errors='coerce')
        if pd.isna(first_ts) or pd.isna(last_ts):
            continue
        out[cid_str] = (first_ts.normalize(), last_ts.normalize())
    return out




def _filter_to_event_windows(df: pd.DataFrame, windows: dict, collection_col: str = 'collection_id', timestamp_col: str = 'local_timestamp') -> pd.DataFrame:
    """Drop rows whose timestamp is outside their collection's (first, last) window.

    Rows for a collection missing from `windows` are kept (no metadata, no filter).
    """

    import numpy as _np

    if df is None or df.empty or not windows or collection_col not in df.columns or timestamp_col not in df.columns:
        return df

    # Normalize all three comparison arrays to plain numpy datetime64[ns] so
    # the comparison doesn't fail when the DataFrame is backed by an extension
    # dtype (PyArrow) and the window series is object-dtype Timestamps.
    ts_arr = pd.to_datetime(df[timestamp_col], errors='coerce').dt.normalize().to_numpy(dtype='datetime64[ns]')

    cid = df[collection_col].astype(str)
    first_arr = pd.to_datetime(
        cid.map(lambda c: windows.get(c, (None, None))[0]),
        errors='coerce',
    ).dt.normalize().to_numpy(dtype='datetime64[ns]')
    last_arr = pd.to_datetime(
        cid.map(lambda c: windows.get(c, (None, None))[1]),
        errors='coerce',
    ).dt.normalize().to_numpy(dtype='datetime64[ns]')

    has_window = (~pd.isna(first_arr)) & (~pd.isna(last_arr))
    in_window = (ts_arr >= first_arr) & (ts_arr <= last_arr)
    keep = _np.where(has_window, in_window, True)
    return df.loc[keep]




def _filter_to_play_observe(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only play/observe rows. If activity_type is missing, return df unchanged."""

    if df is None or df.empty or 'activity_type' not in df.columns:
        return df
    return df.loc[df['activity_type'].isin(['play', 'observe'])]




def _compute_universe_enrichment(df_raw: pd.DataFrame, df_status: pd.DataFrame | None,
                                 start_date: str | None, end_date: str | None) -> dict:
    """Count activities by the scrape/annotation status of their video, within the date range.

    Enrichment status is a per-video fact; here each activity inherits its video's status so
    the counts are consistent with the rest of the modal (daily chart, sampling controls),
    which are activity-based.

    Args:
        df_raw: Raw activities for the selected collections, already restricted to each
            collection's event window and to play/observe rows.
        df_status: enrichment_status.parquet (scraped_ok / annotated_ok per item_id), or None.
        start_date: Inclusive lower bound as 'YYYY-MM-DD', or empty/None for no lower bound.
        end_date: Inclusive upper bound as 'YYYY-MM-DD', or empty/None for no upper bound.

    Returns:
        Dict with integer keys 'activities', 'scraped', 'annotated' — the total activities and
        the activities whose video is scraped / annotated, for the date-filtered universe.
    """

    universe = {"activities": 0, "scraped": 0, "annotated": 0}
    if df_raw is None or df_raw.empty or 'item_id' not in df_raw.columns:
        return universe

    df_uni = df_raw
    start_date = (start_date or "").strip()
    end_date = (end_date or "").strip()
    if 'local_timestamp' in df_uni.columns and (start_date or end_date):
        ts = pd.to_datetime(df_uni['local_timestamp'], errors='coerce')
        mask = ts.notna()
        if start_date:
            mask &= ts.dt.date >= pd.to_datetime(start_date).date()
        if end_date:
            mask &= ts.dt.date <= pd.to_datetime(end_date).date()
        df_uni = df_uni.loc[mask]

    if df_uni.empty:
        return universe

    universe["activities"] = int(len(df_uni))

    if df_status is None or df_status.empty:
        return universe

    if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
        df_status = df_status.reset_index()
    if 'item_id' not in df_status.columns:
        return universe

    status_ids = df_status['item_id'].astype(str)
    uni_ids = df_uni['item_id'].astype(str)

    if 'scraped_ok' in df_status.columns:
        scraped_set = set(status_ids[df_status['scraped_ok'].fillna(False).to_numpy()])
        universe["scraped"] = int(uni_ids.isin(scraped_set).sum())
    if 'annotated_ok' in df_status.columns:
        annotated_set = set(status_ids[df_status['annotated_ok'].fillna(False).to_numpy()])
        universe["annotated"] = int(uni_ids.isin(annotated_set).sum())
    return universe




# Cache for the study-preview frame. The "Check study design" button is pressed
# repeatedly while a user tweaks the date range or sampling thresholds; those tweaks
# never change which collections are read, nor the per-row preprocessing. So a
# preprocessed frame (play/observe filtered, with the day key, scrape/annotation flags
# and event-window flag precomputed) is cached keyed by the collection set.
#
# Two tiers, both keyed by the collection set:
#   - In-process (per web instance, TTL): serves the tweak-and-recheck loop in ~ms.
#   - On disk (GCS/local, write-through, mtime-invalidated): lets the first check after
#     a modal (re)open or on a freshly-scaled instance load a ~50 MB prepared parquet
#     (~0.1 s) instead of rebuilding from the raw window (~2-3 s). The modal also calls
#     the prewarm endpoint on open / collection-change so the build happens during the
#     user's think-time rather than on the first button press.
_PREVIEW_CACHE_TTL_S = 300
_PREVIEW_WINDOW_CACHE_MAXSIZE = 2
_PREVIEW_DISK_PREFIX = "study_precheck_frame__"
_PREVIEW_DISK_MAXFILES = 24
_preview_cache_lock = threading.Lock()
_preview_frame_cache: dict = {}    # frozenset(collection_ids) -> (monotonic_ts, df | None)
_preview_status_cache: dict = {}   # "status" -> (monotonic_ts, df | None)
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
    with _preview_cache_lock:
        hit = _preview_status_cache.get("status")
        if hit is not None and (now - hit[0]) < _PREVIEW_CACHE_TTL_S:
            return hit[1]

    df = _load_enrichment_status_min()
    with _preview_cache_lock:
        _preview_status_cache["status"] = (now, df)
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




def _cache_frame_in_memory(key: frozenset, frame: pd.DataFrame | None, now: float) -> None:
    """Store a frame in the in-process cache, expiring stale entries and capping size."""

    with _preview_cache_lock:
        _preview_frame_cache[key] = (now, frame)
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
    with _preview_cache_lock:
        hit = _preview_frame_cache.get(key)
        if hit is not None and (now - hit[0]) < _PREVIEW_CACHE_TTL_S:
            return True, hit[1]

    try:
        if data_io.exists(storage_location="cache", filename=disk_fn):
            if float(data_io.getmtime(storage_location="cache", filename=disk_fn)) >= _preview_sources_mtime():
                frame = _load_prepared_from_disk(disk_fn)
                if frame is not None:
                    _cache_frame_in_memory(key, frame, now)
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

        frame = _prepare_preview_frame(selected, df_status)
        _cache_frame_in_memory(key, frame, _time.monotonic())
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




def _count_sparse_cells(df_study: pd.DataFrame) -> tuple[int, int]:
    """Return (sparse_cells, total_cells) where a cell is (day, collection_id).

    A cell is 'sparse' if it has 1 <= activities < SPARSE_CELL_MIN_ACTIVITIES.
    Zero-activity cells don't exist in a groupby result, so we only count cells
    that actually contain at least one activity.
    """

    if df_study is None or df_study.empty:
        return 0, 0
    if 'collection_id' not in df_study.columns or 'local_timestamp' not in df_study.columns:
        return 0, 0

    ts = pd.to_datetime(df_study['local_timestamp'], errors='coerce')
    mask = ts.notna()
    if not mask.any():
        return 0, 0

    dates = ts[mask].dt.date
    cids = df_study.loc[mask, 'collection_id'].astype(str)
    cells = pd.Series(1, index=pd.MultiIndex.from_arrays([dates, cids], names=['date', 'collection_id'])).groupby(level=[0, 1]).sum()
    total_cells = int(cells.size)
    sparse_cells = int((cells < SPARSE_CELL_MIN_ACTIVITIES).sum())
    return sparse_cells, total_cells




def _derive_study_issues(stats: dict, sparse_cells: int, total_cells: int, has_total_days: bool, sampling_report: dict | None = None) -> list[dict]:
    """Produce an inline feedback list for the study design.

    Returns issues with severity 'ok' | 'warn' | 'error'. Always returns at
    least one entry — a green 'ok' when no rules trip.
    """

    issues: list[dict] = []
    total_activities = int(stats.get("total_activities", 0))

    if total_activities == 0:
        if has_total_days:
            issues.append({
                "severity": "warn",
                "code": "empty_after_sampling",
                "message": "No activities remain after the date filter and sampling. Widen the date range or relax sampling.",
            })
        else:
            issues.append({
                "severity": "warn",
                "code": "no_activities",
                "message": "The selected collections have no activities in the recoded dataset.",
            })
        return issues

    if total_activities > LARGE_STUDY_THRESHOLD:
        issues.append({
            "severity": "warn",
            "code": "too_big",
            "message": (
                f"Study is large ({total_activities:,} activities). "
                f"Consider a narrower date range, fewer collections, or enabling sampling "
                f"to keep the hub responsive."
            ),
        })

    if sparse_cells > 0 and total_cells > 0:
        issues.append({
            "severity": "warn",
            "code": "sparse_cells",
            "message": (
                f"{sparse_cells:,} of {total_cells:,} day \u00d7 collection cells have fewer than "
                f"{SPARSE_CELL_MIN_ACTIVITIES} activities. Sparse cells may distort analysis."
            ),
        })

    if sampling_report:
        n_excl = int(sampling_report.get('n_excluded_collections', 0) or 0)
        n_down = int(sampling_report.get('n_downsampled_collections', 0) or 0)
        min_cells = sampling_report.get('min_cells_per_collection')
        max_cells = sampling_report.get('max_cells_per_collection')
        if n_excl > 0:
            issues.append({
                "severity": "warn",
                "code": "collections_excluded",
                "message": (
                    f"Sampling excluded {n_excl:,} collection(s) with fewer than {min_cells} "
                    f"qualifying day \u00d7 collection cells."
                ),
            })
        if n_down > 0:
            issues.append({
                "severity": "warn",
                "code": "collections_downsampled",
                "message": (
                    f"Sampling downsampled {n_down:,} collection(s) that had more than {max_cells} "
                    f"qualifying day \u00d7 collection cells."
                ),
            })

    if not issues:
        issues.append({
            "severity": "ok",
            "code": "ok",
            "message": "Study design looks fine.",
        })

    return issues




def _calculate_stats(study_config, save_to_cache=True) -> tuple[dict, pd.DataFrame | None, pd.DataFrame | None]:
    """Calculate stats for a study using enrichment_status.parquet AND the study's specific recoded dataset.

    Returns:
        Tuple of (stats_dict, full_recoded_dataframe, enrichment_status_dataframe). The recoded
        DataFrame is None when no data exists; the enrichment-status DataFrame is None when no
        enrichment_status.parquet is present (or when returning before it is loaded).
    """

    empty_stats = {"total_activities": 0, "unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "activities_scraped": 0, "activities_annotated": 0, "unique_collections": 0}

    study_name = study_config.get("STUDY_NAME")
    if not study_name:
         return empty_stats, None, None

    # If no collections are selected, the study is empty — skip expensive computation
    selected = study_config.get("SELECTED_COLLECTIONS", [])
    if not selected:
         return empty_stats, None, None

    _t_total = _time.perf_counter()

    # 1. Load enrichment status once (used for both dataset creation and stats matching).
    # We previously tried backgrounding this load, but simple_sample_collection_events
    # reloaded the parquet for its diagnostic summary, defeating the parallelism and
    # causing a duplicate read. Serial load + pass-through is simpler and lets callees
    # reuse the DataFrame without a second GCS round-trip.
    _t_phase = _time.perf_counter()
    df_status = None
    if data_io.exists(storage_location="recoded", filename='enrichment_status.parquet'):
        df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
    _t_status = _time.perf_counter() - _t_phase

    # 2. Create the recoded dataset, passing enrichment_status to avoid reloading.
    print(f"Creating/updating recoded dataset for '{study_name}' to calculate stats...")
    _t_phase = _time.perf_counter()
    df_study = create_study_recoded_dataset(
        study_name=study_name, save_to_cache=save_to_cache,
        enrichment_status=df_status, verbose=False)
    _t_recode = _time.perf_counter() - _t_phase

    if df_study is None or df_study.empty:
        print(f"No data found for study '{study_name}'. Removing all cached files for this study.")
        data_io.remove(storage_location="cache", filename=f"{study_name}_recoded.parquet")
        data_io.remove(storage_location="cache", filename=f"{study_name}_explorer_metadata.json")
        data_io.remove(storage_location="cache", filename=f"{study_name}_comp_interpretations.json")
        data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")
        return empty_stats, None, df_status

    # 3. Count unique items. Filter to play/observe within each collection's
    # event window so the displayed "included" counts use the same definition
    # as the per-collection metadata (personas.total_events / active_days) and
    # the "potential" column on the right of the modal. Without this filter the
    # included Activities would include likes, shares, search, follow, and
    # events outside the persona window — making "included" exceed "potential".
    _t_phase = _time.perf_counter()
    df_counts = df_study
    if 'collection_id' in df_study.columns and 'local_timestamp' in df_study.columns:
        windows = _load_collection_event_windows(selected)
        df_counts = _filter_to_event_windows(df_counts, windows)
        df_counts = _filter_to_play_observe(df_counts)

    total_activities = len(df_counts)
    unique_collections = df_counts['collection_id'].nunique() if 'collection_id' in df_counts.columns else 0
    unique_videos = df_counts['item_id'].nunique() if 'item_id' in df_counts.columns else 0
    active_days = int(pd.to_datetime(df_counts['local_timestamp'], errors='coerce').dropna().dt.date.nunique()) if 'local_timestamp' in df_counts.columns else 0

    # 4. Match against enrichment status for scrape/annotation counts
    scraped_videos = 0
    annotated_videos = 0
    # Activity-level included counts by the enrichment status of each activity's video,
    # so the mosaic can label the included band per category (annotated / scraped / not).
    activities_scraped = 0
    activities_annotated = 0

    if df_status is not None and not df_status.empty:
        # Robust alignment: Ensure item_id is a column and use PyArrow strings
        if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
            df_status = df_status.reset_index()

        if 'item_id' in df_status.columns:
            try:
                status_ids = df_status['item_id'].astype("string[pyarrow]")
                study_ids = df_counts['item_id'].astype("string[pyarrow]")
                matched_status = df_status.loc[status_ids.isin(study_ids)].copy()
            except Exception as e:
                print(f"Error during robust index matching: {e}. Falling back to standard matching.")
                study_item_ids = df_counts['item_id'].unique()
                matched_status = df_status.loc[df_status.index.isin(study_item_ids)].copy()
        else:
            study_item_ids = df_counts['item_id'].unique()
            matched_status = df_status.loc[df_status.index.isin(study_item_ids)].copy()

        if 'item_id' not in matched_status.columns and matched_status.index.name == 'item_id':
            matched_status = matched_status.reset_index()

        if 'scraped_ok' in matched_status.columns:
            scraped_videos = int(matched_status['scraped_ok'].fillna(False).sum())
        if 'annotated_ok' in matched_status.columns:
            annotated_videos = int(matched_status['annotated_ok'].fillna(False).sum())

        if 'item_id' in matched_status.columns and 'item_id' in df_counts.columns:
            m_ids = matched_status['item_id'].astype(str)
            study_ids_str = df_counts['item_id'].astype(str)
            if 'scraped_ok' in matched_status.columns:
                scraped_set = set(m_ids[matched_status['scraped_ok'].fillna(False).to_numpy()])
                activities_scraped = int(study_ids_str.isin(scraped_set).sum())
            if 'annotated_ok' in matched_status.columns:
                annotated_set = set(m_ids[matched_status['annotated_ok'].fillna(False).to_numpy()])
                activities_annotated = int(study_ids_str.isin(annotated_set).sum())

    stats = {
        "total_activities": int(total_activities),
        "unique_videos": int(unique_videos),
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "activities_scraped": activities_scraped,
        "activities_annotated": activities_annotated,
        "unique_collections": int(unique_collections),
        "active_days": active_days,
    }

    _t_count = _time.perf_counter() - _t_phase
    _t_stats_total = _time.perf_counter() - _t_total
    print(
        f"[STATS][TIMING] study={study_name} "
        f"status_load={_t_status:.2f}s recode={_t_recode:.2f}s "
        f"count={_t_count:.2f}s total={_t_stats_total:.2f}s"
    )

    return stats, df_study, df_status






def _estimate_from_prepared(frame: pd.DataFrame | None, study_config: dict) -> tuple[dict, list, int, int, dict | None]:
    """Approximate the study sampling counts from a prepared preview frame.

    Operates purely in memory on the cached, preprocessed frame (see
    _prepare_preview_frame): applies the date window and sample-frame filter via
    precomputed columns, then replays the two-stage sampler on the per-(collection, day)
    cell histogram. No I/O and no per-row re-derivation, so it is cheap to call
    repeatedly as the user tweaks the date range or sampling thresholds.

    The gating quantities — total activities, unique collections, and the
    excluded/downsampled collection counts — are reproduced exactly. Per-item counts
    (unique videos and the scrape/annotation breakdown) are unbiased estimates because
    the specific rows the random sampler would keep are not materialised.

    Args:
        frame: Prepared preview frame, or None.
        study_config: The study definition (date range, SAMPLE_FRAME, thresholds).

    Returns:
        Tuple (stats, included_per_day, sparse_cells, total_cells, sampling_report).
    """

    empty = {
        "total_activities": 0, "unique_videos": 0, "scraped_videos": 0,
        "annotated_videos": 0, "activities_scraped": 0, "activities_annotated": 0,
        "unique_collections": 0, "active_days": 0,
    }

    if frame is None or frame.empty:
        return empty, [], 0, 0, None

    def _parse_date(value, default: date) -> date:
        if isinstance(value, str):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return default
        return default

    start_date = _parse_date(study_config.get("START_DATE"), date(1970, 1, 1))
    end_date = _parse_date(study_config.get("END_DATE"), date(2099, 12, 31))
    end_bound = datetime.combine(end_date + timedelta(days=1), time.min)

    mask = (frame["_ts"] >= pd.Timestamp(start_date)) & (frame["_ts"] < pd.Timestamp(end_bound))
    df = frame[mask.to_numpy()]
    if df.empty:
        return empty, [], 0, 0, None

    frame_setting = (study_config.get("SAMPLE_FRAME") or "off").strip()
    if frame_setting == "scraped":
        df = df[df["_scraped"].to_numpy()]
    elif frame_setting == "annotated":
        df = df[df["_annotated"].to_numpy()]
    if df.empty:
        return dict(empty), [], 0, 0, None

    # A blank max ('' / '-') means "no cap" → SAMPLE_NO_CAP, matching the real sampler.
    min_events = parse_sample_threshold(study_config.get("MIN_ACTIVITY_COUNT_PER_GROUP"), 30)
    max_events = parse_sample_threshold(study_config.get("MAX_ACTIVITY_COUNT_PER_GROUP"), 50, uncapped=True)
    min_cells = parse_sample_threshold(study_config.get("MIN_GROUP_COUNT_PER_COLLECTION"), 20)
    max_cells = parse_sample_threshold(study_config.get("MAX_GROUP_COUNT_PER_COLLECTION"), 200, uncapped=True)

    sampling_report = None

    if frame_setting == "off":
        # No sampling: every play/observe row in range is kept.
        capped = df
    else:
        # Replay the two-stage sampler on this light frame (no scrape/annotation merge).
        # Stage 1: drop (collection, day) cells with < min_events rows; the survivors
        # are the qualifying cells. Random within-cell capping happens after Stage 2.
        # collection_id is a category; every groupby below passes observed=True so pandas
        # does NOT materialise the full category × day cartesian product (which would
        # invent empty cells and wreck the cell counts / sparse-cell warning).
        df = df.assign(_cell_n=df.groupby(["collection_id", "_ld"], observed=True)["item_id"].transform("size"))
        qf = df[df["_cell_n"] >= min_events]
        if qf.empty:
            sampling_report = {
                "n_excluded_collections": int(df["collection_id"].nunique()),
                "n_downsampled_collections": 0,
                "min_cells_per_collection": min_cells,
                "max_cells_per_collection": max_cells,
            }
            return dict(empty), [], 0, 0, sampling_report

        cells = qf[["collection_id", "_ld"]].drop_duplicates()
        cells_per_coll = cells.groupby("collection_id", observed=True).size()
        sampling_report = {
            "n_excluded_collections": int((cells_per_coll < min_cells).sum()),
            "n_downsampled_collections": int((cells_per_coll > max_cells).sum()),
            "min_cells_per_collection": min_cells,
            "max_cells_per_collection": max_cells,
        }

        # Stage 2: drop collections with < min_cells qualifying cells; for the rest,
        # keep at most max_cells cells, chosen at random (seeded) to stay unbiased.
        # Skip the random selection entirely when uncapped (keep every qualifying cell).
        kept_colls = set(cells_per_coll[cells_per_coll >= min_cells].index)
        cells = cells[cells["collection_id"].isin(kept_colls)].copy()
        rng = np.random.RandomState(42)
        if max_cells < SAMPLE_NO_CAP:
            cells["_r"] = rng.random(len(cells))
            cells["_rank"] = cells.groupby("collection_id", observed=True)["_r"].rank(method="first")
            cells = cells[cells["_rank"] <= max_cells]
        cells = cells[["collection_id", "_ld"]]

        qf = qf.merge(cells, on=["collection_id", "_ld"], how="inner")

        # Stage 1 cap: keep at most max_events rows per surviving cell, at random.
        # Skip entirely when uncapped (keep every row in the surviving cells).
        if max_events < SAMPLE_NO_CAP:
            qf = qf.assign(_r2=rng.random(len(qf)))
            qf["_row_rank"] = qf.groupby(["collection_id", "_ld"], observed=True)["_r2"].rank(method="first")
            capped = qf[qf["_row_rank"] <= max_events]
        else:
            capped = qf

    if capped.empty:
        return dict(empty), [], 0, 0, sampling_report

    # All counts come straight off the materialised approximate sample, so activity-level
    # and item-level figures are mutually consistent (no scaling fudge). The scrape /
    # annotation flags were precomputed once on the prepared frame.
    item_ids = capped["item_id"]
    is_scraped = capped["_scraped"].to_numpy()
    is_annotated = capped["_annotated"].to_numpy()

    stats = {
        "total_activities": int(len(capped)),
        "unique_videos": int(item_ids.nunique()),
        "scraped_videos": int(item_ids[is_scraped].nunique()),
        "annotated_videos": int(item_ids[is_annotated].nunique()),
        "activities_scraped": int(is_scraped.sum()),
        "activities_annotated": int(is_annotated.sum()),
        "unique_collections": int(capped["collection_id"].nunique()),
        "active_days": int(capped["_ld"].nunique()),
    }

    day_counts = capped.groupby("_ld").size()
    included_per_day = [
        {"date": d.date().isoformat(), "count": int(c)}
        for d, c in day_counts.sort_index().items()
    ]

    cells_final = capped.groupby(["collection_id", "_ld"], observed=True).size()
    total_cells = int(cells_final.size)
    sparse_cells = int((cells_final < SPARSE_CELL_MIN_ACTIVITIES).sum())

    return stats, included_per_day, sparse_cells, total_cells, sampling_report




def _universe_from_prepared(frame: pd.DataFrame | None, study_config: dict) -> tuple[int, int, dict, bool]:
    """Compute the pre-sampling potentials and universe mosaic from the prepared frame.

    Mirrors the previous _load_study_raw_window + _compute_universe_enrichment pair, but
    reuses the cached frame's precomputed event-window and scrape/annotation flags so a
    repeated check does no I/O and no per-row re-derivation.

    Args:
        frame: Prepared preview frame, or None.
        study_config: The study definition (date range).

    Returns:
        Tuple (potential_activities, potential_active_days, universe, has_data) where
        universe has integer keys activities / scraped / annotated (date-filtered, activity
        level) and has_data flags whether any in-window activity exists.
    """

    universe = {"activities": 0, "scraped": 0, "annotated": 0}
    if frame is None or frame.empty:
        return 0, 0, universe, False

    win = frame[frame["_in_window"].to_numpy()]
    if win.empty:
        return 0, 0, universe, False

    potential_activities = int(len(win))
    potential_active_days = int(win["_ld"].nunique())

    start_s = (study_config.get("START_DATE") or "").strip()
    end_s = (study_config.get("END_DATE") or "").strip()
    uni = win
    if start_s or end_s:
        day = win["_ts"].dt.normalize()
        m = win["_ts"].notna()
        if start_s:
            m &= day >= pd.Timestamp(start_s)
        if end_s:
            m &= day <= pd.Timestamp(end_s)
        uni = win[m.to_numpy()]

    universe = {
        "activities": int(len(uni)),
        "scraped": int(uni["_scraped"].sum()),
        "annotated": int(uni["_annotated"].sum()),
    }
    return potential_activities, potential_active_days, universe, True




@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
@permission_required('tab.data_management.studies', 'tab.my_studies')
def list_studies():
    # Always reload from disk/GCS to pick up changes made by the task-runner service
    init_study_defs()

    studies = fyp_cf['study_defs']

    studies_list = []

    # Visibility:
    #   - Admin / users with the Define Studies sub-page permission are "study
    #     managers" — they see every study regardless of per-study USER_ACCESS.
    #   - Users who only have the My Studies tab see the curated subset: studies
    #     where their role appears in USER_ACCESS (or USER_ACCESS contains "all").
    from web_interface.permissions import user_has_permission
    is_manager = (
        current_user.is_admin()
        or user_has_permission(current_user, 'tab.data_management.studies')
    )

    for name, config in studies.items():
        config['STUDY_NAME'] = name

        if is_manager:
            studies_list.append(config)
        else:
            user_access = config.get("USER_ACCESS", [])
            if isinstance(user_access, list) and (
                current_user.role in user_access or 'all' in user_access
            ):
                studies_list.append(config)

    return jsonify(studies_list)







@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def save_study():
    global fyp_cf

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    
    # load studies from disk into memory - overwrite whatever was there previously
    init_study_defs()
    studies = fyp_cf['study_defs'].copy()

    # If updating an existing study, check for actual changes
    if study_name in studies:
        existing_config = studies[study_name]

        # Compare incoming data with existing config
        changed_keys = []
        for key, value in data.copy().items(): # Use copy to safely iterate
            # key might be REFRESH_PCA/REFRESH_METADATA - these shouldn't count as study def changes but separate flags.
            if key in ['REFRESH_PCA', 'REFRESH_METADATA', 'stats']:
                continue

            if key not in existing_config or existing_config[key] != value:
                changed_keys.append(key)
                #print(f"Change detected in {key}: {existing_config.get(key)} -> {value}") # Debug

        # Note: we deliberately do NOT short-circuit when changed_keys is empty.
        # The study's cached artifacts depend on collection/scrape/annotation
        # parquets that can change out-of-band (e.g. new activities ingested
        # into an existing collection_id). run_study_refresh fingerprints those
        # inputs via the sidecar and short-circuits itself when they really are
        # unchanged, so an unnecessary save here becomes a cheap no-op — and a
        # necessary one actually rebuilds.

        # If only USER_ACCESS changed, we don't need to recalculate anything
        if len(changed_keys) == 1 and changed_keys[0] == 'USER_ACCESS':
            print(f"Only USER_ACCESS changed for {study_name}. Saving without recalculation.")
            studies[study_name]['USER_ACCESS'] = data['USER_ACCESS']
            fyp_cf['study_defs'] = studies
            save_study_defs()
            activity_log.record(
                actor=_actor(),
                category=activity_log.CATEGORY_DATA_MANAGEMENT,
                action="study.save",
                target=study_name,
                details={"changed": ["USER_ACCESS"]},
            )
            return jsonify({"status": "success", "study": studies[study_name]})

    # Update config
    if study_name not in studies:
        studies[study_name] = {}

    # Preserve existing stats and last_updated — form data doesn't include these
    # and the Cloud Task will recalculate them asynchronously.
    existing_stats = studies[study_name].get('stats')
    existing_last_updated = studies[study_name].get('last_updated')

    studies[study_name].update(data)

    if existing_stats and 'stats' not in data:
        studies[study_name]['stats'] = existing_stats
    if existing_last_updated and 'last_updated' not in data:
        studies[study_name]['last_updated'] = existing_last_updated
    
    # Extract ephemeral flags (don't save to disk)
    refresh_pca_flag = data.pop('REFRESH_PCA', True)
    refresh_meta_flag = data.pop('REFRESH_METADATA', True)
    
    # Also clean them from the study object in memory just in case 'update' put them there
    # (Since 'data' was passed to update, they ARE in studies[study_name] now)
    studies[study_name].pop('REFRESH_PCA', None)
    studies[study_name].pop('REFRESH_METADATA', None)

    # Invalidate the cached daily-activities snapshot — the saved definition
    # may now have different collections, so the cache would mislead the
    # modal on reopen until a fresh /daily_activities call refreshes it.
    studies[study_name].pop('cached_daily_activities', None)

    # Update timestamp and save definition to disk
    studies[study_name]['last_updated'] = datetime.now(UTC).isoformat()
    fyp_cf['study_defs'] = studies
    save_study_defs()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="study.save",
        target=study_name,
        details={"definition_only": bool(data.get("definition_only"))},
    )

    # Definition-only save: skip heavy refresh (used by "Check data counts" for new studies)
    if data.get("definition_only"):
        return jsonify({"status": "success", "study": studies[study_name]})

    # --- Dispatch heavy refresh work ---
    task_args = {
        "study_name": study_name,
        "refresh_pca": refresh_pca_flag,
        "refresh_metadata": refresh_meta_flag,
    }

    if is_cloud_run():
        # On Cloud Run: dispatch as a Cloud Task and return immediately
        success, msg = start_process("study_refresh", None, task_args=task_args)
        if success:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatched",
                "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
            })
        else:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatch_failed",
                "message": f"Study saved, but background refresh failed to start: {msg}",
            })
    else:
        # Local dev: dispatch to a background thread so the HTTP response can
        # return immediately. The client's `_pollStudyRefresh` will watch the
        # in-process status via `/api/status/study_refresh/<name>`.
        import threading as _threading

        from web_interface.run_study_refresh import run_study_refresh
        from web_interface.task_status import LocalThreadStatusReporter

        status_key = f"study_refresh__{study_name}"
        reporter = LocalThreadStatusReporter(status_key)

        def _run_in_thread():
            try:
                run_study_refresh(reporter=reporter, task_args=task_args)
                reporter.complete()
            except Exception as e:
                print(f"Study refresh failed: {e}")
                reporter.fail(str(e))

        _threading.Thread(target=_run_in_thread, daemon=True, name=status_key).start()

        return jsonify({
            "status": "success",
            "study": studies[study_name],
            "refresh_status": "dispatched",
            "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
        })






@management_bp.route('/api/manage/studies/calculate_stats', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    global fyp_cf
    
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    # Live previews (auto-update on every slider/date tweak) pass PREVIEW_ONLY so we
    # compute-and-return without persisting: no global-config mutation (which is not
    # thread-safe under rapid concurrent calls) and no save_study_defs() write per
    # tweak. The saved stats are refreshed on an explicit Save (run_study_refresh).
    preview_only = bool(data.get("PREVIEW_ONLY"))

    # 1. Backup + install config only when we intend to persist.
    original_config = None
    if not preview_only:
        if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
            original_config = fyp_cf['study_defs'][study_name].copy()
        # If this is a new study (not in defs), we add it. If existing, we overwrite.
        fyp_cf['study_defs'][study_name] = data

    stats_to_persist: dict | None = None
    try:
        # Fast preview path: approximate the sampling counts analytically instead of
        # running the full create_study_recoded_dataset (scrape/annotation merge +
        # random sample). The persisted study build still uses _calculate_stats via
        # run_study_refresh; only this on-demand check uses the heuristic.
        #
        # A preprocessed frame (keyed by the collection set) is cached in process, so the
        # tweak-and-recheck loop (changing only the date range or sampling thresholds)
        # reuses it with no I/O and no per-row recomputation — just masks and a groupby.
        selected = data.get("SELECTED_COLLECTIONS") or []
        df_status = _get_enrichment_status_cached()
        frame = _get_prepared_frame_cached(selected, df_status)

        stats, included_per_day, sparse_cells, total_cells, sampling_report = _estimate_from_prepared(frame, data)
        stats_to_persist = stats

        # Pre-sampling potentials + universe mosaic, both derived from the same frame.
        #   items.potential     = activities in study (how many activities map to items)
        #   scraped.potential    = items in study
        #   annotated.potential  = scraped items in study
        pot_activities, pot_active_days, universe, has_total_days = _universe_from_prepared(frame, data)
        potentials = {
            "collections": len(selected),
            "activities": pot_activities,
            "active_days": pot_active_days,
            "items": int(stats.get("total_activities", 0)),
            "scraped": int(stats.get("unique_videos", 0)),
            "annotated": int(stats.get("scraped_videos", 0)),
        }

        if isinstance(stats, dict):
            stats["universe"] = universe

        issues = _derive_study_issues(stats, sparse_cells, total_cells, has_total_days, sampling_report)

        return jsonify({
            "status": "success",
            "stats": stats,
            "potentials": potentials,
            "universe": universe,
            "included_per_day": included_per_day,
            "issues": issues,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 4. Revert config + persist fresh stats (skipped entirely for live previews,
        # which never touched the global config). Persisting seeds the mosaic on reopen.
        if not preview_only:
            if original_config is not None:
                 if stats_to_persist:
                      original_config['stats'] = stats_to_persist
                 fyp_cf['study_defs'][study_name] = original_config
                 if stats_to_persist:
                      try:
                           save_study_defs()
                      except Exception as _save_err:
                           print(f"[calculate_study_stats] non-fatal: failed to persist stats for '{study_name}': {_save_err}")
            else:
                 # If it was new, remove it (it wasn't there before).
                 if study_name in fyp_cf['study_defs']:
                      del fyp_cf['study_defs'][study_name]




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




@management_bp.route('/api/manage/studies/prewarm_check', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def prewarm_study_check():
    """Warm the preview frame for a collection set ahead of the first 'Check' press.

    The modal calls this on open and whenever the collection selection changes, so the
    (possibly slow) build / disk-load happens during the user's think-time instead of on
    the first button press. Returns immediately; the work runs in a background thread.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    if not selected:
        return jsonify({"status": "noop"}), 200

    key = _preview_frame_key(selected)
    with _preview_cache_lock:
        warm = _preview_frame_cache.get(key)
        ready = warm is not None and (_time.monotonic() - warm[0]) < _PREVIEW_CACHE_TTL_S
        in_flight = key in _preview_warming
    if ready:
        return jsonify({"status": "ready"}), 200
    if in_flight:
        return jsonify({"status": "warming"}), 202

    threading.Thread(target=_prewarm_preview_frame, args=(list(selected),), daemon=True, name="prewarm_check").start()
    return jsonify({"status": "warming"}), 202





def _collections_hash(selected: list) -> str:
    """Return a short stable hash of a selected-collections list."""

    import hashlib
    ids = sorted(str(x) for x in (selected or []))
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16]




@management_bp.route('/api/manage/studies/daily_activities', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def daily_activities():
    """Return activities-per-day across a set of collections for the modal chart.

    Lightweight: reads only `collection_id` + `local_timestamp` columns from
    `collections_recoded.parquet` with a pushdown filter on the selected IDs.
    No date-range filter — the chart shows the full span so the user can pick
    a window visually.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    study_name = data.get("STUDY_NAME")

    if not selected:
        return jsonify({"status": "success", "total_per_day": []})

    filename = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return jsonify({"status": "success", "total_per_day": []})

    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=["collection_id", "local_timestamp", "activity_type"],
            filters=[("collection_id", "in", selected)],
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    windows = _load_collection_event_windows(selected)
    df = _filter_to_event_windows(df, windows)
    df = _filter_to_play_observe(df)

    potentials = {
        "collections": len(selected),
        "activities": 0,
        "active_days": 0,
    }
    if df is not None and not df.empty:
        potentials["activities"] = int(len(df))
        potentials["active_days"] = int(pd.to_datetime(df["local_timestamp"], errors="coerce").dropna().dt.date.nunique())

    total_per_day = _daily_counts(df)
    collections_hash = _collections_hash(selected)

    # Cache on the saved study so subsequent modal opens render the chart
    # instantly. Only persist when the incoming selection matches the saved
    # SELECTED_COLLECTIONS — otherwise the user is editing an unsaved state
    # and caching that would mislead on reopen.
    if study_name:
        saved = fyp_cf.get('study_defs', {}).get(study_name)
        if isinstance(saved, dict):
            saved_hash = _collections_hash(saved.get('SELECTED_COLLECTIONS'))
            if saved_hash == collections_hash:
                saved['cached_daily_activities'] = {
                    "total_per_day": total_per_day,
                    "potentials": potentials,
                    "collections_hash": collections_hash,
                    "computed_at": datetime.now(UTC).isoformat(),
                }
                try:
                    save_study_defs()
                except Exception as _save_err:
                    print(f"[daily_activities] non-fatal: failed to persist cache for '{study_name}': {_save_err}")

    return jsonify({
        "status": "success",
        "total_per_day": total_per_day,
        "potentials": potentials,
        "collections_hash": collections_hash,
    })




@management_bp.route('/api/manage/studies/delete', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def delete_study():
    global fyp_cf
    data = request.json
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    init_study_defs()
    if 'study_defs' not in fyp_cf:
        return jsonify({"error": "No study defs found"}), 404
        
    if study_name in fyp_cf['study_defs']:
        del fyp_cf['study_defs'][study_name]
        save_study_defs()

        for cached_file in [
            f"{study_name}_recoded.parquet",
            f"{study_name}_explorer_metadata.json",
            f"{study_name}_comp_interpretations.json",
            f"{study_name}_PCA.parquet",
        ]:
            data_io.remove(storage_location="cache", filename=cached_file)

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="study.delete",
            target=study_name,
        )
        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404




def _find_raw_file_locations(raw_files: list[str]) -> list[tuple[str, str]]:
    """Return [(storage_location, filename), ...] for each raw file that still
    exists in any of the registered upload locations.

    The location list is derived from the collection-class registry
    (fyp.ingest.registered_raw_locations), so a new platform's upload location
    is probed automatically. Probes each location's ingestion_manifest.json
    first (fast path) and falls back to data_io.exists when the manifest is
    missing or out of sync. Files not found in any location are silently
    skipped — they were already moved or deleted previously.
    """
    found: list[tuple[str, str]] = []
    raw_files_set = set(raw_files)
    if not raw_files_set:
        return found

    upload_locations = registered_raw_locations()
    manifests: dict[str, dict] = {}
    for loc in upload_locations:
        if data_io.exists(storage_location=loc, filename="ingestion_manifest.json"):
            manifests[loc] = data_io.load_json(
                storage_location=loc, filename="ingestion_manifest.json", verbose=False
            ) or {}
        else:
            manifests[loc] = {}

    for fn in raw_files_set:
        for loc in upload_locations:
            if fn in manifests[loc] or data_io.exists(storage_location=loc, filename=fn):
                found.append((loc, fn))
                break

    return found




def _affected_studies_for_collection(collection_id: str) -> list[str]:
    """Return the names of studies whose SELECTED_COLLECTIONS contains collection_id."""
    init_study_defs()
    out: list[str] = []
    for sname, sdef in (fyp_cf.get('study_defs') or {}).items():
        sel = sdef.get('SELECTED_COLLECTIONS') or []
        if collection_id in sel:
            out.append(sname)
    return out




@management_bp.route('/api/manage/collections/affected_studies', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def affected_studies_for_collection():
    """Return the studies that reference a given collection_id. Used by the
    delete-collection confirmation dialog to show what will be refreshed."""
    collection_id = (request.args.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400
    return jsonify({"studies": _affected_studies_for_collection(collection_id)})




@management_bp.route('/api/manage/collections/delete', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def delete_collection():
    """Dispatch a collection_delete Cloud Task. The actual delete (which loads
    and rewrites the 1+ GB collections_recoded.parquet) runs on the task-runner
    so the data-hub doesn't risk OOM or timeout. The UI polls /api/status for
    completion and reads the final result from the task's emitted data payload.
    """
    data = request.json or {}
    collection_id = (data.get("collection_id") or "").strip()
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    from fyp.fyp_config import COLLECTION_DELETE_SCRIPT

    success, msg = start_process(
        "collection_delete",
        COLLECTION_DELETE_SCRIPT,
        task_args={"collection_id": collection_id},
    )
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.delete",
            target=collection_id,
        )
        return jsonify({
            "status": "started",
            "collection_id": collection_id,
            "message": msg,
        })
    return jsonify({"status": "error", "message": msg}), 409







@management_bp.route('/api/manage/collections', methods=['GET'])
@permission_required('tab.data_management.edit_collections')
@login_required
def list_collections():

    if True:#try:
        # Load ddp_metadata from storage
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            df = data_io.load_parquet(
                storage_location="recoded", 
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet", 
                verbose=False,
            )
            
            # Filter for accepted collections
            if ('other', 'accepted') in df.columns:
                df = df[df[('other', 'accepted')]]
                
            # Load annotations
            annotations = {}
            if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
                annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")
                
            # Construct structured dictionaries
            collections = []
            
            # Make sure we don't have pd.NA or similar incompatible types for JSON serialization
            df = df.where(pd.notnull(df), None)
            
            # Helper to convert pandas/pyarrow types cleanly to standard Python types
            def safe_val(val):
                if pd.isna(val) or val is None:
                    return None
                if hasattr(val, "item"):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                if hasattr(val, "isoformat"):
                    return val.isoformat()
                return val

            for index, row in df.iterrows():
                # Use collection_id column if available, otherwise fall back to index
                if 'collection_id' in df.columns:
                    row_id = str(row['collection_id'])
                else:
                    row_id = str(index)
                item = {
                    "id": row_id,
                    "participants": {},
                    "other": {},
                    "personas": {}
                }
                
                # Fetch participant info
                for c in df.columns:
                    if c[0] == 'participants':
                        item['participants'][c[1]] = safe_val(row[c])
                    elif c[0] == 'other':
                        item['other'][c[1]] = safe_val(row[c])
                    elif c[0] == 'personas':
                        item['personas'][c[1]] = safe_val(row[c])
                        
                # Attach annotations (keyed by collection ID, not row index)
                ann = annotations.get(row_id, {})
                item['displayId'] = ann.get('display_collection_id', None)
                item['tags'] = ann.get('annotation_tags', [])
                item['hidden'] = ann.get('hidden', False)

                collections.append(item)

            return jsonify(collections)
        else:
            print(f"{COLLECTIONS_LABEL}_metadata.parquet not found")
            return jsonify([])


@management_bp.route('/api/manage/collection/save_annotation', methods=['POST'])
@permission_required('tab.data_management.edit_collections')
@login_required
def save_collection_annotation():
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    collection_id = data.get('collection_id')
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    try:
        annotations = {}
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
            annotations = data_io.load_json(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json")

        annotations[str(collection_id)] = {
            "display_collection_id": data.get('display_collection_id', None),
            "annotation_tags": data.get('tags', []),
            "hidden": data.get('hidden', False)
        }

        data_io.save_json(
            data=annotations,
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        )
        invalidate_collection_tags_cache()

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="collection.annotation.save",
            target=str(collection_id),
            details={
                "tags": data.get('tags', []),
                "hidden": bool(data.get('hidden', False)),
            },
        )
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@permission_required('tab.data_management.enrichment')
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    # Reload process_stats from GCS so we pick up task-runner writes, and
    # drop any consolidation_impact that has already been fully resolved by
    # downstream refreshes — otherwise the impact panel lingers forever when
    # the UI never happens to call /api/manage/refresh/staleness.
    _evaluate_consolidation_staleness()

    # 1. Load Enrichment Status
    enrichment_status = None
    if data_io.exists(storage_location="recoded", filename='enrichment_status.parquet'):
        enrichment_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')

    total_videos = 0
    scraped_videos = 0
    annotated_videos = 0
    unique_collections = 0

    if enrichment_status is not None and not enrichment_status.empty:
        total_videos = len(enrichment_status)
        if 'scraped_ok' in enrichment_status.columns:
            scraped_videos = int(enrichment_status['scraped_ok'].sum())
        if 'annotated_ok' in enrichment_status.columns:
            annotated_videos = int(enrichment_status['annotated_ok'].sum())

    ddp_metadata = None
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
        ddp_metadata = data_io.load_parquet(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
    if ddp_metadata is not None and not ddp_metadata.empty:
        if ('other', 'accepted') in ddp_metadata.columns:
            unique_collections = int(ddp_metadata[ddp_metadata[('other','accepted')]].index.nunique())
        else:
            unique_collections = int(ddp_metadata.index.nunique())
        
    
    # 2. Get Queue Lengths (per-platform scrape queues + their total)
    scrape_queues_by_platform: dict[str, int] = {}
    annotate_queue_len = 0

    try:
        scrape_queues_by_platform = scrape_queues.queue_lengths()
    except Exception:
        pass
    scrape_queue_len = sum(scrape_queues_by_platform.values())

    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)
        
    # Backstop: resolve a forked fan-out (meta‖pca‖timelines) whose dropped leaf
    # left it un-finalized. The event-driven barrier may miss this if every
    # surviving leaf finished before the grace window; this poll-driven call
    # flips a never-started leaf to "failed" and finalizes once grace passes.
    # No-op when no fan-out is active; Cloud Run only (local mode never forks).
    if is_cloud_run():
        try:
            from .process_routes import resolve_forked_pipeline
            resolve_forked_pipeline()
        except Exception as e:
            print(f"[status] resolve_forked_pipeline failed: {e}")

    consolidate_entry = process_stats.get("consolidate_enrichment", {})

    # Is any consolidate-pipeline step currently running? Used by the UI to
    # pick up live stage progress after a page reload mid-pipeline. The
    # pipeline_in_flight flag covers the brief gap between one step completing
    # and the next step booting up (when no step is technically "running").
    pipeline_step_names = ["consolidate_enrichment"] + PIPELINE_STEPS_ORDER
    any_step_running = any(_is_worker_running(n) for n in pipeline_step_names)
    flag_in_flight = bool(consolidate_entry.get("pipeline_in_flight"))

    # Stale-flag cleanup: a server restart mid-pipeline leaves the flag set
    # with no orchestrator thread to clear it. If the flag is on but nothing
    # is running AND the consolidate step completed >60s ago (longer than
    # any plausible inter-step gap), treat the pipeline as abandoned and
    # clear the flag so the UI stops showing "in flight" forever.
    if flag_in_flight and not any_step_running:
        last_end = consolidate_entry.get("last_run_end_time")
        stale = False
        if last_end:
            try:
                end_dt = datetime.fromisoformat(last_end)
                if (datetime.now(UTC) - end_dt).total_seconds() > 60:
                    stale = True
            except (ValueError, TypeError):
                stale = True
        else:
            stale = True
        if stale:
            consolidate_entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = consolidate_entry
            save_process_stats()
            flag_in_flight = False

    pipeline_active = flag_in_flight or any_step_running

    return jsonify({
        "total_videos": total_videos,
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_collections": unique_collections,
        "scrape_queue_len": scrape_queue_len,
        "scrape_queues": scrape_queues_by_platform,
        "annotate_queue_len": annotate_queue_len,
        "consolidate_stats": {
            **consolidate_entry,
            **processes.get("consolidate_enrichment", {}).get("data", {})
        } or None,
        "consolidate_auto_armed": bool(consolidate_entry.get("auto_armed")),
        "consolidate_auto_armed_auto_refresh": bool(consolidate_entry.get("auto_armed_auto_refresh")),
        "consolidate_pipeline_active": pipeline_active,
        "pipeline_steps": _build_pipeline_step_view(pipeline_active),
        "last_pipeline_partial": bool(consolidate_entry.get("last_pipeline_partial")),
        "last_pipeline_failed_at": consolidate_entry.get("last_pipeline_failed_at"),
        "workers_blocking_consolidate": _workers_blocking_consolidate(),
        "scraper_last_success": max(
            (
                process_stats.get(f"queue_scraper_{p}", {}).get("last_success")
                or process_stats.get("queue_scraper", {}).get("last_success")
                or ""
                for p in scrape_queues_by_platform or ["tiktok"]
            ),
            default=None,
        ) or None,
        "annotator_last_success": process_stats.get("queue_annotator", {}).get("last_success"),
    })






@management_bp.route('/api/manage/enrichment/empty_queue/<queue_type>', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def empty_enrichment_queue(queue_type):
    try:
        if queue_type == "scrape":
            # Optional {"platform": ...} in the body targets one platform's
            # queue; default empties every registered platform's queue.
            body = request.get_json(silent=True) or {}
            requested = body.get("platform")
            targets = [requested] if requested else scrape_queues.registered_platforms()
            for platform in targets:
                scrape_queues.remove_scrape_queue(platform)
            load_process_stats()
            stats_changed = False
            for platform in targets:
                entry = process_stats.get(f"queue_scraper_{platform}", {})
                if "scrape_queue_len" in entry:
                    entry["scrape_queue_len"] = 0
                    stats_changed = True
            # Legacy pre-rename entry, harmless to zero alongside.
            if "scrape_queue_len" in process_stats.get("queue_scraper", {}):
                process_stats["queue_scraper"]["scrape_queue_len"] = 0
                stats_changed = True
            if stats_changed:
                save_process_stats()
        elif queue_type == "annotate":
            if data_io.exists(storage_location='cache', filename='to_annotate.json'):
                data_io.remove(storage_location='cache', filename='to_annotate.json')
            load_process_stats()
            if "annotate_queue_len" in process_stats.get("queue_annotator", {}):
                process_stats["queue_annotator"]["annotate_queue_len"] = 0
                save_process_stats()
        else:
            return jsonify({"error": "Invalid queue type"}), 400

        return jsonify({"status": "success", "message": f"{queue_type.capitalize()} queue emptied."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/queue_voted', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def queue_voted_videos():
    try:
        from web_interface.security import user_manager
        
        # 1. Gather all votes across all users
        all_votes = {} # dict of collection_id -> set of periods
        for user in user_manager.get_all_users().values():
            if not user.machine_annotation_votes:
                continue
            for coll_id, periods in user.machine_annotation_votes.items():
                if coll_id not in all_votes:
                    all_votes[coll_id] = set()
                all_votes[coll_id].update(periods)
                
        if not all_votes:
            return jsonify({"status": "no_votes", "message": "No votes found for machine annotation."})

        # 2. Map periods to item_ids 
        import pandas as pd

        from fyp.organize_datasets import create_collection_unified_dataset
        target_item_ids = set()
        
        for coll_id, periods in all_votes.items():
            try:
                # Need to load using standard DDP logic since timeline cache aggregates and removes item_id
                df_collection = create_collection_unified_dataset(collection_id=coll_id, verbose=False)
                
                if df_collection is not None and not df_collection.empty and 'item_id' in df_collection.columns and 'local_date' in df_collection.columns:
                    # Time periods can be 'YYYY-MM-DD' or 'YYYY-Wxx' or 'YYYY-MM'
                    ts_series = pd.to_datetime(df_collection['local_date'], errors='coerce')
                    
                    for p in periods:
                        # yyyy-mm-dd
                        if len(p) == 10 and p.count('-') == 2:
                            match_mask = ts_series.dt.strftime('%Y-%m-%d') == p
                        # yyyy-mm
                        elif len(p) == 7 and p.count('-') == 1:
                            match_mask = ts_series.dt.strftime('%Y-%m') == p
                        # yyyy-Wxx
                        elif 'W' in p:
                            # pandas isocalendar week
                            def format_week(dt):
                                if pd.isna(dt): return ""
                                iso = dt.isocalendar()
                                return f"{iso.year}-W{iso.week:02d}"
                            match_mask = ts_series.apply(format_week) == p
                        else:
                            continue # Unknown format
                        
                        hits = df_collection.loc[match_mask, 'item_id'].dropna().unique().tolist()
                        target_item_ids.update(hits)
                        
            except Exception as e:
                print(f"Error processing timeline for collection {coll_id}: {e}")

        if not target_item_ids:
             return jsonify({"status": "no_matches", "message": "No specific videos matched the voted time periods."})

        # 3. Check Enrichment Status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
             df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        default_platform = scrape_queues.default_platform()
        new_scrape = []
        new_annotate = []
        item_platform: dict[str, str] = {}

        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                     df_status = df_status.rename(columns={'index': 'item_id'})

            # Convert status ids to set for fast lookup
            status_records = df_status.set_index('item_id').to_dict('index')

            for item in target_item_ids:
                if item in status_records:
                    rec = status_records[item]
                    is_scraped = rec.get('scraped_ok', False)
                    is_annotated = rec.get('annotated_ok', False)
                    scrape_fail = rec.get('scrape_fail', False)
                    annotated_fail = rec.get('annotated_fail', False)
                    has_media = rec.get('video_downloaded', False)
                    plat = rec.get('source_platform')
                    item_platform[item] = plat if isinstance(plat, str) and plat else default_platform

                    # Annotation needs an mp4: metadata-only items (e.g.
                    # YouTube long-form past the media duration cap) are not
                    # annotatable and stay out of the queue.
                    if not is_scraped and not scrape_fail:
                        new_scrape.append(item)
                    elif is_scraped and has_media and not is_annotated and not annotated_fail:
                        new_annotate.append(item)
                else:
                    # Item not in enrichment status -> hasn't been scraped yet
                    new_scrape.append(item)
        else:
            # No enrichment file -> everything needs scraping
            new_scrape = list(target_item_ids)

        new_scrape = list(set(new_scrape))
        new_annotate = list(set(new_annotate))

        # 4. Append to Queues (scrape queues are per-platform). Platforms
        # without a scrape-contract block have no worker to drain a queue, so
        # their items are skipped instead of stranded in an orphan file.
        added_to_scrape: dict[str, int] = {}
        if new_scrape:
            scrapeable = set(scrape_queues.registered_platforms())
            by_platform: dict[str, list[str]] = {}
            for item in new_scrape:
                by_platform.setdefault(item_platform.get(item, default_platform), []).append(item)
            for platform, items in by_platform.items():
                if platform not in scrapeable:
                    print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                    continue
                scrape_queues.append_to_scrape_queue(platform, items)
                added_to_scrape[platform] = len(items)

        def load_queue(fname):
            if data_io.exists(storage_location="cache", filename=fname):
                try:
                    q = data_io.load_json(storage_location="cache", filename=fname)
                    if isinstance(q, list): return q
                except Exception:
                     pass
            return []

        def save_queue(fname, q):
            # deduplicate and save
            q_clean = list(set(q))
            data_io.save_json(data=q_clean, storage_location="cache", filename=fname)

        if new_annotate:
             current_annotate = load_queue("to_annotate.json")
             current_annotate.extend(new_annotate)
             save_queue("to_annotate.json", current_annotate)

        return jsonify({
            "status": "success",
            "added_to_scrape": len(new_scrape),
            "added_to_scrape_by_platform": added_to_scrape,
            "added_to_annotate": len(new_annotate)
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_scrape():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    retry_missing_media = bool(data.get("retry_missing_media", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None

        if data_io.exists(storage_location="cache", filename=recoded_fn):
            # Load only the required column if possible, but load_parquet loads all if columns not provided properly or we can just load the whole file.
            # Actually, calculate_to_scrape only really needs item_id. The full load is fine as the files are usually small enough, but let's just load it.
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)

        if df_study is None or df_study.empty:
            # If not cached or empty, generate from scratch
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)

        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unscraped_videos = []
        if df_status is not None and not df_status.empty:
            # item_id is usually the index in enrichment_status
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                # If index was unnamed, it might become 'index'
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            # Map enrichment_status to our study videos
            study_videos = df_study[['item_id']].copy()
            study_status = study_videos.merge(df_status, on='item_id', how='left')

            # Find videos where scraped_ok is fundamentally False or NaN AND scrape_fail is fundamentally False or NaN
            not_scraped = pd.isna(study_status['scraped_ok']) | (study_status['scraped_ok'] == False)

            # When retry_failed is set, include items that previously failed
            # by dropping the scrape_fail filter — the user is asking us to
            # re-attempt them regardless of past outcome.
            if retry_failed:
                unscraped_mask = not_scraped
            elif 'scrape_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scrape_fail']) | (study_status['scrape_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            elif 'scraped_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scraped_fail']) | (study_status['scraped_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            else:
                unscraped_mask = not_scraped

            unscraped_videos = study_status.loc[unscraped_mask, 'item_id'].dropna().tolist()
        else:
            unscraped_videos = df_study['item_id'].dropna().tolist()

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unscraped_videos = list({str(v) for v in unscraped_videos})

        # Media-gap backfill: items scraped OK but whose media never landed
        # (e.g. a rate-limited media phase saved metadata-only) can't be found
        # via scraped_ok — pick them straight from the study frame. Items over
        # the platform's media duration cap are metadata-only by design and
        # excluded; unknown durations pass (the media phase decides).
        if retry_missing_media and {'scraped_ok', 'video_downloaded', 'item_id'} <= set(df_study.columns):
            per_item = df_study.drop_duplicates(subset=['item_id'])
            gap_mask = (
                (per_item['scraped_ok'].fillna(False) == True)
                & ~(per_item['video_downloaded'].fillna(False) == True)
            )
            gap = per_item[gap_mask]
            gap_platforms = (
                gap['source_platform'].fillna(scrape_queues.default_platform())
                if 'source_platform' in gap.columns
                else pd.Series(scrape_queues.default_platform(), index=gap.index)
            )
            media_gap_videos: set[str] = set()
            for gap_platform, grp in gap.groupby(gap_platforms):
                try:
                    cap = get_scraper(str(gap_platform)).media_duration_cap()
                except Exception:
                    continue  # no scraper registered for this platform
                if 'duration' in grp.columns:
                    dur = pd.to_numeric(grp['duration'], errors='coerce')
                    grp = grp[dur.isna() | (dur <= cap)]
                media_gap_videos |= {str(v) for v in grp['item_id'].dropna()}
            if media_gap_videos:
                print(f"Retry-missing-media: adding {len(media_gap_videos)} scraped-ok "
                      f"items without media to the queue(s).")
            unscraped_videos = list(set(unscraped_videos) | media_gap_videos)

        # Append to the per-platform scrape queues. The study frame carries
        # source_platform per event row; an item never spans platforms.
        default_platform = scrape_queues.default_platform()
        item_platform: dict[str, str] = {}
        if 'source_platform' in df_study.columns:
            plat_map = (
                df_study[['item_id', 'source_platform']]
                .dropna(subset=['item_id'])
                .drop_duplicates(subset=['item_id'])
            )
            item_platform = {
                str(i): (str(p) if isinstance(p, str) and p else default_platform)
                for i, p in zip(plat_map['item_id'], plat_map['source_platform'])
            }

        by_platform: dict[str, list[str]] = {}
        for vid in unscraped_videos:
            by_platform.setdefault(item_platform.get(vid, default_platform), []).append(vid)

        # Platforms without a scrape-contract block have no worker to drain a
        # queue, so their items are skipped instead of stranded in an orphan file.
        scrapeable = set(scrape_queues.registered_platforms())
        queue_len_by_platform: dict[str, int] = {}
        skipped_by_platform: dict[str, int] = {}
        for platform, items in by_platform.items():
            if platform not in scrapeable:
                skipped_by_platform[platform] = len(items)
                print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                continue
            queue_len_by_platform[platform] = scrape_queues.append_to_scrape_queue(platform, items)

        return jsonify({
            "status": "success",
            "videos_to_scrape": sum(queue_len_by_platform.values()),
            "videos_to_scrape_by_platform": queue_len_by_platform,
            "skipped_unscrapeable_by_platform": skipped_by_platform,
        })

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_annotate():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        from fyp.fyp_config import fyp_cf

        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None
        
        if data_io.exists(storage_location="cache", filename=recoded_fn):
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)
            
        if df_study is None or df_study.empty:
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
            
        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unannotated_videos = []
        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            if 'duration' in df_study.columns:
                study_videos = df_study[['item_id', 'duration']].copy()
            else:
                study_videos = df_study[['item_id']].copy()
                
            study_status = study_videos.merge(df_status, on='item_id', how='left')
            
            is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
            
            if 'annotated_ok' in study_status.columns:
                not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)
            else:
                not_annotated_ok = True

            # When retry_failed is set, ignore the annotated_fail column so
            # items that previously failed annotation are re-queued.
            if retry_failed:
                not_annotated_fail = True
            elif 'annotated_fail' in study_status.columns:
                not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
            else:
                not_annotated_fail = True

            unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail

            # Annotation needs an mp4: metadata-only items (e.g. YouTube
            # long-form past the media duration cap) are not annotatable.
            if 'video_downloaded' in study_status.columns:
                unannotated_mask = unannotated_mask & (study_status['video_downloaded'].fillna(False) == True)

            if 'duration' in study_status.columns:
                max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
                duration_ok = (study_status['duration'] < max_dur) | pd.isna(study_status['duration'])
                unannotated_mask = unannotated_mask & duration_ok

            unannotated_videos = study_status.loc[unannotated_mask, 'item_id'].dropna().tolist()
        else:
            unannotated_videos = []

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unannotated_videos = list({str(v) for v in unannotated_videos})

        # Append target payload to global annotate queue
        current_queue = []
        if data_io.exists(storage_location="cache", filename="to_annotate.json"):
            try:
                q = data_io.load_json(storage_location="cache", filename="to_annotate.json")
                if isinstance(q, list): current_queue = q
            except Exception:
                pass
                
        current_queue.extend(unannotated_videos)
        current_queue = list(set(current_queue))

        data_io.save_json(
            data=current_queue,
            storage_location="cache",
            filename="to_annotate.json"
        )

        return jsonify({
            "status": "success",
            "videos_to_annotate": len(current_queue),
        })

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_enrichment():
    from fyp.fyp_config import CONSOLIDATE_ENRICHMENT_SCRIPT

    if _is_worker_running("consolidate_enrichment"):
        return jsonify({"status": "error", "message": "Consolidation already running"}), 409

    data = request.json or {}
    force = bool(data.get("force"))
    # auto_refresh defaults to True — the button means "consolidate + fix the
    # consolidation impact automatically". Force Reconsolidate skips the
    # downstream chain by default to keep it debuggable.
    auto_refresh = bool(data.get("auto_refresh", not force))

    blocking = _workers_blocking_consolidate()
    if blocking:
        if force:
            return jsonify({
                "status": "error",
                "message": f"Cannot force reconsolidate while {', '.join(blocking)} running.",
            }), 409

        # Arm instead of firing — pipeline kicks off when workers go idle.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry["auto_armed"] = True
        entry["auto_armed_force"] = False
        entry["auto_armed_auto_refresh"] = auto_refresh
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
        return jsonify({
            "status": "armed",
            "message": f"Waiting for {', '.join(blocking)} to finish.",
            "blocking": blocking,
        })

    task_args: dict = {}
    if force:
        task_args["force_consolidation"] = True
    if auto_refresh:
        task_args["auto_refresh"] = True

    # Firing now — clear any stale armed flag.
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    success, msg = start_process("consolidate_enrichment", CONSOLIDATE_ENRICHMENT_SCRIPT,
                                 task_args=task_args if task_args else None)
    if success:
        return jsonify({"status": "started", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409


@management_bp.route('/api/manage/enrichment/consolidate/disarm', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_disarm():
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    was_armed = bool(entry.get("auto_armed"))
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()
    return jsonify({"status": "disarmed", "was_armed": was_armed})


@management_bp.route('/api/manage/enrichment/refresh-downstream', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_refresh_downstream():
    """Re-run the downstream refresh pipeline for the stored consolidation impact.

    Powers the "Refresh All Affected" button. It runs the SAME pipeline as the
    consolidate auto-refresh — embeddings → video_map → recode → {meta ‖ pca ‖
    timelines} — against the impact recorded by a prior Consolidate Only run, so
    the niche steps the old per-button cascade skipped are now included and in
    the right order. Writes ``pipeline_plan`` so the step list renders, then
    dispatches via the Cloud Tasks chain (Cloud Run) or the local sequential
    orchestrator (dev).
    """
    from web_interface.run_consolidate_enrichment import (
        _build_downstream_pipeline,
        build_pipeline_chain,
    )

    load_process_stats()
    ps_entry = process_stats.get("consolidate_enrichment", {})
    mem = processes.get("consolidate_enrichment", {}).get("data", {}) or {}
    impact = ps_entry.get("consolidation_impact") or mem.get("consolidation_impact")
    if not impact:
        return jsonify({"status": "noop", "message": "No consolidation impact to refresh."})

    # Don't start on top of a running pipeline.
    if ps_entry.get("pipeline_in_flight") or any(
        _is_worker_running(n) for n in (["consolidate_enrichment"] + PIPELINE_STEPS_ORDER)
    ):
        return jsonify({"status": "error", "message": "A refresh pipeline is already running."}), 409

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        return jsonify({"status": "noop", "message": "Nothing to refresh."})

    now_iso = datetime.now(UTC).isoformat()
    ps_entry["pipeline_plan"] = {"steps": [p["task"] for p in pipeline], "started_ts": now_iso}
    ps_entry["last_pipeline_partial"] = False
    ps_entry["last_pipeline_failed_at"] = None
    ps_entry["last_pipeline_summary"] = "Pipeline in progress — refreshing caches..."
    ps_entry["last_pipeline_summary_ts"] = now_iso
    ps_entry["pipeline_in_flight"] = True
    process_stats["consolidate_enrichment"] = ps_entry
    save_process_stats()

    # In local dev the consolidate worker's last ::DATA:: emission lingers in
    # processes["consolidate_enrichment"]["data"] and the stats / step-view
    # endpoints overlay it on top of process_stats. After a "Consolidate Only"
    # run that emission carries pipeline_plan=None, which would shadow the fresh
    # plan just written and hide the step list. Mirror the new plan into the
    # in-memory copy so both stores agree (no-op on Cloud Run, where there is no
    # in-process consolidate subprocess).
    mem = processes.get("consolidate_enrichment", {}).get("data")
    if isinstance(mem, dict):
        mem["pipeline_plan"] = ps_entry["pipeline_plan"]
        mem["last_pipeline_partial"] = False
        mem["last_pipeline_failed_at"] = None

    if is_cloud_run():
        from ..process_manager import _dispatch_cloud_task
        chain = build_pipeline_chain(pipeline)
        success, msg = _dispatch_cloud_task(chain["next_task"], chain["next_task_args"])
        if not success:
            # Roll back the in-flight flag so the UI doesn't hang.
            load_process_stats()
            entry = process_stats.get("consolidate_enrichment", {})
            entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = entry
            save_process_stats()
            return jsonify({"status": "error", "message": f"Dispatch failed: {msg}"}), 409
    else:
        import threading

        from ..process_manager import _run_local_downstream_pipeline
        threading.Thread(
            target=_run_local_downstream_pipeline, args=(impact,), daemon=True
        ).start()

    return jsonify({"status": "started", "message": "Downstream refresh started."})



def _evaluate_consolidation_staleness() -> dict:
    """Return impact/freshness for the latest consolidation, clearing stale impact.

    Reloads process_stats from GCS, inspects the stored consolidation_impact,
    and removes it when every downstream process has run successfully since the
    impact timestamp. Returns a dict with ``has_impact``, ``impact``, and a
    per-process ``processes`` map — safe to call from any endpoint that needs
    to reason about whether the consolidation impact panel should be visible.
    """
    load_process_stats()

    consolidate_entry = process_stats.get("consolidate_enrichment", {})
    impact = consolidate_entry.get("consolidation_impact")

    if not impact or not impact.get("timestamp"):
        return {"has_impact": False, "impact": None, "processes": {}}

    impact_ts = impact["timestamp"]
    affected_studies = impact.get("affected_study_names", [])
    affected_collections = impact.get("affected_collection_ids", [])

    downstream = {
        "recode_refresh_studies": {
            "label": "Study Definitions",
            "affected": affected_studies,
        },
        "meta_refresh_groups": {
            "label": "Explore Metadata",
            "affected": affected_studies,
        },
        "timelines_refresh": {
            "label": "Timelines",
            "affected": affected_collections,
        },
        "pca_refresh": {
            "label": "Correlations",
            "affected": affected_studies,
        },
    }

    result = {}
    all_fresh = True
    for proc_name, info in downstream.items():
        last_success = process_stats.get(proc_name, {}).get("last_success")
        # A downstream process only blocks impact resolution when the impact
        # actually affects something it owns. With no affected studies (or
        # collections), the corresponding refresh is never dispatched by the
        # auto-pipeline or the manual cascade — so requiring it to have run
        # would pin the impact forever.
        if not info["affected"]:
            stale = False
        else:
            stale = not last_success or last_success < impact_ts
        result[proc_name] = {
            "stale": stale,
            "label": info["label"],
            "affected": info["affected"],
        }
        if stale:
            all_fresh = False

    if all_fresh:
        consolidate_entry.pop("consolidation_impact", None)
        process_stats["consolidate_enrichment"] = consolidate_entry
        save_process_stats()
        # Also drop the in-memory copy. get_enrichment_stats merges
        # process_stats with processes[name]["data"] when building its
        # response, so a lingering in-memory copy would re-surface the
        # impact panel even after we popped it from process_stats.
        in_memory_data = processes.get("consolidate_enrichment", {}).get("data")
        if isinstance(in_memory_data, dict):
            in_memory_data.pop("consolidation_impact", None)
        return {"has_impact": False, "impact": impact, "processes": result}

    return {"has_impact": True, "impact": impact, "processes": result}




@management_bp.route('/api/manage/refresh/staleness', methods=['GET'])
@permission_required('tab.data_management.refresh')
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact."""
    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
    })


@management_bp.route('/api/manage/schema/reload', methods=['POST'])
@permission_required('tab.admin.general')
@login_required
def reload_schema():
    try:
        global fyp_cf
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        return jsonify({"status": "success", "message": "Variable schema reloaded successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert the schema DataFrame to a list of plain-dict records,
    coercing nulls to empty strings so the JSON payload is stable shape.
    """
    out: list[dict] = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            val = row[col]
            try:
                if pd.isna(val):
                    rec[col] = ""
                    continue
            except (TypeError, ValueError):
                pass
            rec[col] = "" if val is None else str(val)
        out.append(rec)
    return out



def _affected_studies_for_hash(new_hash: str) -> list[str]:
    """List study names whose sidecar ``var_schema_hash`` differs from ``new_hash``.

    Used by the admin UI to surface "saving will trigger N study rebuilds"
    before the admin clicks save.  Missing / unreadable sidecars are
    silently skipped — the regular refresh path will rebuild them anyway.
    """
    try:
        files = data_io.listdir(storage_location="cache")
    except Exception:
        return []
    affected: list[str] = []
    for fname in files:
        if not fname.endswith("_recoded.meta.json"):
            continue
        study_name = fname[: -len("_recoded.meta.json")]
        try:
            sidecar = data_io.load_json(storage_location="cache", filename=fname)
        except Exception:
            continue
        if str(sidecar.get("var_schema_hash") or "") != new_hash:
            affected.append(study_name)
    return sorted(affected)



def _var_schema_admin_enabled() -> bool:
    """Off-switch for the schema admin UI.

    Defaults to True; set ``[features].var_schema_admin = false`` in
    ``config.toml`` to disable without redeploying.  Permission gate
    (``tab.admin.schema``) is still required on top of this.
    """
    features = fyp_cf.get("features") or {}
    return bool(features.get("var_schema_admin", True))



@management_bp.route('/api/manage/annotation-versions', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def list_annotation_versions():
    """List recorded annotation versions and the active (promoted) one."""
    try:
        return jsonify({
            "versions": annotation_versioning.list_versions(),
            "active": annotation_versioning.get_active_version(),
            "current": annotation_versioning.current_annotation_version(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/<version>', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_annotation_version(version):
    """Return one version's full record, including its prompt + schema snapshot."""
    try:
        registry = annotation_versioning.load_registry()
        info = registry.get("versions", {}).get(version)
        if info is None:
            return jsonify({"error": "unknown version"}), 404
        # The legacy version predates per-version prompt snapshots; surface the
        # historical file-based prompt so "View" isn't empty for it.
        if version == annotation_versioning.LEGACY_VERSION and not info.get("prompt_text"):
            legacy_prompt = annotation_versioning.legacy_prompt_text()
            if legacy_prompt:
                info = {**info, "prompt_text": legacy_prompt}
        return jsonify({
            "version": version,
            "active": registry.get("active") == version,
            "record": info,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/annotation-versions/promote', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def promote_annotation_version():
    """Promote a version to active and rebuild the global active dataset.

    Updates the registry, re-derives ``machine_annotations_recoded.parquet`` from
    the version archive (fast — no re-refinement), and clears the study RAM
    cache. Per-study datasets still need a study refresh to fully reflect the
    promotion.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")
        if not version:
            return jsonify({"error": "version is required"}), 400
        try:
            annotation_versioning.promote_version(version)
        except KeyError:
            return jsonify({"error": f"unknown version: {version}"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        rebuilt = rebuild_active_annotations_from_archive(verbose=False)
        with study_cache.lock:
            study_cache.cache.clear()

        activity_log.record(
            actor=_actor(),
            category="admin",
            action="annotation_version.promote",
            details={"version": version, "active_rows": rebuilt},
        )
        return jsonify({
            "ok": True,
            "active": version,
            "active_rows": rebuilt,
            "note": "Global active annotations rebuilt. Refresh studies to apply to per-study datasets.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@management_bp.route('/api/manage/studies/<study>/annotation-version', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def set_study_annotation_version(study):
    """Pin (or clear) a study's annotation_version for reproducibility.

    A pinned study merges against that version's annotations (read strictly from
    the archive) instead of the active dataset. Pass a falsy ``version`` to clear
    the pin. The study must be refreshed to rebuild its dataset.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")  # falsy clears the pin
        if "study_defs" not in fyp_cf:
            init_study_defs()
        if study not in fyp_cf["study_defs"]:
            return jsonify({"error": f"unknown study: {study}"}), 404
        if version:
            registry = annotation_versioning.load_registry()
            if version not in registry.get("versions", {}):
                return jsonify({"error": f"unknown version: {version}"}), 404
            fyp_cf["study_defs"][study]["annotation_version"] = version
        else:
            fyp_cf["study_defs"][study].pop("annotation_version", None)
        save_study_defs()
        study_cache.invalidate(study)
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="study.pin_annotation_version",
            details={"study": study, "version": version or None},
        )
        return jsonify({
            "ok": True,
            "study": study,
            "annotation_version": version or None,
            "note": "Refresh the study to rebuild its dataset against this version.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _contract_locked_map(df) -> dict:
    """Return ``{variable_name: {metadata, section}}`` for contract-owned cells.

    ``metadata`` is True when a contract owns the row's role/scale/display_name/
    description — the annotation contract's flattened Gemini columns, or the
    scrape / activity / derived contracts' canonical columns. ``section`` is True
    for every Gemini-origin row (all forced under "AI Annotations") and for every
    scrape / activity / derived contract column (whose section those contracts
    own). The admin editor renders these cells read-only. Degrades to ``{}`` if no
    contract can be loaded, so the editor never breaks on a contract error.
    """
    annotation_cols: set = set()
    scrape_cols: set = set()
    activity_cols: set = set()
    derived_cols: set = set()
    try:
        from fyp import annotation_contract as ac

        annotation_cols = set(ac.contract_column_metadata(ac.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import scrape_contract as sc

        scrape_cols = set(sc.contract_column_metadata(sc.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import activity_contract as acy

        activity_cols = set(acy.contract_column_metadata(acy.load_contract()).keys())
    except Exception:
        pass
    try:
        from fyp import derived_contract as dc

        derived_cols = set(dc.contract_column_metadata(dc.load_contract()).keys())
    except Exception:
        pass
    # Legacy fields owned by past-version registry snapshots (e.g. trend /
    # australian_relevance for annotation; any future retired scrape/activity
    # field) — contract-owned/read-only, and badged "legacy" in the editor. A
    # field a CURRENT contract still owns is NOT legacy.
    legacy_cols: set = set()
    try:
        from fyp import annotation_versioning as av

        legacy_cols |= set(av.union_field_metadata().keys()) - annotation_cols
    except Exception:
        pass
    try:
        from fyp import scrape_versioning as sv

        legacy_scrape = set(sv.union_field_metadata().keys()) - scrape_cols
        legacy_cols |= legacy_scrape
        scrape_cols |= legacy_scrape
    except Exception:
        pass
    try:
        from fyp import activity_versioning as av_act

        legacy_activity = set(av_act.union_field_metadata().keys()) - activity_cols
        legacy_cols |= legacy_activity
        activity_cols |= legacy_activity
    except Exception:
        pass
    if not (annotation_cols or scrape_cols or activity_cols or derived_cols or legacy_cols):
        return {}
    section_owned_cols = scrape_cols | activity_cols | derived_cols
    locked: dict = {}
    for _, row in df.iterrows():
        vn = str(row.get("variable_name", ""))
        src = str(row.get("source", "")).strip()
        is_gemini = src == "Gemini" or src.startswith("derived: Gemini")
        section_owned = vn in section_owned_cols
        is_legacy = vn in legacy_cols
        meta_owned = (
            vn in annotation_cols or vn in scrape_cols
            or vn in activity_cols or vn in derived_cols or is_legacy
        )
        if meta_owned or is_gemini:
            entry = {"metadata": meta_owned, "section": is_gemini or section_owned}
            if is_legacy:
                entry["legacy"] = True
            locked[vn] = entry
    return locked




@management_bp.route('/api/manage/schema', methods=['GET'])
@permission_required('tab.admin.schema')
@login_required
def get_schema():
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    """Return the current schema for the admin editor.

    ``?force_reload=1`` re-reads ``var_schema.csv`` from disk/GCS before
    responding so the editor's Reload button picks up direct edits made
    outside the UI (e.g. ``gsutil cp``).  The initial tab load and the
    post-save refresh omit the flag — they only need in-memory state.
    """
    try:
        from fyp import var_presentation as vp

        if request.args.get("force_reload") in ("1", "true", "yes"):
            global fyp_cf
            fyp_cf = load_var_schema(fyp_cf, verbose=False)
        df = fyp_cf["var_schema"]
        presentation = vp.load_presentation() or vp.empty_presentation()
        return jsonify({
            "rows": _df_to_records(df),
            "columns": list(df.columns),
            "semantic_columns": list(SEMANTIC_COLUMNS),
            "enums": {
                "role": sorted(VAR_SCHEMA_ROLES),
                "scale": sorted(VAR_SCHEMA_SCALES),
            },
            "contract_locked": _contract_locked_map(df),
            "contract_path": "config/annotation_contract.toml",
            "scrape_contract_path": "config/scrape_contract.toml",
            # The presentation store is the only admin-editable payload left
            # (the metadata is contract-owned); its etag guards saves.
            "presentation": presentation.get("surfaces", {}),
            "prio_columns": dict(vp.SURFACE_TO_PRIO_COLUMN),
            "etag": vp.compute_presentation_etag(presentation),
            "current_hash": compute_var_schema_hash(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _payload_to_df(payload_rows: list[dict]) -> pd.DataFrame:
    """Convert API rows into a DataFrame shaped like the current schema."""
    current_columns = list(fyp_cf["var_schema"].columns)
    # Ensure every incoming row has every column the live schema expects
    rows = []
    for r in payload_rows:
        rows.append({col: r.get(col, "") for col in current_columns})
    return pd.DataFrame(rows, columns=current_columns)



@management_bp.route('/api/manage/schema/validate', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def validate_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/schema', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_schema_endpoint():
    """Retired: metadata is contract-owned; only presentation flags are editable."""
    return jsonify({
        "error": "retired",
        "message": "var_schema metadata is contract-owned; edit the contract TOMLs. "
                   "Presentation flags save via POST /api/manage/presentation.",
    }), 410



@management_bp.route('/api/manage/presentation', methods=['POST'])
@permission_required('tab.admin.schema')
@login_required
def save_presentation_endpoint():
    """Persist the global web-surface membership flags (the admin defaults).

    Body: ``{"surfaces": {filter|timeline|viz|display: [variable_name, ...]},
    "etag": <presentation etag from GET /api/manage/schema>}``. Refuses on a
    stale etag (409) or unknown variable names (400). Presentation edits can
    never change the study hash — asserted server-side as a guard.
    """
    global fyp_cf
    if not _var_schema_admin_enabled():
        return jsonify({"error": "schema admin disabled"}), 503
    try:
        from fyp import var_presentation as vp

        body = request.get_json(force=True, silent=False) or {}
        surfaces = body.get("surfaces")
        etag = body.get("etag")
        if not isinstance(surfaces, dict):
            return jsonify({"error": "surfaces must be an object"}), 400
        known = set(fyp_cf["var_schema"]["variable_name"].astype("string"))
        unknown = sorted({
            n for names in surfaces.values() if isinstance(names, list)
            for n in names if n not in known
        })
        if unknown:
            return jsonify({"error": "unknown variables", "unknown": unknown}), 400

        old_hash = compute_var_schema_hash()
        try:
            result = vp.save_presentation(surfaces, expected_etag=etag, updated_by=_actor())
        except vp.PresentationConflict as e:
            return jsonify({
                "error": "conflict",
                "message": str(e),
                "etag": vp.compute_presentation_etag(),
            }), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        new_hash = compute_var_schema_hash()
        hash_changed = new_hash != old_hash
        if hash_changed:
            # Presentation flags are excluded from the hash by design; a change
            # here means something else drifted — surface it loudly.
            print(f"WARNING: presentation save changed the schema hash ({old_hash[:16]} -> {new_hash[:16]}).")
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="var_presentation.save",
            details={"hash_changed": hash_changed},
        )
        return jsonify({"etag": result["etag"], "hash_changed": hash_changed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/inter_coder_reliability', methods=['GET'])
@permission_required('tab.admin.reliability')
@login_required
def get_inter_coder_reliability():
    try:
        results = calculate_inter_coder_reliability()
        if "error" in results:
             return jsonify(results), 400
        return jsonify(results)
    except Exception as e:
        print(f"Error calculating reliability: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/ingestion/sources', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_sources():
    try:
        main_collection = get_main_collection(verbose=False)
        sources = []
        total_pending = 0
        for col in main_collection.collections:
            files: list[dict] = []
            manifest_fn = "ingestion_manifest.json"
            if col.raw_path and data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
                manifest = data_io.load_json(
                    storage_location=col.raw_path, filename=manifest_fn, verbose=False
                ) or {}
                for fn, meta in manifest.items():
                    files.append({
                        "filename": fn,
                        "collection_id": (meta or {}).get("collection_id"),
                        "tags": (meta or {}).get("tags") or [],
                        "tz": (meta or {}).get("tz"),
                    })
            files.sort(key=lambda f: f["filename"])
            pending = len(files)
            total_pending += pending
            sources.append({
                "source_platform": col.source_platform,
                "data_source": col.data_source,
                "raw_path": col.raw_path,
                "class_name": col.__class__.__name__,
                "pending_files": pending,
                "files": files,
                "ingestion_mode": getattr(col, "ingestion_mode", "upload"),
                "zip_member_suffixes": col.zip_member_suffixes(),
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/fetch_aio', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def fetch_aio_data():
    """Trigger download of recent AIO donations and metadata from AWS."""
    from fyp.fyp_config import AIO_FETCH_SCRIPT

    hours_back = 24
    if request.is_json and request.json:
        hours_back = int(request.json.get('hours_back', 24))

    success, msg = start_process(
        "aio_fetch",
        AIO_FETCH_SCRIPT,
        task_args={"hours_back": hours_back},
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409

@management_bp.route('/api/manage/ingestion/upload', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def upload_ingestion_file():
    """Upload one or more raw files with optional collection_id and tags metadata.

    Accepts form fields:
        files: one or more files (also accepts legacy single 'file' key)
        raw_path: storage location key (e.g. 'ddp_raw')
        collection_id: explicit collection ID (used when collection_id_mode is 'single')
        collection_id_mode: 'single' | 'per_file' (default 'per_file')
        tags: JSON-encoded list of tag strings
        tz: optional donor timezone (IANA name like 'Asia/Kolkata' or a fixed
            offset like '+05:30') — the authoritative source for local-time
            conversion, overriding any ambiguous timezone label in the export.
    """
    # Accept both multi-file ('files') and legacy single-file ('file') keys
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400

    raw_path_key = request.form.get('raw_path')
    if not raw_path_key:
        return jsonify({"error": "raw_path missing"}), 400

    # Stage uploads in the local temp dir, then hand off to data_io.move()
    # which routes to GCS (production) or the configured local data dir
    # (dev). Writing directly to the resolved local path skipped GCS
    # entirely on Cloud Run, so manifests pointed at files that only ever
    # lived on the request-handling container's ephemeral filesystem.
    temp_dir = fyp_cf['paths']['temp']
    os.makedirs(temp_dir, exist_ok=True)

    collection_id = request.form.get('collection_id', '').strip()
    collection_id_mode = request.form.get('collection_id_mode', 'per_file')
    tags_json = request.form.get('tags', '[]')
    try:
        tags = json.loads(tags_json) if tags_json else []
    except json.JSONDecodeError:
        tags = []

    # Optional donor timezone (IANA name or fixed offset). Validated here so a
    # typo is rejected at upload rather than silently ignored at ingest time.
    donor_tz = request.form.get('tz', '').strip()
    if donor_tz and parse_donor_timezone(donor_tz) is None:
        return jsonify({
            "error": f"Unrecognised timezone '{donor_tz}'. Use an IANA name "
                     f"(e.g. 'Asia/Kolkata') or a fixed offset (e.g. '+05:30').",
        }), 400

    # Load or create the ingestion manifest for this raw_path
    manifest_fn = "ingestion_manifest.json"
    manifest: dict = {}
    if data_io.exists(storage_location=raw_path_key, filename=manifest_fn):
        manifest = data_io.load_json(
            storage_location=raw_path_key, filename=manifest_fn, verbose=False
        ) or {}

    try:
        uploaded = []
        for file in files:
            if file.filename == '':
                continue
            filename = secure_filename(file.filename)
            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)

            data_io.move(
                src_storage_location="temp",
                dst_storage_location=raw_path_key,
                filename=filename,
                verbose=False,
            )
            # data_io.move() swallows GCS upload failures silently, so confirm
            # the file actually landed before we record it in the manifest.
            if not data_io.exists(storage_location=raw_path_key, filename=filename):
                return jsonify({
                    "error": f"Upload of '{filename}' to '{raw_path_key}' did not persist.",
                }), 500

            if collection_id_mode == "single" and collection_id:
                file_collection_id = collection_id
            else:
                file_collection_id = os.path.splitext(filename)[0]

            manifest[filename] = {
                "collection_id": file_collection_id,
                "tags": tags,
            }
            if donor_tz:
                manifest[filename]["tz"] = donor_tz
            uploaded.append(filename)

        # Save updated manifest
        data_io.save_json(
            data=manifest,
            storage_location=raw_path_key,
            filename=manifest_fn,
            verbose=False
        )

        # Pre-populate collection_annotations.json with tags for each unique collection_id
        if tags:
            _prepopulate_annotations(manifest, tags)

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.upload",
            target=raw_path_key,
            details={
                "files": uploaded,
                "tags": tags,
                "collection_id_mode": collection_id_mode,
                "tz": donor_tz or None,
            },
        )
        return jsonify({
            "status": "success",
            "message": f"{len(uploaded)} file(s) uploaded.",
            "files": uploaded,
        })
    except Exception as e:
        print(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/refresh-collection-metadata', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_collection_metadata():
    """Regenerate _metadata.parquet from scratch using all events."""
    from fyp.fyp_config import COLLECTION_METADATA_REFRESH_SCRIPT

    success, msg = start_process(
        "collection_metadata_refresh",
        COLLECTION_METADATA_REFRESH_SCRIPT,
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409



@management_bp.route('/api/manage/ingestion/refresh', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_ingestion_collection():
    from fyp.fyp_config import INGEST_REFRESH_SCRIPT

    success, msg = start_process("ingest_refresh", INGEST_REFRESH_SCRIPT)
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.refresh",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409




@management_bp.route('/api/manage/ingestion/ledger/unskip', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def unskip_ingestion_ledger_entry():
    """Drop a single filename from the ingestion ledger so it will be
    re-scanned on the next ingestion run. The raw file on disk is left
    untouched.
    """
    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    main_collection = get_main_collection(verbose=False)
    removed = main_collection.remove_from_ledger(filename)
    if not removed:
        return jsonify({
            "status": "noop",
            "message": f"'{filename}' was not in the ledger.",
        })

    main_collection.save_ledger()
    return jsonify({
        "status": "success",
        "message": f"'{filename}' removed from the ledger. It will be rescanned on the next ingestion run.",
    })




@management_bp.route('/api/manage/ingestion/structure/warnings', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_warnings():
    """List structure-drift verdicts awaiting review (quarantined + warned files)."""
    from fyp import structure_sentinel

    try:
        return jsonify(structure_sentinel.review_queue())
    except Exception as e:
        print(f"Error loading structure warnings: {e}")
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ingestion/structure/approve', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_approve():
    """Approve a quarantined file: fold its structure into the learned baseline
    and drop its ledger entry so the next ingestion run ingests it.
    """
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.approve_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

    main_collection = get_main_collection(verbose=False)
    main_collection.remove_from_ledger(filename)
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_approve",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' approved — its structure is now part of the baseline and it will be ingested on the next refresh.",
    })




@management_bp.route('/api/manage/ingestion/structure/reject', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_reject():
    """Reject a quarantined file: mark it manually excluded so it never ingests."""
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.reject_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404

    main_collection = get_main_collection(verbose=False)
    if not main_collection.set_ledger_outcome(
        filename, "manually_excluded", note="rejected via structure review"
    ):
        # No ledger entry yet (e.g. the refresh that quarantined it failed
        # before saving) — stamp one directly so the file is still excluded.
        main_collection.update_ledger([{
            "filename": filename,
            "outcome": "manually_excluded",
            "raw_rows": (entry.get("raw_stats") or {}).get("raw_rows") or 0,
            "final_rows": 0,
            "canonical_collection_id": None,
            "merged_with_siblings": [],
            "platform": entry.get("platform"),
            "source": entry.get("source"),
            "notes": "rejected via structure review",
        }])
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_reject",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' rejected — it is excluded from future ingestion runs.",
    })


@management_bp.route('/api/manage/ingestion/clear_pending', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def clear_pending_uploads():
    """Drop every pending upload across every registered ingester: delete each
    file from its raw_path storage and reset its manifest to an empty dict.
    Lightweight (no parquet I/O), so safe to run inline on the data-hub.
    """
    main_collection = get_main_collection(verbose=False)
    manifest_fn = "ingestion_manifest.json"

    cleared: list[dict] = []
    failures: list[dict] = []
    total_removed = 0

    for col in main_collection.collections:
        if not col.raw_path:
            continue
        if not data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
            continue
        manifest = data_io.load_json(
            storage_location=col.raw_path, filename=manifest_fn, verbose=False
        ) or {}
        if not manifest:
            continue

        removed_here: list[str] = []
        for fn in list(manifest.keys()):
            try:
                if data_io.exists(storage_location=col.raw_path, filename=fn):
                    data_io.remove(storage_location=col.raw_path, filename=fn)
                removed_here.append(fn)
            except Exception as e:
                failures.append({"raw_path": col.raw_path, "filename": fn, "error": str(e)})
                print(f"[clear_pending_uploads] failed to remove {col.raw_path}/{fn}: {e}")

        try:
            data_io.save_json(
                data={},
                storage_location=col.raw_path,
                filename=manifest_fn,
                verbose=False,
            )
        except Exception as e:
            failures.append({"raw_path": col.raw_path, "filename": manifest_fn, "error": str(e)})
            print(f"[clear_pending_uploads] failed to reset manifest for {col.raw_path}: {e}")

        cleared.append({
            "raw_path": col.raw_path,
            "class_name": col.__class__.__name__,
            "removed_files": removed_here,
        })
        total_removed += len(removed_here)

    return jsonify({
        "status": "success",
        "total_removed": total_removed,
        "cleared": cleared,
        "failures": failures,
    })





def _prepopulate_annotations(manifest: dict, tags: list[str]) -> None:
    """Merge tags into collection_annotations.json for each unique collection_id in the manifest."""
    annotations: dict = {}
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        ) or {}

    seen_ids: set = set()
    for _filename, meta in manifest.items():
        cid = meta.get("collection_id")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            existing = annotations.get(cid, {})
            existing_tags = existing.get("annotation_tags", [])
            merged_tags = sorted(set(existing_tags + tags))
            annotations[cid] = {
                "display_collection_id": existing.get("display_collection_id"),
                "annotation_tags": merged_tags,
                "hidden": existing.get("hidden", False),
            }

    data_io.save_json(
        data=annotations,
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_tags.json",
        verbose=False
    )
    invalidate_collection_tags_cache()





@management_bp.route('/api/manage/ingestion/metadata', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""

    from fyp.organize_datasets import COLLECTIONS_LABEL

    collection_ids: list[str] = []
    all_tags: set[str] = set()

    # Get collection IDs from the per-collection metadata parquet — small
    # enough to read in milliseconds, vs. the multi-GB recoded parquet which
    # would block the upload modal for ~5s while the user waits.
    metadata_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if data_io.exists(storage_location="recoded", filename=metadata_fn):
        md = data_io.load_parquet(
            storage_location="recoded",
            filename=metadata_fn,
            verbose=False,
        )
        if md is not None and not md.empty:
            if "collection_id" in md.columns:
                collection_ids = sorted(md["collection_id"].dropna().astype(str).unique().tolist())
            else:
                collection_ids = sorted(str(idx) for idx in md.index.dropna().unique().tolist())

    # Get tags from annotations
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False,
        ) or {}
        for ann in annotations.values():
            for tag in ann.get("annotation_tags", []):
                all_tags.add(tag)

    display_ids = load_display_id_map()

    return jsonify({
        "status": "success",
        "collection_ids": collection_ids,
        "display_ids": display_ids,
        "tags": sorted(list(all_tags)),
    })
