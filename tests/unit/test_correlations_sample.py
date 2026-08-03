"""Correlations Sample panel: date-range filtering + the group/video summary.

A row of the PCA frame is one grouping-factor group (a collection-day), not a
video. The date factor has one value per day, which used to blow past
``factor_value_limit`` and render as a dead "too many distinct values to filter"
note — leaving the time window unfilterable. It is now offered as an inclusive
range instead.
"""

import pandas as pd

from web_interface.services import correlations_service as svc






def _frame():
    """Three collections × ten days, with a per-group video count."""
    rows = []
    for collection in ("c1", "c2", "c3"):
        for day in range(1, 11):
            rows.append({
                "collection_id": collection,
                "local_date": f"2026-03-{day:02d}",
                "local_weekday": "Mon" if day % 2 else "Tue",
                "score": float(day),
                svc.GROUP_SIZE_COL: 10 + day,
            })
    return pd.DataFrame(rows)






def test_date_range_is_inclusive_at_both_ends():
    df = _frame()
    out = svc.apply_factor_filters(df, {"local_date": {"min": "2026-03-03", "max": "2026-03-05"}})

    assert sorted(out["local_date"].unique()) == ["2026-03-03", "2026-03-04", "2026-03-05"]
    assert len(out) == 9   # 3 days × 3 collections






def test_date_range_bounds_are_independently_optional():
    df = _frame()

    open_start = svc.apply_factor_filters(df, {"local_date": {"min": None, "max": "2026-03-02"}})
    assert sorted(open_start["local_date"].unique()) == ["2026-03-01", "2026-03-02"]

    open_end = svc.apply_factor_filters(df, {"local_date": {"min": "2026-03-09", "max": ""}})
    assert sorted(open_end["local_date"].unique()) == ["2026-03-09", "2026-03-10"]






def test_date_range_works_on_a_real_datetime_column():
    """Same behaviour when the column is datetime64 rather than text."""
    df = _frame()
    df["local_date"] = pd.to_datetime(df["local_date"])

    out = svc.apply_factor_filters(df, {"local_date": {"min": "2026-03-08", "max": "2026-03-08"}})

    assert len(out) == 3
    assert out["local_date"].dt.day.unique().tolist() == [8]






def test_range_and_list_selections_combine():
    df = _frame()
    out = svc.apply_factor_filters(df, {
        "local_date": {"min": "2026-03-04", "max": "2026-03-06"},
        "collection_id": ["c2"],
    })

    assert len(out) == 3
    assert out["collection_id"].unique().tolist() == ["c2"]






def test_a_range_on_a_non_date_factor_is_ignored():
    """The control is only ever offered for dates; a stray range must not filter."""
    df = _frame()
    out = svc.apply_factor_filters(df, {"collection_id": {"min": "c1", "max": "c2"}})

    assert len(out) == len(df)






def test_sample_summary_counts_groups_and_videos():
    df = _frame()
    filtered = svc.apply_factor_filters(df, {"collection_id": ["c1"]})

    summary = svc.build_sample_summary(df, filtered)

    assert summary["groups_total"] == 30
    assert summary["groups_selected"] == 10
    assert summary["videos_total"] == 3 * sum(10 + d for d in range(1, 11))
    assert summary["videos_selected"] == sum(10 + d for d in range(1, 11))






def test_sample_summary_without_group_size_reports_no_video_counts():
    """PCA parquets built before group_size existed must still summarise."""
    df = _frame().drop(columns=[svc.GROUP_SIZE_COL])

    summary = svc.build_sample_summary(df, df)

    assert summary["groups_total"] == 30
    assert summary["videos_total"] is None
    assert summary["videos_selected"] is None
