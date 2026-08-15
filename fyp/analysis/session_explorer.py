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
centroid of the last :data:`MEM` members ≤ :data:`CUT`. Up to :data:`MAX_SKIP`
consecutive off-theme videos are tolerated without ending the episode (an ad
mid-binge is the motivating case); they are counted as ``n_skipped`` but are
never members and never enter the centroid. Beyond that the episode closes
(kept if ≥ :data:`MIN_VIDEOS` distinct videos over ≥ :data:`MIN_MINUTES`) and
the scan rewinds to the first tolerated video so it can open the next episode.
``session_id`` boundaries hard-break episodes; repeated plays of a video
already in the episode extend its span but are not new members (a rewatch loop
must not fake a binge).

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

# Segmentation parameters. CUT/MEM/MIN_VIDEOS come from the embedding-entropy
# study (specification-curve validated; `mem` controls drift tolerance).
#
# MIN_MINUTES and MAX_SKIP were retuned 2026-08-10 against the production
# corpus and are NOT the study's values (the study used 3.0 minutes and no
# skip tolerance):
#   * MAX_SKIP: with no tolerance, a single off-theme video ended a run and
#     then seeded the next one, so 99.4% of candidate runs ended under
#     MIN_VIDEOS and long on-theme stretches never surfaced.
#   * MIN_MINUTES: at 3.0 it dropped 65% of the runs that did reach
#     MIN_VIDEOS, penalising fast scrolling — the most binge-like behaviour.
# These are only the fallbacks; `[sessions]` in the config carries the
# operative values (see default_params).
CUT = 0.5
MEM = 6
MIN_VIDEOS = 4
MIN_MINUTES = 1.0
MAX_SKIP = 2

# Off-theme plays with dwell under this many seconds are "flicks" — rejected
# feed noise, not a departure from the theme — and do not spend the MAX_SKIP
# budget (added 2026-08-11; 0 restores the pure-count rule). Validated on the
# production corpus's AIO-00060 session: the count-only rule severed a 4-video
# exercise cluster from the 14-video binge it visibly belonged to, because the
# 3 interleaved off-theme videos (total dwell 3 s) exhausted the budget.
FLICK_SECONDS = 3.0

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
# The play rows the sessions were segmented from, published sorted by
# (collection_id, ts) in small row groups so the detail endpoint's
# collection_id pushdown genuinely prunes — the consolidated activity file's
# row groups span the whole id space, so reading it live decodes ~all rows
# per request.
PLAYS_FILE = "sessions_plays.parquet"
META_FILE = "sessions_meta.json"
CORPUS_MEAN_PREFIX = "embedding_corpus_mean__"

# Per-link intermediate shards written by the chained build (namespaced by the
# run id so a retry of link k overwrites its own shard) and the cross-link
# progress accumulator. The final link concatenates the shards into the four
# single artifact files above, so the read side never changes.
SHARD_PREFIXES = {"sessions": "sessions_shard__", "episodes": "episodes_shard__",
                  "windows": "windows_shard__", "plays": "plays_shard__"}
PROGRESS_PREFIX = "sessions_progress__"

# Row-group size for the published plays artifact: small groups keep each
# group's collection_id min/max stats tight (a chunk's collections are
# contiguous after the per-shard sort), which is what makes the read side's
# pushdown prune.
PLAYS_ROW_GROUP = 32_768

# Vector budget per chain link: ~150k float32 vectors @ dim 1536 ≈ 920 MB.
# Above it a link degrades from one batch-union load (tier 1) to
# per-collection loads (tier 2) — always correct, collections are independent.
MAX_VECTORS_PER_LINK = 150_000

# Map columns that are identifiers or map coordinates, not measurements —
# excluded from the per-session min/max columns (and from the read side's
# trend scan, which mirrors this set in api_sessions_routes).
TREND_EXCLUDE = {"item_id", "niche", "x", "y"}

# Per-fragment / per-session caps on the searchable text blob. Load-bearing:
# without them a long session ships every full caption and the index (cached
# whole in the web process) grows by hundreds of MB corpus-wide.
_SEARCH_FRAGMENT_CAP = 200
_SEARCH_TEXT_CAP = 8_000




def trend_numeric_columns() -> list[str]:
    """Numeric ``video_map`` columns eligible for per-session min/max columns.

    The candidate set is the map writer's own numeric overlay lists (its
    source of truth for what it denormalises) intersected with the columns the
    artifact on disk actually has — a map built before an overlay existed
    simply yields fewer columns. Returns [] when no map artifact exists.
    """
    # Function-level import: video_map pulls in sklearn + the Gemini client,
    # which must not ride along on every session_explorer import (the web
    # process imports this module at boot).
    from fyp.analysis import video_map

    available = data_io.get_parquet_columns(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE)
    if not available:
        return []
    candidates = (["log_plays"] + list(video_map.OVERLAY_NUMERIC)
                  + list(video_map.SCRAPE_OVERLAY_NUMERIC))
    return [c for c in candidates if c in available and c not in TREND_EXCLUDE]




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
        "max_skip": max(int(cfg.get("binge_max_skip", MAX_SKIP)), 0),
        "flick_seconds": max(float(cfg.get("binge_flick_seconds", FLICK_SECONDS)), 0.0),
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




def load_video_features(item_ids: set[str] | None = None,
                        extra_map_cols: list[str] | None = None,
                        include_scrape_text: bool = False) -> pd.DataFrame:
    """Load per-video content features for episode/session characterisation.

    Joins the denormalised map fields (niche, category, annotation scalars,
    story) with scrape-side fields: the author handle (kept out of the
    embeddings, so it is an independent signal for the same-/cross-author
    question) and the video ``duration``. Callers index into it per
    episode/session.

    Args:
        item_ids: Optional item-id subset pushed into both parquet reads, so a
            batch-scoped build holds a batch-sized frame instead of the corpus.
        extra_map_cols: Optional additional ``video_map`` columns to read
            (e.g. :func:`trend_numeric_columns` for the per-session min/max
            index columns); columns absent from the artifact are skipped.
        include_scrape_text: Also read ``desc`` / ``desc_hashtags`` from the
            scrapes frame. Batch-scoped callers only — corpus-wide these text
            columns are hundreds of MB, so the web process's cached
            whole-corpus feature frame must never request them.

    Returns:
        A DataFrame indexed by ``item_id`` with ``niche_name``, ``category``,
        ``story``, ``political_score``, ``sensitivity_score``, ``advertising``,
        ``author`` and ``duration`` (plus any ``extra_map_cols`` /
        scrape-text columns requested).
    """
    id_filter = ([("item_id", "in", [str(i) for i in item_ids])]
                 if item_ids is not None else None)
    map_cols = ["item_id", "niche_name", "category", "story",
                "political_score", "sensitivity_score", "advertising"]
    for col in extra_map_cols or []:
        if col not in map_cols:
            map_cols.append(col)
    mp = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename="video_map.parquet",
        columns=map_cols,
        filters=id_filter,
    )
    if mp is None:
        mp = pd.DataFrame(columns=map_cols)
    feat = mp.copy()
    feat["item_id"] = feat["item_id"].astype("string")
    numeric_cols = ["political_score", "sensitivity_score"] + [
        c for c in (extra_map_cols or []) if c in feat.columns]
    for col in dict.fromkeys(numeric_cols):
        feat[col] = pd.to_numeric(feat[col], errors="coerce")

    # Scrape-side columns, guarded on what the store actually has (the author
    # column is `author_handle` post contract-canonicalisation but
    # `author_uniqueId` in older stores).
    try:
        available = data_io.get_parquet_columns(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.SCRAPES_FILE) or []
    except Exception:
        available = []
    author_col = next((c for c in ("author_handle", "author_uniqueId")
                       if c in available), None)
    scrape_cols = ["item_id"]
    if author_col:
        scrape_cols.append(author_col)
    if "duration" in available:
        scrape_cols.append("duration")
    if include_scrape_text:
        scrape_cols.extend(c for c in ("desc", "desc_hashtags") if c in available)
    scr = None
    if len(scrape_cols) > 1:
        try:
            scr = data_io.load_parquet_selective(
                storage_location=embeddings.STORE_LOCATION,
                filename=embeddings.SCRAPES_FILE,
                columns=scrape_cols,
                filters=id_filter,
            )
        except Exception:
            scr = None
    if scr is None:
        scr = pd.DataFrame({"item_id": pd.Series([], dtype="string")})
    scr = scr.copy()
    if author_col and author_col in scr.columns:
        scr = scr.rename(columns={author_col: "author"})
    if "author" not in scr.columns:
        scr["author"] = pd.Series([None] * len(scr), dtype="string")
    if "duration" in scr.columns:
        scr["duration"] = pd.to_numeric(scr["duration"], errors="coerce")
    else:
        scr["duration"] = pd.Series([None] * len(scr), dtype="float64[pyarrow]")
    scr["item_id"] = scr["item_id"].astype("string")
    scr = scr.drop_duplicates("item_id")
    # A duplicated map row would duplicate the index and break every
    # feat.reindex() caller; keep="last" matches the embedding store's
    # duplicate winner.
    feat = feat.drop_duplicates("item_id", keep="last")
    return feat.merge(scr, on="item_id", how="left").set_index("item_id")




def load_story_texts(item_ids: set[str]) -> dict[str, str]:
    """Per-item AI story summaries for one batch's items, for the search blob.

    ``video_map.parquet``'s ``story`` column is populated only for the 2D
    map's hover-label sample, so the searchable text must come from the
    machine-annotations frame. Batch-scoped callers only (filter pushdown on
    the batch's item ids) — never read corpus-wide.

    Args:
        item_ids: The batch's item ids.

    Returns:
        item_id → story text (missing/empty stories absent).
    """
    if not item_ids:
        return {}
    try:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.ANNOTATIONS_FILE,
            columns=["item_id", "video_story"],
            filters=[("item_id", "in", [str(i) for i in item_ids])],
        )
    except Exception:
        return {}
    if df is None or df.empty or "video_story" not in df.columns:
        return {}
    out: dict[str, str] = {}
    for iid, story in zip(df["item_id"].astype("string"), df["video_story"]):
        if story is None:
            continue
        try:
            if pd.isna(story):
                continue
        except (TypeError, ValueError):
            pass
        text = str(story).strip()
        if text:
            out[str(iid)] = text
    return out




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




# Days added on each side of a study's saved date window when building the
# per-collection coverage intervals: a viewing session straddling a window
# edge would otherwise be truncated at the boundary. The tab still filters to
# the exact study window at read time, so the pad only affects what gets
# segmented, never what a study displays.
COVERAGE_PAD_DAYS = 3

# Same wide fallbacks the study builder applies when a bound is absent or
# unparseable (services/study_data.get_study_date_window) — the window becomes
# a no-op rather than an accidental cut.
_WIDE_START = "1970-01-01"
_WIDE_END = "2099-12-31"




def _study_bound(cfg: dict, key: str, default: str) -> pd.Timestamp:
    """Parse a study's saved date bound, falling back to the wide default."""
    raw = cfg.get(key)
    if isinstance(raw, str) and raw.strip():
        try:
            return pd.Timestamp(raw.strip())
        except ValueError:
            pass
    return pd.Timestamp(default)




def merge_intervals(intervals: list[tuple[pd.Timestamp, pd.Timestamp]],
                    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Merge overlapping/adjacent half-open ``[start, end)`` intervals."""
    merged: list[list[pd.Timestamp]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]




def compute_coverage_spec(study_defs: dict | None = None,
                          pad_days: int = COVERAGE_PAD_DAYS) -> dict[str, list[list[str]]]:
    """Per-collection date windows the sessions build must cover.

    The sessions artifacts only need to span what studies can display: for
    each study, each collection in its ``SELECTED_COLLECTIONS`` contributes
    the study's saved date window (``START_DATE`` inclusive through the end of
    ``END_DATE``, the builder's half-open ``[start, end+1d)`` convention),
    padded by ``pad_days`` on each side so edge-straddling sessions stay
    intact. Overlapping windows from different studies merge into disjoint
    intervals. Collections selected by **no** study are absent from the spec
    — they are not built at all.

    Args:
        study_defs: Study definitions dict (None loads ``studies.json`` from
            the ``recoded`` location directly — no dependency on a
            pre-initialised ``fyp_cf['study_defs']``).
        pad_days: Padding applied to each side of every window.

    Returns:
        ``{collection_id: [["YYYY-MM-DD", "YYYY-MM-DD"], ...]}`` — sorted,
        disjoint, half-open ``[start, end)`` intervals as ISO date strings
        (JSON-stable, so the staleness comparison is exact).
    """
    if study_defs is None:
        if data_io.exists(storage_location="recoded", filename="studies.json"):
            study_defs = data_io.load_json(storage_location="recoded",
                                           filename="studies.json") or {}
        else:
            study_defs = {}

    pad = pd.Timedelta(days=pad_days)
    raw: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for cfg in study_defs.values():
        if not isinstance(cfg, dict):
            continue
        start = _study_bound(cfg, "START_DATE", _WIDE_START) - pad
        # Stored END_DATE means "through the end of that day": +1d exclusive.
        end = _study_bound(cfg, "END_DATE", _WIDE_END) + pd.Timedelta(days=1) + pad
        if end <= start:
            continue
        for cid in cfg.get("SELECTED_COLLECTIONS") or []:
            raw.setdefault(str(cid), []).append((start, end))

    return {
        cid: [[s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")]
              for s, e in merge_intervals(intervals)]
        for cid, intervals in sorted(raw.items())
    }




def coverage_mask(ts: pd.Series, windows: list[list[str]]) -> pd.Series:
    """Boolean mask of timestamps inside any half-open coverage interval."""
    vals = ts.to_numpy(dtype="datetime64[ns]")
    mask = np.zeros(len(vals), dtype=bool)
    for start, end in windows:
        mask |= ((vals >= np.datetime64(pd.Timestamp(start)))
                 & (vals < np.datetime64(pd.Timestamp(end))))
    return pd.Series(mask, index=ts.index)




def discover_covered_collections(coverage: dict[str, list[list[str]]],
                                 collections: list[str] | None = None,
                                 ) -> list[tuple[str, int, int]]:
    """Coverage-scoped discovery with within-window staleness counts.

    The window-scoped counterpart of :func:`discover_collections`: one
    streamed pass over the play rows, restricted to collections present in
    ``coverage``, counting only plays inside each collection's coverage
    intervals — plus how many of those plays are of annotated videos. The two
    counts are exactly what :func:`compute_refresh_plan` compares against the
    per-collection block in ``sessions_meta.json``.

    Args:
        coverage: Per-collection intervals from :func:`compute_coverage_spec`.
        collections: Optional allow-list narrowing the scan further.

    Returns:
        ``[(collection_id, n_plays, n_annotated), ...]`` sorted by
        ``(-n_plays, id)`` (collections with zero in-window plays are
        omitted — there is nothing to segment).
    """
    fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not coverage or not data_io.exists(
            storage_location=embeddings.STORE_LOCATION, filename=fn):
        return []
    allow = set(coverage)
    if collections is not None:
        allow &= {str(c) for c in collections}
    if not allow:
        return []
    available = data_io.get_parquet_columns(
        storage_location=embeddings.STORE_LOCATION, filename=fn) or []
    columns = ["collection_id", "local_timestamp"]
    has_annotated = "annotated_ok" in available
    if has_annotated:
        columns.append("annotated_ok")

    plays: dict[str, int] = {}
    annotated: dict[str, int] = {}
    for rb in data_io.iter_parquet_batches(
            storage_location=embeddings.STORE_LOCATION, filename=fn,
            columns=columns,
            filters=[("activity_type", "==", "play"),
                     ("collection_id", "in", sorted(allow))],
            batch_size=1_048_576):
        df = rb.to_pandas()
        df["_ts"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
        df = df.dropna(subset=["_ts"])
        if has_annotated:
            ann = df["annotated_ok"].fillna(False).astype(bool)
        else:
            ann = pd.Series(False, index=df.index)
        for cid, grp in df.groupby("collection_id", observed=True):
            cid = str(cid)
            mask = coverage_mask(grp["_ts"], coverage[cid])
            n = int(mask.sum())
            if n:
                plays[cid] = plays.get(cid, 0) + n
                annotated[cid] = annotated.get(cid, 0) + int((mask & ann[grp.index]).sum())
    return sorted(((cid, n, annotated.get(cid, 0)) for cid, n in plays.items()),
                  key=lambda t: (-t[1], t[0]))




def collections_meta_block(discovered: list[tuple[str, int, int]],
                           coverage: dict[str, list[list[str]]],
                           built_at: str | None = None) -> dict:
    """Per-collection provenance entries for ``sessions_meta.json``.

    Args:
        discovered: ``(cid, n_plays, n_annotated)`` tuples from
            :func:`discover_covered_collections`.
        coverage: The coverage spec the counts were taken against.
        built_at: ISO timestamp to stamp (None: now).

    Returns:
        ``{cid: {"windows", "n_plays", "n_annotated", "built_at"}}``.
    """
    stamp = built_at or pd.Timestamp.now(tz="UTC").isoformat()
    return {
        cid: {"windows": coverage.get(cid, []), "n_plays": int(n_plays),
              "n_annotated": int(n_annotated), "built_at": stamp}
        for cid, n_plays, n_annotated in discovered
    }




def compute_refresh_plan(discovered: list[tuple[str, int, int]],
                         coverage: dict[str, list[list[str]]],
                         meta: dict | None, params: dict, model: str,
                         trend_cols: list[str], artifacts_exist: bool,
                         plays_schema_ok: bool = True,
                         scope: set[str] | None = None) -> dict:
    """Decide what a sessions refresh must rebuild. Pure — no I/O.

    A collection is **stale** when its current fingerprint — coverage
    windows, in-window play count, in-window annotated count — differs from
    the one recorded in the meta's per-collection block (or it has no
    record). A collection recorded in the meta but absent from ``discovered``
    is **dropped** (it left every study, or its data is gone). Global
    invalidators escalate to a full rebuild because a merge would mix
    incompatible rows: a different embedding model, different segmentation
    params, a different trend-column set (sessions schema drift), a changed
    plays-artifact schema, or missing artifacts/meta (including a meta from
    before the per-collection block existed — the migration path).

    Corpus-mean fingerprint drift is deliberately **not** an invalidator:
    the mean over the full store is statistically stable across appends, so
    refreshed collections centred on the new mean coexist with untouched
    ones on the old. The worker records the drift in the published meta;
    a forced full rebuild realigns everything.

    Args:
        discovered: Current ``(cid, n_plays, n_annotated)`` fingerprint side.
        coverage: Current per-collection windows.
        meta: The existing ``sessions_meta.json`` payload (None when absent).
        params: The run's resolved segmentation params.
        model: The active embedding model id.
        trend_cols: The run's pinned trend-column list.
        artifacts_exist: Whether all published artifact files exist.
        plays_schema_ok: False when the on-disk plays artifact's columns
            differ from the current plays schema (mid-deploy drift).
        scope: Optional allow-list intersecting the refresh set (never the
            drop set — drops are corpus facts, not scoped requests).

    Returns:
        ``{"mode": "full"|"merge"|"noop", "reason": str,
           "refresh": [cid...], "drop": [cid...]}`` — ``refresh`` keeps the
        discovery order (biggest first); in full mode it is every discovered
        collection and ``drop`` is empty (a full publish overwrites).
    """
    all_cids = [cid for cid, _, _ in discovered]

    def _full(reason: str) -> dict:
        return {"mode": "full", "reason": reason, "refresh": all_cids, "drop": []}

    if not artifacts_exist:
        return _full("artifacts missing")
    if not isinstance(meta, dict) or not meta:
        return _full("no meta")
    known = meta.get("collections")
    if not isinstance(known, dict):
        return _full("meta has no per-collection block (pre-upgrade build)")
    if str(meta.get("embedding_model") or "") != str(model):
        return _full(f"embedding model changed "
                     f"({meta.get('embedding_model')} -> {model})")
    if meta.get("params") != params:
        return _full("segmentation params changed")
    if sorted(meta.get("trend_vars") or []) != sorted(trend_cols):
        return _full("trend-column set changed (sessions schema drift)")
    if not plays_schema_ok:
        return _full("plays artifact schema changed (deploy drift)")

    refresh: list[str] = []
    for cid, n_plays, n_annotated in discovered:
        rec = known.get(cid)
        if (not isinstance(rec, dict)
                or rec.get("windows") != coverage.get(cid, [])
                or int(rec.get("n_plays", -1)) != int(n_plays)
                or int(rec.get("n_annotated", -1)) != int(n_annotated)):
            refresh.append(cid)
    if scope is not None:
        refresh = [cid for cid in refresh if cid in scope]
    drop = sorted(set(known) - {cid for cid, _, _ in discovered})

    if not refresh and not drop:
        return {"mode": "noop", "reason": "all collections up to date",
                "refresh": [], "drop": []}
    return {"mode": "merge",
            "reason": f"{len(refresh)} stale, {len(drop)} removed",
            "refresh": refresh, "drop": drop}




def segment_session(seq: list[tuple], U: np.ndarray, cut: float, mem: int,
                    min_videos: int, min_minutes: float,
                    max_skip: int = MAX_SKIP,
                    flick_seconds: float = FLICK_SECONDS) -> list[dict]:
    """Grow focus episodes within one session's embedded plays.

    A run survives up to ``max_skip`` CONSECUTIVE off-theme videos. They are
    tolerated, not absorbed: a skipped video is never a member, never enters
    the centroid, and never extends the span — it is only counted, as
    ``n_skipped``. An ad break in the middle of a binge is the motivating case.

    An off-theme video the viewer merely flicked past (dwell under
    ``flick_seconds``) does not count toward ``max_skip`` at all — a video
    dismissed in under a couple of seconds is feed noise the viewer rejected,
    not a departure from the theme. Only off-theme videos the viewer actually
    watched spend the skip budget. Flicked videos are still tolerated, never
    members, and still count in ``n_skipped`` when the run resumes.
    ``flick_seconds = 0`` disables the rule (every off-theme play counts).

    ``max_skip = 0`` restores the pre-2026-08-10 behaviour, where one off-theme
    video ended the run AND became the first member of the next one. That
    second effect was the damaging one: the theme then had to re-accumulate
    from an anchor that was not the theme, which is why long on-theme stretches
    fragmented into runs too small to keep (99.4% of candidate runs on the
    production corpus ended with fewer than ``min_videos`` videos).

    When the tolerance IS exhausted, the run ends and the scan rewinds to the
    first tolerated video, so the videos that ended one binge are available to
    open the next — they are never silently dropped.

    Args:
        seq: Time-ordered ``(item_id, row_idx, ts, dur)`` tuples for the
            session's embedded plays.
        U: Directional vector store.
        cut: Focus threshold on mean cosine distance to the recent centroid.
        mem: Number of recent members the centroid is taken over.
        min_videos: Minimum distinct videos to keep an episode.
        min_minutes: Minimum span (minutes) to keep an episode.
        max_skip: Consecutive off-theme videos a run tolerates.
        flick_seconds: Dwell (seconds) under which an off-theme video does not
            count toward ``max_skip``. 0 disables.

    Returns:
        A list of episode dicts (raw members + span; geometry/content are
        attributed later by :func:`episode_record`).
    """
    episodes: list[dict] = []
    cur: dict | None = None
    pending: list[int] = []
    pending_counted = 0

    def close(c: dict | None) -> None:
        if c is None or len(c["idx"]) < min_videos:
            return
        if (c["end_ts"] - c["start_ts"]).total_seconds() / 60.0 >= min_minutes:
            episodes.append(c)

    def fresh(iid, ridx, ts, dur) -> dict:
        return {"ids": [iid], "idx": [ridx], "seen": {iid},
                "m_ts": [ts], "m_dur": [dur],
                "start_ts": ts, "end_ts": ts, "n_plays": 1, "n_skipped": 0}

    i = 0
    while i < len(seq):
        iid, ridx, ts, dur = seq[i]
        if cur is None:
            cur = fresh(iid, ridx, ts, dur)
            pending = []
            pending_counted = 0
            i += 1
            continue
        if iid in cur["seen"]:
            # A rewatch extends the span but is not a new member — otherwise a
            # repeat loop collapses the effective rank and fakes a binge.
            cur["n_plays"] += 1
            cur["end_ts"] = ts
            i += 1
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
            # Only now are the tolerated videos INSIDE the binge — a run that
            # ends on an interruption never counts it.
            cur["n_skipped"] += len(pending)
            pending = []
            pending_counted = 0
            i += 1
            continue

        pending.append(i)
        # An unknown dwell cannot prove a flick, so it spends the budget.
        dwell = _num(dur)
        if not (flick_seconds > 0 and dwell is not None and dwell < flick_seconds):
            pending_counted += 1
        if pending_counted <= max_skip:
            i += 1
            continue
        close(cur)
        # Rewind so the tolerated videos get a fair chance to open the next
        # run; the restart point always advances, so this terminates.
        i = pending[0]
        cur = None
        pending = []
        pending_counted = 0
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
        # Off-theme videos the binge survived (see segment_session's max_skip).
        # Reported so a long binge cannot hide how much it tolerated.
        "n_skipped": int(ep.get("n_skipped", 0)),
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




def _search_text(distinct_list: list[str], feat: pd.DataFrame,
                 stories: dict[str, str]) -> str:
    """Build one session's searchable text blob (lowercased, deduped, capped).

    Concatenates the text the detail panel displays — niche names, categories,
    creator handles, video descriptions + hashtags, and the AI story summaries
    — so the overview's free-text search matches exactly what a researcher
    sees when they open the session.
    """
    frags: set[str] = set()
    sub = feat.reindex(distinct_list)
    for col in ("niche_name", "category", "author"):
        if col not in sub.columns:
            continue
        for value in sub[col].dropna().unique():
            text = str(value).strip()
            if text:
                frags.add(text[:_SEARCH_FRAGMENT_CAP])
    for col in ("desc", "desc_hashtags"):
        if col not in sub.columns:
            continue
        for value in sub[col].dropna():
            # desc_hashtags is a LIST column — a cell can be an array of tags.
            if isinstance(value, (list, tuple, np.ndarray)):
                text = " ".join(str(v).strip() for v in value
                                if v is not None and str(v).strip())
            else:
                text = str(value).strip()
            if text:
                frags.add(text[:_SEARCH_FRAGMENT_CAP])
    for iid in distinct_list:
        story = stories.get(iid)
        if story:
            frags.add(story[:_SEARCH_FRAGMENT_CAP])
    return "\n".join(sorted(frags)).lower()[:_SEARCH_TEXT_CAP]




def session_record(cid: str, sess: object, g: pd.DataFrame, id2idx: dict,
                   U: np.ndarray, feat: pd.DataFrame, id_sets: dict,
                   episodes: list[dict], window_n: int = WINDOW_N,
                   max_windows: int = MAX_WINDOWS,
                   trend_cols: list[str] | None = None,
                   stories: dict[str, str] | None = None) -> tuple[dict, list[dict]]:
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
        trend_cols: Numeric feature columns to emit ``vmin_``/``vmax_``
            session-extreme columns for (see :func:`trend_numeric_columns`).
        stories: item_id → story text for the search blob (see
            :func:`load_story_texts`).

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

    # Session-extreme values of the numeric trend variables (over distinct
    # items) + dwell (per-play), so the overview can filter on "session max of
    # <variable>" without touching the map at request time.
    all_feat = feat.reindex(distinct_list)
    extremes: dict[str, float | None] = {}
    for col in trend_cols or []:
        vals = (pd.to_numeric(all_feat[col], errors="coerce")
                if col in all_feat.columns else pd.Series(dtype="float64"))
        extremes[f"vmin_{col}"] = _num(vals.min(), 4)
        extremes[f"vmax_{col}"] = _num(vals.max(), 4)
    extremes["vmin_dwell_s"] = _num(dur.min(), 1)
    extremes["vmax_dwell_s"] = _num(dur.max(), 1)

    return {
        **extremes,
        "search_text": _search_text(distinct_list, feat, stories or {}),
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
                     params: dict | None = None,
                     trend_cols: list[str] | None = None,
                     stories: dict[str, str] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
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
        trend_cols: Numeric feature columns for the session-extreme columns.
        stories: item_id → story text for the search blob.

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
                seq, U, p["cut"], p["mem"], p["min_videos"], p["min_minutes"],
                max_skip=p["max_skip"], flick_seconds=p["flick_seconds"])):
            row = episode_record(ep, cid, s, U, feat, play_ts, mem=p["mem"])
            row["episode_idx"] = ep_idx
            eps.append(row)
        episode_rows.extend(eps)
        srow, wins = session_record(
            cid, s, g, id2idx, U, feat, id_sets, eps,
            window_n=p["window_n"], max_windows=p["max_windows"],
            trend_cols=trend_cols, stories=stories)
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




def sessions_schema(trend_cols: list[str] | None = None) -> dict[str, pa.DataType]:
    """The sessions-index schema for one build's trend-variable set.

    The base columns plus ``search_text`` and a ``vmin_``/``vmax_`` float pair
    per trend variable (and for dwell). All links of one chained run must use
    the same ``trend_cols`` or the shard concat at publish would see
    mismatched schemas — the worker pins the list at link 0.
    """
    extra: dict[str, pa.DataType] = {"search_text": pa.string()}
    for col in list(trend_cols or []) + ["dwell_s"]:
        extra.setdefault(f"vmin_{col}", pa.float64())
        extra.setdefault(f"vmax_{col}", pa.float64())
    return {**_SESSIONS_SCHEMA, **extra}


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
    "n_interleaved": pa.int32(), "n_skipped": pa.int32(),
    "focus": pa.float32(), "diameter": pa.float32(),
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

_PLAYS_SCHEMA: dict[str, pa.DataType] = {
    "collection_id": pa.string(), "session_id": pa.string(),
    "item_id": pa.string(), "ts": pa.timestamp("us"),
    "play_duration": pa.float64(), "source_platform": pa.string(),
    # Per-item display text, baked in at build time so the detail endpoint
    # never has to pushdown-read the corpus annotation/scrape parquets (those
    # files are not clustered by item_id, so such a "pushdown" decodes the
    # whole text column per request). Null on rows whose item has no text.
    "story": pa.string(), "desc": pa.string(), "hashtags": pa.string(),
}

# The plays artifact stores display text capped at the same length the detail
# endpoint ships (api_sessions_routes._STORY_CAP): the artifact is a serving
# cache, not an archive — full text stays in the annotation/scrape parquets.
PLAY_TEXT_CAP = 400
_PLAY_TEXT_COLS = ("story", "desc", "hashtags")




def _capped_text(value) -> str | None:
    """A trimmed, ``PLAY_TEXT_CAP``-capped string, or None for empty cells.

    List cells (``desc_hashtags``) are space-joined before capping.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        value = " ".join(parts)
    else:
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        value = str(value).strip()
    if not value:
        return None
    return value[:PLAY_TEXT_CAP] + "…" if len(value) > PLAY_TEXT_CAP else value




def attach_play_texts(plays: pd.DataFrame, feat: pd.DataFrame,
                      stories: dict[str, str]) -> pd.DataFrame:
    """Attach capped ``story``/``desc``/``hashtags`` columns to a plays frame.

    Per-item text mapped onto the play rows (repeated plays repeat the text —
    parquet compression within the (collection, ts)-sorted row groups absorbs
    that). Arrow-backed strings, so a link-0-sized batch stays in contiguous
    buffers rather than one Python str per cell.

    Args:
        plays: A :func:`load_plays`-shaped frame.
        feat: :func:`load_video_features` result WITH scrape text
            (``include_scrape_text=True``), item_id-indexed.
        stories: :func:`load_story_texts` result (item_id → story).

    Returns:
        ``plays`` with the three text columns added (all-null when the
        sources are empty).
    """
    if plays is None or not len(plays):
        return plays
    item_ids = [str(i) for i in plays["item_id"].drop_duplicates()]
    text = {"story": [_capped_text(stories.get(iid) if stories else None)
                      for iid in item_ids],
            "desc": [None] * len(item_ids),
            "hashtags": [None] * len(item_ids)}
    if feat is not None and len(feat):
        sub = feat.reindex(item_ids)
        for col, src in (("desc", "desc"), ("hashtags", "desc_hashtags")):
            if src in sub.columns:
                text[col] = [_capped_text(v) for v in sub[src]]
    text_df = pd.DataFrame({"item_id": pd.array(item_ids, dtype="string[pyarrow]")})
    for col in _PLAY_TEXT_COLS:
        text_df[col] = pd.array(text[col], dtype="string[pyarrow]")
    plays = plays.copy()
    plays["item_id"] = plays["item_id"].astype("string[pyarrow]")
    return plays.merge(text_df, on="item_id", how="left")




def plays_table(plays: pd.DataFrame) -> pa.Table:
    """One batch's play rows as an arrow Table in the plays-artifact schema.

    Rows are sorted by (collection_id, ts) so the published file's row-group
    ``collection_id`` stats stay tight (each chunk owns a disjoint collection
    set, so per-shard sorting yields a globally clustered artifact).

    Args:
        plays: A :func:`load_plays`-shaped frame (``_ts`` parsed, play rows
            only), optionally carrying the :func:`attach_play_texts` text
            columns (absent ones publish as all-null). An empty frame yields
            an empty, schema-correct table.
    """
    if plays is None or not len(plays):
        return pa.table({col: pa.array([], type=typ)
                         for col, typ in _PLAYS_SCHEMA.items()})
    df = plays.sort_values(["collection_id", "_ts"])
    data = {
        "collection_id": pa.array(df["collection_id"].astype("string"), type=pa.string()),
        "session_id": pa.array(df["session_id"].astype("string"), type=pa.string()),
        "item_id": pa.array(df["item_id"].astype("string"), type=pa.string()),
        "ts": pa.array(df["_ts"]).cast(pa.timestamp("us")),
        "play_duration": pa.array(
            pd.to_numeric(df["play_duration"], errors="coerce"), type=pa.float64()),
        "source_platform": pa.array(df["source_platform"].astype("string"), type=pa.string()),
    }
    for col in _PLAY_TEXT_COLS:
        data[col] = (pa.array(df[col].astype("string"), type=pa.string())
                     if col in df.columns
                     else pa.nulls(len(df), type=pa.string()))
    return pa.table(data)




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
                max_vectors: int = MAX_VECTORS_PER_LINK,
                trend_cols: list[str] | None = None,
                coverage: dict[str, list[list[str]]] | None = None):
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
        trend_cols: Numeric feature columns for the session-extreme
            ``vmin_``/``vmax_`` columns (None resolves the live list — a
            chained worker must pass the list pinned at link 0 instead).
        coverage: Optional per-collection date-window spec (see
            :func:`compute_coverage_spec`). When given, each collection's
            plays are restricted to its intervals before segmentation — a
            collection absent from the spec contributes nothing.

    Returns:
        ``(session_rows, episode_rows, window_rows, plays, stats)`` — ``plays``
        is the batch's loaded play frame (the plays-artifact shard source, so
        the worker never re-reads it); all None when cancelled mid-batch.
    """
    p = {**default_params(), **(params or {})}
    plays = load_plays(cids)
    if coverage is not None and not plays.empty:
        keep = pd.Series(False, index=plays.index)
        for cid in plays["collection_id"].drop_duplicates():
            windows = coverage.get(str(cid))
            if not windows:
                continue
            sel = (plays["collection_id"] == cid).to_numpy(dtype=bool)
            keep[sel] = coverage_mask(plays.loc[sel, "_ts"], windows).to_numpy()
        plays = plays[keep]
    stats = {"n_plays": int(len(plays)), "n_vectors": 0, "tier": 1}
    if plays.empty:
        return [], [], [], plays, stats

    if trend_cols is None:
        trend_cols = trend_numeric_columns()
    batch_ids = [str(i) for i in plays["item_id"].drop_duplicates()]
    feat = load_video_features(item_ids=set(batch_ids), extra_map_cols=trend_cols,
                               include_scrape_text=True)
    stories = load_story_texts(set(batch_ids))
    id_sets = enrichment_id_sets(model, item_ids=set(batch_ids),
                                 include_embedded=False)
    # Bake the per-item display text into the plays frame here, so both the
    # chained worker and the in-process driver publish it with no extra reads.
    plays = attach_play_texts(plays, feat, stories)

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
                return None, None, None, None, None
            srows, erows, wrows = build_collection(
                cid, plays[plays["collection_id"] == cid], id2local, U,
                feat, id_sets, p, trend_cols=trend_cols, stories=stories)
            session_rows.extend(srows)
            episode_rows.extend(erows)
            window_rows.extend(wrows)
    else:
        # Tier 2: the union exceeds the budget — load and free per collection.
        stats["tier"] = 2
        for cid in cids:
            if reporter is not None and reporter.check_cancelled():
                return None, None, None, None, None
            cplays = plays[plays["collection_id"] == cid]
            c_ids = [str(i) for i in cplays["item_id"].drop_duplicates()]
            id2local, U = load_directional_block(model, c_ids, corpus_mean, index)
            id_sets["embedded"] = set(id2local)
            srows, erows, wrows = build_collection(
                cid, cplays, id2local, U, feat, id_sets, p,
                trend_cols=trend_cols, stories=stories)
            session_rows.extend(srows)
            episode_rows.extend(erows)
            window_rows.extend(wrows)
            del U, id2local
    return session_rows, episode_rows, window_rows, plays, stats




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
                       episode_rows: list[dict], window_rows: list[dict],
                       trend_cols: list[str] | None = None,
                       plays: pd.DataFrame | None = None) -> None:
    """Persist one link's rows as its four deterministic shards.

    ``trend_cols`` must be the same list :func:`build_batch` produced the rows
    with (the worker pins it at link 0), so every shard of a run shares one
    sessions schema. ``plays`` is the batch's play frame from
    :func:`build_batch` (None writes an empty, schema-correct plays shard).
    """
    for kind, schema, rows in (("sessions", sessions_schema(trend_cols), session_rows),
                               ("episodes", _EPISODES_SCHEMA, episode_rows),
                               ("windows", _WINDOWS_SCHEMA, window_rows)):
        tbl = _arrow_table(rows, schema)
        data_io.write_parquet_stream(
            storage_location=ARTIFACT_LOCATION,
            filename=shard_filename(kind, run_id, chunk),
            batches=[tbl], schema=tbl.schema)
    ptbl = plays_table(plays)
    data_io.write_parquet_stream(
        storage_location=ARTIFACT_LOCATION,
        filename=shard_filename("plays", run_id, chunk),
        batches=[ptbl], schema=ptbl.schema)




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

    for kind, final in (("plays", PLAYS_FILE), ("episodes", EPISODES_FILE),
                        ("windows", WINDOWS_FILE), ("sessions", SESSIONS_FILE)):
        all_shards = [shard_filename(kind, run_id, k) for k in range(n_chunks)]
        shards = [s for s in all_shards
                  if data_io.exists(storage_location=ARTIFACT_LOCATION, filename=s)]
        if not shards:
            if kind == "plays":
                # A run started before the plays shard existed (mid-run
                # deploy): publish the three original artifacts; the read
                # side falls back to the consolidated activity file.
                if reporter is not None:
                    reporter.log("No 'plays' shards for this run — skipping "
                                 "the plays artifact (pre-upgrade run).")
                continue
            raise RuntimeError(f"publish: no '{kind}' shards found for run {run_id}")
        if len(shards) != len(all_shards):
            if kind == "plays":
                # Mid-run deploy: early links predate the plays shard. The
                # other three kinds are complete, so publish them and let the
                # read side fall back for plays.
                if reporter is not None:
                    reporter.log(f"Incomplete 'plays' shard set "
                                 f"({len(shards)}/{n_chunks}) — skipping the "
                                 f"plays artifact (pre-upgrade links).")
                continue
            raise RuntimeError(
                f"publish: run {run_id} has an incomplete '{kind}' shard set "
                f"({len(all_shards) - len(shards)} of {n_chunks} missing) — "
                f"refusing to publish. Another chain sharing this run_id most "
                f"likely published first.")
        if kind == "plays":
            # A schema-widening deploy mid-run leaves early shards without the
            # newer columns; concat binds every shard to the first shard's
            # schema, so a mixed set cannot publish. Same degradation as an
            # absent set: skip plays, read side falls back.
            col_sets = set()
            for s in shards:
                cols = data_io.get_parquet_columns(
                    storage_location=ARTIFACT_LOCATION, filename=s)
                col_sets.add(tuple(sorted(cols or [])))
            if len(col_sets) > 1:
                if reporter is not None:
                    reporter.log("Mixed 'plays' shard schemas (mid-run deploy) "
                                 "— skipping the plays artifact.")
                continue
        n = data_io.concat_parquet_files(
            src_storage_location=ARTIFACT_LOCATION, src_filenames=shards,
            dst_storage_location=ARTIFACT_LOCATION, dst_filename=final,
            # Small row groups keep the plays file's collection_id stats
            # tight, so the detail endpoint's pushdown prunes.
            batch_size=PLAYS_ROW_GROUP if kind == "plays" else 131_072)
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




def _target_schema(kind: str, trend_cols: list[str]) -> pa.Schema:
    """The published arrow schema for one artifact kind."""
    if kind == "plays":
        return plays_table(None).schema
    dict_schema = {"sessions": sessions_schema(trend_cols),
                   "episodes": _EPISODES_SCHEMA,
                   "windows": _WINDOWS_SCHEMA}[kind]
    return _arrow_table([], dict_schema).schema




def _align_batch(rb: pa.RecordBatch, schema: pa.Schema) -> pa.RecordBatch:
    """Reorder/cast a batch to ``schema`` (no-op when it already matches)."""
    if rb.schema.equals(schema):
        return rb
    return rb.select(schema.names).cast(schema)




def merge_publish_artifacts(run_id: str, n_chunks: int, refresh_cids: list[str],
                            drop_cids: list[str], expected: dict, meta: dict,
                            trend_cols: list[str], reporter=None,
                            covered_collections: int | None = None) -> dict:
    """Fold the run's shards into the existing artifacts, replacing rows.

    The incremental counterpart of :func:`publish_artifacts`: instead of the
    shards *becoming* the artifacts, each artifact is rewritten as (its
    existing rows minus every refreshed/dropped collection) + the run's shard
    rows. Streaming end to end — peak memory is one record batch. The write
    itself stages to a tempfile and lands in one move
    (:func:`fyp.data_io.write_parquet_stream`), and the publish order keeps
    ``sessions_index.parquet`` last, so the read side's freshness gate holds.

    Guard differences from the full publish: coverage/row-count totals are
    the **targeted set's**, not the corpus's; every kind's shard set must be
    complete (there is no plays grace-skip — a merge that skipped plays would
    strand stale rows for the refreshed collections, and setup escalates
    schema drift to a full rebuild before a merge run ever starts); and the
    new-row counts are verified from the shard footers **before** any
    artifact is touched.

    Args:
        run_id: The run whose shards to fold in.
        n_chunks: Number of links (shards per kind).
        refresh_cids: Collections this run re-segmented (their old rows go).
        drop_cids: Collections to remove without replacement (left every
            study, or vanished from the data).
        expected: ``{"sessions": n, ...}`` NEW-row totals from the run.
        meta: The ``sessions_meta.json`` payload; its ``n_*`` counts are
            overwritten with the merged totals here.
        trend_cols: The run's pinned trend columns (sessions schema).
        reporter: Optional status reporter.
        covered_collections: Collections actually segmented by this run —
            compared against ``len(refresh_cids)``.

    Returns:
        ``meta`` (persisted, with merged counts).

    Raises:
        RuntimeError: incomplete run, count mismatch, or schema mismatch.
            Nothing is published in that case; the artifacts stay intact.
    """
    total = len(refresh_cids)
    if covered_collections is not None and int(covered_collections) != total:
        raise RuntimeError(
            f"merge publish: run {run_id} covered {covered_collections} of "
            f"{total} targeted collections — refusing to publish a partial "
            f"merge. Shards kept for inspection.")

    remove_ids = pa.array(sorted({str(c) for c in refresh_cids}
                                 | {str(c) for c in drop_cids}),
                          type=pa.string())
    kinds = (("plays", PLAYS_FILE), ("episodes", EPISODES_FILE),
             ("windows", WINDOWS_FILE), ("sessions", SESSIONS_FILE))

    # Validate every kind BEFORE touching any artifact: complete shard set,
    # new-row totals (from footers — no data read), old-artifact schema.
    shard_sets: dict[str, list[str]] = {}
    for kind, final in kinds:
        shards = [shard_filename(kind, run_id, k) for k in range(n_chunks)]
        missing = [s for s in shards if not data_io.exists(
            storage_location=ARTIFACT_LOCATION, filename=s)]
        if missing:
            raise RuntimeError(
                f"merge publish: run {run_id} has an incomplete '{kind}' "
                f"shard set ({len(missing)} of {n_chunks} missing) — "
                f"refusing to publish.")
        new_rows = sum(data_io.get_parquet_num_rows(
            storage_location=ARTIFACT_LOCATION, filename=s) or 0 for s in shards)
        if kind in expected and new_rows != int(expected[kind]):
            raise RuntimeError(
                f"merge publish: '{kind}' shard rows {new_rows} != expected "
                f"{expected[kind]} — artifacts untouched, shards kept.")
        schema = _target_schema(kind, trend_cols)
        old_cols = data_io.get_parquet_columns(
            storage_location=ARTIFACT_LOCATION, filename=final)
        if old_cols is not None and sorted(old_cols) != sorted(schema.names):
            raise RuntimeError(
                f"merge publish: existing {final} columns differ from the "
                f"current schema (mid-run deploy?) — setup should have "
                f"escalated to a full rebuild. Shards kept.")
        shard_sets[kind] = shards

    merged_counts: dict[str, int] = {}
    for kind, final in kinds:
        schema = _target_schema(kind, trend_cols)
        old_exists = data_io.exists(storage_location=ARTIFACT_LOCATION,
                                    filename=final)
        counts = {"old_kept": 0, "new": 0}

        def _batches(kind=kind, final=final, schema=schema,
                     old_exists=old_exists, counts=counts):
            if old_exists:
                idx = schema.names.index("collection_id")
                for rb in data_io.iter_parquet_batches(
                        storage_location=ARTIFACT_LOCATION, filename=final,
                        batch_size=PLAYS_ROW_GROUP if kind == "plays" else 131_072):
                    rb = _align_batch(rb, schema)
                    mask = pa_compute.invert(
                        pa_compute.is_in(rb.column(idx), value_set=remove_ids))
                    kept = rb.filter(pa_compute.fill_null(mask, True))
                    if kept.num_rows:
                        counts["old_kept"] += kept.num_rows
                        yield kept
            for s in shard_sets[kind]:
                for rb in data_io.iter_parquet_batches(
                        storage_location=ARTIFACT_LOCATION, filename=s,
                        batch_size=PLAYS_ROW_GROUP if kind == "plays" else 131_072):
                    counts["new"] += rb.num_rows
                    yield _align_batch(rb, schema)

        n = data_io.write_parquet_stream(
            storage_location=ARTIFACT_LOCATION, filename=final,
            batches=_batches(), schema=schema)
        if n != counts["old_kept"] + counts["new"]:
            raise RuntimeError(
                f"merge publish: '{kind}' wrote {n} rows != kept "
                f"{counts['old_kept']} + new {counts['new']}")
        merged_counts[kind] = n
        if reporter is not None:
            reporter.log(
                f"Merged {final}: kept {counts['old_kept']:,} rows, "
                f"replaced/added {counts['new']:,} "
                f"({len(refresh_cids)} refreshed, {len(drop_cids)} dropped)"
                + ("" if old_exists else " [no previous artifact]"))

    meta["n_sessions"] = merged_counts["sessions"]
    meta["n_episodes"] = merged_counts["episodes"]
    meta["n_windows"] = merged_counts["windows"]
    meta["n_plays"] = merged_counts["plays"]
    meta["n_collections"] = len(meta.get("collections") or {})
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
                    max_vectors: int = MAX_VECTORS_PER_LINK,
                    coverage: dict[str, list[list[str]]] | None = None) -> dict:
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
        coverage: Optional per-collection date-window spec — discovery and
            segmentation restrict to it, and the meta gains the
            per-collection provenance block (see
            :func:`compute_coverage_spec`).

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

    if coverage is not None:
        discovered = discover_covered_collections(coverage, collections)
        cids = [c for c, _, _ in discovered]
    else:
        discovered = discover_collections(collections)
        cids = [c for c, _ in discovered]
    _log(f"  {len(cids)} collections to segment")
    trend_cols = trend_numeric_columns()
    _log(f"  session min/max columns for {len(trend_cols)} trend variable(s)")

    all_sessions: list[dict] = []
    all_episodes: list[dict] = []
    all_windows: list[dict] = []
    # Per-batch arrow tables (compact) — streamed into the plays artifact at
    # the end, one row group per batch, so collection_id stats stay tight.
    play_tables: list[pa.Table] = []
    n_plays = 0
    for start in range(0, len(cids), batch_size):
        batch = cids[start:start + batch_size]
        srows, erows, wrows, plays, stats = build_batch(
            batch, model, corpus_mean, index, params=p, reporter=reporter,
            max_vectors=max_vectors, trend_cols=trend_cols, coverage=coverage)
        if srows is None:
            _log("Cancelled by user.")
            return {"cancelled": True}
        all_sessions.extend(srows)
        all_episodes.extend(erows)
        all_windows.extend(wrows)
        play_tables.append(plays_table(plays))
        n_plays += int(len(plays))
        done = min(start + batch_size, len(cids))
        if reporter is not None:
            reporter.update_progress(
                int(done / max(len(cids), 1) * 95),
                f"Segmented {done}/{len(cids)} collections "
                f"({len(all_sessions):,} sessions, {len(all_episodes):,} episodes, "
                f"{len(all_windows):,} windows)")

    _log(f"Writing artifacts: {len(all_sessions):,} sessions, "
         f"{len(all_episodes):,} episodes, {len(all_windows):,} low-entropy windows")
    empty_plays = plays_table(None)
    data_io.write_parquet_stream(
        storage_location=ARTIFACT_LOCATION, filename=PLAYS_FILE,
        batches=play_tables or [empty_plays], schema=empty_plays.schema,
    )
    data_io.save_parquet(
        df=_arrow_frame(all_sessions, sessions_schema(trend_cols)),
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
        "trend_vars": trend_cols,
        "n_collections": len(cids),
        "n_sessions": len(all_sessions),
        "n_episodes": len(all_episodes),
        "n_windows": len(all_windows),
        "n_plays": n_plays,
    }
    if coverage is not None:
        meta["collections"] = collections_meta_block(
            discovered, coverage, built_at=meta["built_at"])
    data_io.save_json(data=meta, storage_location=ARTIFACT_LOCATION, filename=META_FILE)
    return meta
