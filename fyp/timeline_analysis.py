
import numpy as np
import pandas as pd
import json
from typing import Any


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





def compute_anomalies(vals: list[float], threshold: float = 3.5) -> list[dict[str, Any]]:
    """Detect anomalies via z-scores.

    Threshold aggressively raised to 3.5 to compensate for the fact that a 7-day moving average collapses standard deviation, naturally inflating Z-scores.

    Args:
        vals: Array of share % values.
        threshold: Z-score threshold for flagging anomalies (default 1.75).

    Returns:
        List of anomaly dicts sorted by absolute z-score descending.
    """
    mean = float(np.mean(vals))
    std = float(np.std(vals))
    results = []
    for i, v in enumerate(vals):
        z = (v - mean) / std if std > 0 else 0.0
        if abs(z) > threshold:
            results.append({
                "index": i,
                "value": round(v, 1),
                "z": round(z, 2),
                "mean": round(mean, 1),
            })
    return sorted(results, key=lambda r: abs(r["z"]), reverse=True)





def compute_break(vals: list[float]) -> dict[str, Any]:
    """Detect the single strongest structural break (mean-shift).

    Requires at least 8 data points (4 on each side of the candidate split).

    Args:
        vals: Array of share % values.

    Returns:
        Dictionary with break index, delta (pp shift), mean_before, mean_after.
        Returns empty delta if series is too short.
    """
    n = len(vals)
    best_i = 0
    best_delta = 0.0

    # Need at least 4 points on each side
    if n < 8:
        return {
            "index": 0,
            "delta": 0.0,
            "mean_before": round(float(np.mean(vals)), 1) if vals else 0.0,
            "mean_after": round(float(np.mean(vals)), 1) if vals else 0.0,
        }

    arr = np.asarray(vals)
    cumsum = np.cumsum(arr)
    total_sum = cumsum[-1]

    for i in range(4, n - 4):
        m1 = cumsum[i - 1] / i
        m2 = (total_sum - cumsum[i - 1]) / (n - i)
        delta = m2 - m1
        if abs(delta) > abs(best_delta):
            best_delta = delta
            best_i = i

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





import math

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
            'volatility'.

    Returns:
        Float interestingness score (higher = more interesting).
    """
    # --- 1. Trend score ---
    trend_change = abs(metrics["trend"]["total_change"])
    trend_score = trend_change * 1.5

    # --- 2. Break score ---
    break_delta = abs(metrics["break"]["delta"])
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


def analyse_timeline(timeline_data: dict, interval: str = "day",
                     first_activity_date: str | None = None) -> dict:
    """Analyse a timeline payload and produce per-variable analysis results.

    Takes the JSON output of get_timeline_data() and computes trend, anomaly,
    break, and volatility metrics for each category within each categorical
    variable. Categories are ranked by interestingness score.

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
        ranked categories with their metrics and scores.
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

    result = {}

    for var_name, var_data in variables.items():
        if var_data.get("type") != "categorical":
            continue

        counts_list = var_data.get("counts", [])
        valid_counts = var_data.get("daily_valid_counts", [])
        video_counts = var_data.get("daily_video_counts", [])

        # Slice to post-first-activity data
        sliced_counts = counts_list[start_offset:] if start_offset > 0 else counts_list
        sliced_valid = valid_counts[start_offset:] if start_offset > 0 else valid_counts
        sliced_video = video_counts[start_offset:] if start_offset > 0 else video_counts
        sliced_dates = dates[start_offset:] if start_offset > 0 else dates
        sliced_labels = date_labels[start_offset:] if start_offset > 0 else date_labels

        if len(sliced_counts) < 4:
            continue

        # Parse all JSON strings once upfront to avoid re-parsing per category
        parsed_counts: list[dict] = []
        for day_counts in sliced_counts:
            if isinstance(day_counts, str):
                try:
                    parsed_counts.append(json.loads(day_counts))
                except Exception:
                    parsed_counts.append({})
            elif isinstance(day_counts, dict):
                parsed_counts.append(day_counts)
            else:
                parsed_counts.append({})

        # Collect all category names from pre-parsed data
        all_cats: set[str] = set()
        for dc in parsed_counts:
            all_cats.update(dc.keys())

        # Pre-compute denominator array once (shared across all categories)
        n_periods = len(parsed_counts)
        denominators = []
        for i in range(n_periods):
            total = 1
            if sliced_valid and i < len(sliced_valid):
                total = sliced_valid[i] if sliced_valid[i] and sliced_valid[i] > 0 else 1
            elif sliced_video and i < len(sliced_video):
                total = sliced_video[i] if sliced_video[i] and sliced_video[i] > 0 else 1
            denominators.append(total)

        # Compute share % time series for each category (using pre-parsed data)
        category_results = []

        for cat in all_cats:
            vals = [(parsed_counts[i].get(cat, 0) / denominators[i]) * 100
                    for i in range(n_periods)]

            # Smooth the values before analysis using a 7-day trailing average to match UI
            vals = moving_average(vals, window=7)

            # Skip categories with no meaningful data
            if not vals or max(vals) == 0:
                continue

            # Compute metrics on smoothed post-first-activity data
            trend = compute_linreg(vals)
            anomalies = compute_anomalies(vals)
            brk = compute_break(vals)
            vol = compute_volatility(vals)

            metrics = {
                "trend": trend,
                "anomalies": anomalies,
                "break": brk,
                "volatility": vol,
            }

            score = compute_interestingness(metrics)

            # Offset anomaly and break indices to full-timeline positions
            for a in anomalies:
                a["index"] = a["index"] + start_offset
            brk["index"] = brk["index"] + start_offset

            # Offset trend intercept to be relative to full timeline index 0
            # intercept was computed for sliced index 0 = full index start_offset
            # So for full index 0: y = intercept - slope * start_offset
            trend["intercept"] = round(trend["intercept"] - trend["slope"] * start_offset, 2)

            # Count total occurrences from pre-parsed data
            total_count = sum(dc.get(cat, 0) for dc in parsed_counts)

            category_results.append({
                "id": cat,
                "label": cat,
                "count": total_count,
                "score": score,
                "trend": trend,
                "anomalies": anomalies[:3],
                "break": brk,
                "volatility": vol,
            })

        # Sort by score descending
        category_results.sort(key=lambda x: x["score"], reverse=True)

        result[var_name] = {
            "categories": category_results,
            "time_labels": sliced_labels if sliced_labels else sliced_dates,
            "n_periods": len(sliced_dates),
            "interval": interval,
            "start_offset": start_offset,
        }

    return result
