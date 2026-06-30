import math
import re
import traceback

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import login_required

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.recode_variables import get_factors_and_features_from_var_schema

from ..data_service import get_pca_df, load_display_id_map

correlations_bp = Blueprint('correlations_bp', __name__)


PCA_MIN_VARIANCE_THRESHOLD = 5.0


def _filter_pca_components_by_variance(numeric_cols, interpretations):
    """
    Filters a list of PCA component names based on their explained variance.
    Always keeps non-PCA components and the PCA component with the highest variance,
    then any other PCA components >= threshold.
    """
    if not interpretations or not numeric_cols:
        return numeric_cols

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
        if var_val >= PCA_MIN_VARIANCE_THRESHOLD:
            filtered_cols.append(col)

    # Combine and return sorted
    return sorted(non_pca_cols + filtered_cols)


@correlations_bp.route('/api/correlations/metadata', methods=['POST'])
@login_required
def api_pca_metadata():

    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    # Get numeric columns and exclude any that have 1 or fewer unique non-null values
    # Also explicitly exclude the unscaled '_raw' tooltip columns from appearing in the UI dropdowns
    all_numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    numeric_cols = [col for col in all_numeric_cols if df[col].nunique(dropna=True) > 1 and not str(col).endswith('_raw')]

    factors, _ = get_factors_and_features_from_var_schema(some_events_df = df, verbose = False)

    if not factors:
        traceback.print_exc()
        raise Exception("No factors found in var_schema")

    # Exclude session_id from factors — not useful for filtering
    factors = [f for f in factors if f.lower() != 'session_id']

    # Build schema_map with display_name from var_schema
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

    # Map PCA components formatted names (e.g. tiktok_native_C13 -> TikTok Native (C13), or var_entropy -> Var (entropy))
    # Check if unrecognized numeric columns begin with a known schema variable base name
    sorted_base_names = sorted(schema_map.keys(), key=len, reverse=True)
    for col in numeric_cols:
        if col in schema_map:
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
                break

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

    # Build factor_values with date handling
    factor_values = {}
    for f in factors:
        is_dt = pd.api.types.is_datetime64_any_dtype(df[f])
        if is_dt or "date" in f.lower():
            vals = df[f].dropna().astype(str).str[:10].unique().tolist()
        else:
            vals = df[f].dropna().unique().tolist()

        if len(vals) < 500:
            formatted_vals = []
            for v in vals:
                v_str = str(v)
                if "week" in f.lower():
                    parts = v_str.split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        v_str = f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            v_str = f"{parts[0]}-{int(week_num):02d}"
                formatted_vals.append(v_str)

            factor_values[f] = sorted(formatted_vals, key=natural_sort_key)

    # Load display_ids for collection_id values
    display_ids = {}
    if 'collection_id' in factors:
        display_map = load_display_id_map()
        don_vals = factor_values.get('collection_id', [])
        for v in don_vals:
            if v in display_map:
                display_ids[v] = display_map[v]

    interpretations = {}
    try:
        inter_path = f"{study}_comp_interpretations.json"
        if data_io.exists(storage_location="cache", filename=inter_path):
            loaded_interps = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
            if loaded_interps:
                interpretations = loaded_interps
    except Exception as e:
        traceback.print_exc()
        print(f"Error loading interpretations: {e}")

    filtered_numeric_cols = _filter_pca_components_by_variance(numeric_cols, interpretations)

    return jsonify({
        "numeric_cols": filtered_numeric_cols,
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "interpretations": interpretations,
        "schema_map": schema_map,
        "display_ids": display_ids
    })


@correlations_bp.route('/api/correlations/data', methods=['POST'])
@login_required
def api_pca_data():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})
    x_col = data.get("x_col")
    y_col = data.get("y_col")
    color_col = data.get("color_col")

    if not study or not x_col or not y_col:
        return jsonify({"error": "Missing params"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col in df.columns:
            is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
            if is_dt or "date" in col.lower():
                mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
            elif "week" in col.lower():
                def format_week(v_str):
                    parts = str(v_str).split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        return f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            return f"{parts[0]}-{int(week_num):02d}"
                    return str(v_str)
                formatted_col = df[col].apply(format_week)
                mask &= formatted_col.astype(str).isin(vals)
            else:
                mask &= df[col].astype(str).isin(vals)

    filtered_df = df[mask].copy()

    filtered_df = filtered_df.dropna(subset=[x_col, y_col])

    total_count = len(filtered_df)

    MAX_POINTS = 5000
    if len(filtered_df) > MAX_POINTS:
        filtered_df = filtered_df.sample(MAX_POINTS)

    # Get factor columns for richer hover tooltips
    factors, _ = get_factors_and_features_from_var_schema(some_events_df=df, verbose=False)

    result_data = []
    has_color = color_col and color_col in filtered_df.columns

    # Build schema_map for friendly display names in tooltips
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
            v_str = str(val)
            parts = v_str.split('-')
            if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                return f"{parts[0]}-{int(parts[1]):02d}"
            elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                week_num = parts[1][1:]
                if week_num.isdigit():
                    return f"{parts[0]}-{int(week_num):02d}"
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
                base_col_name = str(r_col)[:-4] # strip _raw

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

    return jsonify({"data": result_data, "total_count": total_count})


@correlations_bp.route('/api/correlations/correlation_matrix', methods=['POST'])
@login_required
def api_pca_correlation_matrix():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})

    if not study:
        return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None:
        return jsonify({"error": "PCA data not found"}), 404

    # Apply filters
    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col in df.columns:
            is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
            if is_dt or "date" in col.lower():
                mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
            elif "week" in col.lower():
                def format_week(v_str):
                    parts = str(v_str).split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        return f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            return f"{parts[0]}-{int(week_num):02d}"
                    return str(v_str)
                formatted_col = df[col].apply(format_week)
                mask &= formatted_col.astype(str).isin(vals)
            else:
                mask &= df[col].astype(str).isin(vals)
    filtered_df = df[mask].copy()

    # Select only numeric columns for correlation (exclude unscaled '_raw' columns)
    numeric_df = filtered_df.select_dtypes(include=['number'])
    numeric_cols_to_keep = [col for col in numeric_df.columns if not str(col).endswith('_raw')]
    numeric_df = numeric_df[numeric_cols_to_keep]

    # Filter out any columns that are constant within this filtered subset
    numeric_df = numeric_df.loc[:, numeric_df.nunique(dropna=True) > 1]

    if numeric_df.shape[1] < 2:
        return jsonify({"error": "Not enough numeric columns for correlation"}), 400

    # Apply variance threshold filtering
    interpretations = {}
    try:
        inter_path = f"{study}_comp_interpretations.json"
        if data_io.exists(storage_location="cache", filename=inter_path):
            loaded_interps = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
            if loaded_interps:
                interpretations = loaded_interps
    except Exception as e:
        traceback.print_exc()
        print(f"Error loading interpretations for heatmap: {e}")

    filtered_cols = _filter_pca_components_by_variance(numeric_df.columns.tolist(), interpretations)
    numeric_df = numeric_df[filtered_cols]

    if numeric_df.shape[1] < 2:
         return jsonify({"error": "Not enough numeric columns after variance filtering"}), 400

    # Compute Pearson correlations
    corr = numeric_df.corr()

    # Replace NaN with 0 for serialization
    corr = corr.fillna(0.0)

    return jsonify({
        "columns": corr.columns.tolist(),
        "matrix": corr.values.tolist(),
        "count": len(filtered_df)
    })
