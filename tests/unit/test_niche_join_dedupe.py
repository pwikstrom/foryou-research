"""_join_niche_columns must not row-duplicate plays on a duplicated map.

Regression for 2026-08-15: twin embedding shards duplicated 10k item_ids in
video_map.parquet; the left join here would have silently duplicated every
matching play row in every recoded study frame.
"""

import pandas as pd
import pytest

from fyp.analysis import organize_datasets as od






@pytest.fixture
def duplicated_map(monkeypatch):
    dup_map = pd.DataFrame({
        "item_id": pd.array(["a", "a", "b"], dtype="string[pyarrow]"),
        "niche": pd.array([1, 2, 3], dtype="int32[pyarrow]"),
        "niche_name": pd.array(["first", "last", "n_b"], dtype="string[pyarrow]"),
    })
    monkeypatch.setattr(od.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(od.data_io, "get_parquet_columns",
                        lambda **kw: list(dup_map.columns))
    monkeypatch.setattr(od.data_io, "load_parquet_selective",
                        lambda storage_location, filename, columns=None, **kw:
                            dup_map[columns].copy() if columns else dup_map.copy())
    return dup_map






def test_duplicated_map_rows_do_not_duplicate_plays(duplicated_map):
    plays = pd.DataFrame({
        "item_id": pd.array(["a", "b", "c"], dtype="string[pyarrow]"),
        "collection_id": pd.array(["col1"] * 3, dtype="string[pyarrow]"),
    })
    out = od._join_niche_columns(plays)
    assert len(out) == 3
    # keep="last" — the later map row wins, matching the embedding store.
    assert out.loc[out["item_id"] == "a", "niche_name"].iloc[0] == "last"
    assert out.loc[out["item_id"] == "c", "niche_name"].iloc[0] == od._NICHE_UNMAPPED
