"""Equality safety net for the high-cardinality PCA path.

The 2026-08-08/09 pca_refresh OOM fix replaces the all-categories dense
crosstab with a survivors-only frame plus exact long-format/sparse
computations for everything that consumed the full distribution (entropy,
top1, PC sign-fix, axis interpretation, _raw probabilities). These tests
assert the two paths publish the SAME numbers on the same data — the property
that made this design shippable where the rejected top-N cap was not.

The old path is obtained by monkeypatching DENSE_CATEGORY_LIMIT high (the
gate never fires); the new path by patching it low (the gate always fires for
the fixture's cardinality).
"""

import numpy as np
import pandas as pd
import pytest

from fyp import pca as pca_mod
from fyp.pca import (
    transform_categories_to_components_and_diversity,
    transform_category_column_to_counts_df,
)

DROP_RARE = 0.01






def _make_events(n_categories: int = 300, n_groups: int = 40, seed: int = 7,
                 list_valued: bool = False, tie_group: bool = True) -> pd.DataFrame:
    """Synthetic events: zipf-ish category mass so a handful clear 1%.

    Two grouping factors (collection_id, local_date) — the production
    MultiIndex shape that single-factor tests silently pass.
    """
    rng = np.random.default_rng(seed)
    cats = [f"category number {i:04d} with a somewhat long name" for i in range(n_categories)]
    # Zipf-ish: category i has weight 1/(i+1); the head clears 1%, the tail not.
    weights = 1.0 / np.arange(1, n_categories + 1)
    weights /= weights.sum()

    n_rows = 6000
    cat_idx = rng.choice(n_categories, size=n_rows, p=weights)
    coll = rng.choice([f"c{i:02d}" for i in range(n_groups // 4)], size=n_rows)
    date = rng.choice(pd.date_range("2026-01-01", periods=4).strftime("%Y-%m-%d"), size=n_rows)

    values: list = [cats[i] for i in cat_idx]
    if list_valued:
        # Every 10th row carries two categories (explode path).
        values = [[v, cats[(i + 1) % n_categories]] if k % 10 == 0 else [v]
                  for k, (v, i) in enumerate(zip(values, cat_idx))]

    df = pd.DataFrame({
        "the_cat": values,
        "collection_id": pd.array(coll, dtype="string[pyarrow]"),
        "local_date": pd.array(date, dtype="string[pyarrow]"),
    })
    if tie_group:
        # A group whose two most frequent categories tie exactly — exercises
        # idxmax's first-in-column-order tie-break.
        tie = pd.DataFrame({
            "the_cat": [cats[3]] * 5 + [cats[1]] * 5 if not list_valued
                       else [[cats[3]]] * 5 + [[cats[1]]] * 5,
            "collection_id": pd.array(["tie"] * 10, dtype="string[pyarrow]"),
            "local_date": pd.array(["2026-01-01"] * 10, dtype="string[pyarrow]"),
        })
        df = pd.concat([df, tie], ignore_index=True)
    return df






def _both_paths(events: pd.DataFrame, monkeypatch, drop_rare: float = DROP_RARE):
    """Run the dense (old) and high-cardinality (new) paths on the same data."""
    factors = ["collection_id", "local_date"]

    monkeypatch.setattr(pca_mod, "DENSE_CATEGORY_LIMIT", 10**9)
    dense = transform_category_column_to_counts_df(
        events, the_column="the_cat", grouping_factors=factors,
        drop_rare_globally_below=drop_rare)
    assert "pca_full_dist" not in dense.attrs

    # 50: below the fixture's ~300 categories (gate fires) but above its ~14
    # survivors (the survivors cap must NOT bite — production keeps 1000 vs a
    # maximum of 100 survivors at the 1% threshold).
    monkeypatch.setattr(pca_mod, "DENSE_CATEGORY_LIMIT", 50)
    sparse = transform_category_column_to_counts_df(
        events, the_column="the_cat", grouping_factors=factors,
        drop_rare_globally_below=drop_rare)
    assert "pca_full_dist" in sparse.attrs
    return dense, sparse






def _run_transform(counts_df, drop_rare: float = DROP_RARE):
    return transform_categories_to_components_and_diversity(
        counts_df=counts_df, drop_rare_globally_below=drop_rare)






@pytest.mark.parametrize("list_valued", [False, True])
def test_published_outputs_identical(monkeypatch, list_valued):
    events = _make_events(list_valued=list_valued)
    dense, sparse = _both_paths(events, monkeypatch)

    old_result, old_pc, old_xx = _run_transform(dense)
    new_result, new_pc, new_xx = _run_transform(sparse)

    # Same groups, same columns.
    assert list(old_result.columns) == list(new_result.columns)
    pd.testing.assert_index_equal(old_result.index, new_result.index)

    # Entropy: identical distribution, different summation order.
    np.testing.assert_allclose(
        old_result["entropy"].to_numpy(dtype=float),
        new_result["entropy"].to_numpy(dtype=float), rtol=1e-10, atol=1e-12)

    # top1: exact, including the tie group.
    assert old_result["top1"].tolist() == new_result["top1"].tolist()
    assert "tie" in old_result.index.get_level_values(0)

    # PC scores: same probability matrix in, same PCA out.
    np.testing.assert_allclose(
        old_pc.to_numpy(dtype=float), new_pc.to_numpy(dtype=float),
        rtol=1e-8, atol=1e-10)

    # Interpretation: identical strings and picked categories.
    assert old_xx.keys() == new_xx.keys()
    for col in old_xx:
        for key in ("top_positive", "top_negative",
                    "top_positive_cat", "top_negative_cat"):
            assert old_xx[col].get(key) == new_xx[col].get(key), (col, key)
        assert old_xx[col].get("explained_variance_pct") == pytest.approx(
            new_xx[col].get("explained_variance_pct"))






def test_survivor_set_matches_downstream_drop(monkeypatch):
    """The pre-filter must keep exactly what _prepare_probability_matrix kept."""
    events = _make_events()
    dense, sparse = _both_paths(events, monkeypatch)

    global_mass = dense.sum(axis=0)
    global_mass /= global_mass.sum()
    downstream_kept = set(global_mass[global_mass >= DROP_RARE].index)
    assert set(sparse.columns) == downstream_kept






def test_dense_frame_is_bounded_and_zero_padded(monkeypatch):
    events = _make_events()
    dense, sparse = _both_paths(events, monkeypatch)

    # Survivors-only width, full group index (MultiIndex preserved).
    assert sparse.shape[1] < dense.shape[1]
    pd.testing.assert_index_equal(sparse.index, dense.index)
    assert isinstance(sparse.index, pd.MultiIndex)
    assert sparse.index.names == ["collection_id", "local_date"]

    # Surviving columns' counts identical to the full crosstab's.
    pd.testing.assert_frame_equal(sparse, dense[list(sparse.columns)],
                                  check_names=False)






def test_degenerate_fallback_bounded(monkeypatch):
    """Nothing clears the threshold -> bounded top-N head, not everything."""
    rng = np.random.default_rng(3)
    n = 5000
    cats = [f"unique category value number {i:05d}" for i in range(n)]
    events = pd.DataFrame({
        "the_cat": rng.permutation(cats),  # every category appears exactly once
        "collection_id": pd.array(["c1", "c2"] * (n // 2), dtype="string[pyarrow]"),
        "local_date": pd.array(["2026-01-01"] * n, dtype="string[pyarrow]"),
    })
    monkeypatch.setattr(pca_mod, "DENSE_CATEGORY_LIMIT", 100)
    out = transform_category_column_to_counts_df(
        events, the_column="the_cat", grouping_factors=["collection_id", "local_date"],
        drop_rare_globally_below=DROP_RARE)
    assert out.shape[1] == pca_mod.RARE_FALLBACK_TOP_N
    assert out.attrs["pca_full_dist"]["n_categories"] == n






def test_no_threshold_keeps_historical_path(monkeypatch):
    """drop_rare=None (external callers) must never engage the new path."""
    events = _make_events(n_categories=50)
    monkeypatch.setattr(pca_mod, "DENSE_CATEGORY_LIMIT", 10)
    out = transform_category_column_to_counts_df(
        events, the_column="the_cat", grouping_factors=["collection_id", "local_date"])
    assert "pca_full_dist" not in out.attrs
    assert out.shape[1] == 50
