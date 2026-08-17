"""Typicality scoring: closeness to the corpus mean direction, and its
per-niche percentile rank."""

import numpy as np

import fyp.analysis.video_map as video_map






def _corpus():
    """Eight vectors clustered around +x, plus one pointing the other way.

    The last row is the deliberate outlier: it shares no direction with the
    bulk, so it must score lowest on typicality.
    """
    bulk = np.array([
        [1.0, 0.0, 0.0],
        [1.0, 0.1, 0.0],
        [1.0, -0.1, 0.0],
        [1.0, 0.0, 0.1],
        [1.0, 0.0, -0.1],
        [0.9, 0.2, 0.0],
        [0.9, -0.2, 0.0],
        [0.95, 0.0, 0.15],
    ])
    outlier = np.array([[-1.0, 0.0, 0.0]])
    return np.vstack([bulk, outlier]).astype(np.float32)






def test_typicality_ranks_the_outlier_last():
    scores = video_map._typicality(_corpus())

    assert scores.shape == (9,)
    assert scores[-1] == scores.min()
    assert scores[-1] < 0.0 < scores[:-1].min()






def test_typicality_ignores_vector_magnitude():
    """A doubled vector points the same way, so it must score identically.

    Vectors are L2-normalised before the corpus mean is taken; without that,
    a long embedding would read as atypical purely for being long.
    """
    matrix = _corpus()
    scaled = matrix.copy()
    scaled[3] *= 7.0

    np.testing.assert_allclose(
        video_map._typicality(matrix)[3],
        video_map._typicality(scaled)[3],
        rtol=1e-5,
    )






def test_typicality_handles_a_corpus_with_no_mean_direction():
    """Opposed vectors cancel to a zero mean; the score degrades to 0, not NaN."""
    matrix = np.array([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32)

    scores = video_map._typicality(matrix)

    assert not np.isnan(scores).any()
    assert (scores == 0.0).all()






def test_niche_typicality_percentile_follows_the_mean_order():
    labels = np.array([0, 0, 1, 1, 2, 2])
    # Niche 1 is the most typical, niche 0 the least.
    typicality = np.array([0.1, 0.2, 0.8, 0.9, 0.5, 0.6], dtype=np.float32)
    meta = {0: {}, 1: {}, 2: {}}

    video_map._add_niche_typicality(meta, labels, typicality)

    assert meta[0]["typicality"] < meta[2]["typicality"] < meta[1]["typicality"]
    assert meta[0]["typicality_pct"] == 0.0
    assert meta[2]["typicality_pct"] == 50.0
    assert meta[1]["typicality_pct"] == 100.0






def test_niche_typicality_percentile_survives_a_single_niche():
    """One niche means no spread to rank against; it must not divide by zero."""
    meta = {0: {}}

    video_map._add_niche_typicality(
        meta, np.array([0, 0]), np.array([0.4, 0.6], dtype=np.float32)
    )

    assert meta[0]["typicality_pct"] == 0.0
    assert meta[0]["typicality"] == 0.5
