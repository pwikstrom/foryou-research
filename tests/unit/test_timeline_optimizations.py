"""Unit tests for timeline_analysis optimizations.

Tests compute_break, moving_average, and analyse_timeline with synthetic
data to verify correctness after the performance optimizations.
"""
import json
import math
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from fyp.timeline_analysis import (
    analyse_timeline,
    compute_anomalies,
    compute_break,
    compute_linreg,
    compute_volatility,
    moving_average,
)


def test_compute_break_basic():
    """Break detection should find a clear mean-shift."""
    # 20 values at ~10, then 20 values at ~30
    vals = [10.0] * 20 + [30.0] * 20
    result = compute_break(vals)
    assert result["index"] == 20, f"Expected break at 20, got {result['index']}"
    assert abs(result["delta"] - 20.0) < 0.5, f"Expected delta ~20, got {result['delta']}"
    assert abs(result["mean_before"] - 10.0) < 0.5
    assert abs(result["mean_after"] - 30.0) < 0.5
    print("  PASS: compute_break basic")


def test_compute_break_short_series():
    """Short series should return None (no meaningful break can be located)."""
    vals = [1.0, 2.0, 3.0]
    result = compute_break(vals)
    assert result is None
    print("  PASS: compute_break short series")


def test_compute_break_no_shift():
    """Constant series should have near-zero delta."""
    vals = [5.0] * 20
    result = compute_break(vals)
    assert abs(result["delta"]) < 0.1
    print("  PASS: compute_break no shift")


def test_moving_average_basic():
    """Moving average should smooth a step function."""
    vals = [0.0] * 7 + [10.0] * 7
    result = moving_average(vals, window=7)
    assert len(result) == len(vals)
    # The middle of the step should be partially smoothed
    assert result[0] == 0.0
    assert result[-1] == 10.0
    # Value at the boundary should be between 0 and 10
    assert 0.0 < result[7] < 10.0
    print("  PASS: moving_average basic")


def test_moving_average_single_value():
    """Single value should return itself."""
    result = moving_average([5.0], window=7)
    assert result == [5.0]
    print("  PASS: moving_average single value")


def test_moving_average_matches_manual():
    """Verify centered moving average matches manual calculation."""
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = moving_average(vals, window=3)
    # window=3, center=True: each point averages itself and 1 neighbor each side
    # index 0: mean(1,2) = 1.5
    # index 1: mean(1,2,3) = 2.0
    # index 2: mean(2,3,4) = 3.0
    # index 3: mean(3,4,5) = 4.0
    # index 4: mean(4,5) = 4.5
    expected = [1.5, 2.0, 3.0, 4.0, 4.5]
    for i, (r, e) in enumerate(zip(result, expected)):
        assert abs(r - e) < 0.01, f"Index {i}: expected {e}, got {r}"
    print("  PASS: moving_average matches manual")


def test_analyse_timeline_json_parsing():
    """analyse_timeline should handle both dict and JSON string counts."""
    dates = [f"2024-01-{d:02d}" for d in range(1, 32)]
    labels = [f"{d:02d}/01/24" for d in range(1, 32)]

    # Mix of dict and JSON string counts
    counts = []
    for d in range(31):
        c = {"cat_a": 5 + d, "cat_b": 10 - (d % 5)}
        # Alternate between dict and JSON string to test both paths
        counts.append(json.dumps(c) if d % 2 == 0 else c)

    timeline_data = {
        "dates": dates,
        "date_labels": labels,
        "variables": {
            "test_var": {
                "type": "categorical",
                "counts": counts,
                "daily_valid_counts": [15 + d for d in range(31)],
                "daily_video_counts": [20] * 31,
            }
        },
    }

    result = analyse_timeline(timeline_data, interval="day")
    assert "test_var" in result
    cats = result["test_var"]["categories"]
    assert len(cats) == 2
    cat_ids = {c["id"] for c in cats}
    assert cat_ids == {"cat_a", "cat_b"}

    # Each category should have all required fields
    for cat in cats:
        assert "score" in cat
        assert "trend" in cat
        assert "anomalies" in cat
        assert "break" in cat
        assert "volatility" in cat
        assert "count" in cat
        assert cat["count"] > 0

    print("  PASS: analyse_timeline JSON parsing")


def test_analyse_timeline_first_activity_date():
    """first_activity_date should offset the analysis window."""
    dates = [f"2024-01-{d:02d}" for d in range(1, 21)]
    labels = dates

    # Two categories with meaningful variation so both pass the occurrence
    # floor, the zero-variance gate, and the <2-category cull.
    counts = [{"cat_a": 5 + (d % 3), "cat_b": 3 + (d % 5)} for d in range(20)]

    timeline_data = {
        "dates": dates,
        "date_labels": labels,
        "variables": {
            "v": {
                "type": "categorical",
                "counts": counts,
                "daily_valid_counts": [20] * 20,
                "daily_video_counts": [20] * 20,
            }
        },
    }

    # Without first_activity_date
    r1 = analyse_timeline(timeline_data, interval="day")
    assert r1["v"]["start_offset"] == 0

    # With first_activity_date = day 10
    r2 = analyse_timeline(timeline_data, interval="day", first_activity_date="2024-01-10")
    assert r2["v"]["start_offset"] == 9
    assert r2["v"]["n_periods"] == 11

    print("  PASS: analyse_timeline first_activity_date")


def test_compute_linreg():
    """Linear regression on a perfect line."""
    vals = [float(i) for i in range(10)]
    result = compute_linreg(vals)
    assert abs(result["slope"] - 1.0) < 0.01
    assert abs(result["intercept"] - 0.0) < 0.01
    assert abs(result["total_change"] - 9.0) < 0.1
    print("  PASS: compute_linreg")


def test_compute_anomalies():
    """Spike should be detected as anomaly."""
    vals = [10.0] * 50
    vals[25] = 100.0  # Huge spike
    result = compute_anomalies(vals, threshold=3.0)
    assert len(result) >= 1
    assert result[0]["index"] == 25
    print("  PASS: compute_anomalies")


if __name__ == "__main__":
    print("Running timeline optimization tests...\n")
    test_compute_break_basic()
    test_compute_break_short_series()
    test_compute_break_no_shift()
    test_moving_average_basic()
    test_moving_average_single_value()
    test_moving_average_matches_manual()
    test_compute_linreg()
    test_compute_anomalies()
    test_analyse_timeline_json_parsing()
    test_analyse_timeline_first_activity_date()
    print("\nAll tests passed!")
