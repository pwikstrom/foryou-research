"""Sessions tab API: session-quality overview + focused-episode detail.

Serves the artifacts built by the ``sessions_refresh`` worker
(:mod:`fyp.analysis.session_explorer`): a filterable per-session quality/focus
index, a per-session detail payload (the full play sequence + detected focus
episodes with their ordered members), and a lightweight freshness/status
signal. The artifacts are global (all collections); every request is scoped to
the caller's study — a session is only visible when its collection is one the
requested, accessible study actually contains (see :func:`_study_collection_ids`:
selected AND present in the study's built frame).

No embedding vectors are ever touched here: all entropy/focus numbers were
precomputed into the artifacts, and per-item flags come from cheap id-set
membership checks.
"""

import itertools
import time
from functools import lru_cache

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request

import fyp.data_io as data_io
import fyp.embeddings as embeddings
from fyp.analysis import session_explorer
from fyp.fyp_config import fyp_cf
from web_interface.data_service import (
    get_study_collections,
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

# Sort keys the overview accepts (anything else falls back to the focus rank).
_SORT_KEYS = {
    "min_window_cosdist", "min_window_entropy_norm", "duration_min", "n_plays",
    "n_distinct", "n_episodes", "episode_play_frac", "coverage_embedded",
    "start_ts", "total_watch_s", "n_directed_episodes",
}

# In-process caches, invalidated on the artifact file fingerprint (index /
# meta) or a short TTL (the enrichment id sets, which have no single file).
_INDEX_CACHE: dict = {"fingerprint": None, "df": None}
_DIRECTED_CACHE: dict = {"fingerprint": None, "cut": None, "counts": None}
_FLAGS_CACHE: dict = {"ts": 0.0, "model": None, "flags": None}
_FEAT_CACHE: dict = {"ts": 0.0, "df": None}
_FLAGS_TTL_S = 600.0
_FEAT_TTL_S = 600.0

# Story text is for card context only — cap it so a session with 100 plays
# doesn't ship 100 full transcripts.
_STORY_CAP = 400




def _fingerprint(filename: str) -> str | None:
    """Return a size:mtime fingerprint for a cache artifact, or None if absent."""
    fp = data_io.stat(
        storage_location=session_explorer.ARTIFACT_LOCATION, filename=filename,
    )
    return None if fp is None else f"{fp.get('size')}:{fp.get('mtime')}"




def _load_index() -> pd.DataFrame | None:
    """Load (and cache) the sessions index, or None when not built yet."""
    key = _fingerprint(session_explorer.SESSIONS_FILE)
    if key is None:
        return None
    if _INDEX_CACHE["df"] is None or _INDEX_CACHE["fingerprint"] != key:
        df = data_io.load_parquet_selective(
            storage_location=session_explorer.ARTIFACT_LOCATION,
            filename=session_explorer.SESSIONS_FILE,
        )
        if df is None:
            return None
        df = df.copy()
        df["collection_id"] = df["collection_id"].astype("string")
        df["session_id"] = df["session_id"].astype("string")
        _INDEX_CACHE["df"] = df
        _INDEX_CACHE["fingerprint"] = key
    return _INDEX_CACHE["df"]




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
    """Load the artifact provenance meta, or None when absent."""
    if not data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=session_explorer.META_FILE):
        return None
    meta = data_io.load_json(
        storage_location=session_explorer.ARTIFACT_LOCATION,
        filename=session_explorer.META_FILE,
    )
    return meta if isinstance(meta, dict) else None




def _flag_sets() -> dict:
    """Return cached per-item enrichment id sets for the active model.

    Used for the detail payload's per-play ``annotated`` / ``embedded`` /
    ``streamable`` flags. TTL-cached: the sets change only when enrichment
    workers run, and a stale flag merely mislabels a card until the TTL lapses.
    """
    try:
        model = embeddings.active_embedding_backend().model_id()
    except Exception:
        model = None
    now = time.monotonic()
    if (_FLAGS_CACHE["flags"] is not None and _FLAGS_CACHE["model"] == model
            and now - _FLAGS_CACHE["ts"] < _FLAGS_TTL_S):
        return _FLAGS_CACHE["flags"]
    flags = session_explorer.enrichment_id_sets(model) if model else {
        "scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()}
    _FLAGS_CACHE.update({"ts": now, "model": model, "flags": flags})
    return flags




def _features() -> pd.DataFrame:
    """Return the cached per-video feature frame (item_id-indexed).

    The whole-corpus ``video_map`` + scrape-author read is too heavy to repeat
    per detail request; features only change on a map rebuild, so a short TTL
    is plenty.
    """
    now = time.monotonic()
    if _FEAT_CACHE["df"] is not None and now - _FEAT_CACHE["ts"] < _FEAT_TTL_S:
        return _FEAT_CACHE["df"]
    try:
        df = session_explorer.load_video_features()
    except Exception:
        df = pd.DataFrame(columns=["niche_name", "category", "story",
                                   "political_score", "sensitivity_score",
                                   "advertising", "author"])
    _FEAT_CACHE.update({"ts": now, "df": df})
    return df




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
    try:
        df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION, filename="video_map.parquet",
            filters=[("item_id", "in", [str(i) for i in item_ids])],
        )
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty or "item_id" not in df.columns:
        return pd.DataFrame()
    numeric = [c for c in df.columns
               if c not in _TREND_EXCLUDE and pd.api.types.is_numeric_dtype(df[c])]
    if not numeric:
        return pd.DataFrame()
    out = df[["item_id"] + numeric].copy()
    out["item_id"] = out["item_id"].astype("string")
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.drop_duplicates("item_id").set_index("item_id")




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
        as "no trend exists".
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
           "trend": None}
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




@sessions_bp.route('/api/sessions/overview', methods=['GET'])
@permission_required('tab.sessions')
def api_sessions_overview():
    """Filterable, sortable session table scoped to one study.

    Query params: ``study`` (required), ``min_coverage`` (embedded coverage
    floor), ``min_emb_plays``, ``min_plays``, ``min_session_minutes``, ``sort``
    (one of the index metrics; default ``min_window_cosdist``), ``order``
    (``asc``/``desc``), ``limit``.

    ``min_plays``, ``min_session_minutes`` and ``min_coverage`` default to the
    admin-controlled session-list floors (Admin → Site Settings, seeded by
    ``[sessions]`` config); each query param is the per-request override, e.g.
    ``min_plays=0`` to see everything. Excluded sessions still count towards
    ``total_in_study``, so the caller can always say how many the floors
    removed.
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
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric filter"}), 400
    sort = request.args.get('sort') or "min_window_cosdist"
    if sort not in _SORT_KEYS:
        sort = "min_window_cosdist"
    ascending = (request.args.get('order') or 'asc').lower() != 'desc'

    cids = _study_collection_ids(study)
    df = index[index["collection_id"].isin(cids)]
    total_in_study = int(len(df))
    # The three admin-controlled list floors are applied as one block, so the
    # client can report a single "N not listed" count it can reconcile with the
    # rows on screen; min_emb_plays stays a separate ad-hoc quality filter.
    df = df[
        (df["n_plays"].fillna(0) >= min_plays)
        & (df["duration_min"].fillna(0) >= min_minutes)
        & (df["coverage_embedded"].fillna(0) >= min_coverage)
    ]
    total_above_floors = int(len(df))
    df = df[df["n_embedded"].fillna(0) >= min_emb]
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
    df = df.sort_values(sort, ascending=ascending, na_position='last').head(limit)

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
    """Live-read one session's play rows from the consolidated activity file.

    The activity file is clustered by ``collection_id``, so the pushdown filter
    prunes row groups and the read is light enough for a live request (same
    pattern as :mod:`web_interface.semantic_trajectory`). Sessions synthesised
    for null ``session_id`` rows (keys ``na_<idx>``) are recovered by their
    time span instead.

    Args:
        collection_id: The session's collection.
        session_row: The session's row from the index artifact.

    Returns:
        The session's plays, time-sorted, with ``_ts`` parsed.
    """
    from fyp.organize_datasets import COLLECTIONS_LABEL

    sid = str(session_row["session_id"])
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["item_id", "local_timestamp", "play_duration",
                 "session_id", "source_platform"],
        filters=[("collection_id", "==", collection_id),
                 ("activity_type", "==", "play")],
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




def _session_episodes(collection_id: str, session_id: str) -> list[dict]:
    """Load one session's episode rows (members reassembled per episode)."""
    if not data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=session_explorer.EPISODES_FILE):
        return []
    df = data_io.load_parquet_selective(
        storage_location=session_explorer.ARTIFACT_LOCATION,
        filename=session_explorer.EPISODES_FILE,
        filters=[("collection_id", "==", collection_id),
                 ("session_id", "==", session_id)],
    )
    if df is None or df.empty:
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
    if not data_io.exists(storage_location=session_explorer.ARTIFACT_LOCATION,
                          filename=session_explorer.WINDOWS_FILE):
        return []
    df = data_io.load_parquet_selective(
        storage_location=session_explorer.ARTIFACT_LOCATION,
        filename=session_explorer.WINDOWS_FILE,
        filters=[("collection_id", "==", collection_id),
                 ("session_id", "==", session_id)],
    )
    if df is None or df.empty:
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




@sessions_bp.route('/api/sessions/detail', methods=['GET'])
@permission_required('tab.sessions')
def api_sessions_detail():
    """One session's full play sequence + focus episodes + per-item context.

    Query params: ``study``, ``collection_id``, ``session_id`` (all required).
    The collection must belong to the (accessible) study. Each play carries
    enrichment flags and a ``streamable`` verdict — an item is streamable when
    it appears in the study's viewer frame AND its media was downloaded, which
    is exactly what the ``/api/video/<study>/<item_id>`` gate will accept.
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
    if match.empty:
        return jsonify({"error": "Session not found"}), 404
    session_row = match.iloc[0]

    plays = _session_plays(collection_id, session_row)
    episodes = _session_episodes(collection_id, session_id)
    windows = _session_windows(collection_id, session_id)
    feat = _features()
    flags = _flag_sets()
    study_ids = _study_item_ids(study) or frozenset()
    stories = _story_map({str(i) for i in plays["item_id"]})

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
        play_rows.append({
            "seq": seq,
            "item_id": iid,
            "ts": ts.isoformat(),
            "dwell_s": _clean(row.get("play_duration")),
            "platform": _clean(row.get("source_platform")),
            "annotated": iid in flags["annotated"],
            "embedded": iid in flags["embedded"],
            "streamable": (iid in study_ids) and (iid in flags["downloaded"]),
            "niche_name": None if f is None else _clean(f.get("niche_name")),
            "category": None if f is None else _clean(f.get("category")),
            "story": story,
            "author": None if f is None else _clean(f.get("author")),
            "political_score": None if f is None else _clean(f.get("political_score")),
            "sensitivity_score": None if f is None else _clean(f.get("sensitivity_score")),
            "episode_idx": episode_idx,
        })

    # Per-run creator counts and the within-binge trend scan, both computed
    # here rather than baked into the artifact: they need no embedding
    # vectors, so they stay live and a change needs no rebuild.
    trend_feat = _trend_frame({str(i) for i in plays["item_id"]})
    min_n = _trend_min_videos()
    for ep in episodes:
        ids = [m["item_id"] for m in ep["members"]]
        ep["creators"] = _creator_count(ids, feat)
        ep["trend_scan"] = _scan_trend(ep["members"], trend_feat, min_n)
    for w in windows:
        w["creators"] = _creator_count([m["item_id"] for m in w["members"]], feat)

    display = load_display_id_map()
    session = {col: _clean(session_row.get(col)) for col in _OVERVIEW_COLS}
    session["collection_label"] = display.get(collection_id, collection_id)
    return jsonify({
        "session": session,
        "plays": play_rows,
        "episodes": episodes,
        "windows": windows,
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
