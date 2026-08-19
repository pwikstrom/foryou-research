"""The columnar /api/correlations/data payload (2026-08-19 diet).

The old shape carried a server-built hover string plus a {col: value} dict
PER POINT — measured at 5.4 of the 6 MB the endpoint shipped per variable
change. The columnar shape ships each formatted value once per column;
correlations.js assembles hover text and drill-down dicts client-side
(_scatterPointsFrom). These tests pin the columnar contract and the value
semantics the client reconstruction depends on.
"""

import numpy as np
import pandas as pd

from web_interface.services import correlations_service as svc


def _frame():
    rows = []
    for collection in ("c1", "c2"):
        for day in range(1, 6):
            rows.append({
                "collection_id": collection,
                "local_weekday": "Mon" if day % 2 else "Tue",
                "x_metric": float(day),
                "y_metric": float(day) * 2.0,
                "x_metric_raw": float(day) * 100.0,
                svc.VIDEOS_WATCHED_COL: 10 + day,
            })
    df = pd.DataFrame(rows)
    # One NaN factor value: must become null in factors (skipped client-side)
    # and an empty hover cell.
    df.loc[0, "local_weekday"] = np.nan
    return df


def _payload():
    return svc.build_scatter_payload(
        _frame(), "x_metric", "y_metric", "collection_id")


def test_points_are_columnar_and_aligned():
    p = _payload()
    n = p["total_count"]
    assert "data" not in p  # the per-point shape is retired
    assert len(p["points"]["x"]) == len(p["points"]["y"]) == n
    assert len(p["points"]["color"]) == n
    for col in p["hover"]["columns"]:
        assert len(col) == n
    assert len(p["hover"]["labels"]) == len(p["hover"]["columns"])
    for vals in p["factors"].values():
        assert len(vals) == n


def test_first_hover_column_is_the_colour_value():
    p = _payload()
    assert p["hover"]["columns"][0][0] == "c1"
    # colour raw values ride points.color for client-side grouping
    assert set(p["points"]["color"]) == {"c1", "c2"}


def test_factors_carry_raw_strings_and_null_for_nan():
    p = _payload()
    assert p["factors"]["collection_id"][0] == "c1"
    # the NaN weekday must be None (client skips it in the drill-down dict)…
    assert p["factors"]["local_weekday"][0] is None
    # …and empty in its hover column so no "nan" line is rendered.
    weekday_idx = next(
        i for i, lbl in enumerate(p["hover"]["labels"])
        if p["factors"].get("local_weekday") is not None
        and p["hover"]["columns"][i][1] in ("Mon", "Tue"))
    assert p["hover"]["columns"][weekday_idx][0] == ""


def test_raw_columns_become_abs_hover_lines():
    p = _payload()
    abs_labels = [lbl for lbl in p["hover"]["labels"] if lbl.endswith("(Abs)")]
    assert len(abs_labels) == 1
    idx = p["hover"]["labels"].index(abs_labels[0])
    assert p["hover"]["columns"][idx][0] == "100"  # format_value(100.0)
