
import numpy as np
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





def compute_anomalies(vals: list[float], threshold: float = 1.75) -> list[dict[str, Any]]:
    """Detect anomalies via z-scores.

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

    for i in range(4, n - 4):
        m1 = float(np.mean(vals[:i]))
        m2 = float(np.mean(vals[i:]))
        if abs(m2 - m1) > abs(best_delta):
            best_delta = m2 - m1
            best_i = i

    return {
        "index": best_i,
        "delta": round(best_delta, 1),
        "mean_before": round(float(np.mean(vals[:best_i])), 1),
        "mean_after": round(float(np.mean(vals[best_i:])), 1),
    }





def compute_volatility(vals: list[float]) -> dict[str, float]:
    """Compute volatility metrics.

    Args:
        vals: Array of share % values.

    Returns:
        Dictionary with std and mean.
    """
    return {
        "std": round(float(np.std(vals)), 2),
        "mean": round(float(np.mean(vals)), 2),
    }





def compute_interestingness(metrics: dict) -> float:
    """Compute a composite interestingness score from per-category metrics.

    Higher scores indicate more visually salient patterns (strong trends,
    anomalies, structural breaks, high volatility).

    Args:
        metrics: Dictionary with keys 'trend', 'anomalies', 'break', 'volatility'.

    Returns:
        Float interestingness score.
    """
    trend_score = abs(metrics["trend"]["total_change"]) * 1.8
    anomaly_z_vals = [abs(a["z"]) for a in metrics["anomalies"]]
    anomaly_score = max(anomaly_z_vals, default=0) * 4.0
    break_score = abs(metrics["break"]["delta"]) * 1.2
    volatility_score = metrics["volatility"]["std"] * 0.8
    return round(trend_score + anomaly_score + break_score + volatility_score, 1)





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

        # Collect all category names across sliced time periods
        all_cats = set()
        for day_counts in sliced_counts:
            if isinstance(day_counts, str):
                try:
                    day_counts = json.loads(day_counts)
                except Exception:
                    continue
            all_cats.update(day_counts.keys())

        # Compute share % time series for each category (using sliced data only)
        category_results = []

        for cat in all_cats:
            vals = []
            for i, day_counts in enumerate(sliced_counts):
                if isinstance(day_counts, str):
                    try:
                        day_counts = json.loads(day_counts)
                    except Exception:
                        day_counts = {}

                count = day_counts.get(cat, 0)
                total = 1
                if sliced_valid and i < len(sliced_valid):
                    total = sliced_valid[i] if sliced_valid[i] and sliced_valid[i] > 0 else 1
                elif sliced_video and i < len(sliced_video):
                    total = sliced_video[i] if sliced_video[i] and sliced_video[i] > 0 else 1

                share = (count / total) * 100
                vals.append(share)

            # Skip categories with no meaningful data
            if not vals or max(vals) == 0:
                continue

            # Compute metrics on post-first-activity data
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

            # Count total occurrences (sliced only) for context
            total_count = sum(
                (json.loads(dc) if isinstance(dc, str) else dc).get(cat, 0)
                for dc in sliced_counts
            )

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
