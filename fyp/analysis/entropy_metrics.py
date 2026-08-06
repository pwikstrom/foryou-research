"""Entropy and dispersion measures computed directly on dense embeddings.

Defines "how focused / homogeneous is a set of videos" as a property of the
raw dense embedding vectors, **not** of the discrete ``niche`` labels nor the
2D map. The niche-distribution Shannon entropy used by the Semantic Space
trajectory overlay throws away the within-niche geometry; these measures
keep it.

Geometry pipeline (shared by every measure):
    1. **Corpus-mean-centre.** Text-embedding models are anisotropic (all
       vectors share a large common offset — ``gemini-embedding-001`` has a
       baseline cosine of ~0.8), so the corpus mean is subtracted first —
       otherwise every pair looks similar.
    2. **L2-normalise.** Centred vectors are projected to the unit sphere, so
       "distance" is angular (cosine) — the geometry the map/clustering use.

Three companion measures, all derived from those directional vectors ``U``:
    * :func:`spectral_entropy` — von Neumann entropy of the window's Gram
      matrix in bits. The principled "entropy on the embeddings": the effective
      number of orthogonal semantic *directions* the window's content spans.
      ``low H`` = a focused, low-entropy hour. Well-defined even when there are
      far fewer videos than dimensions (it uses the small n x n Gram).
    * :func:`mean_pairwise_cosine_distance` — ``1 - mean_{i<j} cos(u_i, u_j)``.
      The headline-interpretable diversity: "on average, how unalike were the
      videos." Robust to window size (a per-pair average, not a sum).
    * :func:`coherence` / dispersion — ``||mean(U)||`` in ``[0, 1]``; 1 when
      every video points the same way, ~0 when they cancel out.

Ranking guardrails (validated in the embedding-entropy study): never rank
windows on absolute entropy bits (bounded by ``log2(n)``, so small windows
look focused for free — rank on cosine distance or the normalised entropy),
and dedupe repeated plays of the same video before measuring (a rewatch loop
collapses the effective rank and fakes a binge).
"""

import numpy as np

# Numerical floors: vectors landing within EPS_NORM of the corpus mean have no
# stable direction; eigenvalues below EPS_EIG (after normalisation) are treated
# as zero so they neither inflate nor destabilise the entropy sum.
EPS_NORM = 1e-8
EPS_EIG = 1e-12




def to_directional(matrix: np.ndarray, corpus_mean: np.ndarray) -> np.ndarray:
    """Corpus-mean-centre then L2-normalise an embedding matrix.

    Args:
        matrix: An ``(n, d)`` array of raw embeddings.
        corpus_mean: The ``(d,)`` global mean used to remove the model's
            anisotropic common offset before measuring angles.

    Returns:
        An ``(n, d)`` array of unit-norm directional vectors.
    """
    centred = matrix.astype(np.float64) - corpus_mean.astype(np.float64)
    norms = np.linalg.norm(centred, axis=1, keepdims=True)
    norms = np.where(norms < EPS_NORM, EPS_NORM, norms)
    return centred / norms




def spectral_entropy(unit_vectors: np.ndarray, weights: np.ndarray | None = None) -> tuple[float, float]:
    """Von Neumann (spectral) entropy of a window's directional vectors.

    The window-centred Gram matrix ``Z Z^T`` is eigendecomposed; its
    eigenvalues, normalised to sum to one, form a probability distribution
    whose Shannon entropy is the von Neumann entropy of the (normalised)
    covariance. It answers "across how many independent semantic directions is
    the watched content spread" — the embedding-native analogue of niche
    entropy. The Gram matrix is ``n x n`` (number of videos), so the measure is
    defined and cheap even when ``n`` is far below the embedding dimension.

    Args:
        unit_vectors: An ``(n, d)`` array of directional vectors (see
            :func:`to_directional`).
        weights: Optional ``(n,)`` non-negative per-video weights (e.g. watch
            time). ``None`` weights every video equally.

    Returns:
        A tuple ``(entropy_bits, effective_rank)`` where ``effective_rank`` is
        ``2 ** entropy_bits`` (the effective number of semantic directions).
        Returns ``(nan, nan)`` for fewer than two videos.
    """
    n = unit_vectors.shape[0]
    if n < 2:
        return float("nan"), float("nan")

    if weights is None:
        w = np.ones(n, dtype=np.float64)
    else:
        w = np.clip(np.asarray(weights, dtype=np.float64), 0.0, None)
        if w.sum() <= 0:
            w = np.ones(n, dtype=np.float64)
    w = w / w.sum()

    # Weighted window-centring, then a weighted Gram so each video contributes
    # in proportion to its weight (sqrt(w) on each side reproduces the weighted
    # covariance spectrum on the smaller n x n matrix).
    mean = (unit_vectors * w[:, None]).sum(axis=0)
    centred = unit_vectors - mean
    scaled = centred * np.sqrt(w)[:, None]
    gram = scaled @ scaled.T

    vals = np.linalg.eigvalsh(gram)
    vals = np.clip(vals, 0.0, None)
    total = vals.sum()
    if total <= 0:
        return 0.0, 1.0
    p = vals / total
    p = p[p > EPS_EIG]
    entropy = float(-(p * np.log2(p)).sum())
    return entropy, float(2.0 ** entropy)




def mean_pairwise_cosine_distance(unit_vectors: np.ndarray, weights: np.ndarray | None = None) -> float:
    """Mean ``1 - cosine`` over all distinct video pairs in a window.

    Args:
        unit_vectors: An ``(n, d)`` array of directional vectors.
        weights: Optional ``(n,)`` weights; pair ``(i, j)`` is weighted by
            ``w_i * w_j``.

    Returns:
        The weighted mean pairwise cosine distance in ``[0, 2]`` (typically
        ``[0, 1]`` after centring), or ``nan`` for fewer than two videos. Low
        values mean the videos are mutually alike (a homogeneous hour).
    """
    n = unit_vectors.shape[0]
    if n < 2:
        return float("nan")
    sim = unit_vectors @ unit_vectors.T
    iu = np.triu_indices(n, k=1)
    pair_sim = sim[iu]
    if weights is None:
        return 1.0 - float(pair_sim.mean())
    w = np.clip(np.asarray(weights, dtype=np.float64), 0.0, None)
    pw = w[iu[0]] * w[iu[1]]
    if pw.sum() <= 0:
        return 1.0 - float(pair_sim.mean())
    return 1.0 - float((pair_sim * pw).sum() / pw.sum())




def coherence(unit_vectors: np.ndarray, weights: np.ndarray | None = None) -> float:
    """Length of the mean directional vector — a ``[0, 1]`` focus score.

    Args:
        unit_vectors: An ``(n, d)`` array of directional vectors.
        weights: Optional ``(n,)`` weights.

    Returns:
        ``||mean(U)||`` — ``1.0`` when every video points the same way (maximal
        focus, zero dispersion), approaching ``0`` when directions cancel.
    """
    n = unit_vectors.shape[0]
    if n < 1:
        return float("nan")
    if weights is None:
        mean = unit_vectors.mean(axis=0)
    else:
        w = np.clip(np.asarray(weights, dtype=np.float64), 0.0, None)
        if w.sum() <= 0:
            w = np.ones(n, dtype=np.float64)
        mean = (unit_vectors * (w / w.sum())[:, None]).sum(axis=0)
    return float(np.linalg.norm(mean))




def trajectory_geometry(unit_ordered: np.ndarray) -> dict:
    """Shape of a focused run — distinguishes a stationary binge from a drift.

    Two runs can both be locally focused (every video close to the last) yet be
    very different globally: a **stationary binge** sits in one place (small
    diameter), whereas a **directed drift** — the "led deeper" rabbit hole —
    takes small steps but *travels*, ending far from where it began. Straightness
    (net displacement ÷ path length, in the directional embedding space)
    separates them: ~1 = a directed line, ~0 = wandering or returning.

    Args:
        unit_ordered: An ``(k, d)`` array of directional vectors in time order
            (one per distinct video in the run).

    Returns:
        A dict with ``diameter`` (max pairwise cosine distance), ``step_mean``
        (mean consecutive cosine distance), ``path_len`` and ``net_disp``
        (chordal, on the unit sphere), and ``straightness``. Degenerate fields
        are ``nan`` for fewer than two videos.
    """
    k = unit_ordered.shape[0]
    if k < 2:
        return {"diameter": 0.0, "step_mean": 0.0, "path_len": 0.0,
                "net_disp": 0.0, "straightness": float("nan")}
    sim = unit_ordered @ unit_ordered.T
    cosd = np.clip(1.0 - sim, 0.0, 2.0)
    iu = np.triu_indices(k, k=1)
    diameter = float(cosd[iu].max())
    step_cos = cosd[np.arange(k - 1), np.arange(1, k)]
    step_mean = float(step_cos.mean())
    diffs = unit_ordered[1:] - unit_ordered[:-1]
    path_len = float(np.linalg.norm(diffs, axis=1).sum())
    net_disp = float(np.linalg.norm(unit_ordered[-1] - unit_ordered[0]))
    straightness = float(net_disp / path_len) if path_len > 0 else float("nan")
    return {"diameter": diameter, "step_mean": step_mean, "path_len": path_len,
            "net_disp": net_disp, "straightness": straightness}




def window_metrics(
        matrix: np.ndarray,
        corpus_mean: np.ndarray,
        weights: np.ndarray | None = None,
    ) -> dict:
    """Compute all embedding-entropy measures for one window's videos.

    Args:
        matrix: An ``(n, d)`` array of the window's raw embeddings (one row per
            play; repeated plays of the same video are allowed and correctly
            drive entropy down).
        corpus_mean: The ``(d,)`` global mean for anisotropy removal.
        weights: Optional ``(n,)`` per-play weights.

    Returns:
        A metrics dict with ``spectral_entropy_bits``, ``spectral_entropy_norm``,
        ``effective_rank``, ``mean_pairwise_cosine_distance``, ``coherence``,
        ``dispersion``, and ``n_vectors``.
    """
    u = to_directional(matrix, corpus_mean)
    entropy, eff_rank = spectral_entropy(u, weights)
    coh = coherence(u, weights)
    n = int(matrix.shape[0])

    # Absolute spectral entropy is bounded by log2(n), so it cannot be compared
    # across windows of different size — a small window looks "low entropy" for
    # free. The normalised form (fraction of the maximum) and the per-pair
    # cosine distance are the size-robust focus measures; rank on those.
    ent_norm = float(entropy / np.log2(n)) if (n > 1 and np.isfinite(entropy)) else float("nan")
    return {
        "spectral_entropy_bits": entropy,
        "spectral_entropy_norm": ent_norm,
        "effective_rank": eff_rank,
        "mean_pairwise_cosine_distance": mean_pairwise_cosine_distance(u, weights),
        "coherence": coh,
        "dispersion": 1.0 - coh if np.isfinite(coh) else float("nan"),
        "n_vectors": n,
    }
