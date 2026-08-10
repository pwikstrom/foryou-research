"""Session-level data quality and focused-episode ("binge") exploration.

Production port of the embedding-entropy study's episode segmenter. For every
collection it reduces the persistent viewing sessions (the ingest-assigned
``session_id``, 900 s gap) to two artifacts the Sessions tab reads:

* ``cache/sessions_index.parquet`` — one row per session: data-quality
  coverage (what share of the session's videos are scraped / annotated /
  embedded) plus embedding-entropy focus metrics (the minimum sliding-window
  mean pairwise cosine distance over :data:`WINDOW_N` distinct embedded
  videos).
* ``cache/session_episodes.parquet`` — one row per detected **focus episode**
  ("binge" in the UI): a maximal within-session run whose content stays
  semantically focused on the embeddings, with its geometry (stationary binge
  vs directed drift), content attribution (niche / author / valence), and the
  ordered member list the UI's side-by-side players render.
* ``cache/session_windows.parquet`` — one row per **low-entropy window**: up
  to :data:`MAX_WINDOWS` non-overlapping :data:`WINDOW_N`-video windows per
  session with the smallest mean pairwise cosine distance (the session's
  ``min_window_cosdist`` is window 0's score), detector-independent of the
  episode segmentation.

Segmentation rule (per session, on embedded plays, distinct videos): grow the
current episode while the next *distinct* video's mean cosine distance to the
centroid of the last :data:`MEM` members ≤ :data:`CUT`; otherwise close the
episode (kept if ≥ :data:`MIN_VIDEOS` distinct videos over ≥
:data:`MIN_MINUTES`) and start a new one. ``session_id`` boundaries hard-break
episodes; repeated plays of a video already in the episode extend its span but
are not new members (a rewatch loop must not fake a binge).

The artifacts are **global** (all collections) and study-scoped at query time:
per-study caches are sampled and shred sequences, so everything here reads the
full ``recoded/collections_recoded.parquet``. The embedding store is
model-scoped — every read passes an explicit ``model`` so vectors from
different embedding models are never mixed.
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pa_compute

import fyp.data_io as data_io
from fyp.analysis import embedding_store, embeddings, entropy_metrics
from fyp.logging_setup import get_logger
from fyp.organize_datasets import COLLECTIONS_LABEL

logger = get_logger(__name__)

# Locked segmentation parameters from the embedding-entropy study
# (specification-curve validated; `mem` controls drift tolerance).
CUT = 0.5
MEM = 6
MIN_VIDEOS = 4
MIN_MINUTES = 3.0

# Sliding-window width (distinct embedded videos) for the per-session
# low-entropy windows. The per-pair cosine distance is size-robust, but a
# fixed width keeps scores comparable across sessions (and makes the
# normalised spectral entropy comparable across windows too).
WINDOW_N = 6

# How many non-overlapping low-entropy windows to keep per session.
MAX_WINDOWS = 3

# Artifact layout: session/episode tables in "cache", per-model corpus mean in
# "recoded" next to the embedding shards it summarises.
ARTIFACT_LOCATION = "cache"
SESSIONS_FILE = "sessions_index.parquet"
EPISODES_FILE = "session_episodes.parquet"
WINDOWS_FILE = "session_windows.parquet"
META_FILE = "sessions_meta.json"
CORPUS_MEAN_PREFIX = "embedding_corpus_mean__"

# Per-link intermediate shards written by the chained build (namespaced by the
# run id so a retry of link k overwrites its own shard) and the cross-link
# progress accumulator. The final link concatenates the shards into the three
# single artifact files above, so the read side never changes.
SHARD_PREFIXES = {"sessions": "sessions_shard__", "episodes": "episodes_shard__",
                  "windows": "windows_shard__"}
PROGRESS_PREFIX = "sessions_progress__"

# Vector budget per chain link: ~150k float32 vectors @ dim 1536 ≈ 920 MB.
# Above it a link degrades from one batch-union load (tier 1) to
# per-collection loads (tier 2) — always correct, collections are independent.
MAX_VECTORS_PER_LINK = 150_000




def default_params() -> dict:
    """Return the default segmentation/window parameters.

    Values come from the ``[sessions]`` config section, falling back to the
    module constants (the study-locked values) for keys the config omits.
    Per-run ``task_args`` overrides still take precedence over both.
    """
    from fyp.fyp_config import fyp_cf

    cfg = fyp_cf.get("sessions", {})
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "cut": float(cfg.get("binge_cut", CUT)),
        "mem": int(cfg.get("binge_mem", MEM)),
        "min_videos": int(cfg.get("binge_min_videos", MIN_VIDEOS)),
        "min_minutes": float(cfg.get("binge_min_minutes", MIN_MINUTES)),
        "window_n": int(cfg.get("window_n", WINDOW_N)),
        "max_windows": int(cfg.get("max_windows", MAX_WINDOWS)),
    }




def _corpus_mean_filename(model: str) -> str:
    """Return the per-model corpus-mean cache filename (filesystem-safe)."""
    return embedding_store.corpus_mean_filename(model)




def save_corpus_mean(model: str, mean: np.ndarray, count: int) -> None:
    """Persist the corpus mean for ``model`` (delegates to embedding_store).

    Kept for API compatibility; the persistence (incl. the optional
    store-fingerprint stamp) is owned by :mod:`fyp.analysis.embedding_store`.

    Args:
        model: Embedding model id the mean was computed over.
        mean: The ``(d,)`` mean vector.
        count: Number of vectors the mean was computed over (provenance).
    """
    embedding_store.save_corpus_mean(model, mean, count)




def load_corpus_mean(model: str) -> np.ndarray | None:
    """Load the cached corpus mean for ``model``, or None when absent."""
    return embedding_store.load_corpus_mean(model)




def load_directional_store(model: str, reporter=None) -> tuple[dict, np.ndarray, int]:
    """Load one model's embedding store as in-place directional float32 vectors.

    Loads the raw store, computes the corpus mean over exactly these vectors
    (persisting it for provenance), then corpus-mean-centres and L2-normalises
    in place — the shared geometry pipeline of :mod:`fyp.analysis.entropy_metrics`.

    Args:
        model: Embedding model id to load (never mix models in one matrix).
        reporter: Optional status reporter for shard-load progress.

    Returns:
        ``(id_to_idx, U, count)`` where ``U`` is an ``(n, d)`` float32 array of
        directional vectors, ``id_to_idx`` maps item_id to its row, and
        ``count`` is the number of vectors loaded.
    """
    ids, mat = embeddings.load_embeddings(reporter=reporter, model=model)
    if len(ids) == 0:
        return {}, mat, 0
    mat = mat.astype(np.float32, copy=False)
    mean = mat.mean(axis=0, dtype=np.float64)
    save_corpus_mean(model, mean, len(ids))
    _directionalise(mat, mean)
    return {iid: i for i, iid in enumerate(ids)}, mat, len(ids)




def _directionalise(mat: np.ndarray, corpus_mean: np.ndarray) -> np.ndarray:
    """Corpus-mean-centre and L2-normalise ``mat`` in place.

    Row norms via einsum: np.linalg.norm materialises a full (n, d) x*x
    temporary — a second matrix-sized allocation and this pipeline's former
    peak; the einsum reduction allocates only the (n,) output.
    """
    mat -= corpus_mean.astype(np.float32)
    norms = np.sqrt(np.einsum("ij,ij->i", mat, mat))[:, None]
    np.divide(mat, np.where(norms < entropy_metrics.EPS_NORM, entropy_metrics.EPS_NORM, norms), out=mat)
    return mat




def load_directional_block(model: str, item_ids: list, corpus_mean: np.ndarray,
                           index=None) -> tuple[dict, np.ndarray]:
    """Directional vectors for one batch of item ids, from the dense sidecar.

    The batch-scoped counterpart of :func:`load_directional_store`: identical
    maths (centre on the **global** ``corpus_mean``, then L2-normalise), but
    only the requested rows are ever resident. Ids without a stored vector
    are simply absent from the returned map — exactly how the full-store
    ``id2idx`` treated them.

    Args:
        model: Embedding model id.
        item_ids: Item ids to fetch (order defines block row order).
        corpus_mean: The GLOBAL corpus mean (never a batch mean — a batch
            mean silently changes every distance; see the module docstring).
        index: The model's :class:`~fyp.analysis.embedding_store.DenseIndex`
            (None loads it, or yields an empty block when no store exists).

    Returns:
        ``(id_to_row, U_block)`` — map of found item_id to block row, and the
        ``(n_found, d)`` float32 directional block.
    """
    if index is None:
        index = embedding_store.load_index(model)
    if index is None or len(item_ids) == 0:
        return {}, np.empty((0, 1), dtype=np.float32)
    rows, found = index.lookup(item_ids)
    if not found.any():
        return {}, np.empty((0, index.dim), dtype=np.float32)
    U = embedding_store.read_vectors(model, rows, index, dtype=np.float32)
    _directionalise(U, corpus_mean)
    found_ids = [str(i) for i, f in zip(item_ids, found) if f]
    return {iid: i for i, iid in enumerate(found_ids)}, U




def load_video_features(item_ids: set[str] | None = None) -> pd.DataFrame:
    """Load per-video content features for episode/session characterisation.

    Joins the denormalised map fields (niche, category, annotation scalars,
    story) with the scrape author handle (kept out of the embeddings, so it is
    an independent signal for the same-/cross-author question). Callers index
    into it per episode/session.

    Args:
        item_ids: Optional item-id subset pushed into both parquet reads, so a
            batch-scoped build holds a batch-sized frame instead of the corpus.

    Returns:
        A DataFrame indexed by ``item_id`` with ``niche_name``, ``category``,
        ``story``, ``political_score``, ``sensitivity_score``, ``advertising``,
        and ``author``.
    """
    id_filter = ([("item_id", "in", [str(i) for i in item_ids])]
                 if item_ids is not None else None)
    mp = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename="video_map.parquet",
        columns=["item_id", "niche_name", "category", "story",
                 "political_score", "sensitivity_score", "advertising"],
        filters=id_filter,
    )
    if mp is None:
        mp = pd.DataFrame(columns=["item_id", "niche_name", "category", "story",
                                   "political_score", "sensitivity_score", "advertising"])
    feat = mp.copy()
    feat["item_id"] = feat["item_id"].astype("string")
    for col in ("political_score", "sensitivity_score"):
        feat[col] = pd.to_numeric(feat[col], errors="coerce")

    # The scrape author column is `author_handle` post contract-canonicalisation
    # but `author_uniqueId` in older stores; take whichever exists.
    auth = None
    for author_col in ("author_handle", "author_uniqueId"):
        try:
            auth = data_io.load_parquet_selective(
                storage_location=embeddings.STORE_LOCATION,
                filename=embeddings.SCRAPES_FILE,
                columns=["item_id", author_col],
                filters=id_filter,
            )
        except Exception:
            auth = None
        if auth is not None and author_col in auth.columns:
            auth = auth.rename(columns={author_col: "author"})
            break
    if auth is None or "author" not in getattr(auth, "columns", []):
        auth = pd.DataFrame({"item_id": pd.Series([], dtype="string"),
                             "author": pd.Series([], dtype="string")})
    auth["item_id"] = auth["item_id"].astype("string")
    auth = auth.drop_duplicates("item_id")
    return feat.merge(auth, on="item_id", how="left").set_index("item_id")




def enrichment_id_sets(model: str, item_ids: set[str] | None = None,
                       include_embedded: bool = True) -> dict[str, set]:
    """Return per-item enrichment-status id sets used for session coverage.

    Args:
        model: Embedding model id scoping the ``embedded`` set.
        item_ids: Optional item-id subset — pushed into the parquet reads as a
            filter so the returned sets (and their Python-string memory, ~200
            MB unfiltered at 1M scraped ids) stay batch-sized.
        include_embedded: When False, skip the ``embedded`` set's full shard
            scan and return it empty. The artifact build derives that set from
            the loaded vector matrix instead — the two must agree exactly, so
            a second, independent scan was both wasted I/O and a consistency
            risk.

    Returns:
        A dict with ``scraped``, ``downloaded``, ``annotated``, and
        ``embedded`` item-id sets.
    """
    id_filter = ([("item_id", "in", [str(i) for i in item_ids])]
                 if item_ids is not None else None)
    scraped: set[str] = set()
    downloaded: set[str] = set()
    if data_io.exists(storage_location=embeddings.STORE_LOCATION,
                      filename=embeddings.SCRAPES_FILE):
        scr = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.SCRAPES_FILE,
            columns=["item_id", "scraped_ok", "video_downloaded"],
            filters=id_filter,
        )
        if scr is not None and "item_id" in scr.columns:
            ids = scr["item_id"].astype("string")
            if "scraped_ok" in scr.columns:
                scraped = set(ids[scr["scraped_ok"] == True])
            if "video_downloaded" in scr.columns:
                downloaded = set(ids[scr["video_downloaded"] == True])
    annotated = set(embeddings.annotated_ok_item_ids())
    if item_ids is not None:
        annotated &= {str(i) for i in item_ids}
    embedded = (embeddings.embedded_item_ids(model=model)
                if include_embedded else set())
    return {"scraped": scraped, "downloaded": downloaded,
            "annotated": annotated, "embedded": embedded}




def load_plays(collection_ids: list[str] | None = None) -> pd.DataFrame:
    """Load the ``play`` rows for segmentation from the consolidated activity file.

    Args:
        collection_ids: Optional collections to restrict to (filter pushdown);
            None loads every collection.

    Returns:
        A time-parsed DataFrame with ``collection_id``/``item_id``/``_ts``/
        ``play_duration``/``session_id``/``source_platform`` for
        ``activity_type == 'play'`` rows (unparseable timestamps dropped).
    """
    filters: list[tuple] = [("activity_type", "==", "play")]
    if collection_ids is not None:
        filters.append(("collection_id", "in", list(collection_ids)))
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["collection_id", "item_id", "local_timestamp", "play_duration",
                 "session_id", "source_platform"],
        filters=filters,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["collection_id", "item_id", "_ts",
                                     "play_duration", "session_id", "source_platform"])
    df = df.copy()
    # string[pyarrow], not "string": the default python-backed StringDtype
    # materialises one Python str per cell (+684 MB over these two columns at
    # 4.3M plays); the arrow backing keeps them in contiguous buffers.
    df["item_id"] = df["item_id"].astype("string[pyarrow]")
    df["collection_id"] = df["collection_id"].astype("string[pyarrow]")
    df["_ts"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    df = df.drop(columns=["local_timestamp"])
    df = df.dropna(subset=["_ts"])
    df["play_duration"] = pd.to_numeric(df["play_duration"], errors="coerce")
    return df




def discover_collections(collections: list[str] | None = None) -> list[tuple[str, int]]:
    """Collections with play rows, ordered by descending play count.

    One streamed pass over the ``collection_id`` column (a dictionary-encoded
    few MB even at millions of rows). Descending order puts the biggest — the
    most likely to blow a chain link — on link 0, where a failure is cheapest
    to abandon; ties break on collection_id so the ordering (and therefore
    the chain) is deterministic under Cloud Tasks retry.

    Args:
        collections: Optional allow-list of collection ids.

    Returns:
        ``[(collection_id, n_plays), ...]`` sorted by (-n_plays, id).
    """
    fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=fn):
        return []
    allow = {str(c) for c in collections} if collections is not None else None
    counts: dict[str, int] = {}
    for rb in data_io.iter_parquet_batches(
            storage_location=embeddings.STORE_LOCATION, filename=fn,
            columns=["collection_id"],
            filters=[("activity_type", "==", "play")], batch_size=1_048_576):
        for entry in pa_compute.value_counts(rb.column(0)).to_pylist():
            cid = entry["values"]
            if cid is None:
                continue
            cid = str(cid)
            if allow is not None and cid not in allow:
                continue
            counts[cid] = counts.get(cid, 0) + int(entry["counts"])
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))




def segment_session(seq: list[tuple], U: np.ndarray, cut: float, mem: int,
                    min_videos: int, min_minutes: float) -> list[dict]:
    """Grow focus episodes within one session's embedded plays.

    Args:
        seq: Time-ordered ``(item_id, row_idx, ts, dur)`` tuples for the
            session's embedded plays.
        U: Directional vector store.
        cut: Focus threshold on mean cosine distance to the recent centroid.
        mem: Number of recent members the centroid is taken over.
        min_videos: Minimum distinct videos to keep an episode.
        min_minutes: Minimum span (minutes) to keep an episode.

    Returns:
        A list of episode dicts (raw members + span; geometry/content are
        attributed later by :func:`episode_record`).
    """
    episodes: list[dict] = []
    cur: dict | None = None

    def close(c: dict | None) -> None:
        if c is None or len(c["idx"]) < min_videos:
            return
        if (c["end_ts"] - c["start_ts"]).total_seconds() / 60.0 >= min_minutes:
            episodes.append(c)

    def fresh(iid, ridx, ts, dur) -> dict:
        return {"ids": [iid], "idx": [ridx], "seen": {iid},
                "m_ts": [ts], "m_dur": [dur],
                "start_ts": ts, "end_ts": ts, "n_plays": 1}

    for iid, ridx, ts, dur in seq:
        if cur is None:
            cur = fresh(iid, ridx, ts, dur)
            continue
        if iid in cur["seen"]:
            # A rewatch extends the span but is not a new member — otherwise a
            # repeat loop collapses the effective rank and fakes a binge.
            cur["n_plays"] += 1
            cur["end_ts"] = ts
            continue
        centroid = U[cur["idx"][-mem:]].mean(axis=0)
        dist = 1.0 - float(U[ridx] @ centroid)
        if dist <= cut:
            cur["ids"].append(iid)
            cur["idx"].append(ridx)
            cur["seen"].add(iid)
            cur["m_ts"].append(ts)
            cur["m_dur"].append(dur)
            cur["n_plays"] += 1
            cur["end_ts"] = ts
        else:
            close(cur)
            cur = fresh(iid, ridx, ts, dur)
    close(cur)
    return episodes




def _num(value, ndigits: int | None = None) -> float | None:
    """Return ``value`` as a float (optionally rounded), or None when missing.

    PyArrow-backed frames yield ``pd.NA`` from reductions like ``mean()`` /
    ``median()`` when the inputs are all-null; ``float(pd.NA)`` raises, so
    every scalar destined for an artifact row goes through this guard.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    out = float(value)
    if not np.isfinite(out):
        return None
    return round(out, ndigits) if ndigits is not None else out




def _dominant(series: pd.Series) -> tuple[object, float]:
    """Return the modal value of a series and its share."""
    s = series.dropna()
    if s.empty:
        return None, 0.0
    vc = s.value_counts()
    return vc.index[0], round(float(vc.iloc[0]) / float(len(s)), 3)




def _rolling_cosdists(idx: list[int], U: np.ndarray, mem: int) -> list[float | None]:
    """Per-member mean cosine distance to the centroid of the previous members.

    Element ``i`` (``i ≥ 1``) is the distance of member ``i`` to the centroid
    of the previous ``min(i, mem)`` members — the exact quantity the segmenter
    thresholds against ``cut``, so the UI sparkline shows *why* the episode
    held together. Element 0 is None.

    Args:
        idx: Episode members' row indices into ``U``, in time order.
        U: Directional vector store.
        mem: Centroid memory (same as the segmenter's).

    Returns:
        A list aligned to ``idx``.
    """
    out: list[float | None] = [None]
    for i in range(1, len(idx)):
        centroid = U[idx[max(0, i - mem):i]].mean(axis=0)
        out.append(round(1.0 - float(U[idx[i]] @ centroid), 4))
    return out




def episode_record(ep: dict, cid: str, sess: object, U: np.ndarray,
                   feat: pd.DataFrame, play_ts: np.ndarray, mem: int = MEM) -> dict:
    """Reduce one raw episode to a fully-attributed table row.

    Args:
        ep: A raw episode dict from :func:`segment_session`.
        cid: The collection id.
        sess: The session key.
        U: Directional vector store.
        feat: Per-video features indexed by item_id (:func:`load_video_features`).
        play_ts: Sorted int64 timestamps of ALL the collection's plays (used to
            count unembedded plays interleaved inside the episode's span).
        mem: Centroid memory for the rolling-distance series.

    Returns:
        One episode row (JSON/parquet-friendly scalars + member lists).
    """
    idx = np.asarray(ep["idx"])
    Uep = U[idx]
    k = len(idx)
    span_min = round((ep["end_ts"] - ep["start_ts"]).total_seconds() / 60.0, 2)

    geo = entropy_metrics.trajectory_geometry(Uep)
    ent_bits, eff_rank = entropy_metrics.spectral_entropy(Uep)
    focus = entropy_metrics.mean_pairwise_cosine_distance(Uep)

    # Plays of any kind (incl. unembedded) inside the episode's span — measures
    # how much off-corpus content interleaved the focused run.
    lo, hi = np.searchsorted(play_ts, [ep["start_ts"].value, ep["end_ts"].value + 1])
    n_in_span = int(hi - lo)

    f = feat.reindex(ep["ids"])
    niche, niche_share = _dominant(f["niche_name"])
    author, author_share = _dominant(f["author"])
    adv, adv_share = _dominant(f["advertising"])

    dwell = [_num(v, 1) for v in ep["m_dur"]]
    return {
        "collection_id": cid,
        "session_id": str(sess),
        "start_ts": ep["start_ts"].isoformat(),
        "end_ts": ep["end_ts"].isoformat(),
        "duration_min": span_min,
        "n_plays": int(ep["n_plays"]),
        "n_distinct": k,
        "repeat_rate": round(ep["n_plays"] / k, 2),
        "n_interleaved": max(n_in_span - int(ep["n_plays"]), 0),
        "focus": _num(focus, 4),
        "diameter": _num(geo["diameter"], 4),
        "step_mean": _num(geo["step_mean"], 4),
        "straightness": _num(geo["straightness"], 4),
        "spectral_entropy_bits": _num(ent_bits, 4),
        "effective_rank": _num(eff_rank, 3),
        "direction_p": _num(entropy_metrics.direction_permutation_p(Uep), 4),
        "dominant_niche": niche,
        "dominant_niche_share": niche_share,
        "n_niches": int(f["niche_name"].nunique()),
        "n_authors": int(f["author"].nunique()),
        "dominant_author_share": author_share,
        "advertising": None if adv is None or pd.isna(adv) else str(adv),
        "advertising_share": adv_share,
        "mean_political": _num(pd.to_numeric(f["political_score"], errors="coerce").mean(), 4),
        "mean_sensitivity": _num(pd.to_numeric(f["sensitivity_score"], errors="coerce").mean(), 4),
        "member_item_ids": [str(i) for i in ep["ids"]],
        "member_ts": [t.isoformat() for t in ep["m_ts"]],
        "member_dwell_s": dwell,
        "member_rolling_cosdist": _rolling_cosdists(list(ep["idx"]), U, mem),
    }




def low_entropy_windows(emb_seq: list[tuple], U: np.ndarray, window_n: int,
                        max_windows: int = 3) -> list[dict]:
    """The session's lowest-distance ("low-entropy") sliding windows.

    Slides a window of ``window_n`` consecutive *distinct* embedded videos
    (first-occurrence order — repeats are already deduped upstream) across the
    session, scores each window by its mean pairwise cosine distance, and
    greedily keeps up to ``max_windows`` **non-overlapping** windows in
    ascending score order. Because every window has the same size, the
    normalised spectral entropy reported alongside is directly comparable
    across windows too; the distance stays the rank key (the study found it
    the more sensitive of the two).

    Args:
        emb_seq: Time-ordered ``(item_id, row_idx, ts, dwell)`` tuples for the
            session's distinct embedded videos (first play of each).
        U: Directional vector store.
        window_n: Window width (distinct videos).
        max_windows: Maximum number of non-overlapping windows to keep.

    Returns:
        A list of window dicts (ascending distance; may be empty when the
        session has fewer than ``window_n`` distinct embedded videos), each
        with ``mean_cosdist``/``entropy_norm``/member lists.
    """
    n = len(emb_seq)
    if n < window_n:
        return []
    idx = [row for _, row, _, _ in emb_seq]
    scored: list[tuple[float, int]] = []
    for i in range(0, n - window_n + 1):
        d = entropy_metrics.mean_pairwise_cosine_distance(U[idx[i:i + window_n]])
        if np.isfinite(d):
            scored.append((float(d), i))
    scored.sort()

    chosen: list[tuple[float, int]] = []
    taken: list[tuple[int, int]] = []
    for d, i in scored:
        span = (i, i + window_n - 1)
        if any(span[0] <= hi and span[1] >= lo for lo, hi in taken):
            continue
        chosen.append((d, i))
        taken.append(span)
        if len(chosen) >= max_windows:
            break

    out: list[dict] = []
    for d, i in chosen:
        members = emb_seq[i:i + window_n]
        ent_bits, _ = entropy_metrics.spectral_entropy(U[idx[i:i + window_n]])
        ent_norm = float(ent_bits / np.log2(window_n)) if np.isfinite(ent_bits) else None
        start_ts, end_ts = members[0][2], members[-1][2]
        out.append({
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "duration_min": round((end_ts - start_ts).total_seconds() / 60.0, 2),
            "n_distinct": window_n,
            "mean_cosdist": round(d, 4),
            "entropy_norm": round(ent_norm, 4) if ent_norm is not None else None,
            "member_item_ids": [str(m[0]) for m in members],
            "member_ts": [m[2].isoformat() for m in members],
            "member_dwell_s": [_num(m[3], 1) for m in members],
        })
    return out




def session_record(cid: str, sess: object, g: pd.DataFrame, id2idx: dict,
                   U: np.ndarray, feat: pd.DataFrame, id_sets: dict,
                   episodes: list[dict], window_n: int = WINDOW_N,
                   max_windows: int = MAX_WINDOWS) -> tuple[dict, list[dict]]:
    """Reduce one session's plays to a quality/entropy row + its low-entropy windows.

    Args:
        cid: The collection id.
        sess: The session key.
        g: The session's play rows, time-sorted, with ``_ts``/``item_id``/
            ``play_duration``.
        id2idx: item_id → row map into ``U`` (the embedded set).
        U: Directional vector store.
        feat: Per-video features indexed by item_id.
        id_sets: Enrichment id sets from :func:`enrichment_id_sets`.
        episodes: The session's attributed episode rows.
        window_n: Sliding-window width for the low-entropy windows.

    Returns:
        ``(session_row, window_rows)`` — the row for ``sessions_index.parquet``
        and up to :data:`MAX_WINDOWS` attributed low-entropy-window rows.
    """
    n_plays = int(len(g))
    # Plain-Python membership throughout: ``Series.isin(<set>)`` re-hashes the
    # whole (100k+-id) set on every call, which at ~10^5 sessions per corpus
    # turns the build from minutes into hours.
    items_list = [str(i) for i in g["item_id"]]
    seen: set[str] = set()
    distinct_list = [i for i in items_list if not (i in seen or seen.add(i))]
    n_distinct = len(distinct_list)
    start_ts, end_ts = g["_ts"].iloc[0], g["_ts"].iloc[-1]
    dur = pd.to_numeric(g["play_duration"], errors="coerce")

    n_scraped = sum(1 for i in distinct_list if i in id_sets["scraped"])
    n_annotated = sum(1 for i in distinct_list if i in id_sets["annotated"])
    embedded = id_sets["embedded"]
    emb_distinct = [i for i in distinct_list if i in embedded]
    n_embedded = len(emb_distinct)
    emb_plays = sum(1 for i in items_list if i in embedded)

    # Distinct embedded videos in first-play order (rewatches deduped per the
    # study guardrail), each with its first play's timestamp and dwell — the
    # sequence the low-entropy window slides over.
    emb_seq: list[tuple] = []
    seq_seen: set[str] = set()
    for iid, ts, du in zip(items_list, g["_ts"], g["play_duration"]):
        if iid in seq_seen or iid not in id2idx:
            continue
        seq_seen.add(iid)
        emb_seq.append((iid, id2idx[iid], ts, du))
    windows = low_entropy_windows(emb_seq, U, window_n, max_windows=max_windows)
    for w_idx, w in enumerate(windows):
        w["collection_id"] = cid
        w["session_id"] = str(sess)
        w["window_idx"] = w_idx
        w["dominant_niche"], _ = _dominant(feat.reindex(w["member_item_ids"])["niche_name"])
    min_cosdist = windows[0]["mean_cosdist"] if windows else None
    ent_norm = windows[0]["entropy_norm"] if windows else None

    emb_feat = feat.reindex(emb_distinct)
    niche, _ = _dominant(emb_feat["niche_name"]) if len(emb_feat) else (None, 0.0)

    ep_plays = int(sum(e["n_plays"] for e in episodes))
    med_dwell = _num(dur.median(), 1)
    return {
        "collection_id": cid,
        "session_id": str(sess),
        "start_ts": start_ts.isoformat(),
        "end_ts": end_ts.isoformat(),
        "duration_min": round((end_ts - start_ts).total_seconds() / 60.0, 2),
        "n_plays": n_plays,
        "n_distinct": n_distinct,
        "total_watch_s": _num(dur.fillna(0).sum(), 1) or 0.0,
        "median_dwell_s": med_dwell,
        "n_scraped": n_scraped,
        "n_annotated": n_annotated,
        "n_embedded": n_embedded,
        "coverage_scraped": round(n_scraped / n_distinct, 4) if n_distinct else 0.0,
        "coverage_annotated": round(n_annotated / n_distinct, 4) if n_distinct else 0.0,
        "coverage_embedded": round(n_embedded / n_distinct, 4) if n_distinct else 0.0,
        "emb_play_coverage": round(emb_plays / n_plays, 4) if n_plays else 0.0,
        "min_window_cosdist": min_cosdist,
        "min_window_entropy_norm": ent_norm,
        "n_episodes": len(episodes),
        "episode_play_frac": round(ep_plays / n_plays, 4) if n_plays else 0.0,
        "dominant_niche": niche,
        "n_niches": int(emb_feat["niche_name"].nunique()) if len(emb_feat) else 0,
    }, windows




def build_collection(cid: str, plays: pd.DataFrame, id2idx: dict, U: np.ndarray,
                     feat: pd.DataFrame, id_sets: dict,
                     params: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Segment one collection's sessions and build its session + episode rows.

    Every session gets a row (including sessions with no embedded plays — they
    are exactly what the quality filter must be able to see and exclude);
    episodes are detected only on embedded plays.

    Args:
        cid: The collection id.
        plays: The collection's play rows (from :func:`load_plays`).
        id2idx: item_id → row map into ``U``.
        U: Directional vector store.
        feat: Per-video features indexed by item_id.
        id_sets: Enrichment id sets from :func:`enrichment_id_sets`.
        params: Optional parameter overrides (see :func:`default_params`).

    Returns:
        ``(session_rows, episode_rows, window_rows)``.
    """
    p = {**default_params(), **(params or {})}
    plays = plays.sort_values("_ts")
    play_ts = plays["_ts"].astype("int64").to_numpy()

    # Stable session key (isolate rows with no session_id rather than merging them).
    sess = plays["session_id"].astype("string")
    if sess.isna().any():
        # Only build the row-indexed fallback when needed — unconditionally it
        # allocated a full-length Python-string Series per collection that
        # .where() then discarded (session_id is non-null in real data).
        sess = sess.where(sess.notna(), "na_" + pd.Series(plays.index, index=plays.index).astype("string"))
    plays = plays.assign(_sess=sess)

    # One vectorised membership pass per collection; the per-session loop must
    # never call Series.isin against the whole embedded-id set (see
    # session_record's note on why).
    if "_emb" not in plays.columns:
        plays = plays.assign(_emb=plays["item_id"].isin(list(id2idx)))

    session_rows: list[dict] = []
    episode_rows: list[dict] = []
    window_rows: list[dict] = []
    for s, g in plays.groupby("_sess", sort=False):
        emb = g[g["_emb"]]
        seq = [(iid, id2idx[iid], ts, du) for iid, ts, du in
               zip(emb["item_id"], emb["_ts"], emb["play_duration"])]
        eps = []
        for ep_idx, ep in enumerate(segment_session(
                seq, U, p["cut"], p["mem"], p["min_videos"], p["min_minutes"])):
            row = episode_record(ep, cid, s, U, feat, play_ts, mem=p["mem"])
            row["episode_idx"] = ep_idx
            eps.append(row)
        episode_rows.extend(eps)
        srow, wins = session_record(
            cid, s, g, id2idx, U, feat, id_sets, eps,
            window_n=p["window_n"], max_windows=p["max_windows"])
        session_rows.append(srow)
        window_rows.extend(wins)
    return session_rows, episode_rows, window_rows




# Explicit Arrow schemas so `data_io.save_parquet` takes its all-ArrowDtype
# fast path and readers see stable dtypes (CLAUDE.md: PyArrow dtypes always).
_SESSIONS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(), "session_id": pa.string(),
    "start_ts": pa.string(), "end_ts": pa.string(),
    "duration_min": pa.float32(), "n_plays": pa.int32(), "n_distinct": pa.int32(),
    "total_watch_s": pa.float32(), "median_dwell_s": pa.float32(),
    "n_scraped": pa.int32(), "n_annotated": pa.int32(), "n_embedded": pa.int32(),
    "coverage_scraped": pa.float32(), "coverage_annotated": pa.float32(),
    "coverage_embedded": pa.float32(), "emb_play_coverage": pa.float32(),
    "min_window_cosdist": pa.float32(), "min_window_entropy_norm": pa.float32(),
    "n_episodes": pa.int16(), "episode_play_frac": pa.float32(),
    "dominant_niche": pa.string(), "n_niches": pa.int16(),
}

_WINDOWS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(), "session_id": pa.string(), "window_idx": pa.int16(),
    "start_ts": pa.string(), "end_ts": pa.string(), "duration_min": pa.float32(),
    "n_distinct": pa.int16(), "mean_cosdist": pa.float32(), "entropy_norm": pa.float32(),
    "dominant_niche": pa.string(),
    "member_item_ids": pa.large_list(pa.string()),
    "member_ts": pa.large_list(pa.string()),
    "member_dwell_s": pa.large_list(pa.float32()),
}

_EPISODES_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(), "session_id": pa.string(), "episode_idx": pa.int16(),
    "start_ts": pa.string(), "end_ts": pa.string(), "duration_min": pa.float32(),
    "n_plays": pa.int32(), "n_distinct": pa.int32(), "repeat_rate": pa.float32(),
    "n_interleaved": pa.int32(), "focus": pa.float32(), "diameter": pa.float32(),
    "step_mean": pa.float32(), "straightness": pa.float32(),
    "spectral_entropy_bits": pa.float32(), "effective_rank": pa.float32(),
    "direction_p": pa.float32(),
    "dominant_niche": pa.string(), "dominant_niche_share": pa.float32(),
    "n_niches": pa.int16(), "n_authors": pa.int16(),
    "dominant_author_share": pa.float32(), "advertising": pa.string(),
    "advertising_share": pa.float32(), "mean_political": pa.float32(),
    "mean_sensitivity": pa.float32(),
    "member_item_ids": pa.large_list(pa.string()),
    "member_ts": pa.large_list(pa.string()),
    "member_dwell_s": pa.large_list(pa.float32()),
    "member_rolling_cosdist": pa.large_list(pa.float32()),
}




def _arrow_frame(rows: list[dict], schema: dict[str, pa.DataType]) -> pd.DataFrame:
    """Build an all-ArrowDtype DataFrame from row dicts with an explicit schema."""
    data = {}
    for col, typ in schema.items():
        values = [r.get(col) for r in rows]
        data[col] = pd.array(
            pa.array(values, type=typ), dtype=pd.ArrowDtype(typ),
        )
    return pd.DataFrame(data)




def build_batch(cids: list[str], model: str, corpus_mean: np.ndarray | None,
                index=None, params: dict | None = None, reporter=None,
                max_vectors: int = MAX_VECTORS_PER_LINK):
    """Segment one batch of collections against the dense embedding sidecar.

    Peak memory is O(batch): only the batch's plays, features, id sets and
    vectors are resident. The vectors are centred on the **global**
    ``corpus_mean`` — never a batch mean — so any partition of the corpus
    into batches yields identical rows (centring and normalisation are
    per-row; everything downstream is within-session; sessions never cross a
    collection boundary).

    Args:
        cids: The batch's collection ids.
        model: Embedding model id.
        corpus_mean: The global corpus mean (None when no store exists —
            sessions still get quality rows, with no episodes/windows).
        index: The model's DenseIndex (None: loaded here / no store).
        params: Segmentation parameter overrides.
        reporter: Optional status reporter (cancellation checks per
            collection).
        max_vectors: Tier gate — a batch whose embedded-distinct union
            exceeds this loads vectors per collection (tier 2) instead of
            once for the union (tier 1).

    Returns:
        ``(session_rows, episode_rows, window_rows, stats)``; all None when
        cancelled mid-batch.
    """
    p = {**default_params(), **(params or {})}
    plays = load_plays(cids)
    stats = {"n_plays": int(len(plays)), "n_vectors": 0, "tier": 1}
    if plays.empty:
        return [], [], [], stats

    batch_ids = [str(i) for i in plays["item_id"].drop_duplicates()]
    feat = load_video_features(item_ids=set(batch_ids))
    id_sets = enrichment_id_sets(model, item_ids=set(batch_ids),
                                 include_embedded=False)

    if index is None and corpus_mean is not None:
        index = embedding_store.load_index(model)
    if index is not None and corpus_mean is not None:
        _, found = index.lookup(batch_ids)
        n_union = int(found.sum())
    else:
        found = np.zeros(len(batch_ids), dtype=bool)
        n_union = 0
    stats["n_vectors"] = n_union
    tier1 = n_union <= max_vectors

    session_rows: list[dict] = []
    episode_rows: list[dict] = []
    window_rows: list[dict] = []

    if tier1:
        embedded_ids = [i for i, f in zip(batch_ids, found) if f]
        id2local, U = load_directional_block(model, embedded_ids, corpus_mean,
                                             index) if n_union else ({}, np.empty((0, 1), np.float32))
        id_sets["embedded"] = set(id2local)
        for cid in cids:
            if reporter is not None and reporter.check_cancelled():
                return None, None, None, None
            srows, erows, wrows = build_collection(
                cid, plays[plays["collection_id"] == cid], id2local, U,
                feat, id_sets, p)
            session_rows.extend(srows)
            episode_rows.extend(erows)
            window_rows.extend(wrows)
    else:
        # Tier 2: the union exceeds the budget — load and free per collection.
        stats["tier"] = 2
        for cid in cids:
            if reporter is not None and reporter.check_cancelled():
                return None, None, None, None
            cplays = plays[plays["collection_id"] == cid]
            c_ids = [str(i) for i in cplays["item_id"].drop_duplicates()]
            id2local, U = load_directional_block(model, c_ids, corpus_mean, index)
            id_sets["embedded"] = set(id2local)
            srows, erows, wrows = build_collection(
                cid, cplays, id2local, U, feat, id_sets, p)
            session_rows.extend(srows)
            episode_rows.extend(erows)
            window_rows.extend(wrows)
            del U, id2local
    return session_rows, episode_rows, window_rows, stats




def _publish_type(typ: pa.DataType) -> pa.DataType:
    """Downgrade large_list to list for the on-disk schema.

    The historical save_parquet path applied the same downgrade
    (types.downgrade_large_arrow_columns), so on-disk files always carried
    plain list — keep that contract for the read side. NOTE for future
    consumers: pandas 2.2.x `explode()` silently no-ops on large_list;
    api_sessions_routes reads member lists with `list(value)`, never explode.
    """
    return pa.list_(typ.value_type) if pa.types.is_large_list(typ) else typ




def _arrow_table(rows: list[dict], schema: dict[str, pa.DataType]) -> pa.Table:
    """Rows -> pyarrow Table in the published (list, not large_list) schema."""
    return pa.table({
        col: pa.array([r.get(col) for r in rows], type=_publish_type(typ))
        for col, typ in schema.items()})




def shard_filename(kind: str, run_id: str, chunk: int) -> str:
    """Per-link intermediate shard name (deterministic: retries overwrite)."""
    return f"{SHARD_PREFIXES[kind]}{run_id}__{chunk:04d}.parquet"




def write_batch_shards(run_id: str, chunk: int, session_rows: list[dict],
                       episode_rows: list[dict], window_rows: list[dict]) -> None:
    """Persist one link's rows as its three deterministic shards."""
    for kind, schema, rows in (("sessions", _SESSIONS_SCHEMA, session_rows),
                               ("episodes", _EPISODES_SCHEMA, episode_rows),
                               ("windows", _WINDOWS_SCHEMA, window_rows)):
        tbl = _arrow_table(rows, schema)
        data_io.write_parquet_stream(
            storage_location=ARTIFACT_LOCATION,
            filename=shard_filename(kind, run_id, chunk),
            batches=[tbl], schema=tbl.schema)




def sweep_stale_run_files(current_run_id: str) -> None:
    """Remove intermediate files left by abandoned runs (other run ids)."""
    prefixes = tuple(SHARD_PREFIXES.values()) + (PROGRESS_PREFIX,)
    for fn in data_io.listdir(storage_location=ARTIFACT_LOCATION):
        if not fn.startswith(prefixes):
            continue
        if f"__{current_run_id}__" in fn or fn.endswith(f"{current_run_id}.json"):
            continue
        try:
            data_io.remove(storage_location=ARTIFACT_LOCATION, filename=fn)
        except Exception:
            pass




def publish_artifacts(run_id: str, n_chunks: int, expected: dict,
                      meta: dict, reporter=None,
                      covered_collections: int | None = None,
                      total_collections: int | None = None) -> dict:
    """Concatenate the run's shards into the three artifact files + meta.

    Publish order matters: ``sessions_index.parquet`` LAST — its size:mtime
    fingerprint is the tab's freshness gate, so episodes/windows must land
    first or the index would briefly advertise sessions whose detail rows
    don't exist yet. Shards are deleted only after every check below passed.

    Three independent completeness checks, because they fail differently:

    * **Coverage** (``covered_collections`` vs ``total_collections``) — the run
      must have segmented every collection discovery found. This is the one
      that matters under duplicate chains: a Cloud Tasks retry re-delivers the
      same task_args, so concurrent chains share a ``run_id``. The first to
      finish publishes and deletes BOTH the shards and the progress file, so a
      trailing chain rebuilds a progress file covering only its remaining
      chunks and then agrees with its own truncated shard set. Row counts are
      self-consistent in that case and wave it through — a half-corpus artifact
      silently replacing a complete one (observed in prod 2026-08-09).
      Coverage cannot be reset that way.
    * **Shard-set completeness** — every chunk 0..n_chunks-1 must be present.
    * **Row counts** (``expected``) — catches a shard that failed to write.

    Args:
        run_id: The run whose shards to publish.
        n_chunks: Number of links (shards per kind).
        expected: ``{"sessions": n, "episodes": n, "windows": n}`` totals.
        meta: The ``sessions_meta.json`` payload (counts already in it).
        reporter: Optional status reporter.
        covered_collections: Collections actually segmented by this run.
        total_collections: Collections discovery found at link 0.

    Returns:
        ``meta`` (persisted).

    Raises:
        RuntimeError: when the run is incomplete or a row count disagrees.
            Shards are left for inspection and nothing is published, so the
            previous artifacts stay intact.
    """
    if (covered_collections is not None and total_collections is not None
            and int(covered_collections) != int(total_collections)):
        raise RuntimeError(
            f"publish: run {run_id} covered {covered_collections} of "
            f"{total_collections} collections — refusing to publish a partial "
            f"artifact (a concurrent chain sharing this run_id most likely "
            f"published and cleaned up first). Shards kept for inspection.")

    for kind, final in (("episodes", EPISODES_FILE), ("windows", WINDOWS_FILE),
                        ("sessions", SESSIONS_FILE)):
        all_shards = [shard_filename(kind, run_id, k) for k in range(n_chunks)]
        shards = [s for s in all_shards
                  if data_io.exists(storage_location=ARTIFACT_LOCATION, filename=s)]
        if not shards:
            raise RuntimeError(f"publish: no '{kind}' shards found for run {run_id}")
        if len(shards) != len(all_shards):
            raise RuntimeError(
                f"publish: run {run_id} has an incomplete '{kind}' shard set "
                f"({len(all_shards) - len(shards)} of {n_chunks} missing) — "
                f"refusing to publish. Another chain sharing this run_id most "
                f"likely published first.")
        n = data_io.concat_parquet_files(
            src_storage_location=ARTIFACT_LOCATION, src_filenames=shards,
            dst_storage_location=ARTIFACT_LOCATION, dst_filename=final)
        if kind in expected and n != int(expected[kind]):
            raise RuntimeError(
                f"publish: '{kind}' row count {n} != expected {expected[kind]} "
                f"— shards kept for inspection, artifact NOT trusted")
        if reporter is not None:
            reporter.log(f"Published {final} ({n:,} rows from {len(shards)} shard(s))")

    data_io.save_json(data=meta, storage_location=ARTIFACT_LOCATION, filename=META_FILE)
    for kind in SHARD_PREFIXES:
        for k in range(n_chunks):
            fn = shard_filename(kind, run_id, k)
            if data_io.exists(storage_location=ARTIFACT_LOCATION, filename=fn):
                data_io.remove(storage_location=ARTIFACT_LOCATION, filename=fn)
    return meta




def build_artifacts(reporter=None, params: dict | None = None,
                    collections: list[str] | None = None,
                    batch_size: int = 8,
                    max_vectors: int = MAX_VECTORS_PER_LINK) -> dict:
    """Build and persist the session + episode artifacts for all collections.

    In-process driver over :func:`build_batch` — the same batch-scoped
    computation the chained Cloud-Task worker runs, looped locally. Peak
    memory is O(batch), never O(corpus).

    Args:
        reporter: Optional status reporter (progress + cancellation).
        params: Optional parameter overrides (see :func:`default_params`).
        collections: Optional collection-id subset (None = every collection).
        batch_size: Collections per batch.
        max_vectors: Per-batch vector budget (see :func:`build_batch`).

    Returns:
        A summary dict (the persisted ``sessions_meta.json`` payload).
    """
    def _log(msg: str) -> None:
        if reporter is not None:
            reporter.log(msg)
        else:
            logger.info(msg)

    p = {**default_params(), **(params or {})}
    backend = embeddings.active_embedding_backend()
    model = backend.model_id()

    _log(f"Preparing dense embedding store (model={model})...")
    try:
        corpus_mean, n_vectors, store_fp = embedding_store.get_corpus_mean(
            model, reporter=reporter)
        index = embedding_store.load_index(model)
    except (ValueError, embedding_store.CorpusMeanDrift):
        # No vectors for this model — sessions still get quality rows.
        corpus_mean, n_vectors, store_fp, index = None, 0, "", None
    _log(f"  {n_vectors:,} vectors")

    discovered = discover_collections(collections)
    cids = [c for c, _ in discovered]
    _log(f"  {len(cids)} collections to segment")

    all_sessions: list[dict] = []
    all_episodes: list[dict] = []
    all_windows: list[dict] = []
    for start in range(0, len(cids), batch_size):
        batch = cids[start:start + batch_size]
        srows, erows, wrows, stats = build_batch(
            batch, model, corpus_mean, index, params=p, reporter=reporter,
            max_vectors=max_vectors)
        if srows is None:
            _log("Cancelled by user.")
            return {"cancelled": True}
        all_sessions.extend(srows)
        all_episodes.extend(erows)
        all_windows.extend(wrows)
        done = min(start + batch_size, len(cids))
        if reporter is not None:
            reporter.update_progress(
                int(done / max(len(cids), 1) * 95),
                f"Segmented {done}/{len(cids)} collections "
                f"({len(all_sessions):,} sessions, {len(all_episodes):,} episodes, "
                f"{len(all_windows):,} windows)")

    _log(f"Writing artifacts: {len(all_sessions):,} sessions, "
         f"{len(all_episodes):,} episodes, {len(all_windows):,} low-entropy windows")
    data_io.save_parquet(
        df=_arrow_frame(all_sessions, _SESSIONS_SCHEMA),
        storage_location=ARTIFACT_LOCATION, filename=SESSIONS_FILE,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_episodes, _EPISODES_SCHEMA),
        storage_location=ARTIFACT_LOCATION, filename=EPISODES_FILE,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_windows, _WINDOWS_SCHEMA),
        storage_location=ARTIFACT_LOCATION, filename=WINDOWS_FILE,
    )
    meta = {
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "embedding_model": model,
        "embedding_dim": int(index.dim) if index is not None else backend.dim(),
        "corpus_mean_count": n_vectors,
        "store_fingerprint": store_fp,
        "params": p,
        "n_collections": len(cids),
        "n_sessions": len(all_sessions),
        "n_episodes": len(all_episodes),
        "n_windows": len(all_windows),
    }
    data_io.save_json(data=meta, storage_location=ARTIFACT_LOCATION, filename=META_FILE)
    return meta
