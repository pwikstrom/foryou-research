"""The PCA grouping-factor guard must not reject single-collection studies.

Every auto-managed participant study (``__me__<user>``, "Just Me") selects
exactly one collection, so ``collection_id`` is constant in its recoded
dataset. The guard used to terminate whenever ANY grouping factor had one
unique value, which silently denied those studies a ``{study}_PCA.parquet``
and left the Correlations tab permanently empty — even though grouping on
``local_date`` alone is the intended unit for a single-collection study.

The guard still terminates on a grouping factor that carries no usable value
at all, and on a dataset where EVERY grouping factor is constant.
"""

import numpy as np
import pandas as pd

from fyp.pca import calculate_scaled_pca_scores






def _make_study_frame(n_collections: int = 1, n_dates: int = 20,
                      rows_per_group: int = 20, seed: int = 3) -> pd.DataFrame:
    """A minimal but genuine recoded study frame: two features, both roles."""
    rng = np.random.default_rng(seed)
    collections = [f"coll-{i:02d}" for i in range(n_collections)]
    dates = pd.date_range("2026-01-01", periods=n_dates).strftime("%Y-%m-%d")
    categories = ["comedy", "news", "music", "sport"]

    coll_col, date_col = [], []
    for coll in collections:
        for date in dates:
            coll_col += [coll] * rows_per_group
            date_col += [date] * rows_per_group

    n_rows = len(coll_col)
    return pd.DataFrame({
        "collection_id": pd.array(coll_col, dtype="string[pyarrow]"),
        "local_date": pd.array(date_col, dtype="string[pyarrow]"),
        "duration": pd.array(rng.uniform(5, 90, n_rows), dtype="double[pyarrow]"),
        "content_category": pd.array(rng.choice(categories, n_rows),
                                     dtype="string[pyarrow]"),
        "annotated_ok": pd.array([True] * n_rows, dtype="bool[pyarrow]"),
    })






def test_single_collection_study_produces_pca_scores():
    scores, _interpretations = calculate_scaled_pca_scores(
        study_recoded_dataset=_make_study_frame(n_collections=1),
        load_from_cache=False, save_to_cache=False, verbose=False)

    assert scores is not None, "a single-collection study must still get PCA scores"
    # One group per (collection, date) — the constant collection_id stays in
    # the index so the group-stats artifact keeps its collection level.
    assert len(scores) == 20
    assert {"collection_id", "local_date"} <= set(scores.columns)
    assert scores["collection_id"].nunique() == 1






def test_multi_collection_study_is_unchanged():
    scores, _interpretations = calculate_scaled_pca_scores(
        study_recoded_dataset=_make_study_frame(n_collections=3),
        load_from_cache=False, save_to_cache=False, verbose=False)

    assert scores is not None
    assert len(scores) == 60
    assert scores["collection_id"].nunique() == 3






def test_all_constant_grouping_factors_terminates():
    df = _make_study_frame(n_collections=1, n_dates=1, rows_per_group=400)

    scores, _interpretations = calculate_scaled_pca_scores(
        study_recoded_dataset=df, load_from_cache=False,
        save_to_cache=False, verbose=False)

    assert scores is None






def test_all_na_grouping_factor_terminates():
    df = _make_study_frame(n_collections=1)
    df["local_date"] = pd.array([None] * len(df), dtype="string[pyarrow]")

    scores, _interpretations = calculate_scaled_pca_scores(
        study_recoded_dataset=df, load_from_cache=False,
        save_to_cache=False, verbose=False)

    assert scores is None
