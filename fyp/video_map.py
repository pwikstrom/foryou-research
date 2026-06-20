"""Cluster the video embedding store into niches and build a 2D semantic map.

Consumes the dense embeddings written by :mod:`fyp.embeddings` and produces:

    * ``recoded/video_map.parquet`` — one row per embedded video with its niche
      id and (for a sampled subset) 2D map coordinates. The map is sampled
      because a browser scatter cannot usefully render 256k points; clustering,
      however, covers every video.
    * ``recoded/video_niches.json`` — per-niche metadata (Gemini-generated name,
      size, distinctive terms, dominant content categories).

Pipeline: load raw vectors → mean-centre → L2-normalise → PCA(50) →
MiniBatchKMeans (every video gets a niche) → t-SNE on a sample (the visual
map) → Gemini niche naming from centroid-nearest exemplars, with a dedupe
pass so every niche label is unique.
"""

from collections.abc import Callable

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from sklearn.cluster import MiniBatchKMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from sklearn.preprocessing import normalize

import fyp.data_io as data_io
import fyp.embeddings as embeddings
from fyp.fyp_config import fyp_cf

# Output artifacts in the "recoded" store.
MAP_FILE = "video_map.parquet"
NICHES_FILE = "video_niches.json"

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
# Scrape-derived per-play engagement rates (computed at scrape consolidation)
# denormalised into the map file as numeric colour overlays.
SCRAPE_OVERLAY_NUMERIC = [
    "comments_per_play", "faves_per_play", "shares_per_play", "saves_per_play",
]






def _reduce(matrix: np.ndarray, pca_dim: int) -> np.ndarray:
    """Mean-centre, L2-normalise, then PCA-reduce the raw embedding matrix.

    Centring removes the gemini-embedding-001 anisotropy (high baseline
    cosine); randomised SVD keeps PCA tractable at 256k×1536.

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
        ``(aligned_labels, carried_names)`` where ``carried_names`` maps each
        carried-forward (previous) id to the previous name to reuse.
    """
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE):
        return labels.astype(np.int32), {}
    try:
        prev = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE,
            columns=["item_id", prev_id_col, prev_name_col],
        )
    except Exception:
        return labels.astype(np.int32), {}
    if prev is None or prev.empty or prev_id_col not in prev.columns:
        return labels.astype(np.int32), {}

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
        return labels.astype(np.int32), {}

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
    return aligned, carried






def _name_niches(
    item_ids: list[str],
    labels: np.ndarray,
    reduced: np.ndarray,
    stories: pd.Series,
    categories: pd.Series,
    carried_names: dict[int, str] | None = None,
    reporter=None,
) -> dict[int, dict]:
    """Build per-niche metadata: Gemini name, size, top terms, top categories.

    Args:
        item_ids: Item ids aligned to ``labels``/``reduced`` rows.
        labels: Niche id per video.
        reduced: PCA-reduced matrix (for centroid-nearest exemplar selection).
        stories: ``video_story`` Series aligned to ``item_ids``.
        categories: First ``content_category`` Series aligned to ``item_ids``.
        carried_names: Optional map of niche id → name carried over from the
            previous build; these niches keep their name and skip Gemini naming.
        reporter: Optional status reporter.

    Returns:
        Dict niche_id → metadata dict.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    carried_names = carried_names or {}
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

    client = embeddings._get_client()
    naming_model = fyp_cf["machine"]["model"]

    def _exemplars(niche: int) -> str:
        rows = np.where(labels == niche)[0]
        centroid = reduced[rows].mean(axis=0)
        order = np.argsort(np.linalg.norm(reduced[rows] - centroid, axis=1))
        picks = rows[order[:_EXEMPLARS_PER_NICHE]]
        return "\n".join(f"- {str(story_list[i])[:160]}" for i in picks)

    def _ask(prompt: str) -> str | None:
        try:
            resp = client.models.generate_content(model=naming_model, contents=prompt)
            return resp.text.strip().replace("\n", " ")[:48]
        except Exception:
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
        return niche, _ask(prompt) or f"Niche {niche}"

    to_name = [n for n in niches if n not in carried_names]
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(_name, n) for n in to_name]):
            niche, name = fut.result()
            meta[niche]["name"] = name

    renamed = _dedupe_niche_names(meta, _exemplars, _ask)
    if reporter is not None:
        reporter.log(
            f"Named {len(to_name)} niches via {naming_model} "
            f"({len(carried_names)} carried over from previous build, "
            f"{renamed} duplicate labels renamed)."
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
            print(msg)

    _log("Loading embedding store...")
    item_ids, matrix = embeddings.load_embeddings(reporter=reporter)
    if len(item_ids) == 0:
        _log("Embedding store is empty; nothing to map.")
        return {"videos": 0, "niches": 0, "mapped": 0}
    _log(f"Loaded {len(item_ids):,} embeddings ({matrix.shape[1]}d).")

    if reporter is not None:
        reporter.update_progress(20, "Reducing (PCA)...")
    reduced = _reduce(matrix, pca_dim)

    if reporter is not None:
        reporter.update_progress(35, f"Clustering into {n_niches} niches...")
    n_niches = min(n_niches, len(item_ids))
    kmeans = MiniBatchKMeans(n_clusters=n_niches, random_state=0, n_init=5, batch_size=4096)
    labels = kmeans.fit_predict(reduced)
    _log(f"Assigned {len(item_ids):,} videos to {n_niches} niches.")

    # Stabilise niche ids/names against the previous build so saved
    # niche-filtered analyses survive a rebuild (see _align_labels_to_previous).
    if reporter is not None:
        reporter.update_progress(45, "Aligning niche ids to previous build...")
    labels, niche_carry = _align_labels_to_previous(item_ids, labels, "niche", "niche_name")
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

    if reporter is not None:
        reporter.update_progress(60, "Naming niches (Gemini)...")
    niche_meta = _name_niches(
        item_ids, labels, reduced, stories, categories,
        carried_names=niche_carry, reporter=reporter,
    )

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
    tsne = TSNE(n_components=2, perplexity=30, init="pca", random_state=0, max_iter=1000)
    xy_sample = tsne.fit_transform(reduced[sample_idx])

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

    scr_available = data_io.get_parquet_columns(
        storage_location=embeddings.STORE_LOCATION, filename=embeddings.SCRAPES_FILE,
    ) or []
    scrape_numeric = [c for c in SCRAPE_OVERLAY_NUMERIC if c in scr_available]
    scr = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=embeddings.SCRAPES_FILE,
        columns=["item_id", "stats_playCount"] + scrape_numeric,
    )
    scr["item_id"] = scr["item_id"].astype("string")
    scr_by_item = scr.drop_duplicates("item_id").set_index("item_id")
    item_index = pd.Index(item_ids, dtype="string")
    plays = pd.to_numeric(scr_by_item["stats_playCount"].reindex(item_index), errors="coerce")
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
        **overlay_cols,
    })
    data_io.save_parquet(df=map_df, storage_location=embeddings.STORE_LOCATION, filename=MAP_FILE)

    niches_payload = {str(k): v for k, v in niche_meta.items()}
    data_io.save_json(data=niches_payload, storage_location=embeddings.STORE_LOCATION, filename=NICHES_FILE)

    _log(f"Saved {MAP_FILE} ({len(map_df):,} rows) and {NICHES_FILE} ({len(niche_meta)} niches).")
    return {"videos": n, "niches": len(niche_meta), "mapped": int(len(sample_idx))}
