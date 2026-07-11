"""Timeline cache building and read path.

Pure moves from web_interface/data_service.py (Phase 7c)."""

import json

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL, create_collection_unified_dataset
from fyp.utils import ACTIVITY_TYPE_MAP, ENGAGEMENT_TYPES

from .. import explorer_backend as explorer
from .study_data import get_study_sidecar
from .user_variables import load_schema_metadata

# --- Explorer State ---


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


def get_timeline_covered_vars(collection_id, interval='day'):
    """Variables a cached timeline parquet was aggregated with, or None.

    Read from the ``timeline_<cid>_<interval>.aggvars.json`` sidecar written at
    generation time. None means the cache predates coverage tracking.
    """
    fname = f"timeline_{collection_id}_{interval}.aggvars.json"
    try:
        if data_io.exists(storage_location="cache", filename=fname):
            payload = data_io.load_json(storage_location="cache", filename=fname)
            vars_list = payload.get("vars")
            if isinstance(vars_list, list):
                return set(str(v) for v in vars_list)
    except Exception:
        pass
    return None






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
                continue
            # Per-var coverage: a per-user include beyond the vars this parquet
            # was aggregated with has no data until we re-aggregate. Coverage is
            # tracked in a sidecar (not by sniffing columns) so a variable that
            # is absent from the collection's data doesn't trigger a
            # regeneration loop. Missing sidecar => cache predates per-user
            # includes and covers whatever it was built with; fall back to a
            # column sniff for the *requested* vars only.
            covered = get_timeline_covered_vars(collection_id, interval)
            if covered is None:
                # Pre-sidecar cache: it was built from the then-global timeline
                # list, so treat the current global set (plus any column
                # actually present) as covered — only a genuinely new include
                # should force a regeneration.
                meta_fallback = load_schema_metadata({})
                covered = set(meta_fallback.get('timeline_priority', []))
                covered.add('machine_state')
                covered.update(
                    c[: -len('_valid')] for c in sample_df.columns if c.endswith('_valid'))
            uncovered = [v for v in viz_vars if v not in covered]
            if uncovered:
                if verbose:
                    print(f"    [TIMELINE] Cache for {collection_id}/{interval} lacks {uncovered}; regenerating.")
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

    # Regenerate with the UNION of the requested vars and everything the prior
    # cache covered, so one user's per-user include never evicts another's —
    # the study-wide cache only ever grows a superset of aggregated columns.
    prior_covered = get_timeline_covered_vars(collection_id, 'day')
    if prior_covered:
        viz_vars = list(viz_vars) + [v for v in sorted(prior_covered) if v not in viz_vars]

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
        # Coverage sidecar: which vars this parquet was aggregated with. Read by
        # get_timeline_covered_vars so a variable absent from the data doesn't
        # look uncovered and trigger a regeneration loop.
        try:
            data_io.save_json(
                data={"vars": list(viz_vars)},
                storage_location="cache",
                filename=f"timeline_{collection_id}_{interval}.aggvars.json",
            )
        except Exception as e:
            print(f"    [TIMELINE] Failed to write aggvars sidecar for {collection_id}/{interval}: {e}")
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
                      study: str | None = None,
                      extra_vars: list[str] | None = None):
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
        extra_vars: Per-user includes beyond the global timeline set. Known
            schema variables are appended (canonical order) to the aggregation
            set; an uncovered one triggers a one-time cache re-aggregation with
            the union, growing the study-wide cache for every user.
    """

    if 'var_schema' not in fyp_cf:
        print("ERROR: var_schema missing")
        return {}

    # Load Schema Metadata
    meta = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])
    schema_map = meta.get('schema_map', {})
    global_vars = list(viz_vars)
    if extra_vars:
        known = set(meta.get('all_variables_order', []))
        wanted = {v for v in extra_vars if v in known and v not in viz_vars}
        # Canonical order comes from all_variables_order, not request order.
        viz_vars = viz_vars + [v for v in meta.get('all_variables_order', []) if v in wanted]

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
    # For the per-user "Customize variables" panel: the uncomposed global list
    # and the vars already covered by the cached parquet (an include outside
    # this set will pay a one-time re-aggregation on first load).
    # machine_state is a synthetic always-on series prepended server-side; it
    # belongs to the global set so per-user composition can never drop it.
    if 'machine_state' not in global_vars:
        global_vars = ['machine_state'] + global_vars
    result["variables_global"] = global_vars
    covered = get_timeline_covered_vars(collection_id, interval)
    result["variables_covered"] = sorted(covered) if covered is not None else list(variables.keys())
    result["all_variables_order"] = meta.get('all_variables_order', [])
    result["schema_map_lite"] = {
        v: {k: schema_map[v][k] for k in ("display_name", "section", "description") if k in schema_map[v]}
        for v in result["all_variables_order"] if v in schema_map
    }

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

