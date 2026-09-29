"""The Sessions build's shared inputs: artifact names, parameters and loaders.

Artifact filenames and locations, the segmentation parameters, and the
readers the build consumes — plays, the model-scoped directional embedding
store and corpus mean, per-video features and story texts, enrichment id
sets — plus the study coverage spec that decides which collections and date
windows are covered. Every other ``fyp.analysis.sessions`` module builds on
this one.
"""

import numpy as np
import pandas as pd
import pyarrow.compute as pa_compute

import fyp.core.data_io as data_io
from fyp.analysis import embedding_store, embeddings, entropy_metrics
from fyp.analysis.organize_datasets import COLLECTIONS_LABEL
from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)

# Segmentation parameters. CUT/MEM/MIN_VIDEOS come from the embedding-entropy
# study (specification-curve validated; `mem` controls drift tolerance).
#
# MIN_MINUTES and MAX_SKIP are tuned against the production corpus and are
# NOT the study's values (the study used 3.0 minutes and no
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
# budget (0 restores the pure-count rule). Validated on the production
# corpus's AIO-00060 session: the count-only rule severed a 4-video
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
SHARD_PREFIXES = {
    "sessions": "sessions_shard__",
    "episodes": "episodes_shard__",
    "windows": "windows_shard__",
    "plays": "plays_shard__",
}
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
# trend scan, which mirrors this set in web_interface/services/sessions_data.py).
TREND_EXCLUDE = {"item_id", "niche", "x", "y"}

# Per-fragment / per-session caps on the searchable text blob. Load-bearing:
# without them a long session ships every full caption and the index (cached
# whole in the web process) grows by hundreds of MB corpus-wide.
_SEARCH_FRAGMENT_CAP = 200
_SEARCH_TEXT_CAP = 8_000

# Parallel segmentation. The per-session work (segment_session /
# episode_record / session_record) is pure Python and was measured at ~38 of
# a 40-minute full rebuild on one core of an 8-CPU runner. It is
# embarrassingly parallel per session, so a batch is cut into
# (collection, session-chunk) work units of roughly SESSION_CHUNK_PLAYS plays
# — never splitting a session — and run on a forked process pool. Chunks
# rather than whole collections, because the largest collection alone (279k
# plays, 15% of the corpus) would otherwise floor the link at ~6 minutes.
# Units are concatenated in unit order, so the rows come out byte-identical
# to the serial loop whatever the worker count.
SESSION_CHUNK_PLAYS = 4_000
# How many completed units between cancellation checks in pool mode (each
# check is a storage stat).
_CANCEL_CHECK_EVERY = 8


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
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE
    )
    if not available:
        return []
    candidates = (
        ["log_plays"] + list(video_map.OVERLAY_NUMERIC) + list(video_map.SCRAPE_OVERLAY_NUMERIC)
    )
    return [c for c in candidates if c in available and c not in TREND_EXCLUDE]


def default_params() -> dict:
    """Return the default segmentation/window parameters.

    Values come from the ``[sessions]`` config section, falling back to the
    module constants (the study-locked values) for keys the config omits.
    Per-run ``task_args`` overrides still take precedence over both.
    """
    from fyp.core.fyp_config import fyp_cf

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
    np.divide(
        mat, np.where(norms < entropy_metrics.EPS_NORM, entropy_metrics.EPS_NORM, norms), out=mat
    )
    return mat


def vector_cache_enabled() -> bool:
    """``[sessions] vector_cache`` — whole-part caching for the batch build.

    Off by config only; the read-side callers (the tab's context-vector
    lookups on the web service) never use the cache — they fetch a handful
    of rows and must not pull 1.9 GB into the web instance's memory.
    """
    from fyp.core.fyp_config import fyp_cf

    cfg = fyp_cf.get("sessions", {})
    value = cfg.get("vector_cache", True) if isinstance(cfg, dict) else True
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def load_directional_block(
    model: str, item_ids: list, corpus_mean: np.ndarray, index=None, local_cache: bool = False
) -> tuple[dict, np.ndarray]:
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
        local_cache: Serve the dense parts from the per-machine whole-part
            cache (batch builds only — see
            :func:`embedding_store.read_vectors`).

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
    U = embedding_store.read_vectors(model, rows, index, dtype=np.float32, local_cache=local_cache)
    _directionalise(U, corpus_mean)
    found_ids = [str(i) for i, f in zip(item_ids, found) if f]
    return {iid: i for i, iid in enumerate(found_ids)}, U


def load_video_features(
    item_ids: set[str] | None = None,
    extra_map_cols: list[str] | None = None,
    include_scrape_text: bool = False,
) -> pd.DataFrame:
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
    id_filter = [("item_id", "in", [str(i) for i in item_ids])] if item_ids is not None else None
    map_cols = [
        "item_id",
        "niche_name",
        "category",
        "story",
        "political_score",
        "sensitivity_score",
        "advertising",
    ]
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
        c for c in (extra_map_cols or []) if c in feat.columns
    ]
    for col in dict.fromkeys(numeric_cols):
        feat[col] = pd.to_numeric(feat[col], errors="coerce")

    # Scrape-side columns, guarded on what the store actually has (the author
    # column is `author_handle` post contract-canonicalisation but
    # `author_uniqueId` in older stores).
    try:
        available = (
            data_io.get_parquet_columns(
                storage_location=embeddings.STORE_LOCATION, filename=embeddings.SCRAPES_FILE
            )
            or []
        )
    except Exception:
        available = []
    author_col = next((c for c in ("author_handle", "author_uniqueId") if c in available), None)
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


def enrichment_id_sets(
    model: str, item_ids: set[str] | None = None, include_embedded: bool = True
) -> dict[str, set]:
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
    id_filter = [("item_id", "in", [str(i) for i in item_ids])] if item_ids is not None else None
    scraped: set[str] = set()
    downloaded: set[str] = set()
    if data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=embeddings.SCRAPES_FILE):
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
    embedded = embeddings.embedded_item_ids(model=model) if include_embedded else set()
    return {
        "scraped": scraped,
        "downloaded": downloaded,
        "annotated": annotated,
        "embedded": embedded,
    }


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
        columns=[
            "collection_id",
            "item_id",
            "local_timestamp",
            "play_duration",
            "session_id",
            "source_platform",
        ],
        filters=filters,
    )
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "collection_id",
                "item_id",
                "_ts",
                "play_duration",
                "session_id",
                "source_platform",
            ]
        )
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
        storage_location=embeddings.STORE_LOCATION,
        filename=fn,
        columns=["collection_id"],
        filters=[("activity_type", "==", "play")],
        batch_size=1_048_576,
    ):
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


def merge_intervals(
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Merge overlapping/adjacent half-open ``[start, end)`` intervals."""
    merged: list[list[pd.Timestamp]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def compute_coverage_spec(
    study_defs: dict | None = None, pad_days: int = COVERAGE_PAD_DAYS
) -> dict[str, list[list[str]]]:
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
            study_defs = (
                data_io.load_json(storage_location="recoded", filename="studies.json") or {}
            )
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
        cid: [
            [s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")] for s, e in merge_intervals(intervals)
        ]
        for cid, intervals in sorted(raw.items())
    }


def coverage_mask(ts: pd.Series, windows: list[list[str]]) -> pd.Series:
    """Boolean mask of timestamps inside any half-open coverage interval."""
    vals = ts.to_numpy(dtype="datetime64[ns]")
    mask = np.zeros(len(vals), dtype=bool)
    for start, end in windows:
        mask |= (vals >= np.datetime64(pd.Timestamp(start))) & (
            vals < np.datetime64(pd.Timestamp(end))
        )
    return pd.Series(mask, index=ts.index)


def discover_covered_collections(
    coverage: dict[str, list[list[str]]],
    collections: list[str] | None = None,
) -> list[tuple[str, int]]:
    """Coverage-scoped discovery with the within-window play count.

    The window-scoped counterpart of :func:`discover_collections`: one
    streamed pass over the play rows, restricted to collections present in
    ``coverage``, counting only plays inside each collection's coverage
    intervals. That count is what :func:`compute_refresh_plan` compares
    against the per-collection block in ``sessions_meta.json``.

    This tracks the **activity** side only, because the activity file is all
    it can see: ``collections_recoded.parquet`` is written by ingest and
    carries no enrichment columns. Counting plays of annotated videos here
    (probing for an ``annotated_ok`` column that file does not have) would
    silently yield 0 on every install, so new annotations could never mark
    anything stale. Enrichment staleness is a pair of global fingerprints
    instead
    (:func:`annotation_corpus_fingerprint`,
    :func:`embedding_store.store_fingerprint`); do not reintroduce a
    per-collection enrichment count here without joining a file that
    actually holds one.

    Args:
        coverage: Per-collection intervals from :func:`compute_coverage_spec`.
        collections: Optional allow-list narrowing the scan further.

    Returns:
        ``[(collection_id, n_plays), ...]`` sorted by ``(-n_plays, id)``
        (collections with zero in-window plays are omitted — there is
        nothing to segment).
    """
    fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not coverage or not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=fn):
        return []
    allow = set(coverage)
    if collections is not None:
        allow &= {str(c) for c in collections}
    if not allow:
        return []

    plays: dict[str, int] = {}
    for rb in data_io.iter_parquet_batches(
        storage_location=embeddings.STORE_LOCATION,
        filename=fn,
        columns=["collection_id", "local_timestamp"],
        filters=[("activity_type", "==", "play"), ("collection_id", "in", sorted(allow))],
        batch_size=1_048_576,
    ):
        df = rb.to_pandas()
        df["_ts"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
        df = df.dropna(subset=["_ts"])
        for cid, grp in df.groupby("collection_id", observed=True):
            cid = str(cid)
            n = int(coverage_mask(grp["_ts"], coverage[cid]).sum())
            if n:
                plays[cid] = plays.get(cid, 0) + n
    return sorted(plays.items(), key=lambda t: (-t[1], t[0]))


def collections_meta_block(
    discovered: list[tuple[str, int]],
    coverage: dict[str, list[list[str]]],
    built_at: str | None = None,
) -> dict:
    """Per-collection provenance entries for ``sessions_meta.json``.

    Args:
        discovered: ``(cid, n_plays)`` tuples from
            :func:`discover_covered_collections`.
        coverage: The coverage spec the counts were taken against.
        built_at: ISO timestamp to stamp (None: now).

    Returns:
        ``{cid: {"windows", "n_plays", "built_at"}}``.
    """
    stamp = built_at or pd.Timestamp.now(tz="UTC").isoformat()
    return {
        cid: {"windows": coverage.get(cid, []), "n_plays": int(n_plays), "built_at": stamp}
        for cid, n_plays in discovered
    }


def annotation_corpus_fingerprint() -> str:
    """Fingerprint of the consolidated annotation corpus (``size:mtime``).

    The sessions build reads the annotation corpus three ways — the
    ``annotated`` id set (per-session coverage counters), the story texts
    baked into the plays artifact, and the annotation-derived trend columns
    behind the session extremes — so a rewritten corpus invalidates the
    artifacts even when the embedding store did not move (annotations landing
    while the embedding backend is local-only is the motivating case; the
    consolidate pipeline skips embeddings there).

    Size-and-mtime rather than a row count, matching
    :func:`embedding_store.store_fingerprint`: a re-annotation that replaces
    rows without adding any must still register. It errs toward triggering a
    rebuild — the cheap direction.

    Returns:
        ``"{size}:{mtime}"``, or ``""`` when no annotation corpus exists
        (a fresh install — nothing to invalidate against).
    """
    st = data_io.stat(
        storage_location=embeddings.STORE_LOCATION, filename=embeddings.ANNOTATIONS_FILE
    )
    if st is None:
        return ""
    return f"{int(st.get('size', 0))}:{float(st.get('mtime', 0.0))}"
