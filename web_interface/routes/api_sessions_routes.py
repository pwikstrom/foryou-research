"""Sessions tab API: session-quality overview + focused-episode detail.

Serves the artifacts built by the ``sessions_refresh`` worker
(:mod:`fyp.analysis.session_explorer`): a filterable per-session quality/focus
index, a per-session detail payload (the full play sequence + detected focus
episodes with their ordered members), and a lightweight freshness/status
signal. The artifacts are global (all collections, full history); every request
is scoped to the caller's study on BOTH axes — a session is only visible when
its collection is one the requested, accessible study actually contains (see
:func:`_study_collection_ids`: selected AND present in the study's built frame)
AND it started inside the study's date window (see :func:`_in_study_window`).
Neither axis implies the other: the collection set alone would show a ten-day
study every session those donors ever recorded.

All entropy/focus numbers were precomputed into the artifacts, and per-item
flags come from cheap id-set membership checks. The one deliberate exception
is the detail payload's context-play distances: a handful of vectors are
fetched from the dense sidecar per request (ranged reads, never a shard scan)
to explain why the plays just outside a binge/sequence were not part of it.
"""

import itertools
import threading
import time
from functools import lru_cache

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request

import fyp.data_io as data_io
import fyp.embeddings as embeddings
from fyp.analysis import embedding_store, session_explorer
from fyp.fyp_config import fyp_cf
from web_interface.data_service import (
    get_study_collections,
    get_study_date_window,
    get_study_frame_collections,
    load_display_id_map,
)

from ._access import study_access_error
from ..permissions import permission_required
from ..task_status import is_cloud_run

sessions_bp = Blueprint('sessions_bp', __name__)

# Default for the ad-hoc ``min_emb_plays`` quality filter (query-time only —
# the artifact itself is unfiltered). From the embedding-entropy study's donor
# floors, adapted to the single-session grain. The coverage floor that used to
# sit beside it is now an admin setting; see _session_floors.
DEFAULT_MIN_EMB_PLAYS = 5
OVERVIEW_LIMIT_DEFAULT = 200
OVERVIEW_LIMIT_MAX = 1000

# ``[sessions] context_plays``: how many plays either side of a binge / sequence
# the player offers as (clearly marked) context. Unlike the segmentation
# parameters this one is not baked into the artifact — it is read live and sent
# to the client with every overview.
DEFAULT_CONTEXT_PLAYS = 3

# ``[sessions] drift_p`` / ``trend_min_videos`` fallbacks — both are read-side
# thresholds applied to numbers already in the artifact, so changing them
# re-labels immediately and needs no rebuild.
DEFAULT_DRIFT_P = 0.05
DEFAULT_TREND_MIN_VIDEOS = 7

# The session-list floors (plays / minutes / embedded-coverage) are owned by
# the admin settings store, which resolves admin setting > [sessions] config >
# its own fallbacks — so an admin can retune them from Admin → Site Settings
# with no rebuild. See admin_settings.get_session_floors.

# Columns the overview endpoint returns per session row.
_OVERVIEW_COLS = [
    "collection_id", "session_id", "start_ts", "end_ts", "duration_min",
    "n_plays", "n_distinct", "total_watch_s", "median_dwell_s",
    "n_embedded", "coverage_scraped", "coverage_annotated", "coverage_embedded",
    "emb_play_coverage", "min_window_cosdist", "min_window_entropy_norm",
    "n_episodes", "episode_play_frac", "dominant_niche", "n_niches",
]

# Columns the overview's ad-hoc range filters (the collapsible filter panel)
# act on, keyed by their query-param stem: ``<stem>_min`` / ``<stem>_max``.
# The date filter (``f_start_min``/``f_start_max``) is handled separately —
# it compares parsed timestamps, not numerics.
_RANGE_FILTER_COLS = {
    "f_length": "duration_min",
    "f_plays": "n_plays",
    "f_coverage": "coverage_embedded",
    "f_entropy": "min_window_cosdist",
    "f_binges": "n_episodes",
}

# Sort keys the overview accepts (anything else falls back to the focus rank).
_SORT_KEYS = {
    "min_window_cosdist", "min_window_entropy_norm", "duration_min", "n_plays",
    "n_distinct", "n_episodes", "episode_play_frac", "coverage_embedded",
    "start_ts", "total_watch_s", "n_directed_episodes", "collection_id",
}

# Prefix of the per-variable session-extreme columns baked into the index
# (``vmax_<variable>`` / ``vmin_<variable>``; see session_explorer.sessions_schema).
_VARMAX_PREFIX = "vmax_"

# In-process caches, invalidated on their source files' fingerprints (index /
# meta / episodes / windows / enrichment id sets / features) or a short TTL
# (only the corpus mean). Each cache has its own lock (double-checked: probe
# without the lock, re-check under it before building) — the app serves 8
# gunicorn threads, and an unlocked cold cache made every concurrent request
# rebuild a 100 MB frame.
_INDEX_CACHE: dict = {"fingerprint": None, "df": None, "search": None}
_DIRECTED_CACHE: dict = {"fingerprint": None, "cut": None, "counts": None}
# Fingerprint-keyed (not TTL): these rebuilds are corpus-scale reads, so they
# must only happen when a source file actually changed — a TTL made an
# unlucky click every 10 minutes pay tens of seconds of refill.
_FLAGS_CACHE: dict = {"key": None, "model": None, "flags": None, "emb_index": None}
_FEAT_CACHE: dict = {"key": None, "df": None}
_META_CACHE: dict = {"fingerprint": None, "meta": None}
_EPISODES_CACHE: dict = {"fingerprint": None, "df": None}
_WINDOWS_CACHE: dict = {"fingerprint": None, "df": None}
# Slider bounds per (index fingerprint, study, floors) — invariant across the
# user's own range/search filters, so recomputing them per request was waste.
_RANGES_CACHE: dict = {}
_RANGES_CACHE_MAX = 32
# video_map's numeric-column list, learned from the first full read per
# artifact version so later _trend_frame reads can project columns.
# Per-collection play frames for the detail endpoint: the pushdown read of
# sessions_plays.parquet is a GCS round-trip per click, and users hop between
# sessions of the same few collections. Keyed by collection, validated by the
# plays artifact's fingerprint. ~a few MB per donor.
_COLLECTION_PLAYS_CACHE: dict = {}
_COLLECTION_PLAYS_MAX = 8
_collection_plays_lock = threading.Lock()
# Per-binge maxima of the numeric video variables, one row per episode —
# backs the varmax filter's "binges only" scope. Keyed on the episodes +
# video_map fingerprints; the frame itself is tiny (episodes × variables).
_EPVMAX_CACHE: dict = {"key": None, "df": None}
# The active model's corpus mean (for the context-play distances). TTL-cached
# (a small JSON): a stale mean is impossible mid-TTL because the mean file
# only changes on an embeddings rebuild.
_MEAN_CACHE: dict = {"ts": 0.0, "model": None, "mean": None}
# Short-TTL cache over data_io.stat: on GCS every fingerprint probe is a
# network round-trip, and the overview fires on each debounced keystroke.
_STAT_CACHE: dict = {}
_STAT_TTL_S = 15.0
# TTL for the small caches with no single backing file to fingerprint
# (currently only the corpus-mean JSON).
_MEAN_TTL_S = 600.0

_index_lock = threading.Lock()
_epvmax_lock = threading.Lock()
_mean_lock = threading.Lock()
_directed_lock = threading.Lock()
_flags_lock = threading.Lock()
_feat_lock = threading.Lock()
_meta_lock = threading.Lock()
_episodes_lock = threading.Lock()
_windows_lock = threading.Lock()
_ranges_lock = threading.Lock()

# Story text is for card context only — cap it so a session with 100 plays
# doesn't ship 100 full transcripts.
_STORY_CAP = 400




def _fingerprint(filename: str,
                 location: str = session_explorer.ARTIFACT_LOCATION) -> str | None:
    """Return a size:mtime fingerprint for a cache artifact, or None if absent.

    Stat results are held for ``_STAT_TTL_S`` so a burst of requests (each
    overview/detail probes several artifacts) costs one storage round-trip per
    file, not one per request; a rebuild is picked up within the TTL.
    """
    cache_key = f"{location}/{filename}"
    hit = _STAT_CACHE.get(cache_key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _STAT_TTL_S:
        return hit[1]
    fp = data_io.stat(storage_location=location, filename=filename)
    key = None if fp is None else f"{fp.get('size')}:{fp.get('mtime')}"
    _STAT_CACHE[cache_key] = (now, key)
    return key




def _load_index() -> pd.DataFrame | None:
    """Load (and cache) the sessions index, or None when not built yet.

    The cached working frame deliberately EXCLUDES ``search_text`` — the blob
    is ~3/4 of the frame's RAM and only the ``q`` filter reads it, so it is
    held as a separate aligned Series (see :func:`_search_blob`) and the
    row-mask copies the overview makes stay cheap. ``_start_dt`` (parsed
    ``start_ts``) is added once here so no request re-parses 79k strings.
    """
    key = _fingerprint(session_explorer.SESSIONS_FILE)
    if key is None:
        return None
    if _INDEX_CACHE["df"] is not None and _INDEX_CACHE["fingerprint"] == key:
        return _INDEX_CACHE["df"]
    with _index_lock:
        if _INDEX_CACHE["df"] is not None and _INDEX_CACHE["fingerprint"] == key:
            return _INDEX_CACHE["df"]
        df = data_io.load_parquet_selective(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=session_explorer.SESSIONS_FILE,
        )
        if df is None:
            return None
        df = df.copy()
        df["collection_id"] = df["collection_id"].astype("string")
        df["session_id"] = df["session_id"].astype("string")
        search = None
        if "search_text" in df.columns:
            search = df["search_text"].astype("string")
            df = df.drop(columns=["search_text"])
        df["_start_dt"] = pd.to_datetime(df["start_ts"], errors="coerce")
        with _ranges_lock:
            _RANGES_CACHE.clear()
        _INDEX_CACHE.update({"df": df, "search": search, "fingerprint": key})
    return _INDEX_CACHE["df"]




def _search_blob(index: pd.DataFrame) -> pd.Series | None:
    """The index's ``search_text`` Series (row-aligned), or None when absent.

    The cached index holds the blob out-of-frame; a frame that still carries
    the column (an injected test frame) is served from it directly.
    """
    if "search_text" in index.columns:
        return index["search_text"].astype("string")
    if index is _INDEX_CACHE["df"]:
        return _INDEX_CACHE["search"]
    return None




def _start_dt(df: pd.DataFrame) -> pd.Series:
    """Parsed ``start_ts`` — the pre-parsed column when present, else live."""
    if "_start_dt" in df.columns:
        return df["_start_dt"]
    return pd.to_datetime(df["start_ts"], errors="coerce")




def _directed_counts() -> pd.Series | None:
    """Per-session count of DIRECTED binges, indexed by (collection_id, session_id).

    Read from the episodes artifact rather than a column on the session index:
    there are only a few hundred episodes corpus-wide, so aggregating them per
    request (fingerprint-cached) is cheaper than a schema change, and the
    threshold stays live.

    Returns None when the artifact predates ``direction_p`` — the caller must
    then report "not computed" rather than zero, which would read as "no
    session has a directed binge".
    """
    key = _fingerprint(session_explorer.EPISODES_FILE)
    if key is None:
        return None
    cut = _drift_p()
    if (_DIRECTED_CACHE["counts"] is not None
            and _DIRECTED_CACHE["fingerprint"] == key
            and _DIRECTED_CACHE["cut"] == cut):
        return _DIRECTED_CACHE["counts"]
    with _directed_lock:
        if (_DIRECTED_CACHE["counts"] is not None
                and _DIRECTED_CACHE["fingerprint"] == key
                and _DIRECTED_CACHE["cut"] == cut):
            return _DIRECTED_CACHE["counts"]
        df = data_io.load_parquet_selective(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=session_explorer.EPISODES_FILE,
            columns=["collection_id", "session_id", "direction_p"],
        )
        if df is None or "direction_p" not in df.columns:
            return None
        df = df.copy()
        df["collection_id"] = df["collection_id"].astype("string")
        df["session_id"] = df["session_id"].astype("string")
        directed = pd.to_numeric(df["direction_p"], errors="coerce") < cut
        counts = directed.groupby([df["collection_id"], df["session_id"]]).sum().astype("int32")
        _DIRECTED_CACHE.update({"fingerprint": key, "cut": cut, "counts": counts})
    return counts




def _load_meta() -> dict | None:
    """Load (and fingerprint-cache) the artifact provenance meta, or None."""
    key = _fingerprint(session_explorer.META_FILE)
    if key is None:
        return None
    if _META_CACHE["fingerprint"] == key:
        return _META_CACHE["meta"]
    with _meta_lock:
        if _META_CACHE["fingerprint"] == key:
            return _META_CACHE["meta"]
        meta = data_io.load_json(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=session_explorer.META_FILE,
        )
        _META_CACHE.update({
            "fingerprint": key,
            "meta": meta if isinstance(meta, dict) else None,
        })
    return _META_CACHE["meta"]




def _flags_cache_key(model: str | None) -> tuple:
    """Invalidation key for :func:`_flag_sets`: the fingerprints of its sources.

    Scrapes parquet (scraped/downloaded), annotations parquet (annotated) and
    the model's dense-index parquet (the ``emb_index``). Each probe rides the
    15 s ``_STAT_CACHE``, so computing the key is a few cheap stats at most.
    """
    idx_fp = None
    if model:
        idx_fp = _fingerprint(embedding_store._index_filename(model),
                              location=embedding_store.STORE_LOCATION)
    return (
        model,
        _fingerprint(embeddings.SCRAPES_FILE, location=embeddings.STORE_LOCATION),
        _fingerprint(embeddings.ANNOTATIONS_FILE, location=embeddings.STORE_LOCATION),
        idx_fp,
    )




def _flag_sets() -> dict:
    """Return cached per-item enrichment id sets for the active model.

    Used for the detail payload's per-play ``annotated`` / ``embedded`` /
    ``streamable`` flags. Fingerprint-cached on the source files (see
    :func:`_flags_cache_key`): the sets change only when enrichment workers
    rewrite those files, and this rebuild is a corpus-scale read — a TTL made
    requests pay it on a schedule even when nothing had changed.

    ``embedded`` is deliberately left EMPTY here: filling it means scanning
    every embedding shard for ~1.35M ids to answer a few hundred membership
    tests per detail request. The dense sidecar's id → row index answers the
    same question from one small parquet — see :func:`_embedded_ids`.
    """
    try:
        model = embeddings.active_embedding_backend().model_id()
    except Exception:
        model = None
    key = _flags_cache_key(model)
    if _FLAGS_CACHE["flags"] is not None and _FLAGS_CACHE["key"] == key:
        return _FLAGS_CACHE["flags"]
    with _flags_lock:
        if _FLAGS_CACHE["flags"] is not None and _FLAGS_CACHE["key"] == key:
            return _FLAGS_CACHE["flags"]
        flags = (session_explorer.enrichment_id_sets(model, include_embedded=False)
                 if model else {"scraped": set(), "downloaded": set(),
                                "annotated": set(), "embedded": set()})
        emb_index = None
        if model:
            try:
                emb_index = embedding_store.load_index(model)
            except Exception:
                emb_index = None
        _FLAGS_CACHE.update({"key": key, "model": model, "flags": flags,
                             "emb_index": emb_index})
    return _FLAGS_CACHE["flags"]




def _embedded_ids(item_ids: set[str], flags: dict) -> set[str]:
    """Which of ``item_ids`` have a dense embedding.

    A flag set that already carries ``embedded`` ids (an injected one) is
    honoured; the production cache leaves it empty and the sidecar's id → row
    index answers instead — cached by :func:`_flag_sets` (same TTL, same
    model), so nothing is read per request. Returns an empty set when no
    dense store exists, which matches the shard scan's answer for that state.
    """
    if flags.get("embedded"):
        return {str(i) for i in item_ids if str(i) in flags["embedded"]}
    index = _FLAGS_CACHE.get("emb_index")
    if index is None or not item_ids:
        return set()
    ids = [str(i) for i in item_ids]
    try:
        _, found = index.lookup(ids)
    except Exception:
        return set()
    return {i for i, f in zip(ids, found) if f}




def _corpus_mean(model: str) -> np.ndarray | None:
    """The model's cached corpus mean (TTL-cached JSON read), or None."""
    now = time.monotonic()
    if _MEAN_CACHE["model"] == model and now - _MEAN_CACHE["ts"] < _MEAN_TTL_S:
        return _MEAN_CACHE["mean"]
    with _mean_lock:
        if _MEAN_CACHE["model"] == model and now - _MEAN_CACHE["ts"] < _MEAN_TTL_S:
            return _MEAN_CACHE["mean"]
        try:
            mean = embedding_store.load_corpus_mean(model)
        except Exception:
            mean = None
        _MEAN_CACHE.update({"ts": now, "model": model, "mean": mean})
    return _MEAN_CACHE["mean"]




def _attach_context_distances(seqs: list[dict], play_rows: list[dict],
                              n_ctx: int) -> None:
    """Attach each sequence's context-play distances, in place.

    For every binge/low-entropy sequence, the up-to-``n_ctx`` plays just
    before its first member and just after its last are the "context" steps
    the player shows, and the non-member plays between the first and last
    member are its "off-theme" steps. Each gets its cosine distance to the
    centroid of the sequence's member vectors (same directional geometry as
    the artifact's ``rolling_cosdist``), so the researcher can see WHY a
    neighbouring or skipped video was not part of the run. Stored as
    ``context_distances`` on the sequence, keyed ``"<item_id>@<ts>"`` — the
    pair the client identifies a step by.

    Silently a no-op when the dense store / corpus mean is unavailable (the
    payload simply carries no distances) — never an error path.
    """
    if not seqs or not play_rows or n_ctx < 0:
        return
    model = _FLAGS_CACHE.get("model")
    index = _FLAGS_CACHE.get("emb_index")
    if not model or index is None:
        return
    mean = _corpus_mean(model)
    if mean is None:
        return

    # First/last position of each (item_id, ts) in the play sequence — the
    # same matching rule the client's step builder uses.
    pos_first: dict[tuple, int] = {}
    pos_last: dict[tuple, int] = {}
    for i, p in enumerate(play_rows):
        key = (p["item_id"], p["ts"])
        pos_first.setdefault(key, i)
        pos_last[key] = i

    contexts: list[tuple[dict, list[dict]]] = []
    need_ids: set[str] = set()
    for seq in seqs:
        members = seq.get("members") or []
        if not members:
            continue
        first = pos_first.get((members[0]["item_id"], members[0]["ts"]))
        last = pos_last.get((members[-1]["item_id"], members[-1]["ts"]))
        ctx: list[dict] = []
        if first is not None and first > 0:
            ctx.extend(play_rows[max(0, first - n_ctx):first])
        if first is not None and last is not None and last > first:
            member_keys = {(m["item_id"], m["ts"]) for m in members}
            ctx.extend(p for p in play_rows[first:last + 1]
                       if (p["item_id"], p["ts"]) not in member_keys)
        if last is not None and last + 1 < len(play_rows):
            ctx.extend(play_rows[last + 1:last + 1 + n_ctx])
        if not ctx:
            continue
        contexts.append((seq, ctx))
        need_ids.update(m["item_id"] for m in members)
        need_ids.update(p["item_id"] for p in ctx)
    if not contexts:
        return

    try:
        id2row, block = session_explorer.load_directional_block(
            model, sorted(need_ids), mean, index=index)
    except Exception:
        return
    if not id2row:
        return
    for seq, ctx in contexts:
        rows = [id2row[m["item_id"]] for m in seq["members"]
                if m["item_id"] in id2row]
        if not rows:
            continue
        centroid = block[rows].mean(axis=0)
        dists = {}
        for p in ctx:
            row = id2row.get(p["item_id"])
            if row is None:
                continue
            dists[f"{p['item_id']}@{p['ts']}"] = round(
                1.0 - float(block[row] @ centroid), 4)
        if dists:
            seq["context_distances"] = dists




def _features() -> pd.DataFrame:
    """Return the cached per-video feature frame (item_id-indexed).

    The whole-corpus ``video_map`` + scrape-author read is too heavy to repeat
    per detail request; fingerprint-cached on its two source files, so the
    rebuild only ever happens after a map rebuild or a consolidation — never
    on a timer.
    """
    key = (
        _fingerprint("video_map.parquet", location=embeddings.STORE_LOCATION),
        _fingerprint(embeddings.SCRAPES_FILE, location=embeddings.STORE_LOCATION),
    )
    if _FEAT_CACHE["df"] is not None and _FEAT_CACHE["key"] == key:
        return _FEAT_CACHE["df"]
    with _feat_lock:
        if _FEAT_CACHE["df"] is not None and _FEAT_CACHE["key"] == key:
            return _FEAT_CACHE["df"]
        try:
            # Corpus-wide, so no scrape text (desc/hashtags are hundreds of MB at
            # that scale) — the detail endpoint reads those per session instead.
            # The trend-scan numeric columns ride along so _trend_frame can
            # slice this cached frame instead of a per-click pushdown read of
            # video_map.parquet (whose item_id filters never prune row groups
            # — that read was seconds of every detail click).
            try:
                extra_cols = session_explorer.trend_numeric_columns()
            except Exception:
                extra_cols = None
            df = session_explorer.load_video_features(extra_map_cols=extra_cols)
            _FEAT_CACHE["trend_cols"] = extra_cols or []
        except Exception:
            df = pd.DataFrame(columns=["niche_name", "category", "story",
                                       "political_score", "sensitivity_score",
                                       "advertising", "author", "duration"])
        _FEAT_CACHE.update({"key": key, "df": df})
    return _FEAT_CACHE["df"]




def _story_map(item_ids: set[str]) -> dict[str, str]:
    """Per-item AI story summaries for one session's items.

    ``video_map.parquet``'s ``story`` column is populated only for the 2D-map's
    hover-label sample, so stories are read from the machine-annotations frame
    instead (filter pushdown on the session's item ids — a session is a few
    hundred items at most).
    """
    if not item_ids:
        return {}
    try:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.ANNOTATIONS_FILE,
            columns=["item_id", "video_story"],
            filters=[("item_id", "in", list(item_ids))],
        )
    except Exception:
        return {}
    if df is None or df.empty or "video_story" not in df.columns:
        return {}
    out: dict[str, str] = {}
    for iid, story in zip(df["item_id"].astype("string"), df["video_story"]):
        s = _clean(story)
        if s:
            out[str(iid)] = str(s)
    return out




def _scrape_text_map(item_ids: set[str]) -> dict[str, dict]:
    """Per-item scraped caption text for one session's items.

    ``desc`` / ``desc_hashtags`` are deliberately NOT part of the cached
    corpus-wide feature frame (they would add hundreds of MB); like the
    stories, they are pushdown-read per session — a few hundred ids at most.

    Returns:
        item_id → ``{"desc": str | None, "hashtags": str | None}`` (items with
        neither field absent).
    """
    if not item_ids:
        return {}
    try:
        available = data_io.get_parquet_columns(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.SCRAPES_FILE) or []
    except Exception:
        return {}
    cols = [c for c in ("desc", "desc_hashtags") if c in available]
    if not cols:
        return {}
    try:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=embeddings.SCRAPES_FILE,
            columns=["item_id"] + cols,
            filters=[("item_id", "in", list(item_ids))],
        )
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, dict] = {}
    for _, row in df.drop_duplicates(subset=["item_id"]).iterrows():
        desc = _text_value(row.get("desc"))
        hashtags = _text_value(row.get("desc_hashtags"))
        if desc or hashtags:
            out[str(row["item_id"])] = {"desc": desc, "hashtags": hashtags}
    return out




def _play_text_maps(plays: pd.DataFrame) -> tuple[dict[str, str], dict[str, dict]]:
    """Story/scrape-text maps from a plays frame with baked-in text columns.

    The plays artifact stores per-item ``story``/``desc``/``hashtags``
    (already capped at build time — see ``session_explorer.PLAY_TEXT_CAP``),
    so a detail request needs no corpus-parquet reads at all. Returns the
    same shapes as :func:`_story_map` and :func:`_scrape_text_map`.
    """
    stories: dict[str, str] = {}
    scrape_text: dict[str, dict] = {}
    hashtags_col = (plays["hashtags"] if "hashtags" in plays.columns
                    else [None] * len(plays))
    desc_col = plays["desc"] if "desc" in plays.columns else [None] * len(plays)
    for iid, story, desc, hashtags in zip(
            plays["item_id"].astype("string"), plays["story"],
            desc_col, hashtags_col):
        iid = str(iid)
        story = _text_value(story)
        if story and iid not in stories:
            stories[iid] = story
        desc = _text_value(desc)
        hashtags = _text_value(hashtags)
        if (desc or hashtags) and iid not in scrape_text:
            scrape_text[iid] = {"desc": desc, "hashtags": hashtags}
    return stories, scrape_text




def _text_value(value) -> str | None:
    """A displayable string from a text cell; joins list cells (hashtags).

    ``desc_hashtags`` is stored as a LIST column, so a cell can be a numpy
    array / Python list rather than a scalar — ``_clean`` would choke on it.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return " ".join(parts) or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None




# Map columns that are identifiers or map coordinates, not measurements — they
# would "trend" meaninglessly (x/y are a 2D projection, niche is a cluster id).
_TREND_EXCLUDE = {"item_id", "niche", "x", "y"}

# Enumerate every ordering up to this length; sample above it. The Spearman
# null depends only on n, so each length's null is built once per process.
_TREND_MAX_EXACT = 8
_TREND_SAMPLES = 20_000




@lru_cache(maxsize=32)
def _spearman_null(n: int) -> np.ndarray:
    """Sorted ``|rho|`` under a random ordering of ``n`` items.

    Distribution-free in the ranks, so it depends only on ``n`` — building it
    once per length is what makes an exact test affordable per request.
    Deliberately NOT scipy's default p-value: that is a t-approximation which
    returns p ~ 0 for a perfect ordering of 4 items, where the exact answer is
    0.083. On this corpus the approximation turned a 3.4% hit rate into 21.6%.
    """
    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    if n <= _TREND_MAX_EXACT:
        orders = np.array(list(itertools.permutations(range(n))), dtype=float)
    else:
        rng = np.random.default_rng(0)
        orders = np.array([rng.permutation(n) for _ in range(_TREND_SAMPLES)], dtype=float)
    oc = orders - orders.mean(axis=1, keepdims=True)
    return np.sort(np.abs((oc @ xc) / (xc ** 2).sum()))




def _spearman_exact(y: np.ndarray) -> tuple[float, float]:
    """Spearman rho of ``y`` against position, with an exact permutation p."""
    n = len(y)
    ranks = pd.Series(y).rank().to_numpy()
    x = np.arange(n, dtype=float)
    xc, rc = x - x.mean(), ranks - ranks.mean()
    denom = np.sqrt((rc ** 2).sum() * (xc ** 2).sum())
    if denom <= 0:
        return float("nan"), 1.0
    rho = float((rc @ xc) / denom)
    null = _spearman_null(n)
    hits = int((null >= abs(rho) - 1e-12).sum())
    if n <= _TREND_MAX_EXACT:
        # Enumerated null: the observed ordering is one of them, so the count
        # already carries its own floor (2/n!, since reversal ties it).
        return rho, hits / len(null)
    # Sampled null: (1 + hits) / (1 + m), the standard permutation-test
    # estimator. A plain mean can return exactly 0, which claims a certainty
    # the sample cannot support.
    return rho, (1 + hits) / (1 + len(null))




def _benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """BH-adjusted q-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [1.0] * m
    running = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        running = min(running, pvalues[i] * m / (m - rank + 1))
        q[i] = running
    return q




def _trend_frame(item_ids: set[str]) -> pd.DataFrame:
    """Numeric per-video variables for one session's items (item_id-indexed).

    The eligible columns are whatever the map artifact currently stores as a
    number, minus the identifiers and map coordinates — so a newly-annotated
    numeric field joins the scan without a code change. Filtered to the
    session's few hundred items, so this is a small pushdown read, not a
    corpus scan.
    """
    if not item_ids:
        return pd.DataFrame()
    # Sliced from the fingerprint-cached feature frame (which now carries the
    # trend numeric columns) — the old per-click pushdown read of
    # video_map.parquet decoded most of the corpus file every time, because
    # an item_id `in` filter cannot prune row groups whose stats span the
    # whole id space.
    feat = _features()
    if feat is None or feat.empty:
        return pd.DataFrame()
    ids = [str(i) for i in item_ids]
    sub = feat[feat.index.isin(ids)]
    if sub.empty:
        return pd.DataFrame()
    # Only the map's own numeric overlay columns are trend-eligible — the
    # feature frame also carries scrape-side numerics (e.g. duration) that the
    # old map-only read never scanned.
    allowed = set(_FEAT_CACHE.get("trend_cols") or [])
    numeric = [c for c in sub.columns
               if c in allowed and c not in _TREND_EXCLUDE
               and pd.api.types.is_numeric_dtype(sub[c])]
    if not numeric:
        return pd.DataFrame()
    out = sub[numeric].copy()
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[~out.index.duplicated()]




def _min_max_ranges(series: dict[str, np.ndarray]) -> list[dict]:
    """Observed min/max per numeric variable, for the "(more info)" panels.

    Purely descriptive — no test, no threshold — so it is computed for every
    binge/session, including ones too short for the trend scan.

    Args:
        series: variable name → aligned value array (NaN for missing).

    Returns:
        ``[{"variable", "label", "min", "max", "n"}, ...]`` sorted by label;
        variables with no finite value are omitted.
    """
    out = []
    for name, values in series.items():
        ok = np.isfinite(values)
        if not ok.any():
            continue
        out.append({
            "variable": name,
            # dwell_s is per-play, not a var_schema variable, so it has no
            # display name to look up.
            "label": "Dwell (s)" if name == "dwell_s" else _variable_label(name),
            "min": round(float(values[ok].min()), 3),
            "max": round(float(values[ok].max()), 3),
            "n": int(ok.sum()),
        })
    return sorted(out, key=lambda r: r["label"].lower())




def _scan_trend(members: list[dict], feat: pd.DataFrame, min_n: int) -> dict:
    """Find the strongest monotone trend across one binge's ordered members.

    Every numeric variable is tested with an exact permutation Spearman against
    member position, and the resulting p-values are Benjamini-Hochberg adjusted
    ACROSS the variables scanned — without that correction, scanning ~9
    variables on a short run manufactures a "finding" for most binges.

    Args:
        members: The binge's members in time order (each with ``item_id`` and
            ``dwell_s``).
        feat: Numeric per-video variables, item_id-indexed.
        min_n: Fewest non-null points a variable needs to be tested.

    Returns:
        A dict the card renders verbatim: ``scanned`` (how many variables had
        enough data), ``n_members``, ``min_n``, and either ``trend`` (the
        single strongest surviving result) or ``trend: None``. A null trend
        with ``scanned: 0`` means "not testable", which the UI must not present
        as "no trend exists". ``ranges`` (per-variable observed min/max, see
        :func:`_min_max_ranges`) is descriptive and present regardless of the
        ``min_n`` gate — a binge too short to test still has extremes.
    """
    ids = [str(m.get("item_id")) for m in members]
    series: dict[str, np.ndarray] = {}
    if not feat.empty:
        sub = feat.reindex(ids)
        for col in feat.columns:
            series[col] = sub[col].to_numpy(dtype=float)
    # Dwell rides along from the member list — it is per-PLAY, so it never
    # appears in the per-video map, yet it is the variable most likely to
    # trend within a binge (the satiation effect).
    series["dwell_s"] = np.array(
        [np.nan if m.get("dwell_s") is None else float(m["dwell_s"]) for m in members])

    tested = []
    for name, values in series.items():
        ok = np.isfinite(values)
        # A variable that barely varies has no monotone trend to find, and its
        # tie-heavy ranks make the permutation null a poor approximation.
        if int(ok.sum()) < min_n or len(np.unique(values[ok])) < 3:
            continue
        rho, p = _spearman_exact(values[ok])
        if np.isfinite(rho):
            tested.append({"variable": name, "rho": round(rho, 3),
                           "p": round(p, 5), "n": int(ok.sum())})

    out = {"scanned": len(tested), "n_members": len(members), "min_n": min_n,
           "trend": None, "ranges": _min_max_ranges(series)}
    if not tested:
        return out
    for entry, q in zip(tested, _benjamini_hochberg([t["p"] for t in tested])):
        entry["q"] = round(q, 5)
    best = min(tested, key=lambda t: (t["q"], -abs(t["rho"])))
    if best["q"] < 0.05:
        best["direction"] = "rising" if best["rho"] > 0 else "falling"
        best["label"] = _variable_label(best["variable"])
        out["trend"] = best
    return out




@lru_cache(maxsize=1024)
def _variable_label(name: str) -> str:
    """Human-readable name for a scanned variable, from var_schema if present."""
    try:
        schema = fyp_cf.get("var_schema")
        if schema is not None and name in schema.index:
            display = schema.loc[name].get("display_name")
            if isinstance(display, str) and display.strip():
                return display.strip()
    except Exception:
        pass
    return name.replace("_", " ")




def _creator_count(item_ids: list[str], feat: pd.DataFrame) -> dict:
    """Distinct known creators across a run, with how many items are attributed.

    A bare count would silently under-report a run whose videos were never
    scraped: 3 creators across 4 known authors is a different observation from
    3 across 12, so both numbers travel together.
    """
    known = 0
    authors: set[str] = set()
    if not feat.empty and "author" in feat.columns:
        for value in feat.reindex([str(i) for i in item_ids])["author"]:
            if value is None:
                continue
            try:
                if pd.isna(value):
                    continue
            except (TypeError, ValueError):
                pass
            known += 1
            authors.add(str(value))
    return {"n_creators": len(authors), "n_attributed": known,
            "n_items": len(item_ids)}




def _study_collection_ids(study: str) -> set[str]:
    """Collection ids whose sessions belong to ``study`` (already access-checked).

    The study's ``SELECTED_COLLECTIONS`` alone is not the study: a selected
    collection can be dropped from the built dataset entirely by the study's
    date window or its group/activity-count thresholds, and it then appears
    nowhere else in the app. The sessions artifacts are global — built over
    every collection's unsampled activity — so without the intersection the
    tab lists sessions from collections the study does not contain.

    Falls back to the raw selection when the study has never been built (no
    frame to intersect against), which is the only honest answer there.
    """
    selected = {str(d.get("collection_id")) for d in get_study_collections(study)
                if d.get("collection_id")}
    in_frame = get_study_frame_collections(study)
    if in_frame is None:
        return selected
    return selected & in_frame




def _in_study_window(df: pd.DataFrame, study: str) -> pd.Series:
    """Mask of the index rows whose session STARTED inside ``study``'s window.

    The second half of study scoping. The artifact holds every session a
    collection ever recorded, so a study with a narrow date window would
    otherwise list years of sessions it does not contain — the frame's own
    date filter never reaches here, because the artifact is not per-study.

    A session is matched on its start alone: ``start_ts`` is what the table
    shows, sorts and filters on, so "the session's date" means one thing
    everywhere. A session straddling a boundary therefore belongs to the day
    it began on; sessions are short enough that the alternative (span overlap)
    would move a handful of rows and cost that consistency.

    Both the artifact's ``start_ts`` and the study's bounds are wall-clock
    (``local_timestamp``-derived), so this is the same comparison the study
    builder makes — no timezone conversion on either side.
    """
    start, end_bound = get_study_date_window(study)
    ts = _start_dt(df)
    # An unparseable start cannot be placed in the window; NaT compares False
    # on both sides, which drops it — the honest answer for a row that has no
    # date at all.
    return (ts >= start) & (ts < end_bound)




def _sessions_config() -> dict:
    """The live ``[sessions]`` config block (always a dict)."""
    cfg = fyp_cf.get("sessions", {})
    return cfg if isinstance(cfg, dict) else {}




def _context_plays() -> int:
    """``[sessions] context_plays`` from the live config (non-negative)."""
    try:
        return max(int(_sessions_config().get("context_plays", DEFAULT_CONTEXT_PLAYS)), 0)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_PLAYS




def _drift_p() -> float:
    """``[sessions] drift_p`` — the ``direction_p`` cut for calling a binge directed."""
    try:
        return max(min(float(_sessions_config().get("drift_p", DEFAULT_DRIFT_P)), 1.0), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_DRIFT_P




def _trend_min_videos() -> int:
    """``[sessions] trend_min_videos`` — smallest scannable binge, floored at 5.

    Below 5 members the exact permutation test cannot reach any conventional
    threshold at all, so a smaller value would not widen coverage, only
    misrepresent what was tested.
    """
    try:
        return max(int(_sessions_config().get("trend_min_videos", DEFAULT_TREND_MIN_VIDEOS)), 5)
    except (TypeError, ValueError):
        return DEFAULT_TREND_MIN_VIDEOS




def _session_floors() -> dict:
    """The session-list floors, in the units this endpoint filters on.

    Applied at query time, so an admin edit takes effect on the next request
    with no artifact rebuild — the index itself stays complete and every
    excluded session is still counted in ``total_in_study``.

    ``min_coverage`` is converted from the admin-facing percentage to the 0-1
    fraction ``coverage_embedded`` is stored as.
    """
    from web_interface.admin_settings import get_session_floors

    floors = get_session_floors()
    return {
        "min_plays": int(floors["sessions_min_plays"]),
        "min_session_minutes": float(floors["sessions_min_minutes"]),
        "min_coverage": float(floors["sessions_min_coverage_pct"]) / 100.0,
    }




def _display_params(meta: dict | None) -> dict:
    """The limits the tab must describe to the researcher.

    Segmentation/window values come from the artifact's own provenance — they
    describe the binges and sequences actually on screen, which a later config
    edit does not retroactively change — and fall back to the live config only
    for keys an older artifact never recorded. ``context_plays`` is a pure
    display knob, so it is always live.
    """
    params = dict(session_explorer.default_params())
    built = (meta or {}).get("params")
    if isinstance(built, dict):
        params.update({k: v for k, v in built.items() if k in params})
    params["context_plays"] = _context_plays()
    params["drift_p"] = _drift_p()
    params["trend_min_videos"] = _trend_min_videos()
    return params




def _clean(value):
    """JSON-safe scalar: NA/NaN → None, numpy scalars → Python."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value




def _filter_ranges(df: pd.DataFrame) -> dict:
    """Slider bounds for the filter panel, over the floor-passing frame.

    Computed BEFORE the ad-hoc range filters are applied, so the client's
    sliders keep stable endpoints while the user narrows them. A key is None
    when the column has no usable values (e.g. no session has a low-entropy
    score yet), which tells the client to omit that slider.
    """
    out: dict = {}
    ts = _start_dt(df) if "start_ts" in df.columns else pd.Series(dtype="datetime64[ns]")
    out["start_date"] = ([str(ts.min().date()), str(ts.max().date())]
                         if ts.notna().any() else None)
    for col in ("duration_min", "n_plays", "coverage_embedded",
                "min_window_cosdist", "n_episodes"):
        vals = (pd.to_numeric(df[col], errors="coerce")
                if col in df.columns else pd.Series(dtype="float64"))
        out[col] = ([float(vals.min()), float(vals.max())]
                    if vals.notna().any() else None)
    # Per-variable session-max bounds for the variable-picker filter. None
    # (not {}) when the artifact predates the vmax_ columns, so the client can
    # tell "no variables usable" from "filter unavailable — rebuild".
    vmax_cols = [c for c in df.columns if c.startswith(_VARMAX_PREFIX)]
    if vmax_cols:
        var_max: dict = {}
        for col in vmax_cols:
            vals = pd.to_numeric(df[col], errors="coerce")
            if vals.notna().any():
                var_max[col[len(_VARMAX_PREFIX):]] = [float(vals.min()), float(vals.max())]
        out["var_max"] = var_max
        out["var_labels"] = {
            name: ("Dwell (s)" if name == "dwell_s" else _variable_label(name))
            for name in var_max}
    else:
        out["var_max"] = None
        out["var_labels"] = None
    return out




def _cached_filter_ranges(index: pd.DataFrame, pop: np.ndarray, study: str,
                          sig: tuple) -> dict:
    """:func:`_filter_ranges`, cached per (artifact, study scope, floors).

    The bounds are computed BEFORE the user's own range/search filters, so
    for a given index fingerprint + study scope + floor values they never
    change — recomputing ~25 ``to_numeric`` columns per keystroke was pure
    waste. ``sig`` carries everything else the scoped population depends on
    (floor values, the study's date window, its collection-set signature), so
    a study rebuild/edit invalidates without an index rebuild. An injected
    (uncached) index frame computes directly.
    """
    fingerprint = _INDEX_CACHE["fingerprint"]
    if fingerprint is None or index is not _INDEX_CACHE["df"]:
        return _filter_ranges(index[pop])
    key = (fingerprint, study, sig)
    hit = _RANGES_CACHE.get(key)
    if hit is not None:
        return hit
    with _ranges_lock:
        hit = _RANGES_CACHE.get(key)
        if hit is None:
            hit = _filter_ranges(index[pop])
            if len(_RANGES_CACHE) >= _RANGES_CACHE_MAX:
                _RANGES_CACHE.clear()
            _RANGES_CACHE[key] = hit
    return hit




def _opt_query_float(name: str) -> float | None:
    """An optional numeric query param: absent/blank → None, junk → ValueError."""
    raw = request.args.get(name)
    if raw is None or raw.strip() == '':
        return None
    return float(raw)




@sessions_bp.route('/api/sessions/overview', methods=['GET'])
@permission_required('tab.sessions')
def api_sessions_overview():
    """Filterable, sortable, paginated session table scoped to one study.

    Query params: ``study`` (required), ``min_coverage`` (embedded coverage
    floor), ``min_emb_plays``, ``min_plays``, ``min_session_minutes``, ``sort``
    (one of the index metrics; default ``min_window_cosdist``), ``order``
    (``asc``/``desc``), ``limit`` (page size), ``page`` (0-based).

    Only sessions inside the study — its collections AND its date window (see
    :func:`_in_study_window`) — reach any of this; ``total_in_study`` counts
    that population, not the artifact's.

    ``min_plays``, ``min_session_minutes`` and ``min_coverage`` default to the
    admin-controlled session-list floors (Admin → Site Settings, seeded by
    ``[sessions]`` config); each query param is the per-request override, e.g.
    ``min_plays=0`` to see everything. Excluded sessions still count towards
    ``total_in_study``, so the caller can always say how many the floors
    removed.

    The filter panel's ad-hoc range filters ride in as optional pairs:
    ``f_start_min``/``f_start_max`` (ISO dates, inclusive, on ``start_ts``),
    plus ``f_length_*`` (``duration_min``), ``f_plays_*`` (``n_plays``),
    ``f_coverage_*`` (``coverage_embedded``, 0–1), ``f_entropy_*``
    (``min_window_cosdist``) and ``f_binges_*`` (``n_episodes``) — see
    ``_RANGE_FILTER_COLS``. A bounded numeric filter drops sessions whose
    value is missing (an unscored session cannot satisfy an entropy cut).
    The response's ``ranges`` block carries each filter's slider bounds.

    Two further filters need a rebuilt index and degrade silently on an old
    artifact (the response's ``ranges.var_max`` / ``search_available`` flags
    tell the client which are live):

    * ``f_varmax_col`` + ``f_varmax_min``/``f_varmax_max`` — range-filter on
      the session's baked MAX of one numeric video variable (index column
      ``vmax_<f_varmax_col>``); an unknown/absent variable is ignored.
      ``f_varmax_scope=binges`` narrows the same criterion to binges: a
      session passes when at least one of its binges' maxima of the variable
      is in range (per-episode maxima live-computed from the episodes
      artifact + video_map; degrades to session scope when unavailable).
    * ``q`` — free-text search over the per-session ``search_text`` blob
      (stories, niches, categories, creators, captions + hashtags), split on
      whitespace, all terms must match (case-insensitive substring AND).
    """
    study = (request.args.get('study') or '').strip()
    if not study:
        return jsonify({"error": "study is required"}), 400
    denied = study_access_error(study)
    if denied is not None:
        return denied

    index = _load_index()
    if index is None:
        return jsonify({
            "error": "The sessions index has not been built yet. Run the "
                     "'sessions_refresh' task to generate it."
        }), 404

    floors = _session_floors()
    try:
        min_coverage = float(request.args.get('min_coverage', floors["min_coverage"]))
        min_emb = int(request.args.get('min_emb_plays', DEFAULT_MIN_EMB_PLAYS))
        min_plays = int(request.args.get('min_plays', floors["min_plays"]))
        min_minutes = float(request.args.get('min_session_minutes',
                                             floors["min_session_minutes"]))
        limit = min(int(request.args.get('limit', OVERVIEW_LIMIT_DEFAULT)), OVERVIEW_LIMIT_MAX)
        page = max(int(request.args.get('page', 0)), 0)
        range_filters = {stem: (_opt_query_float(f"{stem}_min"),
                                _opt_query_float(f"{stem}_max"))
                         for stem in _RANGE_FILTER_COLS}
        varmax_col = (request.args.get('f_varmax_col') or '').strip()
        varmax_lo = _opt_query_float('f_varmax_min')
        varmax_hi = _opt_query_float('f_varmax_max')
        varmax_scope = (request.args.get('f_varmax_scope') or 'session').strip()
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric filter"}), 400
    search_q = (request.args.get('q') or '').strip()
    f_start_min = f_start_max = None
    try:
        raw = (request.args.get('f_start_min') or '').strip()
        if raw:
            f_start_min = pd.Timestamp(raw)
        raw = (request.args.get('f_start_max') or '').strip()
        if raw:
            # Inclusive day: anything before the following midnight matches.
            f_start_max = pd.Timestamp(raw) + pd.Timedelta(days=1)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid date filter"}), 400
    sort = request.args.get('sort') or "min_window_cosdist"
    if sort not in _SORT_KEYS:
        sort = "min_window_cosdist"
    ascending = (request.args.get('order') or 'asc').lower() != 'desc'

    cids = _study_collection_ids(study)
    window = get_study_date_window(study)
    # All scoping/filter stages are boolean masks over the FULL index; the
    # frame is materialized exactly once, after the last mask. The old
    # stage-by-stage slicing copied the full-width frame ~5 times per request.
    def _np_mask(series) -> np.ndarray:
        return series.fillna(False).to_numpy(dtype=bool)

    # Study scoping is two-axis: collections AND the study's date window. This
    # runs BEFORE total_in_study so every downstream number — the floor counts,
    # the slider bounds, the status line — describes the study, not the
    # artifact.
    in_study = (_np_mask(index["collection_id"].isin(cids))
                & _np_mask(_in_study_window(index, study)))
    total_in_study = int(in_study.sum())
    # The three admin-controlled list floors are applied as one block, so the
    # client can report a single "N not listed" count it can reconcile with the
    # rows on screen; min_emb_plays stays a separate ad-hoc quality filter.
    floors_ok = (in_study
                 & _np_mask(index["n_plays"].fillna(0) >= min_plays)
                 & _np_mask(index["duration_min"].fillna(0) >= min_minutes)
                 & _np_mask(index["coverage_embedded"].fillna(0) >= min_coverage))
    total_above_floors = int(floors_ok.sum())
    pop = floors_ok & _np_mask(index["n_embedded"].fillna(0) >= min_emb)

    # Slider bounds come from the population the sliders act on — after the
    # floors, before the user's own range filters.
    ranges = _cached_filter_ranges(
        index, pop, study,
        (min_plays, min_minutes, min_coverage, min_emb,
         len(cids), hash(frozenset(cids)), window))

    mask = pop.copy()
    if f_start_min is not None or f_start_max is not None:
        ts = _start_dt(index)
        if f_start_min is not None:
            mask &= _np_mask(ts >= f_start_min)
        if f_start_max is not None:
            mask &= _np_mask(ts < f_start_max)
    for stem, col in _RANGE_FILTER_COLS.items():
        lo, hi = range_filters[stem]
        if lo is None and hi is None:
            continue
        if col not in index.columns:
            continue
        vals = pd.to_numeric(index[col], errors="coerce")
        # NaN compares False on both sides, so a bounded filter drops
        # sessions with no value for that metric — deliberately.
        if lo is not None:
            mask &= _np_mask(vals >= lo)
        if hi is not None:
            mask &= _np_mask(vals <= hi)
    # Variable-max filter: same NaN-drops-row semantics. Silently skipped when
    # the column is absent (old artifact, or a variable the map no longer has).
    if varmax_col and (varmax_lo is not None or varmax_hi is not None):
        binge_scoped = False
        if varmax_scope == 'binges':
            # "Binges only": keep sessions where at least ONE binge's max of
            # the variable falls in the range. Sessions without a binge (or
            # whose binges have no value for the variable) drop — the range
            # is a criterion on binges, and they have none satisfying it.
            emax = _episode_vmax()
            if emax is not None and varmax_col in emax.columns:
                vals = pd.to_numeric(emax[varmax_col], errors="coerce")
                ok = vals.notna()
                if varmax_lo is not None:
                    ok &= vals >= varmax_lo
                if varmax_hi is not None:
                    ok &= vals <= varmax_hi
                passing = pd.MultiIndex.from_frame(
                    emax.loc[ok, ["collection_id", "session_id"]])
                keys = pd.MultiIndex.from_arrays(
                    [index["collection_id"], index["session_id"]])
                mask &= keys.isin(passing)
                binge_scoped = True
        if not binge_scoped:
            # Session scope — or the binge scope's data isn't available (no
            # episodes artifact / unknown variable), which degrades to the
            # session-max semantics rather than silently dropping the filter.
            col = f"{_VARMAX_PREFIX}{varmax_col}"
            if col in index.columns:
                vals = pd.to_numeric(index[col], errors="coerce")
                if varmax_lo is not None:
                    mask &= _np_mask(vals >= varmax_lo)
                if varmax_hi is not None:
                    mask &= _np_mask(vals <= varmax_hi)
    # Free-text search over the baked per-session blob (lowercased at build);
    # every whitespace-separated term must match. Ignored on an old artifact.
    blobs = _search_blob(index)
    search_available = blobs is not None
    if search_q and search_available:
        for term in search_q.lower().split():
            mask &= blobs.str.contains(term, regex=False).fillna(False).to_numpy(dtype=bool)
    df = index[mask]
    total_matching = int(len(df))

    # Directed-binge counts join BEFORE the sort so the column is sortable —
    # ranking sessions by it is how a researcher hunts rabbit holes.
    directed = _directed_counts()
    if directed is not None:
        df = df.copy()
        keys = pd.MultiIndex.from_arrays([df["collection_id"], df["session_id"]])
        df["n_directed_episodes"] = directed.reindex(keys).fillna(0).astype("int32").to_numpy()
    if sort not in df.columns:
        # e.g. sorting by directed binges against an artifact that has none.
        sort = "min_window_cosdist"
    df = df.sort_values(sort, ascending=ascending, na_position='last')
    # Pagination: clamp the requested page so a filter change that shrinks the
    # result set never returns an empty page while matches exist.
    if limit > 0:
        page = min(page, max((total_matching - 1) // limit, 0))
        df = df.iloc[page * limit:(page + 1) * limit]
    else:
        page = 0

    display = load_display_id_map()
    sessions = []
    for _, row in df.iterrows():
        rec = {col: _clean(row.get(col)) for col in _OVERVIEW_COLS}
        rec["collection_label"] = display.get(rec["collection_id"], rec["collection_id"])
        # None (not 0) when the artifact predates direction_p: the client must
        # be able to tell "no directed binges" from "never measured".
        rec["n_directed_episodes"] = (
            _clean(row.get("n_directed_episodes")) if directed is not None else None)
        sessions.append(rec)

    meta = _load_meta()
    return jsonify({
        "sessions": sessions,
        "total_in_study": total_in_study,
        "total_above_floors": total_above_floors,
        "total_matching": total_matching,
        "returned": len(sessions),
        "page": page,
        "page_size": limit,
        "ranges": ranges,
        "search_available": search_available,
        "meta": meta,
        "params": _display_params(meta),
        "floors": {"min_plays": min_plays, "min_session_minutes": min_minutes,
                   "min_coverage": min_coverage},
        "defaults": {
            "min_emb_plays": DEFAULT_MIN_EMB_PLAYS,
            "min_plays": floors["min_plays"],
            "min_session_minutes": floors["min_session_minutes"],
            "min_coverage": floors["min_coverage"],
        },
    })




def _session_plays(collection_id: str, session_row: pd.Series) -> pd.DataFrame:
    """Read one session's play rows.

    Preferred source: the ``sessions_plays.parquet`` artifact — play rows
    only, published sorted by (collection_id, ts) in small row groups, so the
    ``collection_id`` pushdown genuinely prunes. Fallback (artifact absent —
    pre-upgrade build — or stale, i.e. it has no rows for this collection):
    the consolidated activity file, whose row-group stats span the whole id
    space, so that read decodes ~all play rows and is the slow path. Sessions
    synthesised for null ``session_id`` rows (keys ``na_<idx>``) are
    recovered by their time span instead.

    Args:
        collection_id: The session's collection.
        session_row: The session's row from the index artifact.

    Returns:
        The session's plays, time-sorted, with ``_ts`` parsed.
    """
    from fyp.organize_datasets import COLLECTIONS_LABEL

    sid = str(session_row["session_id"])
    df = None
    plays_fp = _fingerprint(session_explorer.PLAYS_FILE)
    if plays_fp is not None:
        with _collection_plays_lock:
            entry = _COLLECTION_PLAYS_CACHE.get(collection_id)
        if entry is not None and entry[0] == plays_fp:
            df = entry[1]
        else:
            try:
                df = data_io.load_parquet_selective(
                    storage_location=session_explorer.ARTIFACT_LOCATION,
                    filename=session_explorer.PLAYS_FILE,
                    filters=[("collection_id", "==", collection_id)],
                )
            except Exception:
                df = None
            if df is not None and not df.empty:
                df = df.copy()
                df["_ts"] = pd.to_datetime(df["ts"], errors="coerce")
                with _collection_plays_lock:
                    # Evict oldest insertions beyond the cap (plain dict keeps
                    # insertion order; hit-recency doesn't matter much at 8).
                    while len(_COLLECTION_PLAYS_CACHE) >= _COLLECTION_PLAYS_MAX:
                        _COLLECTION_PLAYS_CACHE.pop(
                            next(iter(_COLLECTION_PLAYS_CACHE)))
                    _COLLECTION_PLAYS_CACHE[collection_id] = (plays_fp, df)
            else:
                df = None
    if df is None:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
            columns=["item_id", "local_timestamp", "play_duration",
                     "session_id", "source_platform"],
            filters=[("collection_id", "==", collection_id),
                     ("activity_type", "==", "play")],
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=["item_id", "_ts", "play_duration",
                                         "source_platform"])
        df = df.copy()
        df["_ts"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    df = df.dropna(subset=["_ts"])
    if sid.startswith("na_"):
        start = pd.Timestamp(str(session_row["start_ts"]))
        end = pd.Timestamp(str(session_row["end_ts"]))
        df = df[df["session_id"].isna() & (df["_ts"] >= start) & (df["_ts"] <= end)]
    else:
        df = df[df["session_id"].astype("string") == sid]
    df["item_id"] = df["item_id"].astype("string")
    return df.sort_values("_ts")




def _artifact_frame(filename: str, cache: dict, lock: threading.Lock) -> pd.DataFrame | None:
    """Load (and fingerprint-cache) one whole detail artifact frame.

    The episodes/windows artifacts are small (KBs–MBs) but their row-group
    stats span the whole collection-id space, so the old per-request pushdown
    read decoded the entire file anyway — holding the frame and slicing in
    pandas turns every detail click's read into a dict lookup.
    """
    key = _fingerprint(filename)
    if key is None:
        return None
    if cache["df"] is not None and cache["fingerprint"] == key:
        return cache["df"]
    with lock:
        if cache["df"] is not None and cache["fingerprint"] == key:
            return cache["df"]
        df = data_io.load_parquet_selective(
            storage_location=session_explorer.ARTIFACT_LOCATION, filename=filename)
        if df is None:
            return None
        df = df.copy()
        df["collection_id"] = df["collection_id"].astype("string")
        df["session_id"] = df["session_id"].astype("string")
        cache.update({"df": df, "fingerprint": key})
    return cache["df"]




def _session_episodes(collection_id: str, session_id: str) -> list[dict]:
    """Load one session's episode rows (members reassembled per episode)."""
    frame = _artifact_frame(session_explorer.EPISODES_FILE, _EPISODES_CACHE,
                            _episodes_lock)
    if frame is None:
        return []
    df = frame[(frame["collection_id"] == collection_id)
               & (frame["session_id"] == session_id)]
    if df.empty:
        return []
    def _as_list(value):
        # List cells come back as numpy arrays / Arrow lists; a bare `or []`
        # trips the ambiguous-truth-value error.
        if value is None:
            return []
        try:
            if pd.isna(value):
                return []
        except (TypeError, ValueError):
            pass
        return list(value)

    episodes = []
    for _, row in df.sort_values("episode_idx").iterrows():
        members = []
        ids = _as_list(row["member_item_ids"])
        ts = _as_list(row["member_ts"])
        dwell = _as_list(row["member_dwell_s"])
        roll = _as_list(row["member_rolling_cosdist"])
        for i, iid in enumerate(ids):
            members.append({
                "item_id": str(iid),
                "ts": ts[i] if i < len(ts) else None,
                "dwell_s": _clean(dwell[i]) if i < len(dwell) else None,
                "rolling_cosdist": _clean(roll[i]) if i < len(roll) else None,
            })
        ep = {col: _clean(row.get(col)) for col in (
            "episode_idx", "start_ts", "end_ts", "duration_min", "n_plays",
            "n_distinct", "repeat_rate", "n_interleaved", "n_skipped",
            "focus", "diameter",
            "step_mean", "straightness", "direction_p", "spectral_entropy_bits",
            "effective_rank", "dominant_niche", "dominant_niche_share",
            "n_niches", "n_authors", "dominant_author_share", "advertising",
            "advertising_share", "mean_political", "mean_sensitivity",
        )}
        ep["members"] = members
        episodes.append(ep)
    return episodes




def _session_windows(collection_id: str, session_id: str) -> list[dict]:
    """Load one session's low-entropy-window rows (members reassembled)."""
    frame = _artifact_frame(session_explorer.WINDOWS_FILE, _WINDOWS_CACHE,
                            _windows_lock)
    if frame is None:
        return []
    df = frame[(frame["collection_id"] == collection_id)
               & (frame["session_id"] == session_id)]
    if df.empty:
        return []

    def _as_list(value):
        if value is None:
            return []
        try:
            if pd.isna(value):
                return []
        except (TypeError, ValueError):
            pass
        return list(value)

    windows = []
    for _, row in df.sort_values("window_idx").iterrows():
        ids = _as_list(row["member_item_ids"])
        ts = _as_list(row["member_ts"])
        dwell = _as_list(row["member_dwell_s"])
        members = [{
            "item_id": str(iid),
            "ts": ts[i] if i < len(ts) else None,
            "dwell_s": _clean(dwell[i]) if i < len(dwell) else None,
        } for i, iid in enumerate(ids)]
        w = {col: _clean(row.get(col)) for col in (
            "window_idx", "start_ts", "end_ts", "duration_min", "n_distinct",
            "mean_cosdist", "entropy_norm", "dominant_niche",
        )}
        w["members"] = members
        windows.append(w)
    return windows




def _episode_vmax() -> pd.DataFrame | None:
    """Per-binge maxima of the numeric video variables (one row per episode).

    Backs the varmax filter's "binges only" scope: a session passes when at
    least ONE of its binges' maxima falls in the requested range, so the
    frame keeps episodes as rows (``collection_id``/``session_id`` + one max
    column per variable, incl. per-play ``dwell_s`` from the artifact's own
    member lists). Live-computed from the current ``video_map`` — same source
    as the trend scan — and cached on the episodes + map fingerprints; the
    result is tiny (episodes × variables). None when no episodes artifact
    exists yet.
    """
    ep_fp = _fingerprint(session_explorer.EPISODES_FILE)
    if ep_fp is None:
        return None
    map_fp = _fingerprint("video_map.parquet", location=embeddings.STORE_LOCATION)
    key = (ep_fp, map_fp)
    if _EPVMAX_CACHE["df"] is not None and _EPVMAX_CACHE["key"] == key:
        return _EPVMAX_CACHE["df"]
    with _epvmax_lock:
        if _EPVMAX_CACHE["df"] is not None and _EPVMAX_CACHE["key"] == key:
            return _EPVMAX_CACHE["df"]
        frame = _artifact_frame(session_explorer.EPISODES_FILE,
                                _EPISODES_CACHE, _episodes_lock)
        if frame is None:
            return None
        exploded = pd.DataFrame({
            "collection_id": frame["collection_id"],
            "session_id": frame["session_id"],
            "item_id": frame["member_item_ids"],
            "dwell_s": frame["member_dwell_s"],
        })
        exploded["_eid"] = np.arange(len(exploded))
        exploded = exploded.explode(["item_id", "dwell_s"], ignore_index=True)
        exploded["item_id"] = exploded["item_id"].astype("string")
        exploded["dwell_s"] = pd.to_numeric(exploded["dwell_s"], errors="coerce")
        feat = _trend_frame(set(exploded["item_id"].dropna()))
        if not feat.empty:
            exploded = exploded.join(feat, on="item_id")
        value_cols = ["dwell_s"] + [c for c in feat.columns]
        agg = exploded.groupby("_eid")[value_cols].max()
        out = (frame[["collection_id", "session_id"]].reset_index(drop=True)
               .join(agg))
        _EPVMAX_CACHE.update({"key": key, "df": out})
    return _EPVMAX_CACHE["df"]




@sessions_bp.route('/api/sessions/detail', methods=['GET'])
@permission_required('tab.sessions')
def api_sessions_detail():
    """One session's full play sequence + focus episodes + per-item context.

    Query params: ``study``, ``collection_id``, ``session_id`` (all required).
    The session must belong to the (accessible) study on both scoping axes —
    its collection AND the study's date window — so a bookmarked link into a
    session the study no longer contains is refused rather than rendered. Each
    play carries enrichment flags and a ``streamable`` verdict — an item is
    streamable when it appears in the study's viewer frame AND its media was
    downloaded, which is exactly what the ``/api/video/<study>/<item_id>``
    gate will accept.
    """
    from .api_viewer_routes import _study_item_ids

    study = (request.args.get('study') or '').strip()
    collection_id = (request.args.get('collection_id') or '').strip()
    session_id = (request.args.get('session_id') or '').strip()
    if not study or not collection_id or not session_id:
        return jsonify({"error": "study, collection_id and session_id are required"}), 400
    denied = study_access_error(study)
    if denied is not None:
        return denied
    if collection_id not in _study_collection_ids(study):
        return jsonify({"error": "Collection not found in this study"}), 403

    index = _load_index()
    if index is None:
        return jsonify({"error": "The sessions index has not been built yet."}), 404
    match = index[(index["collection_id"] == collection_id)
                  & (index["session_id"] == session_id)]
    # The other scoping axis: a session the collection recorded outside the
    # study's date window is not this study's session.
    if not match.empty:
        match = match[_in_study_window(match, study).to_numpy()]
    if match.empty:
        return jsonify({"error": "Session not found"}), 404
    session_row = match.iloc[0]

    plays = _session_plays(collection_id, session_row)
    episodes = _session_episodes(collection_id, session_id)
    windows = _session_windows(collection_id, session_id)
    feat = _features()
    flags = _flag_sets()
    study_ids = _study_item_ids(study) or frozenset()
    session_item_ids = {str(i) for i in plays["item_id"]}
    embedded_ids = _embedded_ids(session_item_ids, flags)
    # A plays artifact built with baked-in text answers story/desc/hashtags
    # directly; only the fallback path (activity file / pre-upgrade artifact)
    # still needs the per-request pushdown reads — which decode the whole
    # text column of the corpus parquets, the tab's dominant per-click cost.
    if "story" in plays.columns:
        stories, scrape_text = _play_text_maps(plays)
    else:
        stories = _story_map(session_item_ids)
        scrape_text = _scrape_text_map(session_item_ids)

    # A play belongs to an episode when its timestamp falls inside the
    # episode's span and its item is one of the episode's members.
    ep_spans = [
        (ep["episode_idx"], pd.Timestamp(ep["start_ts"]), pd.Timestamp(ep["end_ts"]),
         {m["item_id"] for m in ep["members"]})
        for ep in episodes
    ]

    play_rows = []
    for seq, (_, row) in enumerate(plays.iterrows()):
        iid = str(row["item_id"])
        ts = row["_ts"]
        f = feat.loc[iid] if iid in feat.index else None
        episode_idx = None
        for eidx, e_start, e_end, e_members in ep_spans:
            if e_start <= ts <= e_end and iid in e_members:
                episode_idx = eidx
                break
        story = stories.get(iid) or (None if f is None else _clean(f.get("story"))) or None
        if isinstance(story, str) and len(story) > _STORY_CAP:
            story = story[:_STORY_CAP] + "…"
        text = scrape_text.get(iid) or {}
        desc = text.get("desc")
        if isinstance(desc, str) and len(desc) > _STORY_CAP:
            desc = desc[:_STORY_CAP] + "…"
        play_rows.append({
            "seq": seq,
            "item_id": iid,
            "ts": ts.isoformat(),
            "dwell_s": _clean(row.get("play_duration")),
            "duration_s": None if f is None else _clean(f.get("duration")),
            "platform": _clean(row.get("source_platform")),
            "annotated": iid in flags["annotated"],
            "embedded": iid in embedded_ids,
            "streamable": (iid in study_ids) and (iid in flags["downloaded"]),
            "niche_name": None if f is None else _clean(f.get("niche_name")),
            "category": None if f is None else _clean(f.get("category")),
            "story": story,
            "desc": desc,
            "hashtags": text.get("hashtags"),
            "author": None if f is None else _clean(f.get("author")),
            "political_score": None if f is None else _clean(f.get("political_score")),
            "sensitivity_score": None if f is None else _clean(f.get("sensitivity_score")),
            "episode_idx": episode_idx,
        })

    # Distances of the just-outside context plays to each binge/sequence's
    # member centroid — the "why wasn't this one included" signal. Best-effort:
    # a missing dense store simply leaves the payload without them.
    try:
        _attach_context_distances(episodes + windows, play_rows, _context_plays())
    except Exception:
        pass

    # Per-run creator counts and the within-binge trend scan, both computed
    # here rather than baked into the artifact: they need no embedding
    # vectors, so they stay live and a change needs no rebuild.
    trend_feat = _trend_frame(session_item_ids)
    min_n = _trend_min_videos()
    for ep in episodes:
        ids = [m["item_id"] for m in ep["members"]]
        ep["creators"] = _creator_count(ids, feat)
        ep["trend_scan"] = _scan_trend(ep["members"], trend_feat, min_n)
    for w in windows:
        w["creators"] = _creator_count([m["item_id"] for m in w["members"]], feat)

    # Session-level observed min/max of the same variables the binge cards
    # show — live-computed from the current video_map, so it can differ
    # slightly from the index's baked ``vmax_``/``vmin_`` columns after a map
    # rebuild (both are honest; they describe different build moments).
    session_series: dict[str, np.ndarray] = {
        col: trend_feat[col].to_numpy(dtype=float) for col in trend_feat.columns
    } if not trend_feat.empty else {}
    dwell_vals = pd.to_numeric(plays["play_duration"], errors="coerce")
    session_series["dwell_s"] = dwell_vals.to_numpy(dtype=float)
    session_ranges = _min_max_ranges(session_series)

    # Per-play values of the same numeric variables, aligned with ``plays``
    # order — the session line plot's data. (``dwell_s`` already rides on each
    # play row.) Variables with no finite value in this session are omitted.
    play_variables: dict[str, list] = {}
    if not trend_feat.empty and play_rows:
        aligned = trend_feat.reindex([p["item_id"] for p in play_rows])
        for col in trend_feat.columns:
            vals = aligned[col].to_numpy(dtype=float)
            if np.isfinite(vals).any():
                play_variables[col] = [
                    round(float(v), 4) if np.isfinite(v) else None for v in vals]

    display = load_display_id_map()
    session = {col: _clean(session_row.get(col)) for col in _OVERVIEW_COLS}
    session["collection_label"] = display.get(collection_id, collection_id)
    return jsonify({
        "session": session,
        "plays": play_rows,
        "episodes": episodes,
        "windows": windows,
        "session_ranges": session_ranges,
        "play_variables": play_variables,
        "params": _display_params(_load_meta()),
    })




@sessions_bp.route('/api/sessions/status', methods=['GET'])
@permission_required('tab.sessions')
def api_sessions_status():
    """Lightweight freshness signal for the Sessions tab.

    Reports artifact existence/provenance, whether the ``sessions_refresh`` (or
    upstream embeddings) worker is currently running, and whether the artifact
    was built by a different embedding model than the active backend's.
    """
    from web_interface.process_manager import load_process_stats
    from web_interface.routes.management_routes import _is_worker_running

    if is_cloud_run():
        load_process_stats()

    meta = _load_meta()
    exists = _fingerprint(session_explorer.SESSIONS_FILE) is not None
    active_model = None
    try:
        active_model = embeddings.active_embedding_backend().model_id()
    except Exception:
        pass
    built_model = (meta or {}).get("embedding_model")
    model_mismatch = bool(built_model) and bool(active_model) and built_model != active_model

    return jsonify({
        "artifact_exists": bool(exists),
        "built_at": (meta or {}).get("built_at"),
        "meta": meta,
        "active_embedding_model": active_model,
        "model_mismatch": model_mismatch,
        "refresh_running": _is_worker_running("sessions_refresh"),
        "embeddings_updating": _is_worker_running("embeddings_refresh"),
    })
