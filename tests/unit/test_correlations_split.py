"""Correlations "Split by": comparing an association across two levels.

Subsetting answers "what does this look like for these groups"; splitting
answers "does this association differ between them", which is the question a
researcher usually has. The statistical constraint the tests pin down: Fisher's
r-to-z comparison assumes INDEPENDENT samples, which holds only when the split
partitions collections. A within-donor split (weekday) must report both
correlations descriptively and no p-value.
"""

import numpy as np
import pandas as pd

from web_interface.services import correlations_service as svc






def _donor_split_frame(n_per_side=60):
    """Platform-style split: each collection sits entirely on one side.

    x/y correlate positively on side "a" and negatively on side "b", so the
    difference is real and large.
    """
    rng = np.random.RandomState(0)
    rows = []
    for side, sign, offset in (("a", 1.0, 0), ("b", -1.0, 100)):
        for i in range(n_per_side):
            x = float(rng.normal())
            rows.append({
                "collection_id": f"c{offset + i % 10}",
                "platform": side,
                "x": x,
                "y": sign * x + float(rng.normal(scale=0.4)),
            })
    return pd.DataFrame(rows)






def _within_donor_split_frame(n_days=80):
    """Weekday-style split: every collection appears on both sides."""
    rng = np.random.RandomState(1)
    rows = []
    for i in range(n_days):
        for collection in ("c1", "c2", "c3"):
            x = float(rng.normal())
            rows.append({
                "collection_id": collection,
                "local_weekday": "Mon" if i % 2 else "Tue",
                "x": x,
                "y": x + float(rng.normal(scale=0.5)),
            })
    return pd.DataFrame(rows)






def test_split_is_independent_when_collections_are_partitioned():
    assert svc.split_is_independent(_donor_split_frame(), "platform") is True






def test_split_is_not_independent_within_donors():
    assert svc.split_is_independent(_within_donor_split_frame(), "local_weekday") is False






def test_split_is_not_independent_without_a_collection_column():
    """Unverifiable independence must default to False, not to True."""
    df = _donor_split_frame().drop(columns=["collection_id"])
    assert svc.split_is_independent(df, "platform") is False






def test_split_frames_takes_the_two_largest_levels():
    df = pd.DataFrame({"g": ["a"] * 10 + ["b"] * 5 + ["c"] * 2, "x": range(17)})
    pairs, omitted = svc.split_frames(df, "g")

    assert [level for level, _ in pairs] == ["a", "b"]
    assert [len(sub) for _, sub in pairs] == [10, 5]
    assert omitted == ["c"]






def test_fisher_z_flags_a_real_difference():
    z, p = svc.fisher_z_difference(0.8, 60, -0.8, 60)
    assert z > 0
    assert p < 0.001






def test_fisher_z_refuses_degenerate_input():
    assert svc.fisher_z_difference(0.5, 3, 0.2, 50) is None      # n too small
    assert svc.fisher_z_difference(1.0, 50, 0.2, 50) is None      # transform diverges
    assert svc.fisher_z_difference(float("nan"), 50, 0.2, 50) is None






def test_scatter_split_reports_a_p_for_an_independent_split():
    df = _donor_split_frame()
    payload = svc.build_scatter_split(df, "x", "y", "platform")

    assert payload["col"] == "platform"
    assert len(payload["levels"]) == 2
    # Opposite-signed slopes on the two sides.
    rs = [lv["stats"]["r"] for lv in payload["levels"]]
    assert min(rs) < 0 < max(rs)

    comparison = payload["comparison"]
    assert comparison["independent"] is True
    assert comparison["p"] is not None and comparison["p"] < 0.001
    assert "independent samples" in comparison["note"]






def test_scatter_split_withholds_the_p_within_donors():
    """The headline check: no test statistic when the assumption fails."""
    df = _within_donor_split_frame()
    payload = svc.build_scatter_split(df, "x", "y", "local_weekday")

    comparison = payload["comparison"]
    assert comparison["independent"] is False
    assert comparison["p"] is None
    assert comparison["z"] is None
    # Both correlations are still reported — the comparison is descriptive.
    assert all(lv["stats"] is not None for lv in payload["levels"])
    assert comparison["r_difference"] is not None
    assert "not independent" in comparison["note"]






def test_scatter_split_returns_none_with_only_one_usable_level():
    df = _donor_split_frame()
    single = df[df["platform"] == "a"]
    assert svc.build_scatter_split(single, "x", "y", "platform") is None






def test_matrix_split_delta_and_significance():
    df = _donor_split_frame()
    payload = svc.build_matrix_split(df, ["x", "y"], "platform", "pearson")

    assert payload["independent"] is True
    assert len(payload["levels"]) == 2

    # Off-diagonal delta = r(side a) - r(side b), so about +2 for ±0.9.
    delta = payload["delta_matrix"][0][1]
    assert delta > 1.5
    assert payload["p_matrix"][0][1] < 0.001
    assert payload["q_matrix"][0][1] is not None
    # Diagonal carries no difference to report.
    assert payload["delta_matrix"][0][0] is None






def test_matrix_split_has_no_p_within_donors():
    df = _within_donor_split_frame()
    payload = svc.build_matrix_split(df, ["x", "y"], "local_weekday", "pearson")

    assert payload["independent"] is False
    assert payload["delta_matrix"][0][1] is not None
    assert payload["p_matrix"][0][1] is None
    assert payload["q_matrix"][0][1] is None
