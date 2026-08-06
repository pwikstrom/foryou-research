"""Unit tests for the embedding-entropy measures (fyp.analysis.entropy_metrics)."""

import numpy as np
import pytest

from fyp.analysis import entropy_metrics as em


def _directional(vectors: np.ndarray) -> np.ndarray:
    """Centre on the sample mean and normalise (the standard pipeline)."""
    return em.to_directional(vectors, vectors.mean(axis=0))






def test_identical_vectors_are_maximally_focused():
    rng = np.random.default_rng(1)
    v = rng.normal(size=8)
    mat = np.tile(v, (5, 1))
    corpus_mean = rng.normal(size=8)
    u = em.to_directional(mat, corpus_mean)

    assert em.mean_pairwise_cosine_distance(u) == pytest.approx(0.0, abs=1e-5)
    assert em.coherence(u) == pytest.approx(1.0, abs=1e-5)
    ent, eff_rank = em.spectral_entropy(u)
    assert ent == pytest.approx(0.0, abs=1e-3)
    assert eff_rank == pytest.approx(1.0, abs=1e-2)






def test_orthogonal_vectors_have_full_effective_rank():
    n = 4
    u = np.eye(n, 16)
    ent, eff_rank = em.spectral_entropy(u)
    # After window-centring, n orthonormal vectors span n-1 equal directions.
    assert eff_rank == pytest.approx(n - 1, abs=0.05)
    assert em.mean_pairwise_cosine_distance(u) == pytest.approx(1.0, abs=1e-6)






def test_small_windows_return_nan():
    u = np.ones((1, 8))
    ent, eff_rank = em.spectral_entropy(u)
    assert np.isnan(ent) and np.isnan(eff_rank)
    assert np.isnan(em.mean_pairwise_cosine_distance(u))
    assert np.isnan(em.coherence(np.empty((0, 8))))






def test_weights_shift_the_pairwise_distance():
    # Two tight vectors + one outlier: upweighting the tight pair lowers the
    # weighted mean pairwise distance.
    base = np.zeros(8)
    base[0] = 1.0
    outlier = np.zeros(8)
    outlier[1] = 1.0
    u = np.vstack([base, base, outlier])
    unweighted = em.mean_pairwise_cosine_distance(u)
    weighted = em.mean_pairwise_cosine_distance(u, weights=np.array([10.0, 10.0, 0.1]))
    assert weighted < unweighted






def test_window_metrics_normalised_entropy_bounded():
    rng = np.random.default_rng(2)
    mat = rng.normal(size=(6, 16))
    out = em.window_metrics(mat, corpus_mean=mat.mean(axis=0))
    assert out["n_vectors"] == 6
    assert 0.0 <= out["spectral_entropy_norm"] <= 1.0 + 1e-9
    assert out["spectral_entropy_bits"] <= np.log2(6) + 1e-9
    assert out["dispersion"] == pytest.approx(1.0 - out["coherence"], abs=1e-12)






def test_trajectory_geometry_separates_binge_from_drift():
    rng = np.random.default_rng(3)
    # Directed drift: small steps in one consistent direction on the sphere.
    steps = np.linspace(0, 0.9, 8)
    drift = np.vstack([[np.cos(t), np.sin(t)] + [0.0] * 6 for t in steps])
    geo_drift = em.trajectory_geometry(drift)
    # Stationary binge: jitter around a single direction (returns on itself).
    centre = np.zeros(8)
    centre[0] = 1.0
    binge = centre + 0.05 * rng.normal(size=(8, 8))
    binge /= np.linalg.norm(binge, axis=1, keepdims=True)
    geo_binge = em.trajectory_geometry(binge)

    assert geo_drift["straightness"] > 0.9
    assert geo_binge["straightness"] < geo_drift["straightness"]
    assert geo_binge["diameter"] < drift.shape[0] * geo_drift["step_mean"]
