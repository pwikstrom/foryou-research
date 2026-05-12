import json
import os
import time as _time
from datetime import UTC, datetime

import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf, load_var_schema
from fyp.ingest import get_main_collection
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    create_study_recoded_dataset,
)
from fyp.studies import init_study_defs, save_study_defs

from ..data_service import (
    calculate_inter_coder_reliability,
    invalidate_collection_tags_cache,
    load_display_id_map,
)
from ..process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
)
from ..task_status import is_cloud_run, read_task_status

management_bp = Blueprint('management_bp', __name__)


# Downstream refresh steps considered by the auto-pipeline, in the order they
# are dispatched. Ordering matters: recode produces the recoded datasets that
# meta_refresh_groups / pca_refresh consume.
PIPELINE_STEPS_ORDER = [
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




def _calculate_stats(study_config, save_to_cache=True) -> tuple[dict, pd.DataFrame | None]:
    """Calculate stats for a study using enrichment_status.parquet AND the study's specific recoded dataset.

    Returns:
        Tuple of (stats_dict, full_recoded_dataframe). The DataFrame is None when no data exists.
    """

    empty_stats = {"total_activities": 0, "unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0, "unique_collections": 0}

    study_name = study_config.get("STUDY_NAME")
    if not study_name:
         return empty_stats, None

    # If no collections are selected, the study is empty — skip expensive computation
    selected = study_config.get("SELECTED_COLLECTIONS", [])
    if not selected:
         return empty_stats, None

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
        return empty_stats, None

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

        if 'scraped_ok' in matched_status.columns:
            scraped_videos = int(matched_status['scraped_ok'].fillna(False).sum())
        if 'annotated_ok' in matched_status.columns:
            annotated_videos = int(matched_status['annotated_ok'].fillna(False).sum())

    stats = {
        "total_activities": int(total_activities),
        "unique_videos": int(unique_videos),
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
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

    return stats, df_study







@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
def list_studies():
    # Always reload from disk/GCS to pick up changes made by the task-runner service
    init_study_defs()

    studies = fyp_cf['study_defs']

    # Convert to list with name included
    studies_list = []
    
    # User Access Logic
    # Admin sees everything.
    # Others (Researcher/Viewer) see only studies where they are listed in USER_ACCESS
    is_admin = current_user.is_admin()
    username = current_user.username
    
    for name, config in studies.items():
        # Ensure name is in config
        config['STUDY_NAME'] = name
        
        if is_admin:
            studies_list.append(config)
        else:
            # Check USER_ACCESS for this study
            user_access = config.get("USER_ACCESS", [])
            # user_access should be a list of ROLES (e.g. ['viewer', 'researcher', 'student']) or ['all']
            if isinstance(user_access, list) and (current_user.role in user_access or 'all' in user_access):
                studies_list.append(config)
        
    return jsonify(studies_list)







@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
def save_study():
    global fyp_cf

    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    global fyp_cf
    
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    # 1. Backup existing config
    original_config = None
    if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
        original_config = fyp_cf['study_defs'][study_name].copy()
        
    
    # If this is a new study (not in defs), we add it. 
    # If existing, we overwrite.
    fyp_cf['study_defs'][study_name] = data
    
    stats_to_persist: dict | None = None
    try:
        # 3. specific instruction: "Force update of the study dataset"
        # The logic in _calculate_stats calls create_study_recoded_dataset
        stats, df_study = _calculate_stats(data, save_to_cache=False)
        stats_to_persist = stats

        included_per_day = _daily_counts(df_study)

        # Compute pre-filter potentials (collections/activities/active_days/items)
        # from the raw collections data within each collection's play/observe
        # window, restricted to play and observe events.
        has_total_days = False
        potentials = {
            "collections": 0,
            "activities": 0,
            "active_days": 0,
            # Cascade semantics on the enrichment side:
            #   items.potential    = activities in study (how many activities map to items)
            #   scraped.potential  = items in study
            #   annotated.potential = scraped items in study
            "items": int(stats.get("total_activities", 0)),
            "scraped": int(stats.get("unique_videos", 0)),
            "annotated": int(stats.get("scraped_videos", 0)),
        }
        selected = data.get("SELECTED_COLLECTIONS") or []
        potentials["collections"] = len(selected)

        if selected and data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_recoded.parquet"):
            df_raw = data_io.load_parquet_selective(
                storage_location="recoded",
                filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
                columns=["collection_id", "local_timestamp", "activity_type"],
                filters=[("collection_id", "in", selected)],
            )
            if df_raw is not None and not df_raw.empty:
                windows = _load_collection_event_windows(selected)
                df_raw = _filter_to_event_windows(df_raw, windows)
                df_raw = _filter_to_play_observe(df_raw)
                has_total_days = not df_raw.empty

                if has_total_days:
                    potentials["activities"] = int(len(df_raw))
                    potentials["active_days"] = int(pd.to_datetime(df_raw["local_timestamp"], errors="coerce").dropna().dt.date.nunique())

        sparse_cells, total_cells = _count_sparse_cells(df_study)
        sampling_report = None
        if df_study is not None and hasattr(df_study, 'attrs'):
            sampling_report = df_study.attrs.get('sampling_report')
        issues = _derive_study_issues(stats, sparse_cells, total_cells, has_total_days, sampling_report)

        return jsonify({
            "status": "success",
            "stats": stats,
            "potentials": potentials,
            "included_per_day": included_per_day,
            "issues": issues,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 4. Revert config. Also cache fresh stats on the saved config so
        # subsequent modal opens show the full set of metrics — otherwise older
        # studies saved before the current stats shape lose fields on reopen.
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
             # If it was new, remove it?
             # Or keep it? Safer to remove if it wasn't there.
             if study_name in fyp_cf['study_defs']:
                  del fyp_cf['study_defs'][study_name]





def _collections_hash(selected: list) -> str:
    """Return a short stable hash of a selected-collections list."""

    import hashlib
    ids = sorted(str(x) for x in (selected or []))
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16]




@management_bp.route('/api/manage/studies/daily_activities', methods=['POST'])
@login_required
def daily_activities():
    """Return activities-per-day across a set of collections for the modal chart.

    Lightweight: reads only `collection_id` + `local_timestamp` columns from
    `collections_recoded.parquet` with a pushdown filter on the selected IDs.
    No date-range filter — the chart shows the full span so the user can pick
    a window visually.
    """

    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
def delete_study():
    global fyp_cf
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized - Admin only"}), 403
        
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

        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404




# Storage locations that hold raw uploaded files. Order matters only for the
# disambiguation probe in _find_raw_file_locations.
RAW_UPLOAD_LOCATIONS = ("ddp_raw", "aio_raw", "zeeschuimer_raw")




def _find_raw_file_locations(raw_files: list[str]) -> list[tuple[str, str]]:
    """Return [(storage_location, filename), ...] for each raw file that still
    exists in any of the registered upload locations.

    Probes each upload location's ingestion_manifest.json first (fast path) and
    falls back to data_io.exists when the manifest is missing or out of sync.
    Files not found in any location are silently skipped — they were already
    moved or deleted previously.
    """
    found: list[tuple[str, str]] = []
    raw_files_set = set(raw_files)
    if not raw_files_set:
        return found

    manifests: dict[str, dict] = {}
    for loc in RAW_UPLOAD_LOCATIONS:
        if data_io.exists(storage_location=loc, filename="ingestion_manifest.json"):
            manifests[loc] = data_io.load_json(
                storage_location=loc, filename="ingestion_manifest.json", verbose=False
            ) or {}
        else:
            manifests[loc] = {}

    for fn in raw_files_set:
        for loc in RAW_UPLOAD_LOCATIONS:
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
@login_required
def affected_studies_for_collection():
    """Return the studies that reference a given collection_id. Used by the
    delete-collection confirmation dialog to show what will be refreshed."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403
    collection_id = (request.args.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400
    return jsonify({"studies": _affected_studies_for_collection(collection_id)})




@management_bp.route('/api/manage/collections/delete', methods=['POST'])
@login_required
def delete_collection():
    """Dispatch a collection_delete Cloud Task. The actual delete (which loads
    and rewrites the 1+ GB collections_recoded.parquet) runs on the task-runner
    so the data-hub doesn't risk OOM or timeout. The UI polls /api/status for
    completion and reads the final result from the task's emitted data payload.
    """
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized - Admin only"}), 403

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
        return jsonify({
            "status": "started",
            "collection_id": collection_id,
            "message": msg,
        })
    return jsonify({"status": "error", "message": msg}), 409







@management_bp.route('/api/manage/collections', methods=['GET'])
@login_required
def list_collections():
    
    if not current_user.is_admin():
         return jsonify([])

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
@login_required
def save_collection_annotation():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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

        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    if not current_user.is_admin():
         return jsonify({"error": "Unauthorized"}), 403

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
        
    
    # 2. Get Queue Lengths
    scrape_queue_len = 0
    annotate_queue_len = 0
    
    if data_io.exists(storage_location='cache', filename='to_scrape.json'):
        try:
            q = data_io.load_json(storage_location='cache', filename='to_scrape.json')
            if isinstance(q, list): scrape_queue_len = len(q)
        except Exception:
            pass
        
    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)
        
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
        "annotate_queue_len": annotate_queue_len,
        "consolidate_stats": {
            **consolidate_entry,
            **processes.get("consolidate_enrichment", {}).get("data", {})
        } or None,
        "consolidate_auto_armed": bool(consolidate_entry.get("auto_armed")),
        "consolidate_auto_armed_auto_refresh": bool(consolidate_entry.get("auto_armed_auto_refresh")),
        "consolidate_pipeline_active": pipeline_active,
        "workers_blocking_consolidate": _workers_blocking_consolidate(),
        "scraper_last_success": process_stats.get("queue_scraper", {}).get("last_success"),
        "annotator_last_success": process_stats.get("queue_annotator", {}).get("last_success"),
    })






@management_bp.route('/api/manage/enrichment/empty_queue/<queue_type>', methods=['POST'])
@login_required
def empty_enrichment_queue(queue_type):
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
        
    try:
        if queue_type == "scrape":
            if data_io.exists(storage_location='cache', filename='to_scrape.json'):
                data_io.remove(storage_location='cache', filename='to_scrape.json')
            load_process_stats()
            if "scrape_queue_len" in process_stats.get("queue_scraper", {}):
                process_stats["queue_scraper"]["scrape_queue_len"] = 0
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
@login_required
def queue_voted_videos():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    try:
        from web_interface.security import user_manager
        
        # 1. Gather all votes across all users
        all_votes = {} # dict of collection_id -> set of periods
        for user in user_manager.users.values():
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

        new_scrape = []
        new_annotate = []

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
                    
                    # Same logic from user request
                    if not is_scraped and not scrape_fail:
                        new_scrape.append(item)
                    elif is_scraped and not is_annotated and not annotated_fail:
                        new_annotate.append(item)
                else:
                    # Item not in enrichment status -> hasn't been scraped yet
                    new_scrape.append(item)
        else:
            # No enrichment file -> everything needs scraping
            new_scrape = list(target_item_ids)

        new_scrape = list(set(new_scrape))
        new_annotate = list(set(new_annotate))

        # 4. Append to Queues
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

        if new_scrape:
             # We store scrape targets globally 
             current_scrape = load_queue("to_scrape.json")
             current_scrape.extend(new_scrape)
             save_queue("to_scrape.json", current_scrape)

        if new_annotate:
             current_annotate = load_queue("to_annotate.json")
             current_annotate.extend(new_annotate)
             save_queue("to_annotate.json", current_annotate)

        return jsonify({
            "status": "success", 
            "added_to_scrape": len(new_scrape),
            "added_to_annotate": len(new_annotate)
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@login_required
def calculate_to_scrape():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    study_name = data.get("study_name")
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
            
            if 'scrape_fail' in study_status.columns:
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

        # Append target payload to global scrape queue
        current_queue = []
        if data_io.exists(storage_location="cache", filename="to_scrape.json"):
            try:
                q = data_io.load_json(storage_location="cache", filename="to_scrape.json")
                if isinstance(q, list): current_queue = q
            except Exception:
                pass
                
        current_queue.extend(unscraped_videos)
        current_queue = list(set(current_queue))
        
        data_io.save_json(
            data=current_queue,
            storage_location="cache",
            filename="to_scrape.json"
        )

        return jsonify({"status": "success", "videos_to_scrape": len(current_queue)})

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@login_required
def calculate_to_annotate():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    study_name = data.get("study_name")
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

            if 'video_duration' in df_study.columns:
                study_videos = df_study[['item_id', 'video_duration']].copy()
            else:
                study_videos = df_study[['item_id']].copy()
                
            study_status = study_videos.merge(df_status, on='item_id', how='left')
            
            is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
            
            if 'annotated_ok' in study_status.columns:
                not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)
            else:
                not_annotated_ok = True
                
            if 'annotated_fail' in study_status.columns:
                not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
            else:
                not_annotated_fail = True
                
            unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail
            
            if 'video_duration' in study_status.columns:
                max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
                duration_ok = (study_status['video_duration'] < max_dur) | pd.isna(study_status['video_duration'])
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

        return jsonify({"status": "success", "videos_to_annotate": len(current_queue)})

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@login_required
def api_consolidate_enrichment():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
@login_required
def api_consolidate_disarm():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    was_armed = bool(entry.get("auto_armed"))
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()
    return jsonify({"status": "disarmed", "was_armed": was_armed})



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
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
    })


@management_bp.route('/api/manage/schema/reload', methods=['POST'])
@login_required
def reload_schema():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        global fyp_cf
        fyp_cf = load_var_schema(fyp_cf, verbose=False)
        return jsonify({"status": "success", "message": "Variable schema reloaded successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/inter_coder_reliability', methods=['GET'])
@login_required
def get_inter_coder_reliability():
    if not (current_user.is_admin()):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        results = calculate_inter_coder_reliability()
        if "error" in results:
             return jsonify(results), 400
        return jsonify(results)
    except Exception as e:
        print(f"Error calculating reliability: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/ingestion/sources', methods=['GET'])
@login_required
def get_ingestion_sources():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/fetch_aio', methods=['POST'])
@login_required
def fetch_aio_data():
    """Trigger download of recent AIO donations and metadata from AWS."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
@login_required
def upload_ingestion_file():
    """Upload one or more raw files with optional collection_id and tags metadata.

    Accepts form fields:
        files: one or more files (also accepts legacy single 'file' key)
        raw_path: storage location key (e.g. 'ddp_raw')
        collection_id: explicit collection ID (used when collection_id_mode is 'single')
        collection_id_mode: 'single' | 'per_file' (default 'per_file')
        tags: JSON-encoded list of tag strings
    """
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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

        return jsonify({
            "status": "success",
            "message": f"{len(uploaded)} file(s) uploaded.",
            "files": uploaded,
        })
    except Exception as e:
        print(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/refresh-collection-metadata', methods=['POST'])
@login_required
def refresh_collection_metadata():
    """Regenerate _metadata.parquet from scratch using all events."""
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    from fyp.fyp_config import COLLECTION_METADATA_REFRESH_SCRIPT

    success, msg = start_process(
        "collection_metadata_refresh",
        COLLECTION_METADATA_REFRESH_SCRIPT,
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409



@management_bp.route('/api/manage/ingestion/refresh', methods=['POST'])
@login_required
def refresh_ingestion_collection():
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

    from fyp.fyp_config import INGEST_REFRESH_SCRIPT

    success, msg = start_process("ingest_refresh", INGEST_REFRESH_SCRIPT)
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409


@management_bp.route('/api/manage/ingestion/clear_pending', methods=['POST'])
@login_required
def clear_pending_uploads():
    """Drop every pending upload across every registered ingester: delete each
    file from its raw_path storage and reset its manifest to an empty dict.
    Lightweight (no parquet I/O), so safe to run inline on the data-hub.
    """
    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""

    from fyp.organize_datasets import COLLECTIONS_LABEL

    if not current_user.is_admin():
        return jsonify({"error": "Unauthorized"}), 403

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
