
import math

import numpy as np
import pandas as pd
import json
from typing import Any


# --- Tuning constants -----------------------------------------------------
# These are gathered here so future adjustments don't require hunting through
# the body of analyse_timeline().

# A category must have at least this many total events to be analysed.  Scales
# mildly with window length so long-running collections don't accumulate
# hundreds of near-noise categories; never drops below this absolute floor.
MIN_TOTAL_COUNT_ABS = 10
MIN_TOTAL_COUNT_FRACTION_OF_PERIODS = 0.02

# A category must be active on at least this many distinct days — a "trend"
# over one or two active days isn't a trend.
MIN_NONZERO_DAYS = 5

# Z-score (MAD-based) threshold for flagging a smoothed value as an anomaly.
ANOMALY_Z_THRESHOLD = 3.5

# Number of non-zero smoothed values required before anomaly detection is
# considered reliable; sparse mostly-zero series collapse std/MAD and inflate
# Z-scores spuriously.
ANOMALY_MIN_NONZERO = 10

# Minimum absolute total_change (pp) for a trend to be reported — anything
# smaller is dominated by smoothing noise.  Scales with mean share so large
# categories aren't held to the same absolute bar.
TREND_FLOOR_PP = 1.0
TREND_FLOOR_FRACTION_OF_MEAN = 0.05

# Break-on-edge rejection: candidate split indices must sit inside this
# fraction of the window, so the start/end transitions (0 → data → 0) can't
# masquerade as structural breaks.
BREAK_EDGE_FRACTION = 0.10

# Zero-variance variable gate: skip the entire variable if a single category
# owns > DOMINANCE_SHARE on more than DOMINANCE_DAY_FRACTION of the window.
DOMINANCE_SHARE = 0.95
DOMINANCE_DAY_FRACTION = 0.90

# Top-K cap on the number of categories returned per variable.  Deeper than
# the UI's display budget so explicit user selections of mid-ranked cats are
# still covered.
TOP_K_CATEGORIES = 30

# Synthetic bucket name for categories that fall below the occurrence floor.
OTHER_BUCKET_LABEL = "Other"

# Collections with fewer than this many active days (per ('personas',
# 'active_days') in collections_metadata) don't have enough data points for
# the 7-day moving average, break detection, or anomaly stats to produce
# meaningful results.  Enforced at the dropdown (disabled option), at the
# bulk timeline refresh worker (skipped), and at lazy analyse_timeline calls.
MIN_ACTIVE_DAYS_FOR_TIMELINE = 14




def compute_linreg(vals: list[float]) -> dict[str, float]:
    """Compute linear regression (trend) on a time series.

    Args:
        vals: Array of share % values, one per time bucket.

    Returns:
        Dictionary with slope, intercept, total_change, and mean.
    """
    x = np.arange(len(vals))
    slope, intercept = np.polyfit(x, vals, 1)
    return {
        "slope": round(float(slope), 3),
        "intercept": round(float(intercept), 2),
        "total_change": round(float(slope * (len(vals) - 1)), 1),
        "mean": round(float(np.mean(vals)), 1),
    }




def compute_anomalies(vals: list[float], threshold: float = ANOMALY_Z_THRESHOLD) -> list[dict[str, Any]]:
    """Detect anomalies via robust MAD-based z-scores.

    Uses median absolute deviation rather than std because daily category
    share data is heavy-tailed and a single outlier inflates std enough to
    mask other genuine spikes.  Falls back to std when MAD collapses to zero
    (series dominated by a single repeated value).

    Guards against degenerate mostly-zero series: when both std and MAD
    collapse, any small blip produces huge Z-scores.  Categories with fewer
    than ANOMALY_MIN_NONZERO non-zero smoothed periods return [].

    Args:
        vals: Array of share % values.
        threshold: |z| threshold for flagging anomalies.

    Returns:
        List of anomaly dicts sorted by absolute z-score descending.
    """
    nonzero_count = sum(1 for v in vals if v > 0)
    if nonzero_count < ANOMALY_MIN_NONZERO:
        return []

    arr = np.asarray(vals, dtype=np.float64)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))

    if mad > 0:
        z_scores = 0.6745 * (arr - median) / mad
    else:
        std = float(np.std(arr))
        if std > 0:
            z_scores = (arr - float(np.mean(arr))) / std
        else:
            return []

    mean_val = float(np.mean(arr))
    results = []
    for i, z in enumerate(z_scores):
        if abs(z) > threshold:
            results.append({
                "index": int(i),
                "value": round(float(arr[i]), 1),
                "z": round(float(z), 2),
                "mean": round(mean_val, 1),
            })
    return sorted(results, key=lambda r: abs(r["z"]), reverse=True)




def compute_break(vals: list[float]) -> dict[str, Any] | None:
    """Detect the single strongest structural break (mean-shift).

    Candidate split points are restricted to the inner portion of the window
    (BREAK_EDGE_FRACTION on each side), because the initial ramp-up and
    final ramp-down of a series routinely produce large mechanical deltas
    that have nothing to do with a genuine shift in behaviour.

    Args:
        vals: Array of share % values.

    Returns:
        Dictionary with break index, delta (pp shift), mean_before, mean_after,
        or None when the series is too short for a meaningful break.
    """
    n = len(vals)

    edge_buffer = max(4, int(n * BREAK_EDGE_FRACTION))
    lo = edge_buffer
    hi = n - edge_buffer
    if hi - lo < 2:
        return None

    arr = np.asarray(vals, dtype=np.float64)
    cumsum = np.cumsum(arr)
    total_sum = cumsum[-1]

    indices = np.arange(lo, hi)
    m1 = cumsum[indices - 1] / indices
    m2 = (total_sum - cumsum[indices - 1]) / (n - indices)
    deltas = m2 - m1
    best_local = int(np.argmax(np.abs(deltas)))
    best_i = int(indices[best_local])
    best_delta = float(deltas[best_local])

    return {
        "index": best_i,
        "delta": round(best_delta, 1),
        "mean_before": round(float(np.mean(vals[:best_i])), 1),
        "mean_after": round(float(np.mean(vals[best_i:])), 1),
    }




def compute_volatility(vals: list[float]) -> dict[str, float]:
    """Compute volatility metrics including detrended residual std.

    Raw std conflates trend-driven spread with genuine noise.  The
    residual_std removes the linear trend first so only the irregular
    component remains — a category with a clean ramp from 5% to 25%
    will have high std but low residual_std.

    Args:
        vals: Array of share % values.

    Returns:
        Dictionary with std, residual_std, and mean.
    """
    x = np.arange(len(vals))
    slope, intercept = np.polyfit(x, vals, 1)
    trend_line = slope * x + intercept
    residuals = np.array(vals) - trend_line
    return {
        "std": round(float(np.std(vals)), 2),
        "residual_std": round(float(np.std(residuals)), 2),
        "mean": round(float(np.mean(vals)), 2),
    }




def compute_interestingness(metrics: dict) -> float:
    """Compute a composite interestingness score from per-category metrics.

    Higher scores indicate more visually salient patterns.  The score
    combines four signals that capture different kinds of "interesting":

    1. Trend — sustained directional change over the full period.
    2. Anomalies — unusual spikes or dips.  Uses the sum of the top-3
       z-scores so categories with multiple anomalies rank higher than
       those with a single outlier.
    3. Structural break — a step-change in the mean level.  When a
       strong break is present the trend component is discounted to
       avoid double-counting (a big break mechanically inflates the
       linear trend).
    4. Residual volatility — irregular fluctuation *after* removing the
       linear trend.  This rewards genuinely erratic series rather than
       those that simply ramp up/down smoothly.

    All four sub-scores are normalised to roughly comparable 0-20
    ranges before summing, so no single signal dominates via an
    outsized multiplier.

    Args:
        metrics: Dictionary with keys 'trend', 'anomalies', 'break',
            'volatility'.  'break' may be None for degenerate series.

    Returns:
        Float interestingness score (higher = more interesting).
    """
    # --- 1. Trend score ---
    trend_change = abs(metrics["trend"]["total_change"])
    trend_score = trend_change * 1.5

    # --- 2. Break score ---
    brk = metrics.get("break")
    break_delta = abs(brk["delta"]) if brk else 0.0
    break_score = break_delta * 1.2

    # Discount trend when a strong break explains most of the change.
    # A break that accounts for > 50% of the trend means the "trend"
    # is really just the break — halve the trend contribution.
    if trend_change > 0 and break_delta > 0:
        overlap_ratio = min(break_delta / trend_change, 1.0)
        if overlap_ratio > 0.5:
            trend_score *= (1.0 - overlap_ratio * 0.6)

    # --- 3. Anomaly score (sum of top-3 z-scores) ---
    anomaly_z_vals = sorted([abs(a["z"]) for a in metrics["anomalies"]], reverse=True)
    top_z_sum = sum(anomaly_z_vals[:3])
    anomaly_score = top_z_sum * 1.5

    # --- 4. Residual volatility (detrended) ---
    residual_std = metrics["volatility"].get("residual_std", metrics["volatility"]["std"])
    volatility_score = residual_std * 1.5

    raw_score = trend_score + anomaly_score + break_score + volatility_score

    # Volume multiplier — lightly boost categories with larger mean shares
    # so that a 20% category edges out a 0.3% category at equal raw scores.
    # Range ~0.8x to ~1.2x.
    mean_share = metrics["trend"]["mean"]
    volume_multiplier = (math.log10(mean_share + 1) * 0.2) + 0.8

    return round(raw_score * volume_multiplier, 1)




def moving_average(vals: list[float], window: int = 7) -> list[float]:
    """Compute a centred moving average matching the frontend."""
    s = pd.Series(vals)
    smoothed = s.rolling(window, center=True, min_periods=1).mean()
    return [round(v, 2) for v in smoothed.tolist()]




def _parse_daily_counts(sliced_counts: list) -> list[dict]:
    """Coerce each day's count entry into a dict, tolerating JSON strings."""
    parsed: list[dict] = []
    for entry in sliced_counts:
        if isinstance(entry, str):
            try:
                parsed.append(json.loads(entry))
            except Exception:
                parsed.append({})
        elif isinstance(entry, dict):
            parsed.append(entry)
        else:
            parsed.append({})
    return parsed




def _tally_per_category(parsed_counts: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    """Return (total_counts, non_zero_days) per category over the window."""
    totals: dict[str, float] = {}
    nonzero_days: dict[str, int] = {}
    for dc in parsed_counts:
        for cat, count in dc.items():
            if count and count > 0:
                totals[cat] = totals.get(cat, 0) + count
                nonzero_days[cat] = nonzero_days.get(cat, 0) + 1
    return totals, nonzero_days




def _build_denominator(n_periods: int,
                       sliced_valid: list | None,
                       sliced_video: list | None) -> np.ndarray:
    """Pick a per-day denominator (valid annotations → videos → 1)."""
    denom = np.ones(n_periods, dtype=np.float64)
    for i in range(n_periods):
        if sliced_valid and i < len(sliced_valid):
            val = sliced_valid[i]
            if val and val > 0:
                denom[i] = float(val)
                continue
        if sliced_video and i < len(sliced_video):
            val = sliced_video[i]
            if val and val > 0:
                denom[i] = float(val)
    return denom




def _is_zero_variance_variable(per_cat_totals: dict[str, float],
                               parsed_counts: list[dict],
                               denom_arr: np.ndarray) -> bool:
    """True when a single category's share exceeds DOMINANCE_SHARE on most days.

    Such variables are structurally incapable of producing interesting
    timeline patterns, so we skip them entirely rather than waste compute
    generating flat charts for them.
    """
    if not per_cat_totals:
        return True
    dominant = max(per_cat_totals, key=lambda c: per_cat_totals[c])
    n_periods = len(parsed_counts)
    if n_periods == 0:
        return True
    dominant_shares = np.zeros(n_periods, dtype=np.float64)
    for i, dc in enumerate(parsed_counts):
        cnt = dc.get(dominant, 0) or 0
        if denom_arr[i] > 0:
            dominant_shares[i] = cnt / denom_arr[i]
    days_dominant = int(np.sum(dominant_shares > DOMINANCE_SHARE))
    return days_dominant / n_periods > DOMINANCE_DAY_FRACTION




def _vectorised_breaks(smoothed_matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool]:
    """Locate each category's strongest break in a single broadcasted pass.

    Applies the same break-on-edge rejection as the scalar compute_break().

    Returns:
        (best_indices, best_deltas, mean_before, mean_after, has_any_break).
        When the window is too short for a meaningful break, has_any_break
        is False and the arrays are filled with sentinel values.
    """
    n_cats, n_periods = smoothed_matrix.shape
    edge_buffer = max(4, int(n_periods * BREAK_EDGE_FRACTION))
    lo = edge_buffer
    hi = n_periods - edge_buffer
    if hi - lo < 2:
        z = np.zeros(n_cats, dtype=np.float64)
        return (np.full(n_cats, -1, dtype=np.int64), z, z.copy(), z.copy(), False)

    cumsum = np.cumsum(smoothed_matrix, axis=1)
    total_sum = cumsum[:, -1]
    indices = np.arange(lo, hi, dtype=np.int64)
    # Broadcast cumsum[:, indices-1] across candidates for every category.
    left_cumsum = cumsum[:, indices - 1]
    m1 = left_cumsum / indices[np.newaxis, :]
    m2 = (total_sum[:, np.newaxis] - left_cumsum) / (n_periods - indices)[np.newaxis, :]
    deltas = m2 - m1
    best_local = np.argmax(np.abs(deltas), axis=1)
    cat_range = np.arange(n_cats)
    best_idx = indices[best_local]
    best_deltas = deltas[cat_range, best_local]

    # Mean-before / mean-after at the chosen split.
    mean_before = cumsum[cat_range, best_idx - 1] / best_idx
    mean_after = (total_sum - cumsum[cat_range, best_idx - 1]) / (n_periods - best_idx)
    return best_idx, best_deltas, mean_before, mean_after, True




def _vectorised_anomalies(smoothed_matrix: np.ndarray,
                          threshold: float = ANOMALY_Z_THRESHOLD) -> np.ndarray:
    """Compute MAD-based |z| > threshold mask for every category at once.

    Falls back to std-based z-scores when MAD collapses (series dominated by
    a single repeated value).  Categories whose MAD and std are both zero
    get all-False rows.

    Returns a (n_cats, n_periods) boolean mask and the matching z-score
    matrix (same shape) packed as (mask, z_matrix).
    """
    medians = np.median(smoothed_matrix, axis=1, keepdims=True)
    abs_dev = np.abs(smoothed_matrix - medians)
    mads = np.median(abs_dev, axis=1, keepdims=True)

    means = smoothed_matrix.mean(axis=1, keepdims=True)
    stds = smoothed_matrix.std(axis=1, keepdims=True)

    # MAD-based where available, else std-based, else zero.
    z_mad = np.zeros_like(smoothed_matrix)
    mask_mad = mads[:, 0] > 0
    if np.any(mask_mad):
        z_mad[mask_mad] = 0.6745 * (smoothed_matrix[mask_mad] - medians[mask_mad]) / mads[mask_mad]
    mask_std_only = (~mask_mad) & (stds[:, 0] > 0)
    if np.any(mask_std_only):
        z_mad[mask_std_only] = (smoothed_matrix[mask_std_only] - means[mask_std_only]) / stds[mask_std_only]
    return z_mad




def analyse_timeline(timeline_data: dict, interval: str = "day",
                     first_activity_date: str | None = None) -> dict:
    """Analyse a timeline payload and produce per-variable analysis results.

    Takes the JSON output of get_timeline_data() and computes trend, anomaly,
    break, and volatility metrics for each category within each categorical
    variable. Categories are ranked by interestingness score.

    Performance: the per-variable work is now vectorised over all surviving
    categories at once — the Python loop is reserved for packaging results.
    Categories that fall below the occurrence floor are pruned *before* the
    counts/share matrices are built, so high-cardinality variables (hashtags,
    brands) no longer pay for building wide matrices full of noise columns.

    Quality: several sources of meaningless statistics are suppressed —
    degenerate anomalies on sparse series, structural-break candidates near
    window edges, and trends smaller than smoothing noise.  Variables
    dominated by a single category are skipped entirely.  Pruned categories
    are summed into a synthetic "Other" bucket so overall coverage stays
    honest without polluting the top of the ranking.

    When first_activity_date is provided, only data on or after that date is
    analysed. Output indices are stored relative to the full (unsliced) dates
    array so the frontend can map them to the correct x-axis positions.

    Args:
        timeline_data: The dict returned by get_timeline_data().
        interval: The aggregation interval ('day', 'week', 'month').
        first_activity_date: ISO date string (YYYY-MM-DD). If set, data before
            this date is excluded from analysis.

    Returns:
        Dictionary mapping variable names to analysis results, containing
        ranked categories with their metrics and scores.  Each variable entry
        may include an "other_members" list naming the categories that were
        folded into the synthetic "Other" bucket.
    """
    if not timeline_data or "variables" not in timeline_data:
        return {}

    dates = timeline_data.get("dates", [])
    date_labels = timeline_data.get("date_labels", [])
    variables = timeline_data.get("variables", {})

    # Determine the start offset if first_activity_date is provided
    start_offset = 0
    if first_activity_date and dates:
        for idx, d in enumerate(dates):
            if d >= first_activity_date:
                start_offset = idx
                break

    result: dict = {}

    for var_name, var_data in variables.items():
        var_result = _analyse_variable(
            var_data=var_data,
            dates=dates,
            date_labels=date_labels,
            start_offset=start_offset,
            interval=interval,
        )
        if var_result is not None:
            result[var_name] = var_result

    return result




def _analyse_variable(var_data: dict,
                      dates: list[str],
                      date_labels: list[str],
                      start_offset: int,
                      interval: str) -> dict | None:
    """Produce the analysis entry for a single categorical variable.

    Returns None when the variable should be omitted (wrong type, too short,
    zero-variance, or < 2 survivors after filtering).
    """
    if var_data.get("type") != "categorical":
        return None

    counts_list = var_data.get("counts", [])
    valid_counts = var_data.get("daily_valid_counts", [])
    video_counts = var_data.get("daily_video_counts", [])

    sliced_counts = counts_list[start_offset:] if start_offset > 0 else counts_list
    sliced_valid = valid_counts[start_offset:] if start_offset > 0 else valid_counts
    sliced_video = video_counts[start_offset:] if start_offset > 0 else video_counts
    sliced_dates = dates[start_offset:] if start_offset > 0 else dates
    sliced_labels = date_labels[start_offset:] if start_offset > 0 else date_labels

    if len(sliced_counts) < 4:
        return None

    parsed_counts = _parse_daily_counts(sliced_counts)
    n_periods = len(parsed_counts)

    per_cat_totals, per_cat_nonzero = _tally_per_category(parsed_counts)
    if not per_cat_totals:
        return None

    denom_arr = _build_denominator(n_periods, sliced_valid, sliced_video)

    # (#12) Zero-variance gate: one category dominates nearly every day.
    if _is_zero_variance_variable(per_cat_totals, parsed_counts, denom_arr):
        return None

    # (#3) Apply occurrence floors BEFORE building matrices.
    min_total_count = max(MIN_TOTAL_COUNT_ABS,
                          int(n_periods * MIN_TOTAL_COUNT_FRACTION_OF_PERIODS))
    kept_cats: list[str] = []
    dropped_cats: list[str] = []
    for cat, total in per_cat_totals.items():
        if total >= min_total_count and per_cat_nonzero.get(cat, 0) >= MIN_NONZERO_DAYS:
            kept_cats.append(cat)
        else:
            dropped_cats.append(cat)

    if not kept_cats:
        return None

    # (#11) Fold dropped categories into a synthetic "Other" series when
    # there's something to fold and the label doesn't clash with a real cat.
    include_other = (bool(dropped_cats)
                     and OTHER_BUCKET_LABEL not in per_cat_totals)

    analysis_cats = sorted(kept_cats)
    if include_other:
        analysis_cats.append(OTHER_BUCKET_LABEL)

    # (#8) A variable with fewer than 2 analysable categories produces a
    # flat chart — skip the whole thing.
    if len(analysis_cats) < 2:
        return None

    # Build the share matrix from the backend's pre-computed share_series so
    # the analysis overlays and the chart traces use the same numbers.  The
    # "Other" bucket's per-day share is the sum of dropped categories' shares,
    # which is exact because all shares were computed against the same per-day
    # denominator (`share_denominator` in var_data).
    share_series_raw = var_data.get("share_series") or []
    sliced_shares = share_series_raw[start_offset:] if start_offset > 0 else share_series_raw
    parsed_shares = _parse_daily_counts(sliced_shares)

    if len(parsed_shares) == n_periods:
        df_shares = pd.DataFrame(parsed_shares).fillna(0)
        if include_other:
            existing_dropped = [c for c in dropped_cats if c in df_shares.columns]
            if existing_dropped:
                df_shares[OTHER_BUCKET_LABEL] = df_shares[existing_dropped].sum(axis=1)
            else:
                df_shares[OTHER_BUCKET_LABEL] = 0.0
        share_matrix = df_shares.reindex(columns=analysis_cats,
                                         fill_value=0).values.T.astype(np.float64)
    else:
        # Legacy fallback for callers that pass a result without share_series
        # (e.g. old fixtures, external callers).  Keeps the function from
        # crashing but warns so we notice the cache mismatch.
        if share_series_raw:
            print(f"WARN: share_series/counts length mismatch "
                  f"({len(parsed_shares)} vs {n_periods}); recomputing from counts.")
        df_counts = pd.DataFrame(parsed_counts).fillna(0)
        if include_other:
            existing_dropped = [c for c in dropped_cats if c in df_counts.columns]
            if existing_dropped:
                df_counts[OTHER_BUCKET_LABEL] = df_counts[existing_dropped].sum(axis=1)
            else:
                df_counts[OTHER_BUCKET_LABEL] = 0.0
        counts_matrix = df_counts.reindex(columns=analysis_cats,
                                          fill_value=0).values.T.astype(np.float64)
        share_matrix = (counts_matrix / denom_arr[np.newaxis, :]) * 100.0

    n_cats = share_matrix.shape[0]

    # 7-day centred moving average — preserves the smoothing semantics used
    # by all downstream metrics (slope, breaks, anomalies, volatility).
    smoothed_df = pd.DataFrame(share_matrix.T).rolling(
        7, center=True, min_periods=1).mean()
    smoothed_matrix = np.round(smoothed_df.values.T, 2)

    # --- Vectorised per-category metrics (#1) ---
    x = np.arange(n_periods, dtype=np.float64)
    x_mean = x.mean()
    x_var = float(np.sum((x - x_mean) ** 2))
    y_means = smoothed_matrix.mean(axis=1)
    if x_var > 0:
        slopes = ((smoothed_matrix - y_means[:, np.newaxis])
                  * (x - x_mean)[np.newaxis, :]).sum(axis=1) / x_var
    else:
        slopes = np.zeros(n_cats)
    intercepts = y_means - slopes * x_mean
    total_changes = slopes * (n_periods - 1)

    # Volatility (raw std + residual std after removing linear trend).
    trend_lines = (slopes[:, np.newaxis] * x[np.newaxis, :]
                   + intercepts[:, np.newaxis])
    residuals = smoothed_matrix - trend_lines
    residual_stds = residuals.std(axis=1)
    stds = smoothed_matrix.std(axis=1)

    # Break detection (#7 edge rejection baked in).
    brk_idx, brk_delta, brk_before, brk_after, has_breaks = _vectorised_breaks(smoothed_matrix)

    # MAD-based anomalies (#10).
    z_matrix = _vectorised_anomalies(smoothed_matrix)
    anomaly_mask = np.abs(z_matrix) > ANOMALY_Z_THRESHOLD
    nonzero_counts = (smoothed_matrix > 0).sum(axis=1)

    # --- Package per-category results ---
    category_results: list[dict] = []
    for cat_idx, cat in enumerate(analysis_cats):
        mean_val = float(y_means[cat_idx])
        if mean_val == 0 and smoothed_matrix[cat_idx].max() == 0:
            # Defensive: purely-zero series should not have survived the
            # occurrence filter, but skip just in case (e.g. all counts
            # fell on days outside the denominator).
            continue

        # The "Other" bucket is a residual aggregate of heterogeneous
        # low-occurrence categories — its rising/falling/spiking signals
        # aren't interpretable (any of the 10k+ bundled cats could be
        # driving them) and letting it compete for interestingness crowds
        # out real categories.  Emit a minimal marker entry without stats.
        if include_other and cat == OTHER_BUCKET_LABEL:
            reported_count = int(sum(per_cat_totals.get(c, 0)
                                     for c in dropped_cats))
            category_results.append({
                "id": cat,
                "label": cat,
                "count": reported_count,
                "score": None,
                "trend": None,
                "anomalies": [],
                "break": None,
                "volatility": None,
                "render_worthy": False,
                "is_other": True,
            })
            continue

        slope = float(slopes[cat_idx])
        intercept = float(intercepts[cat_idx])
        total_change = float(total_changes[cat_idx])

        # (#6) Suppress trends that are below the smoothing-noise floor —
        # they're not trends, just the shape of the moving average.
        trend_floor = max(TREND_FLOOR_PP, TREND_FLOOR_FRACTION_OF_MEAN * mean_val)
        if abs(total_change) < trend_floor:
            trend = {
                "slope": 0.0,
                "intercept": round(mean_val, 2),
                "total_change": 0.0,
                "mean": round(mean_val, 1),
            }
        else:
            trend = {
                "slope": round(slope, 3),
                "intercept": round(intercept, 2),
                "total_change": round(total_change, 1),
                "mean": round(mean_val, 1),
            }

        # Anomalies — enforce the sparse-series guard.
        anomalies: list[dict] = []
        if nonzero_counts[cat_idx] >= ANOMALY_MIN_NONZERO:
            idxs = np.where(anomaly_mask[cat_idx])[0]
            for i in idxs:
                anomalies.append({
                    "index": int(i),
                    "value": round(float(smoothed_matrix[cat_idx, i]), 1),
                    "z": round(float(z_matrix[cat_idx, i]), 2),
                    "mean": round(mean_val, 1),
                })
            anomalies.sort(key=lambda r: abs(r["z"]), reverse=True)

        # (#5) Break: None when the window is too short for detection.
        if not has_breaks or brk_idx[cat_idx] < 0:
            brk: dict | None = None
        else:
            brk = {
                "index": int(brk_idx[cat_idx]),
                "delta": round(float(brk_delta[cat_idx]), 1),
                "mean_before": round(float(brk_before[cat_idx]), 1),
                "mean_after": round(float(brk_after[cat_idx]), 1),
            }

        vol = {
            "std": round(float(stds[cat_idx]), 2),
            "residual_std": round(float(residual_stds[cat_idx]), 2),
            "mean": round(mean_val, 2),
        }

        metrics = {"trend": trend, "anomalies": anomalies,
                   "break": brk, "volatility": vol}
        score = compute_interestingness(metrics)

        # Offset anomaly and break indices to full-timeline positions.
        for a in anomalies:
            a["index"] = a["index"] + start_offset
        if brk is not None:
            brk["index"] = brk["index"] + start_offset

        # Offset trend intercept to be relative to full timeline index 0.
        trend["intercept"] = round(trend["intercept"]
                                   - trend["slope"] * start_offset, 2)

        # (#9) render_worthy: does this category have ANY feature strong
        # enough to surface a chip in the UI?  Pre-computing this avoids
        # duplicating the thresholds in JS.
        render_worthy = (
            abs(trend["total_change"]) > 4
            or len(anomalies) > 0
            or (brk is not None and abs(brk["delta"]) > 4)
            or vol["std"] > 2.5
        )

        reported_count = int(per_cat_totals.get(cat, 0))

        category_results.append({
            "id": cat,
            "label": cat,
            "count": reported_count,
            "score": score,
            "trend": trend,
            "anomalies": anomalies[:3],
            "break": brk,
            "volatility": vol,
            "render_worthy": bool(render_worthy),
            "is_other": False,
        })

    # (#8) Variable-level cull post-filter.
    if len(category_results) < 2:
        return None

    # Sort by score descending, but pin the "Other" bucket to the end
    # regardless of its (null) score so it doesn't crowd out real cats.
    category_results.sort(
        key=lambda r: (r.get("is_other", False),
                       -(r["score"] if r["score"] is not None else 0)))

    # (#4) Cap the response at TOP_K_CATEGORIES. "Other" is exempt from
    # the cap — it's a residual bucket, not a competing category.
    if len(category_results) > TOP_K_CATEGORIES:
        real = [r for r in category_results if not r.get("is_other")]
        other = [r for r in category_results if r.get("is_other")]
        category_results = real[:TOP_K_CATEGORIES] + other

    entry: dict = {
        "categories": category_results,
        "time_labels": sliced_labels if sliced_labels else sliced_dates,
        "n_periods": len(sliced_dates),
        "interval": interval,
        "start_offset": start_offset,
    }
    if include_other:
        entry["other_members"] = sorted(dropped_cats)
    return entry
