"""The semantic map's clustering warm-starts from the previous build's niches.

2026-09-03: an append of 97 vectors to ~620k re-clustered with a fresh
k-means++ start into a partition where only 90 of 150 niches kept enough
overlap with their predecessor to carry their name; the other 60 were renamed
through Gemini — 244 s of a 466 s run — for no real change. Starting the fit
from each previous niche's members averaged in the current PCA space makes
the new clustering a refinement of the old one.

Usage:
    python -m pytest tests/unit/test_video_map_warm_start.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.cluster import MiniBatchKMeans

import fyp.data_io as data_io
from fyp.analysis import video_map as vm


def _prev_map(monkeypatch, item_ids, niches):
    df = pd.DataFrame({"item_id": item_ids, "niche": niches, "niche_name": [f"n{n}" for n in niches]})
    monkeypatch.setattr(data_io, "exists", lambda **k: True)
    monkeypatch.setattr(data_io, "load_parquet_selective",
                        lambda storage_location="", filename="", columns=None, **k: df[columns].copy())


def _blobs(seed=0, n_per=40, k=3, dim=5):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, dim)) * 10
    X = np.vstack([centres[i] + rng.normal(size=(n_per, dim)) for i in range(k)])
    labels = np.repeat(np.arange(k), n_per)
    ids = [f"v{i}" for i in range(len(labels))]
    return ids, X.astype(np.float32), labels


def test_centroids_are_previous_niche_means_in_the_current_space(monkeypatch):
    ids, X, prev_labels = _blobs()
    _prev_map(monkeypatch, ids, prev_labels + 10)   # previous ids 10,11,12

    cents, reason = vm._warm_start_centroids(ids, X, n_niches=3)

    assert cents is not None and cents.shape == (3, X.shape[1])
    for k in range(3):
        np.testing.assert_allclose(cents[k], X[prev_labels == k].mean(axis=0), rtol=1e-5)
    assert "3 niches" in reason


def test_cold_start_when_nothing_usable(monkeypatch):
    ids, X, prev_labels = _blobs()
    monkeypatch.setattr(data_io, "exists", lambda **k: False)
    assert vm._warm_start_centroids(ids, X, 3) == (None, "no previous map")

    _prev_map(monkeypatch, ids, prev_labels)
    cents, reason = vm._warm_start_centroids(ids, X, n_niches=4)
    assert cents is None and "wants 4" in reason

    # items the previous map never saw: no shared members
    _prev_map(monkeypatch, [f"other{i}" for i in range(len(ids))], prev_labels)
    assert vm._warm_start_centroids(ids, X, 3)[0] is None

    # a niche whose members mostly vanished cannot seed a centroid
    thin = prev_labels.copy()
    thin[prev_labels == 2] = 0
    thin[:2] = 2
    _prev_map(monkeypatch, ids, thin)
    cents, reason = vm._warm_start_centroids(ids, X, 3)
    assert cents is None and "surviving members" in reason


def test_warm_start_keeps_the_partition_after_a_small_append(monkeypatch):
    """The point of the change: a few new points must not redraw the niches."""
    ids, X, prev_labels = _blobs(seed=1, n_per=60)
    # A previous build clustered these (ids permuted, as a fresh run would give).
    _prev_map(monkeypatch, ids, (prev_labels + 1) % 3)
    # Append a handful of new points near each blob.
    rng = np.random.default_rng(7)
    extra = np.vstack([X[prev_labels == k][:1] + rng.normal(scale=0.5, size=(2, X.shape[1]))
                       for k in range(3)]).astype(np.float32)
    ids2 = ids + [f"new{i}" for i in range(len(extra))]
    X2 = np.vstack([X, extra])

    cents, _ = vm._warm_start_centroids(ids2, X2, 3)
    assert cents is not None
    labels = MiniBatchKMeans(n_clusters=3, init=cents, n_init=1, random_state=0,
                             batch_size=64).fit_predict(X2)

    # Every old point stays with the niche it started in (up to relabelling).
    aligned, carried = vm._align_labels_to_previous(ids2, labels, "niche", "niche_name")
    prev_ids = (prev_labels + 1) % 3
    assert (aligned[:len(ids)] == prev_ids).all(), "warm-started niches must keep their members"
    assert len(carried) == 3, "every previous name carries over"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
