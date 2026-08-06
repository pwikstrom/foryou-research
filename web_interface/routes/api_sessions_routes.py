"""Sessions tab API: session-quality overview + focused-episode detail.

Serves the artifacts built by the ``sessions_refresh`` worker
(:mod:`fyp.analysis.session_explorer`): a filterable per-session quality/focus
index, a per-session detail payload (the full play sequence + detected focus
episodes with their ordered members), and a lightweight freshness/status
signal. The artifacts are global (all collections); every request is scoped to
the caller's study — a session is only visible when its collection belongs to
the requested, accessible study.

No embedding vectors are ever touched here: all entropy/focus numbers were
precomputed into the artifacts, and per-item flags come from cheap id-set
membership checks.
"""

import time

import pandas as pd
from flask import Blueprint, jsonify, request

import fyp.data_io as data_io
import fyp.embeddings as embeddings
from fyp.analysis import session_explorer
from web_interface.data_service import get_study_collections, load_display_id_map

from ._access import study_access_error
from ..permissions import permission_required
from ..task_status import is_cloud_run

sessions_bp = Blueprint('sessions_bp', __name__)

# Default quality floors offered by the UI (query-time only — the artifact
# itself is unfiltered). Derived from the embedding-entropy study's donor
# floors, adapted to the single-session grain.
DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_MIN_EMB_PLAYS = 5
OVERVIEW_LIMIT_DEFAULT = 200
OVERVIEW_LIMIT_MAX = 1000

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
    "start_ts", "total_watch_s",
}

# In-process caches, invalidated on the artifact file fingerprint (index /
# meta) or a short TTL (the enrichment id sets, which have no single file).
_INDEX_CACHE: dict = {"fingerprint": None, "df": None}
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




def _study_collection_ids(study: str) -> set[str]:
    """Collection ids belonging to ``study`` (already access-checked)."""
    return {str(d.get("collection_id")) for d in get_study_collections(study)
            if d.get("collection_id")}




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
    floor), ``min_emb_plays``, ``min_plays``, ``sort`` (one of the index
    metrics; default ``min_window_cosdist``), ``order`` (``asc``/``desc``),
    ``limit``.
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

    try:
        min_coverage = float(request.args.get('min_coverage', DEFAULT_MIN_COVERAGE))
        min_emb = int(request.args.get('min_emb_plays', DEFAULT_MIN_EMB_PLAYS))
        min_plays = int(request.args.get('min_plays', 0))
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
    df = df[
        (df["coverage_embedded"].fillna(0) >= min_coverage)
        & (df["n_embedded"].fillna(0) >= min_emb)
        & (df["n_plays"].fillna(0) >= min_plays)
    ]
    total_matching = int(len(df))
    df = df.sort_values(sort, ascending=ascending, na_position='last').head(limit)

    display = load_display_id_map()
    sessions = []
    for _, row in df.iterrows():
        rec = {col: _clean(row.get(col)) for col in _OVERVIEW_COLS}
        rec["collection_label"] = display.get(rec["collection_id"], rec["collection_id"])
        sessions.append(rec)

    meta = _load_meta()
    return jsonify({
        "sessions": sessions,
        "total_in_study": total_in_study,
        "total_matching": total_matching,
        "returned": len(sessions),
        "meta": meta,
        "defaults": {
            "min_coverage": DEFAULT_MIN_COVERAGE,
            "min_emb_plays": DEFAULT_MIN_EMB_PLAYS,
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
            "n_distinct", "repeat_rate", "n_interleaved", "focus", "diameter",
            "step_mean", "straightness", "spectral_entropy_bits",
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

    display = load_display_id_map()
    session = {col: _clean(session_row.get(col)) for col in _OVERVIEW_COLS}
    session["collection_label"] = display.get(collection_id, collection_id)
    return jsonify({
        "session": session,
        "plays": play_rows,
        "episodes": episodes,
        "windows": windows,
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
