"""_join_niche_columns must carry the embedding-geometry measures honestly.

``typicality_pct`` and ``niche_isolation_pct`` travel from video_map.parquet
into every study frame as numeric measures, which puts them in the PCA /
correlations feature set. Two properties matter and neither is obvious from
the join code alone: an unmapped video must get a null rather than a
stand-in number (a fabricated measurement would be indistinguishable from a
real one downstream), and a map file written before these columns existed
must still produce them, so an install that has not rebuilt its map yet
merges instead of raising.
"""

import pandas as pd
import pytest

from fyp.analysis import organize_datasets as od






def _install_map(monkeypatch, video_map: pd.DataFrame) -> None:
    """Point the join at an in-memory video map."""
    monkeypatch.setattr(od.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(od.data_io, "get_parquet_columns",
                        lambda **kw: list(video_map.columns))
    monkeypatch.setattr(od.data_io, "load_parquet_selective",
                        lambda storage_location, filename, columns=None, **kw:
                            video_map[columns].copy() if columns else video_map.copy())






@pytest.fixture
def full_map(monkeypatch):
    video_map = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "niche": pd.array([1, 2], dtype="int32[pyarrow]"),
        "niche_name": pd.array(["n_a", "n_b"], dtype="string[pyarrow]"),
        "typicality_pct": pd.array([12.5, 87.5], dtype="double[pyarrow]"),
        "niche_isolation_pct": pd.array([40.0, 60.0], dtype="double[pyarrow]"),
    })
    _install_map(monkeypatch, video_map)
    return video_map






def test_measures_are_joined_onto_mapped_rows(full_map):
    plays = pd.DataFrame({
        "item_id": pd.array(["a", "b", "a"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["col1"] * 3, dtype="string[pyarrow]"),
    })

    out = od._join_niche_columns(plays)

    assert out["typicality_pct"].tolist() == [12.5, 87.5, 12.5]
    assert out["niche_isolation_pct"].tolist() == [40.0, 60.0, 40.0]






def test_unmapped_rows_get_a_null_measure_not_a_stand_in(full_map):
    """The niche label degrades to 'unmapped'; the numbers must not degrade.

    A 0 or a corpus-mean fill would read downstream as a real measurement of
    an unusually atypical video, which it is not.
    """
    plays = pd.DataFrame({
        "item_id": pd.array(["a", "missing"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["col1"] * 2, dtype="string[pyarrow]"),
    })

    out = od._join_niche_columns(plays)

    assert out["niche_name"].tolist() == ["n_a", od._NICHE_UNMAPPED]
    assert out["typicality_pct"].isna().tolist() == [False, True]
    assert out["niche_isolation_pct"].isna().tolist() == [False, True]






def test_a_map_without_the_measures_still_produces_the_columns(monkeypatch):
    """A pre-existing map file predates these columns — backfill, do not raise."""
    _install_map(monkeypatch, pd.DataFrame({
        "item_id": pd.array(["a"], dtype="string[pyarrow]"),
        "niche": pd.array([1], dtype="int32[pyarrow]"),
        "niche_name": pd.array(["n_a"], dtype="string[pyarrow]"),
    }))
    plays = pd.DataFrame({
        "item_id": pd.array(["a", "b"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["col1"] * 2, dtype="string[pyarrow]"),
    })

    out = od._join_niche_columns(plays)

    for col in ("typicality_pct", "niche_isolation_pct"):
        assert col in out.columns, col
        assert out[col].isna().all(), col
        assert str(out[col].dtype) == "double[pyarrow]", col






def test_a_re_merge_refreshes_stale_measures(full_map):
    """Idempotence: values from a previous map build must not survive a re-join."""
    plays = pd.DataFrame({
        "item_id": pd.array(["a"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["col1"], dtype="string[pyarrow]"),
        "typicality_pct": pd.array([99.0], dtype="double[pyarrow]"),
        "niche_isolation_pct": pd.array([99.0], dtype="double[pyarrow]"),
    })

    out = od._join_niche_columns(plays)

    assert out["typicality_pct"].tolist() == [12.5]
    assert out["niche_isolation_pct"].tolist() == [40.0]
    assert len(out) == 1
