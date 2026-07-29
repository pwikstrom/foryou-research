"""Correlations tab backend logic.

Builds the metadata / scatter / correlation-matrix payloads served by
``routes/api_correlations_routes.py`` (which stays a thin auth + request
layer). All tunable thresholds live in the ``[correlations]`` section of
``config/config.toml``; the analysis-side knobs (minimum group size,
interpretation cutoff) are read from the same section by ``fyp/analysis/pca.py``.
"""

import math
import re

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.logging_setup import get_logger
from fyp.recode_variables import get_factors_and_features_from_var_schema

from .study_data import load_display_id_map
from .user_variables import load_schema_metadata

logger = get_logger(__name__)


# Declarative manifest of the stat views the tab offers (semantic-space
# ``_OVERLAYS`` idiom). Served in the metadata payload; the frontend builds
# the view toggle from it and maps ``key`` to a renderer, so a later phase
# adds a view by appending an entry here + registering a renderer in
# ``correlations.js`` — no template or control-flow changes.
STAT_VIEWS = [
    {"key": "scatter", "label": "Scatter"},
    {"key": "heatmap", "label": "Heatmap"},
]

_CORR_DEFAULTS = {
    "min_variance_pct": 5.0,
    "max_scatter_points": 5000,
    "factor_value_limit": 500,
    "correlation_method": "pearson",
}

# Deterministic seed for the scatter point-cap sample.
SCATTER_SAMPLE_SEED = 0

_VALID_CORRELATION_METHODS = ("pearson", "spearman")






def corr_setting(key):
    """Return a ``[correlations]`` config value, falling back to the default."""
    return fyp_cf.get("correlations", {}).get(key, _CORR_DEFAULTS[key])






def correlation_method() -> str:
    """The configured matrix correlation method, guarded to a valid value."""
    method = str(corr_setting("correlation_method")).lower()
    if method not in _VALID_CORRELATION_METHODS:
        logger.warning(f"Invalid [correlations] correlation_method '{method}', using pearson")
        return "pearson"
    return method






def format_week_value(value) -> str:
    """Normalise a week label to zero-padded ``YYYY-WW``; pass other values through.

    Accepts both ``2025-3`` and ``2025-W3`` style labels.
    """
    v_str = str(value)
    parts = v_str.split('-')
    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4:
        week = parts[1]
        if week.lower().startswith('w'):
            week = week[1:]
        if week.isdigit():
            return f"{parts[0]}-{int(week):02d}"
    return v_str






def apply_factor_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Return the rows of ``df`` matching the per-factor value lists in ``filters``.

    Date-like columns are compared on their ``YYYY-MM-DD`` prefix and week
    columns through :func:`format_week_value`, mirroring how the values were
    presented to the client in ``factor_values``.
    """
    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col not in df.columns:
            continue
        is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
        if is_dt or "date" in col.lower():
            mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
        elif "week" in col.lower():
            mask &= df[col].map(format_week_value).isin([str(v) for v in vals])
        else:
            mask &= df[col].astype(str).isin(vals)
    return df[mask].copy()






def load_interpretations(study: str) -> dict:
    """Load ``{study}_comp_interpretations.json`` from cache, or {} if absent."""
    try:
        inter_path = f"{study}_comp_interpretations.json"
        if data_io.exists(storage_location="cache", filename=inter_path):
            loaded = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
            if loaded:
                return loaded
    except Exception as e:
        logger.warning(f"Error loading interpretations for {study}: {e}")
    return {}






def filter_components_by_variance(numeric_cols, interpretations):
    """
    Filters a list of PCA component names based on their explained variance.
    Always keeps non-PCA components and the PCA component with the highest variance,
    then any other PCA components >= the configured [correlations] min_variance_pct.
    """
    if not interpretations or not numeric_cols:
        return numeric_cols

    threshold = float(corr_setting("min_variance_pct"))
    pca_cols_with_var = []
    non_pca_cols = []

    # Match PCA components (e.g., ends with _C and a number like _C1)
    pca_pattern = re.compile(r'_C\d+$')

    # Extract variances for the columns that have them
    for col in numeric_cols:
        if pca_pattern.search(col):
            var_val = 0.0
            if col in interpretations and 'explained_variance_pct' in interpretations[col]:
                try:
                    var_val = float(interpretations[col]['explained_variance_pct'])
                except (ValueError, TypeError):
                    pass
            pca_cols_with_var.append((col, var_val))
        else:
            # Not a PCA component, always keep it
            non_pca_cols.append(col)

    if not pca_cols_with_var:
        return numeric_cols

    # Sort descending by variance
    pca_cols_with_var.sort(key=lambda x: x[1], reverse=True)

    # Always keep the top one
    top_col = pca_cols_with_var[0][0]
    filtered_cols = [top_col]

    # Keep others that meet the threshold
    for col, var_val in pca_cols_with_var[1:]:
        if var_val >= threshold:
            filtered_cols.append(col)

    # Combine and return sorted
    return sorted(non_pca_cols + filtered_cols)






def _build_schema_map(numeric_cols) -> tuple[dict, dict]:
    """Display names for factors + derived columns, and each column's base variable.

    Returns ``(schema_map, numeric_col_bases)``: ``schema_map`` maps a variable
    or derived column to ``{"display_name": ...}``; ``numeric_col_bases`` maps a
    derived numeric column (``advertising_C0``, ``x_entropy``) to the schema
    variable it was computed from — the per-user variable-prefs surface operates
    on those base names.
    """
    schema_map = {}
    if 'var_schema' in fyp_cf and isinstance(fyp_cf['var_schema'], pd.DataFrame):
        vs = fyp_cf['var_schema']
        for _, row in vs.iterrows():
            var_name = str(row.get('variable_name', ''))
            entry = {}
            if 'display_name' in row:
                dname = str(row['display_name'])
                if dname and dname.lower() != 'nan' and dname.strip():
                    entry['display_name'] = dname.strip()
            if entry:
                schema_map[var_name] = entry

    # Map PCA components formatted names (e.g. tiktok_native_C13 -> TikTok Native (C13),
    # or var_entropy -> Var (entropy)) via longest-prefix matching against schema names.
    numeric_col_bases = {}
    sorted_base_names = sorted(schema_map.keys(), key=len, reverse=True)
    for col in numeric_cols:
        if col in schema_map:
            numeric_col_bases[col] = col
            continue

        for base_name in sorted_base_names:
            if col.startswith(base_name + '_'):
                raw_suffix = col[len(base_name) + 1:]

                # Format suffix: replace underscores with spaces
                formatted_suffix = raw_suffix.replace('_', ' ')

                if 'display_name' in schema_map[base_name]:
                    display_name = f"{schema_map[base_name]['display_name']} ({formatted_suffix})"
                else:
                    display_name = f"{base_name} ({formatted_suffix})"

                schema_map[col] = {'display_name': display_name}
                numeric_col_bases[col] = base_name
                break

    return schema_map, numeric_col_bases






def build_metadata_payload(df: pd.DataFrame, study: str) -> dict | None:
    """Build the /api/correlations/metadata response for a study's PCA frame.

    Returns None when no factors are found in the var_schema (route -> 500).
    """
    # Get numeric columns and exclude any that have 1 or fewer unique non-null values
    # Also explicitly exclude the unscaled '_raw' tooltip columns from appearing in the UI dropdowns
    all_numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    numeric_cols = [col for col in all_numeric_cols if df[col].nunique(dropna=True) > 1 and not str(col).endswith('_raw')]

    factors, _ = get_factors_and_features_from_var_schema(some_events_df=df, verbose=False)

    if not factors:
        return None

    # Exclude session_id from factors — not useful for filtering
    factors = [f for f in factors if f.lower() != 'session_id']

    schema_map, numeric_col_bases = _build_schema_map(numeric_cols)

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

    # Build factor_values with date handling. Factors with too many distinct
    # values get no checkbox list; they are reported as truncated instead.
    factor_value_limit = int(corr_setting("factor_value_limit"))
    factor_values = {}
    truncated_factors = []
    for f in factors:
        is_dt = pd.api.types.is_datetime64_any_dtype(df[f])
        if is_dt or "date" in f.lower():
            vals = df[f].dropna().astype(str).str[:10].unique().tolist()
        else:
            vals = df[f].dropna().unique().tolist()

        if len(vals) >= factor_value_limit:
            truncated_factors.append(f)
            continue

        if "week" in f.lower():
            formatted_vals = [format_week_value(v) for v in vals]
        else:
            formatted_vals = [str(v) for v in vals]

        factor_values[f] = sorted(formatted_vals, key=natural_sort_key)

    # Load display_ids for collection_id values
    display_ids = {}
    if 'collection_id' in factors:
        display_map = load_display_id_map()
        don_vals = factor_values.get('collection_id', [])
        for v in don_vals:
            if v in display_map:
                display_ids[v] = display_map[v]

    interpretations = load_interpretations(study)

    filtered_numeric_cols = filter_components_by_variance(numeric_cols, interpretations)

    # Per-user variable-prefs inputs (the shared "viz" surface, like Explore):
    # the gear panel operates on base variable names; the frontend composes
    # (global ∪ include) − exclude and filters derived columns through
    # numeric_col_bases. schema_map entries gain the section used for the
    # panel's grouping.
    prefs_meta = load_schema_metadata({})
    for var, section in (prefs_meta.get("schema_map") or {}).items():
        if var in schema_map and isinstance(section, dict) and section.get("section"):
            schema_map[var].setdefault("section", section["section"])

    return {
        "numeric_cols": filtered_numeric_cols,
        "numeric_col_bases": {c: b for c, b in numeric_col_bases.items() if c in filtered_numeric_cols},
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "truncated_factors": truncated_factors,
        "interpretations": interpretations,
        "schema_map": schema_map,
        "display_ids": display_ids,
        "viz_priority": prefs_meta.get("viz_priority", []),
        "all_variables_order": prefs_meta.get("all_variables_order", []),
        "section_order": prefs_meta.get("section_order", []),
        "views": STAT_VIEWS,
    }






def build_scatter_payload(df: pd.DataFrame, filters: dict, x_col: str, y_col: str,
                          color_col: str | None) -> dict:
    """Build the /api/correlations/data response (filtered scatter points)."""
    filtered_df = apply_factor_filters(df, filters)

    filtered_df = filtered_df.dropna(subset=[x_col, y_col])

    total_count = len(filtered_df)

    # Deterministic sample so the same request always shows the same points
    max_points = int(corr_setting("max_scatter_points"))
    if len(filtered_df) > max_points:
        filtered_df = filtered_df.sample(max_points, random_state=SCATTER_SAMPLE_SEED)

    # Get factor columns for richer hover tooltips
    factors, _ = get_factors_and_features_from_var_schema(some_events_df=df, verbose=False)

    result_data = []
    has_color = color_col and color_col in filtered_df.columns

    # Flat display-name map for tooltips
    schema_map = {}
    if 'var_schema' in fyp_cf and isinstance(fyp_cf['var_schema'], pd.DataFrame):
        vs = fyp_cf['var_schema']
        for _, row in vs.iterrows():
            var_name = str(row.get('variable_name', ''))
            dname = str(row.get('display_name', ''))
            if dname and dname.lower() != 'nan' and dname.strip():
                schema_map[var_name] = dname.strip()

    # Build richer hover text with grouping factors
    factor_cols_in_df = [f for f in factors if f in filtered_df.columns and f != color_col]

    # Get display IDs for collection_id
    display_map = {}
    if 'collection_id' in factor_cols_in_df or color_col == 'collection_id':
        display_map = load_display_id_map()

    # Helper function to format specific values
    def format_value(col_name, val):
        if pd.isna(val) or val is None:
            return "N/A"
        # Truncate dates to just YYYY-MM-DD
        if "date" in col_name.lower() or isinstance(val, (pd.Timestamp, np.datetime64)):
            return str(val)[:10]
        if "week" in col_name.lower():
            return format_week_value(val)
        # Resolve display IDs
        if col_name == 'collection_id' and str(val) in display_map:
            return display_map[str(val)]
        # Format numeric values (comma for thousands, up to 4 precision/significant digits)
        if isinstance(val, (int, float, np.integer, np.floating)):
            if val == 0:
                return "0"
            try:
                # Calculate required decimals for 4 significant digits
                decimals = 4 - int(math.floor(math.log10(abs(val)))) - 1
                if decimals <= 0:
                    rounded_val = int(round(val, decimals))
                    formatted = f"{rounded_val:,}"
                else:
                    formatted = f"{round(val, decimals):,}"
                    if '.' in formatted:
                        formatted = formatted.rstrip('0').rstrip('.')
                return formatted if formatted else "0"
            except Exception:
                return str(val)

        return str(val)

    # Identify unscaled absolute numeric features
    raw_numeric_cols = [c for c in filtered_df.columns if str(c).endswith('_raw')]

    # Prepare sorted bases for suffix extraction on PCA components
    sorted_base_names = sorted(schema_map.keys(), key=len, reverse=True)

    for row in filtered_df.itertuples():
        x_val = getattr(row, x_col)
        y_val = getattr(row, y_col)

        c_val = "Default"
        if has_color:
            c_val = format_value(color_col, getattr(row, color_col))

        # Build hover text with all grouping factors
        color_col_display = schema_map.get(color_col, color_col)
        hover_parts = [f"{color_col_display}: {c_val}"]

        for fc in factor_cols_in_df:
            fv = getattr(row, fc, None)
            if fv is not None:
                fc_display = schema_map.get(fc, fc)
                fv_formatted = format_value(fc, fv)
                hover_parts.append(f"{fc_display}: {fv_formatted}")

        # Inject absolute unscaled values
        for r_col in raw_numeric_cols:
            r_val = getattr(row, r_col, None)
            if r_val is not None and not pd.isna(r_val):
                base_col_name = str(r_col)[:-4]  # strip _raw

                # Try base name, then parse for PCA suffixes natively
                r_display = schema_map.get(base_col_name)
                if not r_display:
                    r_display = base_col_name
                    for b_name in sorted_base_names:
                        if base_col_name.startswith(b_name + '_'):
                            formatted_suf = base_col_name[len(b_name) + 1:].replace('_', ' ')
                            r_display = f"{schema_map[b_name]} ({formatted_suf})"
                            break

                r_val_formatted = format_value(base_col_name, r_val)

                hover_parts.append(f"{r_display} (Abs): {r_val_formatted}")

        txt = "<br>".join(hover_parts)

        # Collect raw factor values for drill-down to Video Analysis
        factors_dict = {}
        if has_color:
            factors_dict[color_col] = str(getattr(row, color_col))
        for fc in factor_cols_in_df:
            fv = getattr(row, fc, None)
            if fv is not None and not pd.isna(fv):
                factors_dict[fc] = str(fv)

        result_data.append({
            "x": x_val,
            "y": y_val,
            "color_val": getattr(row, color_col) if has_color else "Default",
            "text": txt,
            "factors": factors_dict
        })

    return {"data": result_data, "total_count": total_count}






def build_matrix_payload(df: pd.DataFrame, filters: dict, study: str) -> tuple[dict | None, str | None]:
    """Build the /api/correlations/correlation_matrix response.

    Returns ``(payload, None)`` on success or ``(None, error_message)`` when
    too few usable numeric columns remain.
    """
    filtered_df = apply_factor_filters(df, filters)

    # Select only numeric columns for correlation (exclude unscaled '_raw' columns)
    numeric_df = filtered_df.select_dtypes(include=['number'])
    numeric_cols_to_keep = [col for col in numeric_df.columns if not str(col).endswith('_raw')]
    numeric_df = numeric_df[numeric_cols_to_keep]

    # Filter out any columns that are constant within this filtered subset
    numeric_df = numeric_df.loc[:, numeric_df.nunique(dropna=True) > 1]

    if numeric_df.shape[1] < 2:
        return None, "Not enough numeric columns for correlation"

    # Apply variance threshold filtering
    interpretations = load_interpretations(study)

    filtered_cols = filter_components_by_variance(numeric_df.columns.tolist(), interpretations)
    numeric_df = numeric_df[filtered_cols]

    if numeric_df.shape[1] < 2:
        return None, "Not enough numeric columns after variance filtering"

    method = correlation_method()
    corr = numeric_df.corr(method=method)

    # Undefined correlations serialize as null (not a fake r = 0)
    matrix = [[None if pd.isna(v) else float(v) for v in row] for row in corr.values]

    return {
        "columns": corr.columns.tolist(),
        "matrix": matrix,
        "count": len(filtered_df),
        "method": method,
    }, None
