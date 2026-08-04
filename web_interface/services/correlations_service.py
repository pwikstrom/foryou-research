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
from scipy import stats as scipy_stats
from statsmodels.stats.multitest import multipletests

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.logging_setup import get_logger
from fyp.recode_variables import (
    get_factors_and_features_from_var_schema,
    get_grouping_factors_from_var_schema,
)

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
    {"key": "group_stats", "label": "Group diff."},
]

_CORR_DEFAULTS = {
    "min_variance_pct": 5.0,
    "max_components_per_variable": 3,
    "max_scatter_points": 5000,
    "factor_value_limit": 500,
    "correlation_method": "pearson",
    "minimum_group_size": 10,
    "permanova_permutations": 999,
}

# Per-group video count written by the PCA worker (see fyp.analysis.pca).
# Current parquets carry it as `videos_watched` (a contract-declared variable:
# consumption intensity, offered on the axes/matrix like any numeric column);
# parquets built before 2026-08 carry the retired `group_size` name instead,
# which stays excluded from the dropdowns/matrix/centering and feeds only the
# "N groups covering M videos" Sample-panel counts. Every read is optional.
GROUP_SIZE_COL = "group_size"
VIDEOS_WATCHED_COL = "videos_watched"

# Coverage of the scatter's confidence ellipses: chi-square(2 df) quantile at
# 95%. Stated on the UI wherever the ellipses are drawn.
ELLIPSE_COVERAGE = 0.95

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






def total_videos(df: pd.DataFrame) -> int | None:
    """Total videos the study's groups average over, for the unit banner.

    Read from the per-group video count when the PCA parquet carries one
    (``videos_watched``, or the retired ``group_size`` name on parquets built
    before 2026-08); None otherwise.
    """
    col = VIDEOS_WATCHED_COL if VIDEOS_WATCHED_COL in df.columns else GROUP_SIZE_COL
    if col not in df.columns:
        return None
    total = pd.to_numeric(df[col], errors="coerce").sum()
    return None if pd.isna(total) else int(total)






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
    """Trim each variable's PCA components to the interpretable leading few.

    The cap is **per variable**, not global: a high-cardinality variable
    (146 niches) legitimately spreads its variance thinly across a dozen
    components, so a flat variance floor either keeps all of them or, raised
    enough to trim them, silently drops a small variable's genuinely important
    third component. Keeping the top ``max_components_per_variable`` of each
    variable represents every variable and drops only the PCA tail.

    Each variable's leading component is always kept (so no variable vanishes
    from the tab); the remaining slots additionally require the
    ``min_variance_pct`` floor. Non-PCA columns (plain numerics, entropies)
    pass through untouched.

    Args:
        numeric_cols: Candidate numeric column names.
        interpretations: ``{component: {"explained_variance_pct": ...}}``.

    Returns:
        The kept column names, sorted.
    """
    if not interpretations or not numeric_cols:
        return numeric_cols

    threshold = float(corr_setting("min_variance_pct"))
    max_per_variable = max(1, int(corr_setting("max_components_per_variable")))

    # Match PCA components (e.g., ends with _C and a number like _C1)
    pca_pattern = re.compile(r'_C\d+$')

    by_variable: dict[str, list[tuple[str, float]]] = {}
    non_pca_cols = []
    for col in numeric_cols:
        match = pca_pattern.search(col)
        if not match:
            # Not a PCA component, always keep it
            non_pca_cols.append(col)
            continue
        var_val = 0.0
        if col in interpretations and 'explained_variance_pct' in interpretations[col]:
            try:
                var_val = float(interpretations[col]['explained_variance_pct'])
            except (ValueError, TypeError):
                pass
        base = col[:match.start()]
        by_variable.setdefault(base, []).append((col, var_val))

    if not by_variable:
        return numeric_cols

    filtered_cols = []
    for comps in by_variable.values():
        comps.sort(key=lambda cv: (-cv[1], cv[0]))
        for rank, (col, var_val) in enumerate(comps[:max_per_variable]):
            if rank == 0 or var_val >= threshold:
                filtered_cols.append(col)

    return sorted(non_pca_cols + filtered_cols)






def apply_within_collection_centering(df: pd.DataFrame, cols) -> tuple[pd.DataFrame, bool]:
    """Subtract each collection's mean from ``cols`` (a fixed-effects move).

    Removes between-collection composition, so remaining associations are
    within-collection. Returns ``(df, applied)`` — a no-op when there is no
    ``collection_id`` column or none of ``cols`` is present.
    """
    cols = [c for c in cols if c in df.columns]
    if 'collection_id' not in df.columns or not cols:
        return df, False
    out = df.copy()
    vals = out[cols].astype('float64')
    group_keys = out['collection_id'].astype(str).values
    means = vals.groupby(group_keys).transform('mean')
    out[cols] = vals - means
    return out, True






def compute_regression_stats(x, y) -> dict | None:
    """OLS regression readout for the scatter: slope, 95% CI, r, R², p, n.

    Computed on the full filtered set (never the display sample). Returns
    None when fewer than 3 paired observations or a degenerate x.
    """
    x = pd.to_numeric(pd.Series(list(x)), errors='coerce').astype('float64')
    y = pd.to_numeric(pd.Series(list(y)), errors='coerce').astype('float64')
    mask = x.notna() & y.notna()
    x, y = x[mask].to_numpy(), y[mask].to_numpy()
    n = len(x)
    if n < 3 or float(np.std(x)) == 0.0:
        return None
    try:
        res = scipy_stats.linregress(x, y)
    except ValueError:
        return None
    if not np.isfinite(res.slope):
        return None
    t_crit = float(scipy_stats.t.ppf(0.975, n - 2))
    return {
        "slope": float(res.slope),
        "intercept": float(res.intercept),
        "stderr": float(res.stderr),
        "ci_low": float(res.slope - t_crit * res.stderr),
        "ci_high": float(res.slope + t_crit * res.stderr),
        "r": float(res.rvalue),
        "r2": float(res.rvalue ** 2),
        "p": float(res.pvalue),
        "n": int(n),
    }






def compute_group_ellipses(df: pd.DataFrame, x_col: str, y_col: str,
                           color_col: str | None) -> list[dict]:
    """Per-colour-group mean + covariance for true confidence ellipses.

    Computed on the full filtered set. The frontend eigendecomposes the 2×2
    covariance and scales the axes by chi²₂ at ``ELLIPSE_COVERAGE``. Groups
    with fewer than 3 points are skipped.
    """
    if color_col and color_col in df.columns:
        group_keys = df[color_col].astype(str)
    else:
        group_keys = pd.Series("Default", index=df.index)

    out = []
    for group, sub in df.groupby(group_keys.values):
        x = pd.to_numeric(sub[x_col], errors='coerce').astype('float64')
        y = pd.to_numeric(sub[y_col], errors='coerce').astype('float64')
        mask = x.notna() & y.notna()
        x, y = x[mask].to_numpy(), y[mask].to_numpy()
        if len(x) < 3:
            continue
        cov = np.cov(x, y)
        if not np.all(np.isfinite(cov)):
            continue
        out.append({
            "group": str(group),
            "n": int(len(x)),
            "mean_x": float(np.mean(x)),
            "mean_y": float(np.mean(y)),
            "cov": [[float(cov[0, 0]), float(cov[0, 1])],
                    [float(cov[1, 0]), float(cov[1, 1])]],
        })
    return out






def pairwise_correlation_stats(numeric_df: pd.DataFrame, method: str):
    """Pairwise-complete correlation matrix with n, p and BH-adjusted q.

    Returns ``(r, p, q, n)`` as k×k numpy arrays (n int, others float with
    NaN for undefined cells). q is Benjamini–Hochberg across the upper
    triangle's finite p-values — one family per requested matrix.
    """
    cols = list(numeric_df.columns)
    k = len(cols)
    arr = numeric_df.astype('float64').to_numpy()

    r = np.full((k, k), np.nan)
    p = np.full((k, k), np.nan)
    n = np.zeros((k, k), dtype=int)

    for i in range(k):
        for j in range(i, k):
            xi, yj = arr[:, i], arr[:, j]
            mask = ~np.isnan(xi) & ~np.isnan(yj)
            nij = int(mask.sum())
            n[i, j] = n[j, i] = nij
            if i == j:
                r[i, j] = 1.0
                continue
            if nij < 3:
                continue
            x, y = xi[mask], yj[mask]
            if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
                continue
            try:
                if method == "spearman":
                    res = scipy_stats.spearmanr(x, y)
                else:
                    res = scipy_stats.pearsonr(x, y)
                rij, pij = float(res[0]), float(res[1])
            except Exception:
                continue
            if np.isfinite(rij):
                r[i, j] = r[j, i] = rij
            if np.isfinite(pij):
                p[i, j] = p[j, i] = pij

    # Benjamini–Hochberg across the unique pairs
    q = np.full((k, k), np.nan)
    upper = [(i, j) for i in range(k) for j in range(i + 1, k)]
    finite_pairs = [(i, j) for i, j in upper if np.isfinite(p[i, j])]
    if finite_pairs:
        pvals = [p[i, j] for i, j in finite_pairs]
        _, qvals, _, _ = multipletests(pvals, method="fdr_bh")
        for (i, j), qv in zip(finite_pairs, qvals):
            q[i, j] = q[j, i] = float(qv)

    return r, p, q, n






def _matrix_to_json(mat) -> list:
    """k×k float array -> nested lists with None for non-finite cells."""
    return [[None if not np.isfinite(v) else float(v) for v in row] for row in mat]






def load_group_stats(study: str) -> dict | None:
    """Load the worker-precomputed ``{study}_corr_stats.json``, or None."""
    try:
        filename = f"{study}_corr_stats.json"
        if data_io.exists(storage_location="cache", filename=filename):
            payload = data_io.load_json(storage_location="cache", filename=filename, verbose=False)
            if isinstance(payload, dict):
                return payload
    except Exception as e:
        logger.warning(f"Error loading group stats for {study}: {e}")
    return None






def build_status_payload(study: str) -> dict:
    """Freshness signal for the tab: is the PCA artifact behind the study data?

    Compares the mtimes of ``{study}_recoded.parquet`` and ``{study}_PCA.parquet``
    (both in the ``cache`` location). Informational only — the tab keeps
    rendering; the frontend shows a banner when stale.
    """
    def _mtime(filename):
        try:
            if data_io.exists(storage_location="cache", filename=filename):
                return data_io.getmtime(storage_location="cache", filename=filename)
        except Exception:
            pass
        return None

    pca_mtime = _mtime(f"{study}_PCA.parquet")
    recoded_mtime = _mtime(f"{study}_recoded.parquet")
    stale = bool(pca_mtime is not None and recoded_mtime is not None
                 and recoded_mtime > pca_mtime + 1)
    return {
        "has_pca": pca_mtime is not None,
        "pca_built_at": pca_mtime,
        "recoded_updated_at": recoded_mtime,
        "stale": stale,
    }






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
    numeric_cols = [col for col in all_numeric_cols
                    if df[col].nunique(dropna=True) > 1
                    and not str(col).endswith('_raw')
                    and str(col) != GROUP_SIZE_COL]

    factors, _ = get_factors_and_features_from_var_schema(some_events_df=df, verbose=False)

    if not factors:
        return None

    # Exclude session_id from factors — not useful for filtering
    factors = [f for f in factors if f.lower() != 'session_id']

    schema_map, numeric_col_bases = _build_schema_map(numeric_cols)

    # Anonymised display names for the scatter legend's collection values.
    display_ids = {}
    if 'collection_id' in factors and 'collection_id' in df.columns:
        display_map = load_display_id_map()
        for v in df['collection_id'].dropna().astype(str).unique():
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

    # Unit-of-analysis banner inputs: each row of the PCA frame is one
    # grouping-factor group (e.g. a collection-day), not a video.
    grouping = get_grouping_factors_from_var_schema(verbose=False)
    grouping_display = [
        schema_map.get(g, {}).get("display_name", g) for g in grouping
    ]
    unit = {
        "grouping_factors": grouping,
        "grouping_display": grouping_display,
        "n_groups": int(len(df)),
        "videos_total": total_videos(df),
        "min_group_size": int(corr_setting("minimum_group_size")),
    }

    return {
        "numeric_cols": filtered_numeric_cols,
        "numeric_col_bases": {c: b for c, b in numeric_col_bases.items() if c in filtered_numeric_cols},
        "factor_cols": sorted(factors),
        "interpretations": interpretations,
        "schema_map": schema_map,
        "display_ids": display_ids,
        "viz_priority": prefs_meta.get("viz_priority", []),
        "all_variables_order": prefs_meta.get("all_variables_order", []),
        "section_order": prefs_meta.get("section_order", []),
        "views": STAT_VIEWS,
        "default_method": correlation_method(),
        "ellipse_coverage": ELLIPSE_COVERAGE,
        "unit": unit,
    }






def build_scatter_payload(df: pd.DataFrame, x_col: str, y_col: str,
                          color_col: str | None, center: bool = False) -> dict:
    """Build the /api/correlations/data response over the whole study.

    There is no row filtering: the study is the sample (exclusions and event
    windows belong in study definitions, where they are documented).

    ``center=True`` demeans the two axis columns within each collection
    before anything else, so both the points and the statistics describe
    within-collection variation.
    """
    filtered_df = df.dropna(subset=[x_col, y_col])

    centered = False
    if center:
        filtered_df, centered = apply_within_collection_centering(
            filtered_df, [x_col, y_col] if x_col != y_col else [x_col])

    total_count = len(filtered_df)

    # Regression readout + confidence-ellipse inputs are computed on the FULL
    # data — never on the capped display sample below.
    color_for_groups = color_col if (color_col and color_col in filtered_df.columns) else None
    regression_stats = compute_regression_stats(filtered_df[x_col], filtered_df[y_col])
    group_ellipses = compute_group_ellipses(filtered_df, x_col, y_col, color_for_groups)

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

    return {
        "data": result_data,
        "total_count": total_count,
        "stats": regression_stats,
        "group_ellipses": group_ellipses,
        "ellipse_coverage": ELLIPSE_COVERAGE,
        "centered": centered,
    }






def build_matrix_payload(df: pd.DataFrame, study: str,
                         method: str | None = None,
                         center: bool = False) -> tuple[dict | None, str | None]:
    """Build the /api/correlations/correlation_matrix response (whole study).

    Pairwise-complete correlations with per-pair n, p and Benjamini–Hochberg
    q. Columns are ordered by variable family (base schema variable) so the
    heatmap can separate same-family blocks — cross-family cells compare
    components from *different* per-variable PCA bases and are captioned as
    such in the UI. Returns ``(payload, None)`` on success or
    ``(None, error_message)`` when too few usable numeric columns remain.
    """
    if method not in _VALID_CORRELATION_METHODS:
        method = correlation_method()

    filtered_df = df

    centered = False
    if center:
        centerable = [c for c in filtered_df.select_dtypes(include=['number']).columns
                      if not str(c).endswith('_raw') and str(c) != GROUP_SIZE_COL]
        filtered_df, centered = apply_within_collection_centering(filtered_df, centerable)

    # Select only numeric columns for correlation (exclude the unscaled '_raw'
    # columns and the group_size provenance column)
    numeric_df = filtered_df.select_dtypes(include=['number'])
    numeric_cols_to_keep = [col for col in numeric_df.columns
                            if not str(col).endswith('_raw') and str(col) != GROUP_SIZE_COL]
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

    # Order columns by variable family so same-basis components sit together
    _, col_bases = _build_schema_map(list(numeric_df.columns))
    families = {c: col_bases.get(c, c) for c in numeric_df.columns}
    ordered_cols = sorted(numeric_df.columns, key=lambda c: (families[c], str(c)))
    numeric_df = numeric_df[ordered_cols]

    r, p, q, n = pairwise_correlation_stats(numeric_df, method)

    return {
        "columns": ordered_cols,
        "families": [families[c] for c in ordered_cols],
        "matrix": _matrix_to_json(r),
        "p_matrix": _matrix_to_json(p),
        "q_matrix": _matrix_to_json(q),
        "n_matrix": [[int(v) for v in row] for row in n],
        "count": len(filtered_df),
        "method": method,
        "centered": centered,
    }, None
