"""Cluster the video embedding store into niches and build a 2D semantic map.

Consumes the dense embeddings written by :mod:`fyp.embeddings` and produces:

    * ``recoded/video_map.parquet`` — one row per embedded video with its niche
      id, its typicality and niche-isolation percentiles, and (for a sampled
      subset) 2D map coordinates. The map is sampled because a browser scatter
      cannot usefully render 256k points; clustering, however, covers every
      video. The percentile columns are joined into every study frame as
      ordinary analysis variables (organize_datasets._join_niche_columns).
    * ``recoded/video_niches.json`` — per-niche metadata (name, size,
      distinctive terms, dominant content categories, typicality, and the
      genuinely nearest niches measured in the clustering space).
    * ``recoded/video_map_meta.json`` — build provenance (embedding model/dim,
      vector count, naming mode, build timestamp).

Pipeline: load raw vectors (active embedding backend's model only) →
mean-centre → L2-normalise → PCA(50) → MiniBatchKMeans (every video gets a
niche) → t-SNE on a sample (the visual map) → niche naming from
centroid-nearest exemplars, with a dedupe pass so every niche label is unique.

Niche naming uses Gemini when it is configured; otherwise it degrades to
deterministic term-based labels (the top c-TF-IDF terms), so a fully-local
install builds a complete map with no cloud calls. A future ``local_llm``
naming mode could route the naming prompts through the local annotation
backend's model instead — the ``_ask``/``ask_fn`` seam in ``_name_niches`` /
``_dedupe_niche_names`` is the intended hook.
"""

import hashlib
import re
from collections.abc import Callable

import numpy as np
import pandas as pd
from google import genai
from scipy.optimize import linear_sum_assignment
from sklearn.cluster import MiniBatchKMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import normalize

import fyp.core.gemini_client as gemini_client
import fyp.data_io as data_io
import fyp.embeddings as embeddings
from fyp.logging_setup import get_logger

logger = get_logger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf

# Output artifacts in the "recoded" store. The meta file is separate from
# NICHES_FILE because that JSON's consumers iterate it assuming every key is a
# niche id.
MAP_FILE = "video_map.parquet"
NICHES_FILE = "video_niches.json"
MAP_META_FILE = "video_map_meta.json"

# Defaults. n_niches mirrors niche_detection's micro-genre granularity; the
# map sample keeps the dashboard scatter renderable while clustering stays
# full-corpus. The analysis tabs surface the fine niche directly and rely on
# their existing top-K + "Other"/rare-pruning logic to tame the cardinality.
DEFAULT_N_NICHES = 150
DEFAULT_MAP_SAMPLE = 30000
DEFAULT_PCA_DIM = 50

# Minimum share of a fresh cluster's members that must overlap a previous
# cluster for that cluster to inherit the previous id/name (see
# _align_labels_to_previous).
_ALIGN_MIN_OVERLAP = 0.5

_EXEMPLARS_PER_NICHE = 10
_TERMS_PER_NICHE = 8

# The last-resort label shape, e.g. "Niche 406". Reaching it means naming failed
# for that cluster; the pattern is matched on carried-over names so a rebuild
# retries them instead of inheriting the failure forever (see _name_niches).
_GENERIC_NAME_RE = re.compile(r"Niche \d+")

# Annotation scalar fields denormalised into the map file so the dashboard can
# colour the scatter by them. Numeric fields drive a continuous colourscale;
# categorical fields drive a discrete legend. (Sparse fields like
# scene_sentiments_* are excluded — they are non-null on <1% of videos.)
OVERLAY_NUMERIC = [
    "political_score", "sensitivity_score", "speech_vs_music", "faces_age_estimate",
]
OVERLAY_CATEGORICAL = [
    "australian_relevance", "tiktok_native", "trend", "advertising", "aigc",
    "main_gender", "main_ethnicity",
]
# Scrape-derived per-1K-play engagement rates (computed at scrape time)
# denormalised into the map file as numeric colour overlays.
SCRAPE_OVERLAY_NUMERIC = [
    "comments_per_K_play", "faves_per_K_play", "shares_per_K_play", "saves_per_K_play",
]
# Scrape-side categorical overlays (discrete legend). source_platform is
# single-valued while annotation/embeddings stay TikTok-only, but the overlay is
# wired so additional platforms surface automatically once they are embedded.
SCRAPE_OVERLAY_CATEGORICAL = [
    "source_platform",
]

# Cached Vertex client for niche naming. Distinct from the embeddings client
# (embeddings._get_client), which is pinned to the embedding endpoint location
# (us-central1) where the generation model is NOT served. Naming is a generation
# call, so it must use the configured generation location ([machine].location,
# e.g. "global").
_naming_client: genai.Client | None = None


def _get_naming_client() -> genai.Client:
    """Return a cached client at the generation endpoint for niche naming.

    Honours whichever Gemini mode is configured — Vertex AI or the plain Gemini
    API — via :func:`fyp.core.gemini_client.make_client`. Naming is a text-only
    call, so both modes serve it; in Vertex mode it uses the
    ``[machine].location`` generation endpoint (not the embedding endpoint).

    Returns:
        A :class:`google.genai.Client`.

    Raises:
        GeminiNotConfiguredError: When no usable Gemini mode is configured.
    """
    global _naming_client
    if _naming_client is None:
        _naming_client = gemini_client.make_client(
            location=_cf()["machine"]["gemini"]["location"]
        )
    return _naming_client






def _naming_available() -> bool:
    """Whether Gemini is configured for niche naming.

    When False the map still builds — niches get deterministic term-based
    labels instead of Gemini-generated ones (see ``_term_name``).

    Returns:
        True iff a usable Gemini mode resolves.
    """
    mode, _ = gemini_client.gemini_mode()
    return mode is not None






def _term_name(meta: dict[int, dict], niche: int) -> str:
    """Deterministic niche label from its most distinctive c-TF-IDF terms.

    Args:
        meta: Niche id → metadata dict (must carry ``terms``).
        niche: The niche id to name.

    Returns:
        A short title-cased label, e.g. ``"Cat Mischief / Funny Pets"``.
    """
    terms = [t for t in (meta[niche].get("terms") or []) if str(t).strip()]
    if not terms:
        return f"Niche {niche}"
    return " / ".join(str(t).title() for t in terms[:2])[:48]






def _reduce(matrix: np.ndarray, pca_dim: int) -> np.ndarray:
    """Mean-centre, L2-normalise, then PCA-reduce the raw embedding matrix.

    Centring removes the anisotropy (high baseline cosine) common to dense
    embedding models; randomised SVD keeps PCA tractable at 256k×1536.

    Args:
        matrix: Raw ``(n, dim)`` embedding matrix.
        pca_dim: Target PCA dimensionality.

    Returns:
        The reduced ``(n, pca_dim)`` float32 matrix.
    """
    centred = normalize(matrix - matrix.mean(axis=0))
    pca_dim = min(pca_dim, centred.shape[1], centred.shape[0])
    pca = PCA(n_components=pca_dim, svd_solver="randomized", random_state=0)
    return pca.fit_transform(centred).astype(np.float32)






def _typicality(matrix: np.ndarray) -> np.ndarray:
    """Score each video by how close it sits to the corpus average.

    The score is the cosine similarity between a video and the corpus mean
    direction: high means the video sits near the middle of what the corpus
    is about, low means it is distinctive. Vectors are L2-normalised before
    the mean is taken, so the score reflects semantic direction only and is
    never inflated by an embedding's magnitude.

    This cannot be read off the 2D map. t-SNE places a video next to its
    nearest neighbours and is free to put that neighbourhood anywhere on the
    page, so the middle of the picture is not the middle of the corpus.

    Args:
        matrix: Raw ``(n, dim)`` embedding matrix.

    Returns:
        The ``(n,)`` float32 similarity to the corpus mean direction.
    """
    unit = normalize(matrix)
    mean_dir = unit.mean(axis=0)
    norm = float(np.linalg.norm(mean_dir))
    if norm == 0.0:
        return np.zeros(unit.shape[0], dtype=np.float32)
    return (unit @ (mean_dir / norm)).astype(np.float32)






def _percentile_rank(values: np.ndarray) -> np.ndarray:
    """Rank values into 0-100 percentiles, averaging ties.

    Raw typicality is a cosine against the corpus mean direction, so its
    values sit in a narrow band whose position drifts every time the corpus
    grows. That makes the raw score unusable as a stored analysis variable —
    a value in one build is not comparable to the same video's value in the
    next. The percentile is comparable: "more typical than N% of the corpus"
    means the same thing in every build, and it is already the vocabulary the
    Semantic Space UI states in words.

    Args:
        values: The ``(n,)`` scores to rank.

    Returns:
        The ``(n,)`` float32 percentile of each value among all the values,
        from ``100/n`` (lowest) to ``100`` (highest).
    """
    if values.size == 0:
        return values.astype(np.float32)
    ranks = pd.Series(values).rank(pct=True, method="average").to_numpy()
    return (100.0 * ranks).astype(np.float32)






def _per_video_niche_value(niche_meta: dict[int, dict], labels: np.ndarray,
                           key: str) -> np.ndarray:
    """Spread a per-niche metric across that niche's videos.

    Args:
        niche_meta: Niche id → metadata dict.
        labels: Niche id per video.
        key: The ``niche_meta`` key to read.

    Returns:
        The ``(n,)`` float64 value per video, NaN wherever the niche declines
        to report one — a corpus with a single niche has nothing to be
        isolated from, and a null is the honest answer there.
    """
    return np.array([niche_meta[int(lab)].get(key) for lab in labels], dtype=np.float64)






def _add_niche_typicality(
    niche_meta: dict[int, dict],
    labels: np.ndarray,
    typicality: np.ndarray,
) -> None:
    """Add each niche's mean typicality plus its rank among niches, in place.

    Raw cosine scores over dense embeddings sit in a narrow band, so the
    absolute mean is hard to read on its own. ``typicality_pct`` is the
    niche's percentile rank among all niches, which is what the UI states in
    words ("more typical than 82% of niches").

    Args:
        niche_meta: Niche id → metadata dict; mutated in place.
        labels: Niche id per video.
        typicality: Per-video typicality aligned to ``labels``.
    """
    means = {niche: float(typicality[labels == niche].mean()) for niche in niche_meta}
    ranked = sorted(means, key=lambda niche: means[niche])
    denom = max(len(ranked) - 1, 1)
    for rank, niche in enumerate(ranked):
        niche_meta[niche]["typicality"] = round(means[niche], 4)
        niche_meta[niche]["typicality_pct"] = round(100.0 * rank / denom, 1)






def _add_niche_neighbours(
    niche_meta: dict[int, dict],
    labels: np.ndarray,
    reduced: np.ndarray,
    n_neighbours: int = 5,
) -> None:
    """Add each niche's nearest niches and its isolation, in place.

    This is the one thing the 2D map cannot honestly show. t-SNE preserves a
    point's local neighbourhood but is free to strand a whole niche far from
    its true relatives, so which niches are drawn adjacent is a weak guide to
    which are actually alike. Measuring it on the same reduced vectors the
    clustering consumed answers the question the picture only gestures at.

    Neighbours are stored as niche ids, not names: ids are held stable across
    rebuilds by :func:`_align_labels_to_previous` while names can be
    regenerated, so the reader resolves the current name itself.

    Args:
        niche_meta: Niche id → metadata dict; mutated in place.
        labels: Niche id per video.
        reduced: The PCA-reduced matrix the clustering ran on.
        n_neighbours: How many nearest niches to keep per niche (capped at the
            number of other niches that exist).
    """
    niches = sorted(niche_meta)
    limit = min(n_neighbours, len(niches) - 1)
    if limit < 1:
        # One niche has no neighbours, and nothing to be isolated from.
        for niche in niches:
            niche_meta[niche].update(nearest=[], isolation=None, isolation_pct=None)
        return

    centroids = np.vstack([reduced[labels == niche].mean(axis=0) for niche in niches])
    dist = np.linalg.norm(centroids[:, None, :] - centroids[None, :, :], axis=-1)
    np.fill_diagonal(dist, np.inf)
    for i, niche in enumerate(niches):
        order = np.argsort(dist[i])[:limit]
        niche_meta[niche]["nearest"] = [int(niches[j]) for j in order]

    # Isolation — how far a niche sits from its NEAREST neighbour. A different
    # question from typicality (distance from the corpus mean), and measurably
    # independent of it: a niche can sit far from the average yet keep close
    # company, or be thoroughly ordinary yet have nothing beside it. Reported as
    # a percentile for the same reason typicality is — the raw distances are in
    # arbitrary PCA units and mean nothing on their own.
    nearest = dist.min(axis=1)
    ranked = sorted(range(len(niches)), key=lambda i: nearest[i])
    denom = max(len(ranked) - 1, 1)
    for rank, i in enumerate(ranked):
        niche_meta[niches[i]]["isolation"] = round(float(nearest[i]), 4)
        niche_meta[niches[i]]["isolation_pct"] = round(100.0 * rank / denom, 1)






def _neighbour_preservation(
    reduced: np.ndarray,
    xy: np.ndarray,
    k: int = 10,
    probe: int = 2000,
) -> dict:
    """Measure how much of the true neighbourhood structure the 2D map keeps.

    For a random probe of projected videos, the share of each one's ``k``
    nearest neighbours in the clustering space that are still among its ``k``
    nearest on the rendered map. A 2D projection cannot honour every
    relationship at once, and the ones it drops leave no visible trace, so
    without this number the map's accuracy is unfalsifiable. Publishing it
    turns the tab's "read closeness as evidence, not as measurement" caveat
    into a reproducible statistic.

    Compared against ``chance`` — the share a random neighbourhood would score
    — which is what shows the layout is doing real work even when the absolute
    figure looks low.

    Args:
        reduced: PCA-reduced rows of the videos that were projected.
        xy: Their ``(n, 2)`` map coordinates, in the same row order.
        k: Neighbourhood size.
        probe: How many videos to measure. The exact figure is quadratic in the
            corpus; a probe of a couple of thousand is stable to about a point.

    Returns:
        Dict with ``k``/``score``/``chance``/``probe``/``mapped``, or an empty
        dict when the sample is too small to have neighbourhoods at all.
    """
    n = int(xy.shape[0])
    k = min(k, n - 1)
    if n < 3 or k < 1:
        return {}
    probe = min(probe, n)
    idx = np.random.RandomState(0).choice(n, probe, replace=False)
    # k+1 then drop the first column: a point is always its own nearest.
    hi = NearestNeighbors(n_neighbors=k + 1).fit(reduced)
    lo = NearestNeighbors(n_neighbors=k + 1).fit(xy)
    near_hi = hi.kneighbors(reduced[idx], return_distance=False)[:, 1:]
    near_lo = lo.kneighbors(xy[idx], return_distance=False)[:, 1:]
    kept = np.mean([len(set(a) & set(b)) for a, b in zip(near_hi, near_lo)]) / k
    return {
        "k": int(k),
        "score": round(float(kept), 4),
        "chance": round(float(k / max(n - 1, 1)), 6),
        "probe": int(probe),
        "mapped": n,
    }






_WARM_START_MIN_MEMBERS = 5


def _warm_start_centroids(item_ids: list[str], reduced: np.ndarray,
                          n_niches: int) -> tuple[np.ndarray | None, str]:
    """Initial centroids for the clustering, taken from the previous build.

    A fresh k-means++ run redraws every niche boundary even when the corpus
    barely moved: on 2026-09-03 an append of 97 vectors to 619,845 re-clustered
    into a partition where only 90 of 150 niches overlapped their predecessor
    enough to keep their name, so 60 were re-named through Gemini (244 s) for
    no real change. Starting the fit from the previous partition — each old
    niche's members averaged in the CURRENT reduced space, since PCA is refit
    every run — makes the new clustering a refinement of the old one: near-total
    overlap, names carried, and niche identity stable across rebuilds.

    Returns ``(centroids, reason)``; ``centroids`` is None when there is nothing
    usable to start from (no previous map, a different niche count, or too few
    members surviving for some niche), in which case the caller cold-starts.
    """
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE):
        return None, "no previous map"
    try:
        prev = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE,
            columns=["item_id", "niche"])
    except Exception as exc:
        return None, f"previous map unreadable ({type(exc).__name__})"
    if prev is None or prev.empty or "niche" not in prev.columns:
        return None, "previous map has no niches"
    prev["item_id"] = prev["item_id"].astype("string")
    prev_niche = pd.to_numeric(
        prev.drop_duplicates("item_id").set_index("item_id")["niche"], errors="coerce"
    ).reindex(pd.Index(item_ids, dtype="string")).to_numpy()
    known = ~np.isnan(prev_niche)
    if not known.any():
        return None, "no shared items with the previous map"
    old_ids = np.unique(prev_niche[known]).astype(int)
    if old_ids.size != n_niches:
        return None, f"previous map has {old_ids.size} niches, this build wants {n_niches}"
    centroids = np.empty((n_niches, reduced.shape[1]), dtype=reduced.dtype)
    for k, old_id in enumerate(old_ids):
        members = known & (prev_niche == old_id)
        if int(members.sum()) < _WARM_START_MIN_MEMBERS:
            return None, f"previous niche {old_id} has only {int(members.sum())} surviving members"
        centroids[k] = reduced[members].mean(axis=0)
    return centroids, f"{n_niches} niches, {int(known.sum()):,} shared items"


def _align_labels_to_previous(
    item_ids: list[str],
    labels: np.ndarray,
    prev_id_col: str,
    prev_name_col: str,
) -> tuple[np.ndarray, dict[int, str]]:
    """Relabel fresh cluster ids to match the previous build's ids.

    Cluster ids from a fresh KMeans/agglomeration run are arbitrary, so a saved
    analysis filtered on a niche id/name could silently point at a different
    cluster after a rebuild. This aligns the new labels to the previous build by
    maximising membership overlap — a Hungarian assignment on the new×old
    contingency table over shared item_ids — and carries the previous id and
    name forward for each strongly matched cluster. Clusters that split, merge,
    or are genuinely new get fresh ids above the previous maximum.

    Args:
        item_ids: Item ids aligned to ``labels`` rows.
        labels: Fresh integer cluster id per video.
        prev_id_col: Column in the previous map holding the integer cluster id.
        prev_name_col: Column in the previous map holding the cluster name.

    Returns:
        ``(aligned_labels, carried_names, previous_per_item)`` — ``carried_names``
        maps each carried-forward (previous) id to the previous name to reuse,
        and ``previous_per_item`` is each video's niche in the PREVIOUS build
        (NaN where the video is new, None when there was no previous map).
        Because the aligned labels live in the previous build's id space, the
        two arrays compare directly: that is how a rebuild reports how many
        videos actually changed niche.
    """
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE):
        return labels.astype(np.int32), {}, None
    try:
        prev = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE,
            columns=["item_id", prev_id_col, prev_name_col],
        )
    except Exception:
        return labels.astype(np.int32), {}, None
    if prev is None or prev.empty or prev_id_col not in prev.columns:
        return labels.astype(np.int32), {}, None

    prev["item_id"] = prev["item_id"].astype("string")
    prev = prev.dropna(subset=[prev_id_col])
    old_name_by_id = {
        int(i): str(n)
        for i, n in prev.groupby(prev_id_col)[prev_name_col].first().items()
    }
    old_per_item = pd.to_numeric(
        prev.drop_duplicates("item_id").set_index("item_id")[prev_id_col],
        errors="coerce",
    ).reindex(pd.Index(item_ids, dtype="string")).to_numpy()

    new_ids = np.unique(labels)
    old_ids = np.array(sorted(old_name_by_id.keys()))
    if old_ids.size == 0:
        return labels.astype(np.int32), {}, None

    new_pos = {int(c): i for i, c in enumerate(new_ids)}
    old_pos = {int(c): j for j, c in enumerate(old_ids)}
    contingency = np.zeros((new_ids.size, old_ids.size), dtype=np.int64)
    shared = ~np.isnan(old_per_item)
    for new_lab, old_lab in zip(labels[shared], old_per_item[shared].astype(int)):
        if old_lab in old_pos:
            contingency[new_pos[int(new_lab)], old_pos[old_lab]] += 1

    row_ind, col_ind = linear_sum_assignment(-contingency)
    new_sizes = {int(c): int((labels == c).sum()) for c in new_ids}
    mapping: dict[int, int] = {}
    carried: dict[int, str] = {}
    for r, c in zip(row_ind, col_ind):
        overlap = contingency[r, c]
        new_id = int(new_ids[r])
        if overlap <= 0 or new_sizes[new_id] == 0:
            continue
        if overlap / new_sizes[new_id] < _ALIGN_MIN_OVERLAP:
            continue
        old_id = int(old_ids[c])
        mapping[new_id] = old_id
        carried[old_id] = old_name_by_id[old_id]

    next_id = int(old_ids.max()) + 1
    for new_id in new_ids:
        if int(new_id) not in mapping:
            mapping[int(new_id)] = next_id
            next_id += 1

    aligned = np.array([mapping[int(l)] for l in labels], dtype=np.int32)
    return aligned, carried, old_per_item






def _name_niches(
    item_ids: list[str],
    labels: np.ndarray,
    reduced: np.ndarray,
    stories: pd.Series,
    categories: pd.Series,
    carried_names: dict[int, str] | None = None,
    reporter=None,
) -> dict[int, dict]:
    """Build per-niche metadata: name, size, top terms, top categories.

    Names come from Gemini when it is configured, else deterministically from
    each niche's top c-TF-IDF terms (see the module docstring).

    Args:
        item_ids: Item ids aligned to ``labels``/``reduced`` rows.
        labels: Niche id per video.
        reduced: PCA-reduced matrix (for centroid-nearest exemplar selection).
        stories: ``video_story`` Series aligned to ``item_ids``.
        categories: First ``content_category`` Series aligned to ``item_ids``.
        carried_names: Optional map of niche id → name carried over from the
            previous build; these niches keep their name and skip Gemini naming.
            Entries whose name is a generic ``"Niche N"`` are dropped and named
            again, so an earlier naming failure does not become permanent.
        reporter: Optional status reporter.

    Returns:
        Dict niche_id → metadata dict.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # A carried-over "Niche 12" means naming failed on an earlier build. Carrying
    # it forward again would make a single transient failure permanent — the only
    # escape being a full reset_labels rebuild — so drop those and let this build
    # retry them. Copied rather than mutated: the caller still holds this dict.
    carried_in = carried_names or {}
    carried_names = {
        niche: name
        for niche, name in carried_in.items()
        if not _GENERIC_NAME_RE.fullmatch(str(name).strip())
    }
    n_retried = len(carried_in) - len(carried_names)
    story_list = stories.tolist()
    vectorizer = TfidfVectorizer(
        max_features=8000, min_df=3, max_df=0.4,
        stop_words="english", ngram_range=(1, 2),
    )
    tfidf = vectorizer.fit_transform([s if isinstance(s, str) else "" for s in story_list])
    vocab = np.array(vectorizer.get_feature_names_out())

    niches = sorted(int(c) for c in set(labels))
    meta: dict[int, dict] = {}
    for niche in niches:
        rows = np.where(labels == niche)[0]
        mean_tfidf = np.asarray(tfidf[rows].mean(axis=0)).ravel()
        terms = vocab[mean_tfidf.argsort()[::-1][:_TERMS_PER_NICHE]].tolist()
        cat_counts = categories.iloc[rows].value_counts().head(3)
        meta[niche] = {
            "size": int(len(rows)),
            "terms": terms,
            "top_categories": [str(c) for c in cat_counts.index.tolist()],
            "name": carried_names.get(niche, f"Niche {niche}"),
        }

    def _exemplars(niche: int) -> str:
        rows = np.where(labels == niche)[0]
        centroid = reduced[rows].mean(axis=0)
        order = np.argsort(np.linalg.norm(reduced[rows] - centroid, axis=1))
        picks = rows[order[:_EXEMPLARS_PER_NICHE]]
        return "\n".join(f"- {str(story_list[i])[:160]}" for i in picks)

    to_name = [n for n in niches if n not in carried_names]

    # No Gemini configured: deterministic term-based labels instead of LLM
    # ones, so a fully-local install still gets a named map. The dedupe pass
    # runs with a no-op ask_fn, which resolves collisions via its
    # deterministic distinctive-term suffix path.
    if not _naming_available():
        for niche in to_name:
            meta[niche]["name"] = _term_name(meta, niche)
        renamed = _dedupe_niche_names(meta, _exemplars, lambda prompt: None)
        if reporter is not None:
            reporter.log(
                f"Named {len(to_name)} niches from top terms (Gemini not "
                f"configured; {len(carried_names)} carried over from previous "
                f"build, {n_retried} generic labels retried, {renamed} duplicate "
                f"labels renamed)."
            )
        return meta

    client = _get_naming_client()
    naming_model = _cf()["machine"]["gemini"]["model"]
    naming_errors: list[str] = []

    def _ask(prompt: str) -> str | None:
        try:
            resp = client.models.generate_content(model=naming_model, contents=prompt)
            return resp.text.strip().replace("\n", " ")[:48]
        except Exception as e:
            naming_errors.append(f"{type(e).__name__}: {e}")
            return None

    # Labels already fixed by carry-over; fresh names should steer clear of
    # them so the dedupe pass below has less to repair.
    taken_at_start = sorted(set(carried_names.values()))

    def _name(niche: int) -> tuple[int, str]:
        avoid = (
            "\nThese labels are taken by other clusters — do not reuse any of them:\n"
            f"{', '.join(taken_at_start)}\n"
        ) if taken_at_start else ""
        prompt = (
            f"These are summaries of TikTok videos in one cluster:\n{_exemplars(niche)}\n\n"
            "Give a SHORT 2-4 word label naming this micro-genre. "
            "Be specific enough to set it apart from similar micro-genres "
            "(e.g. prefer 'Cat Mischief Clips' over a generic 'Pet Antics').\n"
            f"{avoid}"
            "Reply with only the label."
        )
        # A failed (or empty) naming call degrades to the same deterministic
        # term-based label the no-Gemini path uses, not to a bare "Niche 406".
        # Some clusters fail every time — a safety block on the exemplar
        # summaries, say — so the generic label would otherwise be permanent.
        return niche, _ask(prompt) or _term_name(meta, niche)

    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(_name, n) for n in to_name]):
            niche, name = fut.result()
            meta[niche]["name"] = name

    renamed = _dedupe_niche_names(meta, _exemplars, _ask)
    if naming_errors:
        mode, _ = gemini_client.gemini_mode()
        warn = (
            f"WARNING: {len(naming_errors)} niche-naming call(s) failed "
            f"(model={naming_model}, gemini_mode={mode}); affected niches fall "
            f"back to term-based labels. First error: {naming_errors[0][:300]}"
        )
        if reporter is not None:
            reporter.log(warn)
        else:
            logger.warning(warn)
    if reporter is not None:
        reporter.log(
            f"Named {len(to_name)} niches via {naming_model} "
            f"({len(carried_names)} carried over from previous build, "
            f"{n_retried} generic labels retried, {renamed} duplicate labels "
            f"renamed)."
        )
    return meta






def _dedupe_niche_names(
    meta: dict[int, dict],
    exemplars_fn: Callable[[int], str],
    ask_fn: Callable[[str], str | None],
) -> int:
    """Rename niches whose labels collide so every niche label is unique.

    Naming happens per-cluster in parallel (and carried-over names from
    previous builds are never re-generated), so distinct clusters can end up
    with the same Gemini label — which renders as confusing duplicate labels
    on the map. Collisions are resolved largest-first: the biggest niche in
    each group keeps the label, the rest are re-prompted with the full list
    of taken labels. If Gemini still collides or errors, the label gets a
    deterministic suffix from the niche's most distinctive term.

    Args:
        meta: Niche id → metadata dict (mutated in place: ``name`` updated).
        exemplars_fn: Returns the exemplar-summaries block for a niche id.
        ask_fn: Sends a prompt to the naming model, returns the reply or None.

    Returns:
        Number of niches that were renamed.
    """
    def _key(name: str) -> str:
        return " ".join(name.lower().split())

    groups: dict[str, list[int]] = {}
    for niche, m in meta.items():
        groups.setdefault(_key(m["name"]), []).append(niche)

    renamed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        for niche in sorted(group, key=lambda n: -meta[n]["size"])[1:]:
            old = meta[niche]["name"]
            taken_names = sorted({m["name"] for n, m in meta.items() if n != niche})
            taken_keys = {_key(n) for n in taken_names}
            prompt = (
                f"These are summaries of TikTok videos in one cluster:\n{exemplars_fn(niche)}\n\n"
                f'The label "{old}" already belongs to a different cluster, and so do '
                f"all of these:\n{', '.join(taken_names)}\n\n"
                "Give a SHORT 2-4 word label for THIS cluster that captures what makes "
                "it distinct and is not in the list above. Reply with only the label."
            )
            new = None
            for _ in range(2):
                cand = ask_fn(prompt)
                if cand and _key(cand) not in taken_keys:
                    new = cand
                    break
            if new is None:
                term = (meta[niche].get("terms") or ["misc"])[0]
                new = f"{old} ({term})"[:48]
                if _key(new) in taken_keys:
                    new = f"{old[:40]} ({niche})"
            meta[niche]["name"] = new
            renamed += 1
    return renamed






def build_niche_map(
    n_niches: int = DEFAULT_N_NICHES,
    map_sample: int = DEFAULT_MAP_SAMPLE,
    pca_dim: int = DEFAULT_PCA_DIM,
    reset_labels: bool = False,
    reporter=None,
) -> dict:
    """Cluster the embedding store and persist the niche map + metadata.

    Args:
        n_niches: Number of MiniBatchKMeans micro-genres.
        map_sample: Max videos projected to 2D for the visual map.
        pca_dim: PCA dimensionality used for clustering and projection.
        reset_labels: When True, regenerate every niche name from scratch via
            Gemini instead of carrying stable names forward from the previous
            build. Cluster ids stay aligned to the previous build either way.
        reporter: Optional status reporter.

    Returns:
        Dict summary with ``videos``, ``niches``, ``mapped`` counts.
    """
    def _log(msg: str) -> None:
        if reporter is not None:
            reporter.log(msg)
        else:
            logger.info(msg)

    embed_backend = embeddings.active_embedding_backend()
    embed_model = embed_backend.model_id()
    _log(f"Loading embedding store (model={embed_model})...")
    item_ids, matrix = embeddings.load_embeddings(reporter=reporter, model=embed_model)
    if len(item_ids) == 0:
        _log(f"Embedding store holds no vectors for {embed_model}; nothing to map. "
             "Run an embeddings refresh first (a backend switch starts from an "
             "empty store for the new model).")
        return {"videos": 0, "niches": 0, "mapped": 0}
    _log(f"Loaded {len(item_ids):,} embeddings ({matrix.shape[1]}d).")

    if reporter is not None:
        reporter.update_progress(20, "Reducing (PCA)...")
    reduced = _reduce(matrix, pca_dim)
    # Computed on the raw vectors, before PCA: _reduce centres and normalises,
    # which puts every video the same distance from the origin and destroys
    # exactly the signal this measures.
    typicality = _typicality(matrix)

    if reporter is not None:
        reporter.update_progress(35, f"Clustering into {n_niches} niches...")
    n_niches = min(n_niches, len(item_ids))
    # Warm-start from the previous partition unless the operator asked for a
    # reset: the fit then refines the old niches instead of redrawing them, so
    # names carry over and Gemini is only asked about niches that really moved.
    # reset_labels is the escape hatch — a cold k-means++ start lets genuinely
    # new niches form when the corpus has drifted.
    init_centroids, warm_reason = (None, "reset_labels") if reset_labels else \
        _warm_start_centroids(item_ids, reduced, n_niches)
    if init_centroids is not None:
        _log(f"Clustering warm-started from the previous build ({warm_reason}).")
        kmeans = MiniBatchKMeans(n_clusters=n_niches, init=init_centroids, n_init=1,
                                 random_state=0, batch_size=4096)
    else:
        _log(f"Clustering cold-started with k-means++ ({warm_reason}).")
        kmeans = MiniBatchKMeans(n_clusters=n_niches, random_state=0, n_init=5, batch_size=4096)
    labels = kmeans.fit_predict(reduced)
    _log(f"Assigned {len(item_ids):,} videos to {n_niches} niches.")

    # Stabilise niche ids/names against the previous build so saved
    # niche-filtered analyses survive a rebuild (see _align_labels_to_previous).
    if reporter is not None:
        reporter.update_progress(45, "Aligning niche ids to previous build...")
    labels, niche_carry, prev_niche_per_item = _align_labels_to_previous(
        item_ids, labels, "niche", "niche_name")
    # How much did the partition actually move? This is what lets the refresh
    # pipeline skip the study/timeline rebuilds after a map run that changed
    # nothing — a warm-started append typically moves a handful of videos.
    # A cold start (or no previous map) means every assignment is new.
    cold_start = init_centroids is None
    if prev_niche_per_item is None or cold_start:
        niche_changed = len(item_ids)
        new_videos = len(item_ids) if prev_niche_per_item is None else int(
            np.isnan(prev_niche_per_item).sum())
    else:
        known = ~np.isnan(prev_niche_per_item)
        new_videos = int((~known).sum())
        niche_changed = int((labels[known] != prev_niche_per_item[known].astype(np.int32)).sum())
    _log(f"Niche assignment: {niche_changed:,} video(s) changed niche, "
         f"{new_videos:,} newly mapped (cold_start={cold_start}).")
    if reset_labels:
        # Force a full re-naming: cluster ids stay aligned to the previous build
        # (saved niche-filtered analyses survive) but every name is regenerated.
        niche_carry = {}
        _log("reset_labels=True: regenerating all niche names (no carry-over).")

    # Pull video_story + first content_category aligned to item_ids for naming.
    if reporter is not None:
        reporter.update_progress(50, "Loading stories for niche naming...")
    anno = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=embeddings.ANNOTATIONS_FILE,
        columns=["item_id", "video_story", "content_category"] + OVERLAY_NUMERIC + OVERLAY_CATEGORICAL,
    )
    anno["item_id"] = anno["item_id"].astype("string")
    anno = anno.set_index("item_id")
    aligned = anno.reindex(pd.Index(item_ids, dtype="string"))
    stories = aligned["video_story"].astype("string").fillna("").reset_index(drop=True)
    categories = aligned["content_category"].apply(
        lambda x: str(x[0]) if x is not None and hasattr(x, "__len__") and len(x) > 0 else "none"
    ).reset_index(drop=True)

    naming_mode = "gemini" if _naming_available() else "terms"
    if reporter is not None:
        reporter.update_progress(60, f"Naming niches ({naming_mode})...")
    niche_meta = _name_niches(
        item_ids, labels, reduced, stories, categories,
        carried_names=niche_carry, reporter=reporter,
    )
    _add_niche_typicality(niche_meta, labels, typicality)
    _add_niche_neighbours(niche_meta, labels, reduced)

    # 2D map on a sample (the full corpus is too large to scatter in a browser).
    if reporter is not None:
        reporter.update_progress(75, "Projecting 2D map (t-SNE)...")
    rng = np.random.RandomState(0)
    n = len(item_ids)
    if n > map_sample:
        sample_idx = np.sort(rng.choice(n, map_sample, replace=False))
    else:
        sample_idx = np.arange(n)
    _log(f"Projecting {len(sample_idx):,} sampled videos to 2D...")
    n_sampled = len(sample_idx)
    if n_sampled < 3:
        # Too few points for t-SNE (perplexity must be < n_samples) — a tiny
        # corpus gets a trivial deterministic layout instead of a failed build.
        xy_sample = np.column_stack([np.arange(n_sampled, dtype=np.float64),
                                     np.zeros(n_sampled, dtype=np.float64)])
    else:
        # sklearn requires perplexity < n_samples; the usual ~n/3 heuristic
        # keeps small corpora (e.g. a 20-video pilot) working, capped at the
        # historical 30 for full-size builds.
        perplexity = min(30.0, max(2.0, (n_sampled - 1) / 3))
        tsne = TSNE(n_components=2, perplexity=perplexity, init="pca",
                    random_state=0, max_iter=1000)
        xy_sample = tsne.fit_transform(reduced[sample_idx])

    preservation = _neighbour_preservation(reduced[sample_idx], xy_sample)
    if preservation:
        _log(f"Layout keeps {100 * preservation['score']:.0f}% of true k="
             f"{preservation['k']} neighbours (chance "
             f"{100 * preservation['chance']:.3f}%).")

    x = np.full(n, np.nan, dtype=np.float64)
    y = np.full(n, np.nan, dtype=np.float64)
    x[sample_idx] = xy_sample[:, 0]
    y[sample_idx] = xy_sample[:, 1]

    # Denormalise the dashboard hover/overlay fields into the map file so the
    # web route stays light (no 256k-row annotation load per request). The
    # story snippet is only kept for mapped points to keep the file small.
    if reporter is not None:
        reporter.update_progress(88, "Denormalising hover fields...")
    mapped_mask = np.zeros(n, dtype=bool)
    mapped_mask[sample_idx] = True
    story = stories.str.slice(0, 140).to_numpy().astype(object)
    story[~mapped_mask] = ""
    niche_names = [niche_meta[int(lab)]["name"] for lab in labels]

    # Two per-video analysis variables, denormalised here so the study merge is
    # a single join against this file (see organize_datasets._join_niche_columns).
    # Typicality rides as its percentile because the raw cosine is not
    # comparable across rebuilds; isolation is a per-video copy of the video's
    # niche's isolation percentile, which answers a question typicality does not
    # — how much company that micro-genre keeps, rather than how mainstream it is.
    typicality_pct = _percentile_rank(typicality)
    isolation_pct = _per_video_niche_value(niche_meta, labels, "isolation_pct")

    scr_available = data_io.get_parquet_columns(
        storage_location=embeddings.STORE_LOCATION, filename=embeddings.SCRAPES_FILE,
    ) or []
    scrape_numeric = [c for c in SCRAPE_OVERLAY_NUMERIC if c in scr_available]
    scrape_categorical = [c for c in SCRAPE_OVERLAY_CATEGORICAL if c in scr_available]
    scr = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=embeddings.SCRAPES_FILE,
        columns=["item_id", "play_count"] + scrape_numeric + scrape_categorical,
    )
    scr["item_id"] = scr["item_id"].astype("string")
    scr_by_item = scr.drop_duplicates("item_id").set_index("item_id")
    item_index = pd.Index(item_ids, dtype="string")
    plays = pd.to_numeric(scr_by_item["play_count"].reindex(item_index), errors="coerce")
    log_plays = np.log10(plays.fillna(0).clip(lower=0).to_numpy() + 1.0)

    # Denormalise the annotation overlay scalars (aligned to item_ids order).
    overlay_cols: dict = {}
    for col in OVERLAY_NUMERIC:
        if col in aligned.columns:
            overlay_cols[col] = pd.array(
                pd.to_numeric(aligned[col].reset_index(drop=True), errors="coerce"),
                dtype="double[pyarrow]",
            )
    for col in OVERLAY_CATEGORICAL:
        if col in aligned.columns:
            overlay_cols[col] = pd.array(
                aligned[col].reset_index(drop=True).astype("string").fillna("unknown"),
                dtype="string[pyarrow]",
            )
    # Per-play engagement rates joined from the consolidated scrapes.
    for col in scrape_numeric:
        overlay_cols[col] = pd.array(
            pd.to_numeric(scr_by_item[col].reindex(item_index), errors="coerce").reset_index(drop=True),
            dtype="double[pyarrow]",
        )
    # Categorical scrape overlays joined the same way.
    for col in scrape_categorical:
        overlay_cols[col] = pd.array(
            scr_by_item[col].reindex(item_index).astype("string").fillna("unknown").reset_index(drop=True),
            dtype="string[pyarrow]",
        )

    if reporter is not None:
        reporter.update_progress(90, "Persisting map...")
    map_df = pd.DataFrame({
        "item_id": pd.array(item_ids, dtype="string[pyarrow]"),
        "niche": pd.array(labels.astype(np.int32), dtype="int32[pyarrow]"),
        "niche_name": pd.array(niche_names, dtype="string[pyarrow]"),
        "x": pd.array(x, dtype="double[pyarrow]"),
        "y": pd.array(y, dtype="double[pyarrow]"),
        "story": pd.array(story.tolist(), dtype="string[pyarrow]"),
        "category": pd.array(categories.tolist(), dtype="string[pyarrow]"),
        "log_plays": pd.array(np.round(log_plays, 3), dtype="double[pyarrow]"),
        "typicality": pd.array(np.round(typicality, 4), dtype="double[pyarrow]"),
        "typicality_pct": pd.array(np.round(typicality_pct, 2), dtype="double[pyarrow]"),
        "niche_isolation_pct": pd.array(isolation_pct, dtype="double[pyarrow]"),
        **overlay_cols,
    })
    data_io.save_parquet(df=map_df, storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE)

    niches_payload = {str(k): v for k, v in niche_meta.items()}
    data_io.save_json(data=niches_payload, storage_location=embeddings.STORE_LOCATION, filename=NICHES_FILE)

    # Build provenance — a separate file (NICHES_FILE consumers assume every
    # key there is a niche id). Lets the UI label the map with the embedding
    # model that built it and detect a backend switch as staleness.
    # A content fingerprint of the (item_id, niche) assignment, order-independent.
    # The parquet itself is rewritten on every build (fresh 2D coordinates, a new
    # built_at), so a file stat cannot tell a real re-clustering from a re-layout;
    # the study-cache freshness check reads THIS instead.
    try:
        _row_hashes = pd.util.hash_pandas_object(
            pd.Series(labels.astype(np.int64), index=pd.Index(item_ids, dtype="string")),
            index=True).to_numpy()
        _row_hashes.sort()
        niche_assignment_hash = hashlib.blake2b(
            _row_hashes.tobytes(), digest_size=16).hexdigest()
    except Exception as exc:
        _log(f"Could not fingerprint the niche assignment ({exc}); "
             f"study caches will fall back to the file stat.")
        niche_assignment_hash = None

    meta_payload = {
        "embedding_model": embed_model,
        "niche_assignment_hash": niche_assignment_hash,
        "niche_changed": int(niche_changed),
        "new_videos": int(new_videos),
        "cold_start": bool(cold_start),
        "dim": int(matrix.shape[1]),
        "n_vectors": int(n),
        "naming_mode": naming_mode,
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "n_niches": len(niche_meta),
        "neighbour_preservation": preservation,
    }
    data_io.save_json(data=meta_payload, storage_location=embeddings.STORE_LOCATION, filename=MAP_META_FILE)

    _log(f"Saved {MAP_FILE} ({len(map_df):,} rows), {NICHES_FILE} ({len(niche_meta)} niches) "
         f"and {MAP_META_FILE} (model={embed_model}, naming={naming_mode}).")
    return {"videos": n, "niches": len(niche_meta), "mapped": int(len(sample_idx)),
            "niche_changed": int(niche_changed), "new_videos": int(new_videos),
            "cold_start": bool(cold_start)}
