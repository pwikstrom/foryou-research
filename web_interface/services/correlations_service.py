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
    {"key": "group_stats", "label": "Group differences"},
]

_CORR_DEFAULTS = {
    "min_variance_pct": 5.0,
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

# Views whose numbers come from the pca_refresh worker's whole-study artifacts
# and therefore ignore the Sample panel entirely. The frontend disables the
# panel (with an explanation) while one of these is active, rather than letting
# it silently no-op.
FILTER_IMMUNE_VIEWS = ("group_stats",)

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






def apply_factor_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Return the rows of ``df`` matching the per-factor selections in ``filters``.

    Two selection shapes are accepted per factor:

    * a **list of values** — membership, as presented in ``factor_values``.
      Date-like columns are compared on their ``YYYY-MM-DD`` prefix and week
      columns through :func:`format_week_value`.
    * a **dict** ``{"min": "YYYY-MM-DD", "max": "YYYY-MM-DD"}`` — an inclusive
      range, used for the activity-date window. Either bound may be absent or
      blank, meaning "open at that end". A date column has too many distinct
      values for a checkbox list (see ``factor_value_limit``), which used to
      leave the time window unfilterable altogether.

    Args:
        df: The group-level PCA frame.
        filters: Factor name → list of values, or a ``{"min", "max"}`` dict.

    Returns:
        A copy of the matching rows.
    """
    mask = pd.Series(True, index=df.index)
    for col, sel in filters.items():
        if col not in df.columns:
            continue
        is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
        is_date_like = is_dt or "date" in col.lower()

        if isinstance(sel, dict):
            # Range selection. Comparing the YYYY-MM-DD prefix as text is exact
            # for ISO dates and matches how the bounds were offered.
            if not is_date_like:
                continue
            day = df[col].astype(str).str[:10]
            lo = str(sel.get("min") or "")[:10]
            hi = str(sel.get("max") or "")[:10]
            if lo:
                mask &= day >= lo
            if hi:
                mask &= day <= hi
            continue

        vals = sel
        if is_date_like:
            mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
        elif "week" in col.lower():
            mask &= df[col].map(format_week_value).isin([str(v) for v in vals])
        else:
            mask &= df[col].astype(str).isin(vals)
    return df[mask].copy()






def build_sample_summary(df: pd.DataFrame, filtered_df: pd.DataFrame) -> dict:
    """Describe what the Sample panel currently selects.

    The unit of analysis here is a grouping-factor group (a collection-day),
    not a video — the single most common misreading of this tab — so the panel
    header states both the group count and the number of videos those groups
    average over.

    Args:
        df: The full group-level PCA frame for the study.
        filtered_df: The same frame after :func:`apply_factor_filters`.

    Returns:
        Dict with total/selected group counts and, when the PCA parquet carries
        ``group_size``, the matching video counts (else None).
    """
    def _videos(frame: pd.DataFrame) -> int | None:
        col = VIDEOS_WATCHED_COL if VIDEOS_WATCHED_COL in frame.columns else GROUP_SIZE_COL
        if col not in frame.columns:
            return None
        total = pd.to_numeric(frame[col], errors="coerce").sum()
        return None if pd.isna(total) else int(total)

    return {
        "groups_total": int(len(df)),
        "groups_selected": int(len(filtered_df)),
        "videos_total": _videos(df),
        "videos_selected": _videos(filtered_df),
    }






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






# --- Split comparison -------------------------------------------------------
#
# Subsetting answers "what does this look like for these groups"; splitting
# answers "does this association differ between these groups", which is usually
# the actual research question. Both views run the same live computation twice
# — no new precomputation — so the cost is one extra pass over the group frame.
#
# The honesty constraint: comparing two correlations with Fisher's r-to-z
# assumes INDEPENDENT samples. That holds when the split partitions collections
# (platform, a donor attribute), because then no donor contributes to both
# sides. It does not hold for a within-donor split (weekday, week), where the
# same donors appear in both levels. ``split_is_independent`` decides which, and
# the dependent case reports both correlations descriptively with no p-value
# rather than a number that would read as more than it is.


# At most this many levels are compared; a Δ needs exactly two sides, so the
# two largest are used and the rest are named in ``levels_omitted``.
SPLIT_LEVELS = 2

# A factor with more levels than this is not offered as a split — collection_id
# (one level per donor) and the date factors would otherwise qualify.
SPLIT_MAX_LEVELS = 12


def split_frames(df: pd.DataFrame, split_col: str) -> tuple[list[tuple[str, pd.DataFrame]], list[str]]:
    """Split ``df`` into its two largest levels of ``split_col``.

    Args:
        df: Already-filtered group-level frame.
        split_col: The factor to split on.

    Returns:
        ``(pairs, omitted)`` where ``pairs`` is up to ``SPLIT_LEVELS``
        ``(level, subframe)`` tuples ordered by size (largest first), and
        ``omitted`` names the levels that did not make the cut.
    """
    if split_col not in df.columns:
        return [], []
    labels = df[split_col].astype(str)
    counts = labels.value_counts()
    chosen = list(counts.index[:SPLIT_LEVELS])
    omitted = [str(v) for v in counts.index[SPLIT_LEVELS:]]
    return [(str(v), df[labels == v]) for v in chosen], omitted






def split_is_independent(df: pd.DataFrame, split_col: str) -> bool:
    """Whether the split partitions collections, making the levels independent.

    True when every collection sits entirely on one side of the split — no
    donor contributes rows to both levels — which is the condition Fisher's
    r-to-z comparison needs. A within-donor split (weekday, week) is False.

    Falls back to False when there is no collection column to reason about:
    claiming independence we cannot verify would be the harmful error.
    """
    if "collection_id" not in df.columns or split_col not in df.columns:
        return False
    per_collection = df.groupby(df["collection_id"].astype(str))[split_col].nunique(dropna=True)
    return bool((per_collection <= 1).all())






def fisher_z_difference(r1: float, n1: int, r2: float, n2: int) -> tuple[float, float] | None:
    """Two-sided test that two independent correlations differ.

    Fisher's r-to-z transform: ``z = (z1 - z2) / sqrt(1/(n1-3) + 1/(n2-3))``.

    Args:
        r1: Correlation in the first level.
        n1: Pairwise-complete n in the first level.
        r2: Correlation in the second level.
        n2: Pairwise-complete n in the second level.

    Returns:
        ``(z, p)``, or None when either side is too small (n <= 3) or |r| is 1
        (the transform diverges).
    """
    if n1 <= 3 or n2 <= 3:
        return None
    if not (np.isfinite(r1) and np.isfinite(r2)):
        return None
    if abs(r1) >= 1.0 or abs(r2) >= 1.0:
        return None
    z1 = np.arctanh(r1)
    z2 = np.arctanh(r2)
    se = np.sqrt(1.0 / (n1 - 3) + 1.0 / (n2 - 3))
    if se == 0:
        return None
    z = float((z1 - z2) / se)
    p = float(2.0 * scipy_stats.norm.sf(abs(z)))
    return z, p






def build_scatter_split(filtered_df: pd.DataFrame, x_col: str, y_col: str,
                        split_col: str) -> dict | None:
    """Per-level regressions for the scatter, plus a comparison of the two.

    Args:
        filtered_df: Rows already filtered and dropna'd on both axes.
        x_col: X-axis column.
        y_col: Y-axis column.
        split_col: The factor to split on.

    Returns:
        Dict with one entry per compared level (each carrying the full
        :func:`compute_regression_stats` readout) and a ``comparison`` block,
        or None when fewer than two levels have usable data.
    """
    pairs, omitted = split_frames(filtered_df, split_col)
    levels = []
    for value, sub in pairs:
        stats = compute_regression_stats(sub[x_col], sub[y_col])
        levels.append({"value": value, "n_groups": int(len(sub)), "stats": stats})

    usable = [lv for lv in levels if lv["stats"]]
    if len(usable) < 2:
        return None

    independent = split_is_independent(filtered_df, split_col)
    a, b = usable[0], usable[1]
    comparison = {
        "independent": independent,
        "note": _independence_note(split_col, independent, split_col),
        "r_difference": float(a["stats"]["r"] - b["stats"]["r"]),
        "z": None,
        "p": None,
    }
    if independent:
        test = fisher_z_difference(
            a["stats"]["r"], a["stats"]["n"], b["stats"]["r"], b["stats"]["n"],
        )
        if test is not None:
            comparison["z"], comparison["p"] = test

    return {
        "col": split_col,
        "levels": levels,
        "levels_omitted": omitted,
        "comparison": comparison,
    }






def build_matrix_split(filtered_df: pd.DataFrame, cols: list, split_col: str,
                       method: str) -> dict | None:
    """Per-level correlation matrices plus the cellwise difference.

    Same live computation as the single-sample heatmap, run once per level. The
    ``p_matrix`` tests the *difference* (Fisher r-to-z) and is all-None for a
    within-donor split, where the two correlations are not independent.

    Args:
        filtered_df: Rows already filtered (and centered, if requested).
        cols: The ordered numeric columns to correlate.
        split_col: The factor to split on.
        method: "pearson" or "spearman".

    Returns:
        Dict with the two per-level matrices, the delta matrix and the test, or
        None when fewer than two levels carry enough rows.
    """
    pairs, omitted = split_frames(filtered_df, split_col)
    if len(pairs) < 2:
        return None

    per_level = []
    for value, sub in pairs:
        if len(sub) < 3:
            continue
        r, _p, _q, n = pairwise_correlation_stats(sub[cols], method)
        per_level.append({"value": value, "n_groups": int(len(sub)), "r": r, "n": n})

    if len(per_level) < 2:
        return None

    a, b = per_level[0], per_level[1]
    k = len(cols)
    delta = np.full((k, k), np.nan)
    p = np.full((k, k), np.nan)
    n_min = np.zeros((k, k), dtype=int)

    independent = split_is_independent(filtered_df, split_col)
    for i in range(k):
        for j in range(k):
            ra, rb = a["r"][i, j], b["r"][i, j]
            n_min[i, j] = int(min(a["n"][i, j], b["n"][i, j]))
            if i == j or not (np.isfinite(ra) and np.isfinite(rb)):
                continue
            delta[i, j] = ra - rb
            if independent:
                test = fisher_z_difference(float(ra), int(a["n"][i, j]),
                                           float(rb), int(b["n"][i, j]))
                if test is not None:
                    p[i, j] = test[1]

    # BH across the unique pairs, matching the single-sample matrix's family.
    q = np.full((k, k), np.nan)
    upper = [(i, j) for i in range(k) for j in range(i + 1, k)]
    finite = [(i, j) for i, j in upper if np.isfinite(p[i, j])]
    if finite:
        _, qvals, _, _ = multipletests([p[i, j] for i, j in finite], method="fdr_bh")
        for (i, j), qv in zip(finite, qvals):
            q[i, j] = q[j, i] = float(qv)

    return {
        "col": split_col,
        "levels": [
            {"value": lv["value"], "n_groups": lv["n_groups"], "matrix": _matrix_to_json(lv["r"])}
            for lv in (a, b)
        ],
        "levels_omitted": omitted,
        "delta_matrix": _matrix_to_json(delta),
        "p_matrix": _matrix_to_json(p),
        "q_matrix": _matrix_to_json(q),
        "n_matrix": [[int(v) for v in row] for row in n_min],
        "independent": independent,
        "note": _independence_note(split_col, independent, split_col),
    }






def _independence_note(split_col: str, independent: bool, display: str) -> str:
    """The one-line caveat shown wherever a split comparison is presented."""
    if independent:
        return (f"Each collection sits entirely on one side of {display}, so the two "
                f"correlations come from independent samples and the difference is tested "
                f"directly (Fisher r-to-z).")
    return (f"The same collections appear on both sides of {display}, so the two "
            f"correlations are not independent and no test of the difference is "
            f"reported — compare the two values and their confidence intervals instead.")






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






def load_reliability_map() -> dict:
    """Load the worker-written per-variable reliability artifact, or {}."""
    from fyp.analysis.reliability import RELIABILITY_FILENAME

    try:
        if data_io.exists(storage_location="cache", filename=RELIABILITY_FILENAME):
            payload = data_io.load_json(storage_location="cache",
                                        filename=RELIABILITY_FILENAME, verbose=False)
            if isinstance(payload, dict):
                return payload.get("variables", {}) or {}
    except Exception as e:
        logger.warning(f"Error loading reliability artifact: {e}")
    return {}






def _spearman_brown(item_reliability: float, k: int) -> float:
    """Reliability of a k-item mean given item-level reliability."""
    if item_reliability <= 0:
        return 0.0
    return (k * item_reliability) / (1.0 + (k - 1) * item_reliability)






def column_reliability(columns, col_bases) -> dict:
    """Per-column group-level reliability for the disattenuation toggle.

    A derived column inherits its base variable's item-level estimate, then
    Spearman–Brown at the configured minimum group size converts it to a
    conservative group-mean reliability (real groups have >= that many
    videos, so their true reliability is at least this).
    """
    variables = load_reliability_map()
    if not variables:
        return {}
    k = max(1, int(corr_setting("minimum_group_size")))

    out = {}
    for col in columns:
        base = col_bases.get(col, col)
        est = variables.get(base)
        if not est:
            continue
        item_r = float(est.get("reliability", 0))
        if item_r <= 0:
            continue
        out[col] = {
            "group_r": round(_spearman_brown(item_r, k), 4),
            "item_r": round(item_r, 4),
            "source": est.get("source"),
            "n": est.get("n"),
            "base": base,
        }
    return out






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

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

    # Build factor_values with date handling. A date factor gets a min/max range
    # instead of a checkbox list — one row per day means it always blew past
    # factor_value_limit, which used to render as a dead "too many values to
    # filter" note and left the time window unfilterable. Non-date factors past
    # the limit are still reported as truncated.
    factor_value_limit = int(corr_setting("factor_value_limit"))
    factor_values = {}
    factor_ranges = {}
    truncated_factors = []
    for f in factors:
        is_dt = pd.api.types.is_datetime64_any_dtype(df[f])
        is_date_like = is_dt or "date" in f.lower()
        if is_date_like:
            vals = df[f].dropna().astype(str).str[:10].unique().tolist()
        else:
            vals = df[f].dropna().unique().tolist()

        if is_date_like and vals:
            days = sorted(str(v) for v in vals)
            factor_ranges[f] = {"min": days[0], "max": days[-1], "n_values": len(days)}
            continue

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
        "min_group_size": int(corr_setting("minimum_group_size")),
    }

    # Factors offerable to "Split by". A comparison needs at least two levels
    # and stays readable only for a handful, so collection_id (one level per
    # donor) and the date factors are excluded by the cardinality bound. Each
    # entry carries whether the split partitions collections, which is what
    # decides whether the difference can be formally tested.
    split_cols = []
    for f in sorted(factors):
        values = factor_values.get(f) or []
        if not (2 <= len(values) <= SPLIT_MAX_LEVELS):
            continue
        split_cols.append({
            "col": f,
            "display_name": schema_map.get(f, {}).get("display_name", f),
            "n_levels": len(values),
            "independent": split_is_independent(df, f),
        })

    return {
        "numeric_cols": filtered_numeric_cols,
        "numeric_col_bases": {c: b for c, b in numeric_col_bases.items() if c in filtered_numeric_cols},
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "factor_ranges": factor_ranges,
        "truncated_factors": truncated_factors,
        "filter_immune_views": list(FILTER_IMMUNE_VIEWS),
        "split_cols": split_cols,
        "split_levels": SPLIT_LEVELS,
        "sample": build_sample_summary(df, df),
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






def build_scatter_payload(df: pd.DataFrame, filters: dict, x_col: str, y_col: str,
                          color_col: str | None, center: bool = False,
                          split_col: str | None = None) -> dict:
    """Build the /api/correlations/data response (filtered scatter points).

    ``center=True`` demeans the two axis columns within each collection
    before anything else, so both the points and the statistics describe
    within-collection variation.

    ``split_col`` switches from one regression over everything to one per level
    of that factor, plus a comparison of the two slopes — "does this association
    hold for everyone?" rather than "what does this subset look like". The
    points are coloured by the split so the two clouds are legible.
    """
    filtered_df = apply_factor_filters(df, filters)
    # Summarised before the per-axis dropna, so the Sample panel reports what the
    # filters select rather than what these two axes happen to have values for.
    sample = build_sample_summary(df, filtered_df)

    filtered_df = filtered_df.dropna(subset=[x_col, y_col])

    # Splitting drives the colouring: two differently-coloured clouds with a
    # regression line each is the whole point of the view.
    if split_col and split_col in filtered_df.columns:
        color_col = split_col
    else:
        split_col = None

    centered = False
    if center:
        filtered_df, centered = apply_within_collection_centering(
            filtered_df, [x_col, y_col] if x_col != y_col else [x_col])

    total_count = len(filtered_df)

    # Regression readout + confidence-ellipse inputs are computed on the FULL
    # filtered set — never on the capped display sample below.
    color_for_groups = color_col if (color_col and color_col in filtered_df.columns) else None
    regression_stats = compute_regression_stats(filtered_df[x_col], filtered_df[y_col])
    group_ellipses = compute_group_ellipses(filtered_df, x_col, y_col, color_for_groups)

    split_payload = None
    if split_col:
        split_payload = build_scatter_split(filtered_df, x_col, y_col, split_col)

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
        "sample": sample,
        "stats": regression_stats,
        "split": split_payload,
        "group_ellipses": group_ellipses,
        "ellipse_coverage": ELLIPSE_COVERAGE,
        "centered": centered,
    }






def build_matrix_payload(df: pd.DataFrame, filters: dict, study: str,
                         method: str | None = None,
                         center: bool = False,
                         split_col: str | None = None) -> tuple[dict | None, str | None]:
    """Build the /api/correlations/correlation_matrix response.

    Pairwise-complete correlations with per-pair n, p and Benjamini–Hochberg
    q. Columns are ordered by variable family (base schema variable) so the
    heatmap can separate same-family blocks — cross-family cells compare
    components from *different* per-variable PCA bases and are captioned as
    such in the UI. Returns ``(payload, None)`` on success or
    ``(None, error_message)`` when too few usable numeric columns remain.
    """
    if method not in _VALID_CORRELATION_METHODS:
        method = correlation_method()

    filtered_df = apply_factor_filters(df, filters)

    sample = build_sample_summary(df, filtered_df)

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

    # The split matrices reuse the column set chosen above, so a cell means the
    # same thing in the combined and the per-level views.
    split_payload = None
    if split_col and split_col in filtered_df.columns:
        split_payload = build_matrix_split(
            filtered_df.assign(**{c: numeric_df[c] for c in ordered_cols}),
            ordered_cols, split_col, method,
        )

    return {
        "columns": ordered_cols,
        "split": split_payload,
        "families": [families[c] for c in ordered_cols],
        "matrix": _matrix_to_json(r),
        "p_matrix": _matrix_to_json(p),
        "q_matrix": _matrix_to_json(q),
        "n_matrix": [[int(v) for v in row] for row in n],
        "count": len(filtered_df),
        "sample": sample,
        "method": method,
        "centered": centered,
        # Per-column group-level reliability for the disattenuation toggle
        # (empty until the pca_refresh worker has written the artifact)
        "reliability": column_reliability(ordered_cols, col_bases),
        "reliability_k": max(1, int(corr_setting("minimum_group_size"))),
    }, None
