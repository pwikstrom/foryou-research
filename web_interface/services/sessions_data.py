"""The Sessions tab's data layer: artifact loading, caches, and study scoping.

Reads the artifacts the ``sessions_refresh`` worker builds
(:mod:`fyp.analysis.session_explorer`) — the per-session index, directed
counts, per-item flags and features, episodes, windows and per-collection
play rows — each behind a fingerprint-keyed cache, and scopes them to a
study (its collections, date window and sampled day-cells). The HTTP surface
is ``routes/api_sessions_routes.py``; the trend statistics are
:mod:`web_interface.services.sessions_stats`.
"""

import threading
import time

import numpy as np
import pandas as pd

import fyp.analysis.embeddings as embeddings
import fyp.analysis.sessions.inputs as sessions_inputs
import fyp.core.data_io as data_io
from fyp.analysis import embedding_store
from fyp.core.fyp_config import fyp_cf
from web_interface.services.study_data import (
    get_study_collections,
    get_study_date_window,
    get_study_frame_collections,
    get_study_selected_cells,
)

from . import sessions_stats

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

# Prefix of the per-variable session-extreme columns baked into the index
# (``vmax_<variable>`` / ``vmin_<variable>``; see sessions_publish.sessions_schema).
VARMAX_PREFIX = "vmax_"

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


def artifact_fingerprint(
    filename: str, location: str = sessions_inputs.ARTIFACT_LOCATION
) -> str | None:
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


def load_index() -> pd.DataFrame | None:
    """Load (and cache) the sessions index, or None when not built yet.

    The cached working frame deliberately EXCLUDES ``search_text`` — the blob
    is ~3/4 of the frame's RAM and only the ``q`` filter reads it, so it is
    held as a separate aligned Series (see :func:`search_blob`) and the
    row-mask copies the overview makes stay cheap. ``start_dt`` (parsed
    ``start_ts``) is added once here so no request re-parses 79k strings.
    """
    key = artifact_fingerprint(sessions_inputs.SESSIONS_FILE)
    if key is None:
        return None
    if _INDEX_CACHE["df"] is not None and _INDEX_CACHE["fingerprint"] == key:
        return _INDEX_CACHE["df"]
    with _index_lock:
        if _INDEX_CACHE["df"] is not None and _INDEX_CACHE["fingerprint"] == key:
            return _INDEX_CACHE["df"]
        df = data_io.load_parquet_selective(
            storage_location=sessions_inputs.ARTIFACT_LOCATION,
            filename=sessions_inputs.SESSIONS_FILE,
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


def search_blob(index: pd.DataFrame) -> pd.Series | None:
    """The index's ``search_text`` Series (row-aligned), or None when absent.

    The cached index holds the blob out-of-frame; a frame that still carries
    the column (an injected test frame) is served from it directly.
    """
    if "search_text" in index.columns:
        return index["search_text"].astype("string")
    if index is _INDEX_CACHE["df"]:
        return _INDEX_CACHE["search"]
    return None


def start_dt(df: pd.DataFrame) -> pd.Series:
    """Parsed ``start_ts`` — the pre-parsed column when present, else live."""
    if "_start_dt" in df.columns:
        return df["_start_dt"]
    return pd.to_datetime(df["start_ts"], errors="coerce")


def directed_counts() -> pd.Series | None:
    """Per-session count of DIRECTED binges, indexed by (collection_id, session_id).

    Read from the episodes artifact rather than a column on the session index:
    there are only a few hundred episodes corpus-wide, so aggregating them per
    request (fingerprint-cached) is cheaper than a schema change, and the
    threshold stays live.

    Returns None when the artifact predates ``direction_p`` — the caller must
    then report "not computed" rather than zero, which would read as "no
    session has a directed binge".
    """
    key = artifact_fingerprint(sessions_inputs.EPISODES_FILE)
    if key is None:
        return None
    cut = _drift_p()
    if (
        _DIRECTED_CACHE["counts"] is not None
        and _DIRECTED_CACHE["fingerprint"] == key
        and _DIRECTED_CACHE["cut"] == cut
    ):
        return _DIRECTED_CACHE["counts"]
    with _directed_lock:
        if (
            _DIRECTED_CACHE["counts"] is not None
            and _DIRECTED_CACHE["fingerprint"] == key
            and _DIRECTED_CACHE["cut"] == cut
        ):
            return _DIRECTED_CACHE["counts"]
        df = data_io.load_parquet_selective(
            storage_location=sessions_inputs.ARTIFACT_LOCATION,
            filename=sessions_inputs.EPISODES_FILE,
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


def load_meta() -> dict | None:
    """Load (and fingerprint-cache) the artifact provenance meta, or None."""
    key = artifact_fingerprint(sessions_inputs.META_FILE)
    if key is None:
        return None
    if _META_CACHE["fingerprint"] == key:
        return _META_CACHE["meta"]
    with _meta_lock:
        if _META_CACHE["fingerprint"] == key:
            return _META_CACHE["meta"]
        meta = data_io.load_json(
            storage_location=sessions_inputs.ARTIFACT_LOCATION,
            filename=sessions_inputs.META_FILE,
        )
        _META_CACHE.update(
            {
                "fingerprint": key,
                "meta": meta if isinstance(meta, dict) else None,
            }
        )
    return _META_CACHE["meta"]


def flags_cache_key(model: str | None) -> tuple:
    """Invalidation key for :func:`flag_sets`: the fingerprints of its sources.

    Scrapes parquet (scraped/downloaded), annotations parquet (annotated) and
    the model's dense-index parquet (the ``emb_index``). Each probe rides the
    15 s ``_STAT_CACHE``, so computing the key is a few cheap stats at most.
    """
    idx_fp = None
    if model:
        idx_fp = artifact_fingerprint(
            embedding_store._index_filename(model), location=embedding_store.STORE_LOCATION
        )
    return (
        model,
        artifact_fingerprint(embeddings.SCRAPES_FILE, location=embeddings.STORE_LOCATION),
        artifact_fingerprint(embeddings.ANNOTATIONS_FILE, location=embeddings.STORE_LOCATION),
        idx_fp,
    )


def flag_sets() -> dict:
    """Return cached per-item enrichment id sets for the active model.

    Used for the detail payload's per-play ``annotated`` / ``embedded`` /
    ``streamable`` flags. Fingerprint-cached on the source files (see
    :func:`flags_cache_key`): the sets change only when enrichment workers
    rewrite those files, and this rebuild is a corpus-scale read — a TTL made
    requests pay it on a schedule even when nothing had changed.

    ``embedded`` is deliberately left EMPTY here: filling it means scanning
    every embedding shard for ~1.35M ids to answer a few hundred membership
    tests per detail request. The dense sidecar's id → row index answers the
    same question from one small parquet — see :func:`embedded_ids`.
    """
    try:
        model = embeddings.active_embedding_backend().model_id()
    except Exception:
        model = None
    key = flags_cache_key(model)
    if _FLAGS_CACHE["flags"] is not None and _FLAGS_CACHE["key"] == key:
        return _FLAGS_CACHE["flags"]
    with _flags_lock:
        if _FLAGS_CACHE["flags"] is not None and _FLAGS_CACHE["key"] == key:
            return _FLAGS_CACHE["flags"]
        flags = (
            sessions_inputs.enrichment_id_sets(model, include_embedded=False)
            if model
            else {"scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()}
        )
        emb_index = None
        if model:
            try:
                emb_index = embedding_store.load_index(model)
            except Exception:
                emb_index = None
        _FLAGS_CACHE.update({"key": key, "model": model, "flags": flags, "emb_index": emb_index})
    return _FLAGS_CACHE["flags"]


def embedded_ids(item_ids: set[str], flags: dict) -> set[str]:
    """Which of ``item_ids`` have a dense embedding.

    A flag set that already carries ``embedded`` ids (an injected one) is
    honoured; the production cache leaves it empty and the sidecar's id → row
    index answers instead — cached by :func:`flag_sets` (same TTL, same
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


def attach_context_distances(seqs: list[dict], play_rows: list[dict], n_ctx: int) -> None:
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
            ctx.extend(play_rows[max(0, first - n_ctx) : first])
        if first is not None and last is not None and last > first:
            member_keys = {(m["item_id"], m["ts"]) for m in members}
            ctx.extend(
                p for p in play_rows[first : last + 1] if (p["item_id"], p["ts"]) not in member_keys
            )
        if last is not None and last + 1 < len(play_rows):
            ctx.extend(play_rows[last + 1 : last + 1 + n_ctx])
        if not ctx:
            continue
        contexts.append((seq, ctx))
        need_ids.update(m["item_id"] for m in members)
        need_ids.update(p["item_id"] for p in ctx)
    if not contexts:
        return

    try:
        id2row, block = sessions_inputs.load_directional_block(
            model, sorted(need_ids), mean, index=index
        )
    except Exception:
        return
    if not id2row:
        return
    for seq, ctx in contexts:
        rows = [id2row[m["item_id"]] for m in seq["members"] if m["item_id"] in id2row]
        if not rows:
            continue
        centroid = block[rows].mean(axis=0)
        dists = {}
        for p in ctx:
            row = id2row.get(p["item_id"])
            if row is None:
                continue
            dists[f"{p['item_id']}@{p['ts']}"] = round(1.0 - float(block[row] @ centroid), 4)
        if dists:
            seq["context_distances"] = dists


def features() -> pd.DataFrame:
    """Return the cached per-video feature frame (item_id-indexed).

    The whole-corpus ``video_map`` + scrape-author read is too heavy to repeat
    per detail request; fingerprint-cached on its two source files, so the
    rebuild only ever happens after a map rebuild or a consolidation — never
    on a timer.
    """
    key = (
        artifact_fingerprint("video_map.parquet", location=embeddings.STORE_LOCATION),
        artifact_fingerprint(embeddings.SCRAPES_FILE, location=embeddings.STORE_LOCATION),
    )
    if _FEAT_CACHE["df"] is not None and _FEAT_CACHE["key"] == key:
        return _FEAT_CACHE["df"]
    with _feat_lock:
        if _FEAT_CACHE["df"] is not None and _FEAT_CACHE["key"] == key:
            return _FEAT_CACHE["df"]
        try:
            # Corpus-wide, so no scrape text (desc/hashtags are hundreds of MB at
            # that scale) — the detail endpoint reads those per session instead.
            # The trend-scan numeric columns ride along so trend_frame can
            # slice this cached frame instead of a per-click pushdown read of
            # video_map.parquet (whose item_id filters never prune row groups
            # — that read was seconds of every detail click).
            try:
                extra_cols = sessions_inputs.trend_numeric_columns()
            except Exception:
                extra_cols = None
            df = sessions_inputs.load_video_features(extra_map_cols=extra_cols)
            _FEAT_CACHE["trend_cols"] = extra_cols or []
        except Exception:
            df = pd.DataFrame(
                columns=[
                    "niche_name",
                    "category",
                    "story",
                    "political_score",
                    "sensitivity_score",
                    "advertising",
                    "author",
                    "duration",
                ]
            )
        _FEAT_CACHE.update({"key": key, "df": df})
    return _FEAT_CACHE["df"]


def story_map(item_ids: set[str]) -> dict[str, str]:
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
        s = clean(story)
        if s:
            out[str(iid)] = str(s)
    return out


def scrape_text_map(item_ids: set[str]) -> dict[str, dict]:
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
        available = (
            data_io.get_parquet_columns(
                storage_location=embeddings.STORE_LOCATION, filename=embeddings.SCRAPES_FILE
            )
            or []
        )
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


def play_text_maps(plays: pd.DataFrame) -> tuple[dict[str, str], dict[str, dict]]:
    """Story/scrape-text maps from a plays frame with baked-in text columns.

    The plays artifact stores per-item ``story``/``desc``/``hashtags``
    (already capped at build time — see ``sessions_publish.PLAY_TEXT_CAP``),
    so a detail request needs no corpus-parquet reads at all. Returns the
    same shapes as :func:`story_map` and :func:`scrape_text_map`.
    """
    stories: dict[str, str] = {}
    scrape_text: dict[str, dict] = {}
    hashtags_col = plays["hashtags"] if "hashtags" in plays.columns else [None] * len(plays)
    desc_col = plays["desc"] if "desc" in plays.columns else [None] * len(plays)
    for iid, story, desc, hashtags in zip(
        plays["item_id"].astype("string"), plays["story"], desc_col, hashtags_col
    ):
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
    array / Python list rather than a scalar — ``clean`` would choke on it.
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


def trend_frame(item_ids: set[str]) -> pd.DataFrame:
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
    feat = features()
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
    numeric = [
        c
        for c in sub.columns
        if c in allowed and c not in _TREND_EXCLUDE and pd.api.types.is_numeric_dtype(sub[c])
    ]
    if not numeric:
        return pd.DataFrame()
    out = sub[numeric].copy()
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[~out.index.duplicated()]


def study_collection_ids(study: str) -> set[str]:
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
    selected = {
        str(d.get("collection_id")) for d in get_study_collections(study) if d.get("collection_id")
    }
    in_frame = get_study_frame_collections(study)
    if in_frame is None:
        return selected
    return selected & in_frame


def in_study_window(df: pd.DataFrame, study: str) -> pd.Series:
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
    ts = start_dt(df)
    # An unparseable start cannot be placed in the window; NaT compares False
    # on both sides, which drops it — the honest answer for a row that has no
    # date at all.
    return (ts >= start) & (ts < end_bound)


def in_study_cells(df: pd.DataFrame, study: str) -> pd.Series:
    """Mask of the index rows whose (collection, start day) the study admitted.

    The third half of study scoping, and a no-op (all True) unless the study
    was built with day sampling. The study builder samples whole viewing
    sessions within a sampled day, so a session belongs to a sampled study
    exactly when its start day is one of its collection's admitted cells —
    the same start-day rule :func:`in_study_window` uses, so the two axes
    never disagree about which day a session is on.
    """
    cells = get_study_selected_cells(study)
    if cells is None:
        return pd.Series(True, index=df.index)
    # Vectorised day floor rather than per-row string formatting: this runs
    # over the whole index on every list request.
    days = start_dt(df).dt.floor("D")
    cids = df["collection_id"].astype(str)
    keys = pd.MultiIndex.from_arrays([cids, days])
    admitted = pd.MultiIndex.from_tuples(
        [(cid, pd.Timestamp(day)) for cid, ds in cells.items() for day in ds] or [("", pd.NaT)]
    )
    return pd.Series(keys.isin(admitted), index=df.index)


def cells_signature(study: str):
    """Hashable fingerprint of the study's admitted cells (None when unsampled)."""
    cells = get_study_selected_cells(study)
    if cells is None:
        return None
    return hash(frozenset((cid, day) for cid, ds in cells.items() for day in ds))


def _sessions_config() -> dict:
    """The live ``[sessions]`` config block (always a dict)."""
    cfg = fyp_cf.get("sessions", {})
    return cfg if isinstance(cfg, dict) else {}


def context_plays() -> int:
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


def trend_min_videos() -> int:
    """``[sessions] trend_min_videos`` — smallest scannable binge, floored at 5.

    Below 5 members the exact permutation test cannot reach any conventional
    threshold at all, so a smaller value would not widen coverage, only
    misrepresent what was tested.
    """
    try:
        return max(int(_sessions_config().get("trend_min_videos", DEFAULT_TREND_MIN_VIDEOS)), 5)
    except (TypeError, ValueError):
        return DEFAULT_TREND_MIN_VIDEOS


def session_floors() -> dict:
    """The session-list floors, in the units this endpoint filters on.

    Applied at query time, so an admin edit takes effect on the next request
    with no artifact rebuild — the index itself stays complete and every
    excluded session is still counted in ``total_in_study``.

    ``min_coverage`` is converted from the admin-facing percentage to the 0-1
    fraction ``coverage_embedded`` is stored as.
    """
    from web_interface.services.admin_settings import get_session_floors

    floors = get_session_floors()
    return {
        "min_plays": int(floors["sessions_min_plays"]),
        "min_session_minutes": float(floors["sessions_min_minutes"]),
        "min_coverage": float(floors["sessions_min_coverage_pct"]) / 100.0,
    }


def display_params(meta: dict | None) -> dict:
    """The limits the tab must describe to the researcher.

    Segmentation/window values come from the artifact's own provenance — they
    describe the binges and sequences actually on screen, which a later config
    edit does not retroactively change — and fall back to the live config only
    for keys an older artifact never recorded. ``context_plays`` is a pure
    display knob, so it is always live.
    """
    params = dict(sessions_inputs.default_params())
    built = (meta or {}).get("params")
    if isinstance(built, dict):
        params.update({k: v for k, v in built.items() if k in params})
    params["context_plays"] = context_plays()
    params["drift_p"] = _drift_p()
    params["trend_min_videos"] = trend_min_videos()
    return params


def clean(value):
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
    ts = start_dt(df) if "start_ts" in df.columns else pd.Series(dtype="datetime64[ns]")
    out["start_date"] = [str(ts.min().date()), str(ts.max().date())] if ts.notna().any() else None
    for col in ("duration_min", "n_plays", "coverage_embedded", "min_window_cosdist", "n_episodes"):
        vals = (
            pd.to_numeric(df[col], errors="coerce")
            if col in df.columns
            else pd.Series(dtype="float64")
        )
        out[col] = [float(vals.min()), float(vals.max())] if vals.notna().any() else None
    # Per-variable session-max bounds for the variable-picker filter. None
    # (not {}) when the artifact predates the vmax_ columns, so the client can
    # tell "no variables usable" from "filter unavailable — rebuild".
    vmax_cols = [c for c in df.columns if c.startswith(VARMAX_PREFIX)]
    if vmax_cols:
        var_max: dict = {}
        for col in vmax_cols:
            vals = pd.to_numeric(df[col], errors="coerce")
            if vals.notna().any():
                var_max[col[len(VARMAX_PREFIX) :]] = [float(vals.min()), float(vals.max())]
        out["var_max"] = var_max
        out["var_labels"] = {
            name: ("Dwell (s)" if name == "dwell_s" else sessions_stats.variable_label(name))
            for name in var_max
        }
    else:
        out["var_max"] = None
        out["var_labels"] = None
    return out


def cached_filter_ranges(index: pd.DataFrame, pop: np.ndarray, study: str, sig: tuple) -> dict:
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


def session_plays(collection_id: str, session_row: pd.Series) -> pd.DataFrame:
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
    from fyp.analysis.organize_datasets import COLLECTIONS_LABEL

    sid = str(session_row["session_id"])
    df = None
    plays_fp = artifact_fingerprint(sessions_inputs.PLAYS_FILE)
    if plays_fp is not None:
        with _collection_plays_lock:
            entry = _COLLECTION_PLAYS_CACHE.get(collection_id)
        if entry is not None and entry[0] == plays_fp:
            df = entry[1]
        else:
            try:
                df = data_io.load_parquet_selective(
                    storage_location=sessions_inputs.ARTIFACT_LOCATION,
                    filename=sessions_inputs.PLAYS_FILE,
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
                        _COLLECTION_PLAYS_CACHE.pop(next(iter(_COLLECTION_PLAYS_CACHE)))
                    _COLLECTION_PLAYS_CACHE[collection_id] = (plays_fp, df)
            else:
                df = None
    if df is None:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
            columns=[
                "item_id",
                "local_timestamp",
                "play_duration",
                "session_id",
                "source_platform",
            ],
            filters=[("collection_id", "==", collection_id), ("activity_type", "==", "play")],
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=["item_id", "_ts", "play_duration", "source_platform"])
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
    key = artifact_fingerprint(filename)
    if key is None:
        return None
    if cache["df"] is not None and cache["fingerprint"] == key:
        return cache["df"]
    with lock:
        if cache["df"] is not None and cache["fingerprint"] == key:
            return cache["df"]
        df = data_io.load_parquet_selective(
            storage_location=sessions_inputs.ARTIFACT_LOCATION, filename=filename
        )
        if df is None:
            return None
        df = df.copy()
        df["collection_id"] = df["collection_id"].astype("string")
        df["session_id"] = df["session_id"].astype("string")
        cache.update({"df": df, "fingerprint": key})
    return cache["df"]


def session_episodes(collection_id: str, session_id: str) -> list[dict]:
    """Load one session's episode rows (members reassembled per episode)."""
    frame = _artifact_frame(sessions_inputs.EPISODES_FILE, _EPISODES_CACHE, _episodes_lock)
    if frame is None:
        return []
    df = frame[(frame["collection_id"] == collection_id) & (frame["session_id"] == session_id)]
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
            members.append(
                {
                    "item_id": str(iid),
                    "ts": ts[i] if i < len(ts) else None,
                    "dwell_s": clean(dwell[i]) if i < len(dwell) else None,
                    "rolling_cosdist": clean(roll[i]) if i < len(roll) else None,
                }
            )
        ep = {
            col: clean(row.get(col))
            for col in (
                "episode_idx",
                "start_ts",
                "end_ts",
                "duration_min",
                "n_plays",
                "n_distinct",
                "repeat_rate",
                "n_interleaved",
                "n_skipped",
                "focus",
                "diameter",
                "step_mean",
                "straightness",
                "direction_p",
                "spectral_entropy_bits",
                "effective_rank",
                "dominant_niche",
                "dominant_niche_share",
                "n_niches",
                "n_authors",
                "dominant_author_share",
                "advertising",
                "advertising_share",
                "mean_political",
                "mean_sensitivity",
            )
        }
        ep["members"] = members
        episodes.append(ep)
    return episodes


def session_windows(collection_id: str, session_id: str) -> list[dict]:
    """Load one session's low-entropy-window rows (members reassembled)."""
    frame = _artifact_frame(sessions_inputs.WINDOWS_FILE, _WINDOWS_CACHE, _windows_lock)
    if frame is None:
        return []
    df = frame[(frame["collection_id"] == collection_id) & (frame["session_id"] == session_id)]
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
        members = [
            {
                "item_id": str(iid),
                "ts": ts[i] if i < len(ts) else None,
                "dwell_s": clean(dwell[i]) if i < len(dwell) else None,
            }
            for i, iid in enumerate(ids)
        ]
        w = {
            col: clean(row.get(col))
            for col in (
                "window_idx",
                "start_ts",
                "end_ts",
                "duration_min",
                "n_distinct",
                "mean_cosdist",
                "entropy_norm",
                "dominant_niche",
            )
        }
        w["members"] = members
        windows.append(w)
    return windows


def episode_vmax() -> pd.DataFrame | None:
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
    ep_fp = artifact_fingerprint(sessions_inputs.EPISODES_FILE)
    if ep_fp is None:
        return None
    map_fp = artifact_fingerprint("video_map.parquet", location=embeddings.STORE_LOCATION)
    key = (ep_fp, map_fp)
    if _EPVMAX_CACHE["df"] is not None and _EPVMAX_CACHE["key"] == key:
        return _EPVMAX_CACHE["df"]
    with _epvmax_lock:
        if _EPVMAX_CACHE["df"] is not None and _EPVMAX_CACHE["key"] == key:
            return _EPVMAX_CACHE["df"]
        frame = _artifact_frame(sessions_inputs.EPISODES_FILE, _EPISODES_CACHE, _episodes_lock)
        if frame is None:
            return None
        exploded = pd.DataFrame(
            {
                "collection_id": frame["collection_id"],
                "session_id": frame["session_id"],
                "item_id": frame["member_item_ids"],
                "dwell_s": frame["member_dwell_s"],
            }
        )
        exploded["_eid"] = np.arange(len(exploded))
        exploded = exploded.explode(["item_id", "dwell_s"], ignore_index=True)
        exploded["item_id"] = exploded["item_id"].astype("string")
        exploded["dwell_s"] = pd.to_numeric(exploded["dwell_s"], errors="coerce")
        feat = trend_frame(set(exploded["item_id"].dropna()))
        if not feat.empty:
            exploded = exploded.join(feat, on="item_id")
        value_cols = ["dwell_s"] + list(feat.columns)
        agg = exploded.groupby("_eid")[value_cols].max()
        out = frame[["collection_id", "session_id"]].reset_index(drop=True).join(agg)
        _EPVMAX_CACHE.update({"key": key, "df": out})
    return _EPVMAX_CACHE["df"]
