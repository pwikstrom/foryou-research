"""The run-scoped enrichment preload loads each blob once, not once per study.

Pinned 2026-09-03: recode_refresh_studies over five studies downloaded
scrapes_recoded.parquet (327 MB) five times and machine_annotations_recoded
.parquet (479 MB) four times — 3.5 GB and 54 s of a 179 s run — because every
study's path ends in _filter_enrichment_data, which reads from storage whenever
the study's tutti_data has no frame yet. Inside `enrichment_preload()` the first
study parks the full frame and later studies filter the parked copy.

Invariants:
  * outside a preload, behaviour is unchanged (one load per call);
  * inside, one load per label for the whole run, lazily on first need;
  * callers never receive the parked frame by reference;
  * __exit__ drops the stash, and a nested block defers to the outer owner.

Usage:
    python -m pytest tests/unit/test_enrichment_preload.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

import fyp.analysis.datasets.common as datasets_common
import fyp.analysis.datasets.loading as datasets_loading
import fyp.analysis.organize_datasets as od
import fyp.core.data_io as data_io


@pytest.fixture
def counted_storage(monkeypatch):
    """Stand in for the two recoded blobs; count how often each is read."""
    frames = {
        f"{datasets_common._scrapes_label()}_recoded.parquet": pd.DataFrame(
            {"item_id": ["a", "b", "c"], "views": [1, 2, 3]}
        ),
        f"{datasets_common._machine_annotations_label()}_recoded.parquet": pd.DataFrame(
            {"item_id": ["a", "c", "d"], "topic": ["x", "y", "z"]}
        ),
    }
    loads: list[str] = []

    def _exists(storage_location="recoded", filename="", **kw):
        return filename in frames

    def _load(storage_location="recoded", filename="", **kw):
        loads.append(filename)
        return frames[filename].copy()

    monkeypatch.setattr(data_io, "exists", _exists)
    monkeypatch.setattr(data_io, "load_parquet", _load)
    return loads


def _filter(unique_videos):
    tutti: dict = {}
    datasets_loading._filter_enrichment_data(
        tutti, set(unique_videos), study_name="s", verbose=False
    )
    return tutti


def test_without_a_preload_every_call_loads(counted_storage):
    _filter({"a"})
    _filter({"a"})
    assert len(counted_storage) == 4  # 2 labels x 2 calls


def test_inside_a_preload_each_blob_loads_once(counted_storage):
    with datasets_loading.enrichment_preload():
        first = _filter({"a", "b"})
        second = _filter({"c"})
        third = _filter({"zzz"})
    assert len(counted_storage) == 2, counted_storage
    assert sorted(first[datasets_common._scrapes_label()]["item_id"]) == ["a", "b"]
    assert list(second[datasets_common._scrapes_label()]["item_id"]) == ["c"]
    assert list(second[datasets_common._machine_annotations_label()]["item_id"]) == ["c"]
    assert third[datasets_common._scrapes_label()].empty


def test_callers_never_get_the_parked_frame(counted_storage):
    with datasets_loading.enrichment_preload():
        out = _filter({"a", "b", "c"})
        parked = datasets_loading._ENRICHMENT_PRELOAD[datasets_common._scrapes_label()]
        assert out[datasets_common._scrapes_label()] is not parked
        out[datasets_common._scrapes_label()].loc[:, "views"] = -1
        assert (parked["views"] > 0).all(), "a caller mutated the parked frame"


def test_exit_releases_the_stash_even_on_error(counted_storage):
    with pytest.raises(RuntimeError):
        with datasets_loading.enrichment_preload():
            _filter({"a"})
            assert datasets_loading._ENRICHMENT_PRELOAD
            raise RuntimeError("study blew up")
    assert datasets_loading._ENRICHMENT_PRELOAD is None
    _filter({"a"})
    assert len(counted_storage) == 4  # back to one load per call


def test_nested_block_defers_to_the_outer_owner(counted_storage):
    with datasets_loading.enrichment_preload():
        _filter({"a"})
        with datasets_loading.enrichment_preload():
            _filter({"b"})
        # The inner exit must not have emptied the outer run's stash.
        assert (
            datasets_loading._ENRICHMENT_PRELOAD is not None
            and datasets_loading._ENRICHMENT_PRELOAD
        )
        _filter({"c"})
    assert len(counted_storage) == 2
    assert datasets_loading._ENRICHMENT_PRELOAD is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
