"""Study-stats and date-window helpers shared by the management endpoints.

Pure moves from ``web_interface/routes/management_routes.py`` (Phase 7b) —
event-window filtering, study stats calculation/estimation, design feedback,
and consolidation-staleness evaluation. ``_calculate_stats``,
``_compute_universe_enrichment`` and the window filters are also consumed by
the ``run_study_refresh`` worker (via the ``management_routes`` shim).
"""

import time as _time
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    SAMPLE_NO_CAP,
    create_study_recoded_dataset,
    parse_sample_threshold,
)

from ..process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
)

LARGE_STUDY_THRESHOLD = 500_000
SPARSE_CELL_MIN_ACTIVITIES = 10




def get_study_activity_cap() -> int:
    """Return the hard cap on activities per study ([studies] max_activities).

    Falls back to LARGE_STUDY_THRESHOLD when the config section is absent
    (pre-existing installs whose config.toml predates the [studies] section).
    """

    try:
        from fyp.fyp_config import fyp_cf
        return int(fyp_cf.get("studies", {}).get("max_activities", LARGE_STUDY_THRESHOLD))
    except Exception:
        return LARGE_STUDY_THRESHOLD




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

    cap = get_study_activity_cap()
    if total_activities > cap:
        issues.append({
            "severity": "error",
            "code": "too_big",
            "message": (
                f"This design would contain ~{total_activities:,} activities; the cap is "
                f"{cap:,}. Narrow the date range, drop collections, or enable sampling "
                f"before saving."
            ),
        })
    elif total_activities > 0.6 * cap:
        issues.append({
            "severity": "warn",
            "code": "large",
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
                f"{sparse_cells:,} of {total_cells:,} day × collection cells have fewer than "
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
                    f"qualifying day × collection cells."
                ),
            })
        if n_down > 0:
            issues.append({
                "severity": "warn",
                "code": "collections_downsampled",
                "message": (
                    f"Sampling downsampled {n_down:,} collection(s) that had more than {max_cells} "
                    f"qualifying day × collection cells."
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

    _t_phase = _time.perf_counter()
    stats = compute_study_dataset_stats(df_study, df_status, selected)
    _t_count = _time.perf_counter() - _t_phase
    _t_stats_total = _time.perf_counter() - _t_total
    print(
        f"[STATS][TIMING] study={study_name} "
        f"status_load={_t_status:.2f}s recode={_t_recode:.2f}s "
        f"count={_t_count:.2f}s total={_t_stats_total:.2f}s"
    )

    return stats, df_study, df_status




def compute_study_dataset_stats(df_study: pd.DataFrame, df_status: pd.DataFrame | None,
                                selected: list) -> dict:
    """Count a study dataset's activities/videos and their enrichment status.

    The single definition of the per-study stats dict persisted to
    ``studies.json`` — used by both the study refresh (via ``_calculate_stats``)
    and the bulk recode refresh, so the two writers can never diverge in keys.

    Filters to play/observe within each collection's event window so the
    displayed "included" counts use the same definition as the per-collection
    metadata (personas.total_events / active_days) and the "potential" column
    on the right of the modal. Without this filter the included Activities
    would include likes, shares, search, follow, and events outside the
    persona window — making "included" exceed "potential".

    Args:
        df_study: The study's recoded dataset.
        df_status: Projected enrichment_status frame, or ``None``.
        selected: The study's selected collection ids (event-window lookup).

    Returns:
        Stats dict with total_activities / unique_videos / scraped_videos /
        annotated_videos / activities_scraped / activities_annotated /
        unique_collections / active_days.
    """
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

    return {
        "total_activities": int(total_activities),
        "unique_videos": int(unique_videos),
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "activities_scraped": activities_scraped,
        "activities_annotated": activities_annotated,
        "unique_collections": int(unique_collections),
        "active_days": active_days,
    }




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




# Per-frame column picks on the preview cells table: (activity count, distinct-item
# count, item-count column for the scraped subset, ... for the annotated subset).
# Annotation implies scraping (an item cannot be annotated without its video), so the
# scraped subset of the 'annotated' frame is the frame itself.
_CELLS_FRAME_COLS = {
    "off":        ("n_act", "n_items", "n_items_scraped", "n_items_annotated"),
    "activities": ("n_act", "n_items", "n_items_scraped", "n_items_annotated"),
    "events":     ("n_act", "n_items", "n_items_scraped", "n_items_annotated"),
    "scraped":    ("n_act_scraped", "n_items_scraped", "n_items_scraped", "n_items_annotated"),
    "annotated":  ("n_act_annotated", "n_items_annotated", "n_items_annotated", "n_items_annotated"),
}
_CELLS_FRAME_ACT_SUB = {
    # (scraped-activities column, annotated-activities column) within each frame.
    "off":        ("n_act_scraped", "n_act_annotated"),
    "activities": ("n_act_scraped", "n_act_annotated"),
    "events":     ("n_act_scraped", "n_act_annotated"),
    "scraped":    ("n_act_scraped", "n_act_annotated"),
    "annotated":  ("n_act_annotated", "n_act_annotated"),
}
# Which coll_stats calibration pair applies to each item column.
_CELLS_ITEM_RATIO = {
    "n_items": ("u_items", "sum_cell_items"),
    "n_items_scraped": ("u_items_scraped", "sum_cell_items_scraped"),
    "n_items_annotated": ("u_items_annotated", "sum_cell_items_annotated"),
}




def _cells_for_selection(cells: pd.DataFrame | None, study_config: dict) -> pd.DataFrame | None:
    """Filter the corpus cells table to a study definition's collections."""

    if cells is None or cells.empty:
        return None
    selected = {str(c) for c in (study_config.get("SELECTED_COLLECTIONS") or [])}
    if not selected:
        return None
    out = cells[cells["collection_id"].isin(selected)]
    return out if not out.empty else None




def _estimate_items(df: pd.DataFrame, frac: pd.Series, item_col: str,
                    coll_stats: pd.DataFrame | None) -> int:
    """Estimate study-level distinct items from per-cell item counts.

    Per-cell expected items scale with the kept-row fraction, but summing cells
    double-counts items recurring across a collection's days. Per collection,
    with U true uniques whose per-cell occurrences total S, keeping item-mass s
    misses an average item with probability ~(1 - s/S)^(S/U), so the expected
    distinct count is U * (1 - (1 - s/S)^(S/U)) — exact at s == S, linear (~s)
    when s is small, saturating in between.
    """

    if item_col not in df.columns:
        return 0
    est = df[item_col].to_numpy(dtype="float64") * frac.to_numpy(dtype="float64")
    per_coll = pd.Series(est, index=df["collection_id"].to_numpy()).groupby(level=0).sum()

    if coll_stats is None or coll_stats.empty:
        return int(round(float(per_coll.sum())))
    u_col, s_col = _CELLS_ITEM_RATIO[item_col]
    if u_col not in coll_stats.columns or s_col not in coll_stats.columns:
        return int(round(float(per_coll.sum())))

    cs = coll_stats.set_index("collection_id")
    u = pd.to_numeric(cs[u_col], errors="coerce")
    s_full = pd.to_numeric(cs[s_col], errors="coerce")
    suffix = item_col[len("n_items"):]
    sh_name = "u_items_shared" + suffix
    u_shared = pd.to_numeric(cs[sh_name], errors="coerce") if sh_name in cs.columns else None

    def _saturate(s_kept: float, u_c: float, s_c: float) -> float:
        if u_c <= 0 or s_c <= 0:
            return s_kept
        p = min(max(s_kept / s_c, 0.0), 1.0)
        return u_c * (1.0 - (1.0 - p) ** (s_c / u_c))

    # Stage 1 (within collections): saturate each collection's estimated item
    # mass to its true distinct count, then split that into items exclusive to
    # the collection (which can never double-count) and its share of the
    # cross-collection pool.
    excl_total = 0.0
    shared_mass = 0.0
    for cid, s_kept in per_coll.items():
        u_c = float(u.get(cid) or 0.0)
        est_c = _saturate(float(s_kept), u_c, float(s_full.get(cid) or 0.0))
        sh_c = float(u_shared.get(cid) or 0.0) if u_shared is not None else 0.0
        sh_frac = min(sh_c / u_c, 1.0) if u_c > 0 else 0.0
        excl_total += est_c * (1.0 - sh_frac)
        shared_mass += est_c * sh_frac

    # Stage 2 (across collections): saturate only the shared pool against the
    # "__shared__" calibration row. Exact at full selection; a uniform-overlap
    # approximation within the shared pool for subsets.
    if "__shared__" in cs.index and shared_mass > 0:
        shared_mass = _saturate(shared_mass, float(u.get("__shared__") or 0.0),
                                float(s_full.get("__shared__") or 0.0))
    return int(round(excl_total + shared_mass))




def _estimate_from_cells(cells: pd.DataFrame | None, coll_stats: pd.DataFrame | None,
                         study_config: dict) -> tuple[dict, list, int, int, dict | None]:
    """Approximate the study sampling counts from the corpus preview cells.

    Cell-level port of _estimate_from_prepared: the sampler's grain IS the
    (collection, day) cell, so the two-stage replay is pure arithmetic here —
    per-cell kept counts are min(cell_n, max_events), and the seeded Stage-2
    cell cap draws over a canonically sorted cell list. Activity-level totals
    are exact except under Stage-2 downsampling (where WHICH cells survive is
    a seeded draw, as it always was); item-level figures (unique videos and the
    scrape/annotation breakdown) are estimates calibrated per collection.

    Returns:
        Tuple (stats, included_per_day, sparse_cells, total_cells, sampling_report)
        — same shape as _estimate_from_prepared.
    """

    empty = {
        "total_activities": 0, "unique_videos": 0, "scraped_videos": 0,
        "annotated_videos": 0, "activities_scraped": 0, "activities_annotated": 0,
        "unique_collections": 0, "active_days": 0,
    }

    df = _cells_for_selection(cells, study_config)
    if df is None:
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
    mask = (df["day"] >= pd.Timestamp(start_date)) & (df["day"] <= pd.Timestamp(end_date))
    df = df[mask.to_numpy()]
    if df.empty:
        return empty, [], 0, 0, None

    frame_setting = (study_config.get("SAMPLE_FRAME") or "off").strip()
    act_col, item_col, item_scraped_col, item_annotated_col = _CELLS_FRAME_COLS.get(
        frame_setting, _CELLS_FRAME_COLS["off"])
    act_scraped_col, act_annotated_col = _CELLS_FRAME_ACT_SUB.get(
        frame_setting, _CELLS_FRAME_ACT_SUB["off"])

    # Cells empty under this frame don't exist as cells (row-level filtering
    # removed them entirely in the frame-based path).
    df = df[df[act_col].to_numpy() > 0]
    if df.empty:
        return dict(empty), [], 0, 0, None

    min_events = parse_sample_threshold(study_config.get("MIN_ACTIVITY_COUNT_PER_GROUP"), 30)
    max_events = parse_sample_threshold(study_config.get("MAX_ACTIVITY_COUNT_PER_GROUP"), 50, uncapped=True)
    min_cells = parse_sample_threshold(study_config.get("MIN_GROUP_COUNT_PER_COLLECTION"), 20)
    max_cells = parse_sample_threshold(study_config.get("MAX_GROUP_COUNT_PER_COLLECTION"), 200, uncapped=True)

    sampling_report = None

    if frame_setting == "off":
        kept_cells = df
        kept_n = df[act_col]
    else:
        # Stage 1: qualifying cells need >= min_events rows.
        qf = df[df[act_col].to_numpy() >= min_events]
        if qf.empty:
            sampling_report = {
                "n_excluded_collections": int(df["collection_id"].nunique()),
                "n_downsampled_collections": 0,
                "min_cells_per_collection": min_cells,
                "max_cells_per_collection": max_cells,
            }
            return dict(empty), [], 0, 0, sampling_report

        cells_per_coll = qf.groupby("collection_id").size()
        sampling_report = {
            "n_excluded_collections": int((cells_per_coll < min_cells).sum()),
            "n_downsampled_collections": int((cells_per_coll > max_cells).sum()),
            "min_cells_per_collection": min_cells,
            "max_cells_per_collection": max_cells,
        }

        # Stage 2: drop collections with < min_cells qualifying cells; cap the
        # rest at max_cells cells via the seeded draw (canonical sort keeps the
        # draw deterministic for a given corpus + selection).
        kept_colls = set(cells_per_coll[cells_per_coll >= min_cells].index)
        qf = qf[qf["collection_id"].isin(kept_colls)]
        if max_cells < SAMPLE_NO_CAP and not qf.empty:
            qf = qf.sort_values(["collection_id", "day"], kind="mergesort").copy()
            rng = np.random.RandomState(42)
            qf["_r"] = rng.random(len(qf))
            qf["_rank"] = qf.groupby("collection_id")["_r"].rank(method="first")
            qf = qf[qf["_rank"] <= max_cells]

        kept_cells = qf
        # Stage 1 cap: at most max_events rows kept per surviving cell.
        kept_n = np.minimum(qf[act_col], max_events) if not qf.empty else qf[act_col]

    if kept_cells.empty or int(pd.Series(kept_n).sum()) == 0:
        return dict(empty), [], 0, 0, sampling_report

    kept_n = pd.Series(np.asarray(kept_n, dtype="int64"), index=kept_cells.index)
    frac = kept_n / kept_cells[act_col]

    stats = {
        "total_activities": int(kept_n.sum()),
        "unique_videos": _estimate_items(kept_cells, frac, item_col, coll_stats),
        "scraped_videos": _estimate_items(kept_cells, frac, item_scraped_col, coll_stats),
        "annotated_videos": _estimate_items(kept_cells, frac, item_annotated_col, coll_stats),
        "activities_scraped": int(round(float((kept_cells[act_scraped_col] * frac).sum()))),
        "activities_annotated": int(round(float((kept_cells[act_annotated_col] * frac).sum()))),
        "unique_collections": int(kept_cells["collection_id"].nunique()),
        "active_days": int(kept_cells["day"].nunique()),
    }

    day_counts = kept_n.groupby(kept_cells["day"].to_numpy()).sum()
    included_per_day = [
        {"date": pd.Timestamp(d).date().isoformat(), "count": int(c)}
        for d, c in day_counts.sort_index().items()
    ]

    total_cells = int(len(kept_cells))
    sparse_cells = int((kept_n < SPARSE_CELL_MIN_ACTIVITIES).sum())

    return stats, included_per_day, sparse_cells, total_cells, sampling_report




def _universe_from_cells(cells: pd.DataFrame | None, study_config: dict) -> tuple[int, int, dict, bool]:
    """Compute the pre-sampling potentials and universe mosaic from the preview cells.

    Cell-level port of _universe_from_prepared: potentials cover every
    in-event-window activity regardless of the date range; the universe mosaic
    additionally applies the date window. All three universe figures are exact
    (activity-level sums).
    """

    universe = {"activities": 0, "scraped": 0, "annotated": 0}
    df = _cells_for_selection(cells, study_config)
    if df is None:
        return 0, 0, universe, False

    win = df[df["n_act_inwin"].to_numpy() > 0]
    if win.empty:
        return 0, 0, universe, False

    potential_activities = int(win["n_act_inwin"].sum())
    potential_active_days = int(win["day"].nunique())

    start_s = (study_config.get("START_DATE") or "").strip()
    end_s = (study_config.get("END_DATE") or "").strip()
    uni = win
    if start_s or end_s:
        m = win["day"].notna()
        if start_s:
            m &= win["day"] >= pd.Timestamp(start_s)
        if end_s:
            m &= win["day"] <= pd.Timestamp(end_s)
        uni = win[m.to_numpy()]

    universe = {
        "activities": int(uni["n_act_inwin"].sum()),
        "scraped": int(uni["n_act_inwin_scraped"].sum()),
        "annotated": int(uni["n_act_inwin_annotated"].sum()),
    }
    return potential_activities, potential_active_days, universe, True




def _evaluate_consolidation_staleness() -> dict:
    """Return impact/freshness for the latest consolidation, clearing stale impact.

    Reloads process_stats from GCS, inspects the stored consolidation_impact,
    and removes it when every downstream process has run successfully since the
    impact timestamp. Returns a dict with ``has_impact``, ``impact``, and a
    per-process ``processes`` map — safe to call from any endpoint that needs
    to reason about whether the consolidation impact panel should be visible.

    Each ``processes`` entry carries a ready-to-render ``note`` and a ``gates``
    flag. Only gating processes decide whether the impact is resolved; the
    corpus-global steps (embeddings, semantic map, sessions) report staleness
    for their cards but never pin the impact, because the pipeline can
    legitimately skip them — a local-only embedding backend is not dispatchable
    from Cloud Run, and an impact waiting on a step that will never run would
    never clear.
    """
    load_process_stats()

    consolidate_entry = process_stats.get("consolidate_enrichment", {})
    impact = consolidate_entry.get("consolidation_impact")

    if not impact or not impact.get("timestamp"):
        return {"has_impact": False, "impact": None, "processes": {}}

    impact_ts = impact["timestamp"]
    affected_studies = impact.get("affected_study_names", [])
    affected_collections = impact.get("affected_collection_ids", [])
    new_annotations = int(impact.get("new_annotation_item_count") or 0)

    def _n(count: int, singular: str, plural: str) -> str:
        return f"{count:,} {singular if count == 1 else plural}"

    def _studies(n: int) -> str:
        return f"({_n(n, 'study', 'studies')} need{'s' if n == 1 else ''} refresh)"

    def _collections(n: int) -> str:
        return f"({_n(n, 'collection', 'collections')} need{'s' if n == 1 else ''} refresh)"

    # One entry per card on the Dataset Assembly page. ``count`` is how much the
    # step would process and drives both the badge and its visibility; it
    # mirrors the condition _build_downstream_pipeline uses to decide whether to
    # dispatch the step at all, so a badge appears exactly when the next
    # Consolidate & Refresh would run it. ``affected`` stays the concrete
    # name list where there is one (it is what the impact panel enumerates).
    downstream = {
        "recode_refresh_studies": {
            "label": "Study Definitions",
            "affected": affected_studies,
            "count": len(affected_studies),
            "note": _studies,
        },
        "meta_refresh_groups": {
            "label": "Explore Metadata",
            "affected": affected_studies,
            "count": len(affected_studies),
            "note": _studies,
        },
        "timelines_refresh": {
            "label": "Timelines",
            "affected": affected_collections,
            "count": len(affected_collections),
            "note": _collections,
        },
        "pca_refresh": {
            "label": "Correlations",
            "affected": affected_studies,
            "count": len(affected_studies),
            "note": _studies,
        },
        # Corpus-global steps: display-only (gates=False), see the docstring.
        # All three key off new annotations; sessions also off new activity.
        "embeddings_refresh": {
            "label": "Semantic Embeddings",
            "affected": [],
            "count": new_annotations,
            "note": lambda n: f"({_n(n, 'new annotation', 'new annotations')} to embed)",
            "gates": False,
        },
        "video_map_refresh": {
            "label": "Semantic Map",
            "affected": [],
            "count": new_annotations,
            "note": lambda n: f"({_n(n, 'new embedding', 'new embeddings')} to map)",
            "gates": False,
        },
        "sessions_refresh": {
            "label": "Sessions",
            "affected": affected_collections,
            "count": len(affected_collections) or new_annotations,
            "note": (_collections if affected_collections
                     else lambda n: "(new annotations — refresh needed)"),
            "gates": False,
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
        if not info["count"]:
            stale = False
        else:
            stale = not last_success or last_success < impact_ts
        result[proc_name] = {
            "stale": stale,
            "label": info["label"],
            "affected": info["affected"],
            "count": info["count"],
            "note": info["note"](info["count"]) if stale else "",
            "gates": info.get("gates", True),
        }
        if stale and info.get("gates", True):
            all_fresh = False

    if all_fresh:
        # Reload right before mutating. This runs on every browser poll, and
        # save_process_stats writes the WHOLE consolidate entry from this
        # process's memory: the copy loaded at the top of this function is
        # already seconds old by here (one status read per pipeline step), and a
        # write by another instance in that window would be put back to its
        # pre-write state. 2026-09-03: a stats poll 0.7 s after "Consolidate"
        # was armed erased the fresh `auto_armed` flag this way, and the armed
        # refresh never fired.
        load_process_stats()
        fresh_entry = process_stats.get("consolidate_enrichment", {})
        if fresh_entry.pop("consolidation_impact", None) is not None:
            process_stats["consolidate_enrichment"] = fresh_entry
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




def _evaluate_version_promotion_staleness() -> dict:
    """Return staleness for the last preferred-annotation-version promotion.

    Promoting a version rebuilds the global active annotation parquet, but
    per-study recoded datasets stay on the previous version until
    ``recode_refresh_studies`` runs (which forks the meta/pca/timelines
    cascade, so that single process is a sufficient freshness proxy). The
    promote endpoint writes a ``promotion_impact`` marker into process_stats;
    this evaluator reports it and clears it once the refresh has succeeded
    after the promotion. The signal is deliberately global — studies pinned to
    a specific annotation version never read the active parquet, but tracking
    them here isn't worth the coupling.

    Returns:
        ``{has_impact, impact, stale}`` — ``stale`` is True while a study
        refresh is still owed.
    """
    load_process_stats()

    entry = process_stats.get("annotation_versions", {})
    impact = entry.get("promotion_impact")
    if not impact or not impact.get("timestamp"):
        return {"has_impact": False, "impact": None, "stale": False}

    last_success = process_stats.get("recode_refresh_studies", {}).get("last_success")
    stale = not last_success or last_success < impact["timestamp"]
    if not stale:
        entry.pop("promotion_impact", None)
        process_stats["annotation_versions"] = entry
        save_process_stats()
        return {"has_impact": False, "impact": impact, "stale": False}

    return {"has_impact": True, "impact": impact, "stale": True}
