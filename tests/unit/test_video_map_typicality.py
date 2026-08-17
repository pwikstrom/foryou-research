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






def test_niche_neighbours_are_ordered_by_real_distance():
    """Niche 0 sits beside niche 1; niche 2 is far from both."""
    labels = np.array([0, 0, 1, 1, 2, 2])
    reduced = np.array([
        [0.0, 0.0], [0.2, 0.0],
        [1.0, 0.0], [1.2, 0.0],
        [9.0, 0.0], [9.2, 0.0],
    ], dtype=np.float32)
    meta = {0: {}, 1: {}, 2: {}}

    video_map._add_niche_neighbours(meta, labels, reduced, n_neighbours=2)

    assert meta[0]["nearest"] == [1, 2]
    assert meta[2]["nearest"] == [1, 0]
    # A niche is never its own neighbour.
    assert all(n not in meta[n]["nearest"] for n in meta)






def test_niche_neighbours_respect_the_requested_count():
    labels = np.array([0, 1, 2, 3])
    reduced = np.array([[0.0], [1.0], [2.0], [3.0]], dtype=np.float32)
    meta = {0: {}, 1: {}, 2: {}, 3: {}}

    video_map._add_niche_neighbours(meta, labels, reduced, n_neighbours=2)

    assert all(len(meta[n]["nearest"]) == 2 for n in meta)
    assert meta[0]["nearest"] == [1, 2]






def test_neighbour_preservation_is_perfect_for_a_faithful_layout():
    """A 2D layout that is just the first two columns keeps every neighbour."""
    rng = np.random.RandomState(0)
    reduced = rng.rand(200, 2).astype(np.float32)

    got = video_map._neighbour_preservation(reduced, reduced.copy(), k=5, probe=50)

    assert got["score"] == 1.0
    assert got["k"] == 5
    assert got["mapped"] == 200
    assert got["probe"] == 50






def test_neighbour_preservation_collapses_for_a_scrambled_layout():
    """A layout unrelated to the real space scores near chance, not near 1."""
    rng = np.random.RandomState(0)
    reduced = rng.rand(400, 8).astype(np.float32)
    scrambled = rng.rand(400, 2).astype(np.float32)

    got = video_map._neighbour_preservation(reduced, scrambled, k=10, probe=200)

    assert got["score"] < 0.2
    assert got["chance"] == round(10 / 399, 6)






def test_neighbour_preservation_declines_a_sample_too_small_to_have_neighbours():
    tiny = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32)

    assert video_map._neighbour_preservation(tiny, tiny.copy()) == {}






def test_niche_typicality_percentile_survives_a_single_niche():
    """One niche means no spread to rank against; it must not divide by zero."""
    meta = {0: {}}

    video_map._add_niche_typicality(
        meta, np.array([0, 0]), np.array([0.4, 0.6], dtype=np.float32)
    )

    assert meta[0]["typicality_pct"] == 0.0
    assert meta[0]["typicality"] == 0.5
