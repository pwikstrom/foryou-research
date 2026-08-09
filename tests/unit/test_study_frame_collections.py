"""``get_study_frame_collections``: what a study's built dataset actually contains.

A study's ``SELECTED_COLLECTIONS`` is a request, not a result — the date window
and the group/activity-count thresholds can drop a selected collection to zero
rows. Surfaces that render "the study's data" from a global artifact (the
Sessions tab) must scope to the frame, so this helper's fallbacks matter:
``None`` (unbuilt study) and ``set()`` (built, but nothing survived) mean
different things and must never be conflated.
"""

import pyarrow as pa
import pytest

from web_interface.services import study_data


@pytest.fixture(autouse=True)
def clear_cache():
    with study_data._frame_collections_lock:
        study_data._frame_collections_cache.clear()
    yield




def _batches(*chunks):
    """A fake iter_parquet_batches yielding one collection_id batch per chunk."""
    return [pa.record_batch({"collection_id": pa.array(list(chunk), type=pa.string())})
            for chunk in chunks]




def test_none_when_the_study_frame_does_not_exist(monkeypatch):
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: None)
    assert study_data.get_study_frame_collections("nope") is None




def test_none_for_a_falsy_study_name():
    assert study_data.get_study_frame_collections("") is None




def test_reads_the_sidecar_cells_without_touching_the_parquet(monkeypatch):
    """Sampling active: selected_cells keys are the frame's collections."""
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: 1.0)
    monkeypatch.setattr(study_data, "get_study_sidecar", lambda study: {
        "sampling_active": True,
        "selected_cells": {"colA": ["2026-01-01"], "colB": ["2026-01-02"]},
    })

    def _boom(**kwargs):
        raise AssertionError("must not read the parquet when the sidecar answers")

    monkeypatch.setattr(study_data.data_io, "iter_parquet_batches", _boom)
    assert study_data.get_study_frame_collections("s") == {"colA", "colB"}




def test_falls_back_to_the_frame_column_without_sampling(monkeypatch):
    """Streamed: distinct ids accumulate across batches, nulls dropped."""
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: 1.0)
    monkeypatch.setattr(study_data, "get_study_sidecar", lambda study: {"sampling_active": False})
    monkeypatch.setattr(study_data.data_io, "iter_parquet_batches",
                        lambda **kwargs: _batches(["colA", "colA", None], ["colC", "colA"]))
    assert study_data.get_study_frame_collections("s") == {"colA", "colC"}




def test_empty_frame_is_an_empty_set_not_none(monkeypatch):
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: 1.0)
    monkeypatch.setattr(study_data, "get_study_sidecar", lambda study: None)
    monkeypatch.setattr(study_data.data_io, "iter_parquet_batches", lambda **kwargs: _batches([]))
    assert study_data.get_study_frame_collections("s") == set()




def test_unreadable_frame_is_none(monkeypatch):
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: 1.0)
    monkeypatch.setattr(study_data, "get_study_sidecar", lambda study: None)

    def _raise(**kwargs):
        raise OSError("corrupt parquet")

    monkeypatch.setattr(study_data.data_io, "iter_parquet_batches", _raise)
    assert study_data.get_study_frame_collections("s") is None




def test_cache_is_keyed_on_the_frame_mtime(monkeypatch):
    mtimes = {"value": 1.0}
    reads = {"n": 0}
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda study: mtimes["value"])
    monkeypatch.setattr(study_data, "get_study_sidecar", lambda study: None)

    def _read(**kwargs):
        reads["n"] += 1
        return _batches([f"col{reads['n']}"])

    monkeypatch.setattr(study_data.data_io, "iter_parquet_batches", _read)

    assert study_data.get_study_frame_collections("s") == {"col1"}
    assert study_data.get_study_frame_collections("s") == {"col1"}
    assert reads["n"] == 1

    # A refresh worker rewrote the parquet — the cached answer must not survive.
    mtimes["value"] = 2.0
    assert study_data.get_study_frame_collections("s") == {"col2"}
    assert reads["n"] == 2
