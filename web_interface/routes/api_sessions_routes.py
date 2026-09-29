"""Sessions tab API: session-quality overview + focused-episode detail.

Serves the artifacts built by the ``sessions_refresh`` worker
(:mod:`fyp.analysis.session_explorer`): a filterable per-session quality/focus
index, a per-session detail payload (the full play sequence + detected focus
episodes with their ordered members), and a lightweight freshness/status
signal. The loading, caching and study scoping live in
:mod:`web_interface.services.sessions_data`, the trend statistics in
:mod:`web_interface.services.sessions_stats`. The artifacts are global (all
collections, full history); every request is scoped to the caller's study on
BOTH axes — a session is only visible when its collection is one the
requested, accessible study actually contains (see
:func:`sessions_data.study_collection_ids`: selected AND present in the
study's built frame) AND it started inside the study's date window (see
:func:`sessions_data.in_study_window`) AND, for a day-sampled study, on a day
the sample admitted (see :func:`sessions_data.in_study_cells`). No axis
implies another: the collection set alone would show a ten-day study every
session those donors ever recorded, and the window alone lists sessions on
dropped days whose every video is "not in this study".

Admins may widen the list to the whole artifact (``scope=all``, honoured only
for an admin account — see :func:`_admin_all_scope`); rows then carry
``in_study`` so the client can mark which ones the study actually contains.
Admin playback is independent of the list scope: an admin streams any
downloaded video, so their ``streamable`` verdict ignores frame membership.

All entropy/focus numbers were precomputed into the artifacts, and per-item
flags come from cheap id-set membership checks. The one deliberate exception
is the detail payload's context-play distances: a handful of vectors are
fetched from the dense sidecar per request (ranged reads, never a shard scan)
to explain why the plays just outside a binge/sequence were not part of it.
"""

import threading

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user

import fyp.analysis.embeddings as embeddings
from fyp.analysis import session_explorer
from web_interface.services.study_data import (
    get_study_date_window,
    load_display_id_map,
)

from ..auth.permissions import permission_required
from ..services import sessions_data, sessions_stats
from ..tasks.task_status import is_cloud_run
from ._access import study_access_error

sessions_bp = Blueprint("sessions_bp", __name__)

# Default for the ad-hoc ``min_emb_plays`` quality filter (query-time only —
# the artifact itself is unfiltered). From the embedding-entropy study's donor
# floors, adapted to the single-session grain. The coverage floor is an admin
# setting; see sessions_data.session_floors.
DEFAULT_MIN_EMB_PLAYS = 5
OVERVIEW_LIMIT_DEFAULT = 200
OVERVIEW_LIMIT_MAX = 1000

# The session-list floors (plays / minutes / embedded-coverage) are owned by
# the admin settings store, which resolves admin setting > [sessions] config >
# its own fallbacks — so an admin can retune them from Admin → Site Settings
# with no rebuild. See admin_settings.get_session_floors.

# Columns the overview endpoint returns per session row.
_OVERVIEW_COLS = [
    "collection_id",
    "session_id",
    "start_ts",
    "end_ts",
    "duration_min",
    "n_plays",
    "n_distinct",
    "total_watch_s",
    "median_dwell_s",
    "n_embedded",
    "coverage_scraped",
    "coverage_annotated",
    "coverage_embedded",
    "emb_play_coverage",
    "min_window_cosdist",
    "min_window_entropy_norm",
    "n_episodes",
    "episode_play_frac",
    "dominant_niche",
    "n_niches",
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
    "min_window_cosdist",
    "min_window_entropy_norm",
    "duration_min",
    "n_plays",
    "n_distinct",
    "n_episodes",
    "episode_play_frac",
    "coverage_embedded",
    "start_ts",
    "total_watch_s",
    "n_directed_episodes",
    "collection_id",
}
# video_map's numeric-column list, learned from the first full read per
# artifact version so later trend_frame reads can project columns.
# Whole detail responses, keyed by the session plus every input that can
# change the payload (artifact fingerprints, the study frame's mtime, the
# enrichment-flags key). Users hop back and forth between sessions; without
# this every revisit re-paid the full assembly (~2s: embedding byte-range
# reads for context distances + per-play row building).
_DETAIL_RESPONSE_CACHE: dict = {}
_DETAIL_RESPONSE_MAX = 64
_detail_response_lock = threading.Lock()


def _detail_cache_version(study: str) -> tuple:
    """Everything (besides the session identity) the detail payload reads."""
    from ..services.study_data import get_recoded_mtime

    try:
        model = embeddings.active_embedding_backend().model_id()
    except Exception:
        model = None
    return (
        sessions_data.artifact_fingerprint(session_explorer.PLAYS_FILE),
        sessions_data.artifact_fingerprint(session_explorer.EPISODES_FILE),
        sessions_data.artifact_fingerprint(session_explorer.WINDOWS_FILE),
        sessions_data.artifact_fingerprint(session_explorer.SESSIONS_FILE),
        get_recoded_mtime(study),
        sessions_data.flags_cache_key(model),
    )


# Story text is for card context only — cap it so a session with 100 plays
# doesn't ship 100 full transcripts.
_STORY_CAP = 400


def _admin_all_scope() -> bool:
    """True when an ADMIN asked for the unscoped, whole-artifact view.

    ``scope=all`` from a non-admin is silently ignored — the scoped list is
    the only view a viewer account gets, so the flag never becomes a way
    around study access.
    """
    if (request.args.get("scope") or "").strip() != "all":
        return False
    try:
        return bool(current_user.is_admin())
    except Exception:
        return False


def _admin_playback() -> bool:
    """True when the caller streams any downloaded video (admin accounts)."""
    try:
        return bool(current_user.is_admin())
    except Exception:
        return False


def _opt_query_float(name: str) -> float | None:
    """An optional numeric query param: absent/blank → None, junk → ValueError."""
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return None
    return float(raw)


@sessions_bp.route("/api/sessions/overview", methods=["GET"])
@permission_required("tab.sessions")
def api_sessions_overview():
    """Filterable, sortable, paginated session table scoped to one study.

    Query params: ``study`` (required), ``min_coverage`` (embedded coverage
    floor), ``min_emb_plays``, ``min_plays``, ``min_session_minutes``, ``sort``
    (one of the index metrics; default ``min_window_cosdist``), ``order``
    (``asc``/``desc``), ``limit`` (page size), ``page`` (0-based).

    Only sessions inside the study — its collections AND its date window (see
    :func:`in_study_window`) — reach any of this; ``total_in_study`` counts
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
    study = (request.args.get("study") or "").strip()
    if not study:
        return jsonify({"error": "study is required"}), 400
    denied = study_access_error(study)
    if denied is not None:
        return denied

    index = sessions_data.load_index()
    if index is None:
        return jsonify(
            {
                "error": "The sessions index has not been built yet. Run the "
                "'sessions_refresh' task to generate it."
            }
        ), 404

    floors = sessions_data.session_floors()
    try:
        min_coverage = float(request.args.get("min_coverage", floors["min_coverage"]))
        min_emb = int(request.args.get("min_emb_plays", DEFAULT_MIN_EMB_PLAYS))
        min_plays = int(request.args.get("min_plays", floors["min_plays"]))
        min_minutes = float(request.args.get("min_session_minutes", floors["min_session_minutes"]))
        limit = min(int(request.args.get("limit", OVERVIEW_LIMIT_DEFAULT)), OVERVIEW_LIMIT_MAX)
        page = max(int(request.args.get("page", 0)), 0)
        range_filters = {
            stem: (_opt_query_float(f"{stem}_min"), _opt_query_float(f"{stem}_max"))
            for stem in _RANGE_FILTER_COLS
        }
        varmax_col = (request.args.get("f_varmax_col") or "").strip()
        varmax_lo = _opt_query_float("f_varmax_min")
        varmax_hi = _opt_query_float("f_varmax_max")
        varmax_scope = (request.args.get("f_varmax_scope") or "session").strip()
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric filter"}), 400
    search_q = (request.args.get("q") or "").strip()
    f_start_min = f_start_max = None
    try:
        raw = (request.args.get("f_start_min") or "").strip()
        if raw:
            f_start_min = pd.Timestamp(raw)
        raw = (request.args.get("f_start_max") or "").strip()
        if raw:
            # Inclusive day: anything before the following midnight matches.
            f_start_max = pd.Timestamp(raw) + pd.Timedelta(days=1)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid date filter"}), 400
    sort = request.args.get("sort") or "min_window_cosdist"
    if sort not in _SORT_KEYS:
        sort = "min_window_cosdist"
    ascending = (request.args.get("order") or "asc").lower() != "desc"

    cids = sessions_data.study_collection_ids(study)
    window = get_study_date_window(study)
    all_scope = _admin_all_scope()

    # All scoping/filter stages are boolean masks over the FULL index; the
    # frame is materialized exactly once, after the last mask. The old
    # stage-by-stage slicing copied the full-width frame ~5 times per request.
    def _np_mask(series) -> np.ndarray:
        return series.fillna(False).to_numpy(dtype=bool)

    # Study scoping is two-axis: collections AND the study's date window. This
    # runs BEFORE total_in_study so every downstream number — the floor counts,
    # the slider bounds, the status line — describes the study, not the
    # artifact.
    in_study = (
        _np_mask(index["collection_id"].isin(cids))
        & _np_mask(sessions_data.in_study_window(index, study))
        & _np_mask(sessions_data.in_study_cells(index, study))
    )
    # An admin's "all sessions" view lists the whole artifact; the study mask
    # is kept so each row can still say whether the study contains it.
    population = np.ones(len(index), dtype=bool) if all_scope else in_study
    total_in_study = int(population.sum())
    # The three admin-controlled list floors are applied as one block, so the
    # client can report a single "N not listed" count it can reconcile with the
    # rows on screen; min_emb_plays stays a separate ad-hoc quality filter.
    floors_ok = (
        population
        & _np_mask(index["n_plays"].fillna(0) >= min_plays)
        & _np_mask(index["duration_min"].fillna(0) >= min_minutes)
        & _np_mask(index["coverage_embedded"].fillna(0) >= min_coverage)
    )
    total_above_floors = int(floors_ok.sum())
    pop = floors_ok & _np_mask(index["n_embedded"].fillna(0) >= min_emb)

    # Slider bounds come from the population the sliders act on — after the
    # floors, before the user's own range filters.
    ranges = sessions_data.cached_filter_ranges(
        index,
        pop,
        study,
        (
            min_plays,
            min_minutes,
            min_coverage,
            min_emb,
            len(cids),
            hash(frozenset(cids)),
            window,
            sessions_data.cells_signature(study),
            all_scope,
        ),
    )

    mask = pop.copy()
    if f_start_min is not None or f_start_max is not None:
        ts = sessions_data.start_dt(index)
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
        if varmax_scope == "binges":
            # "Binges only": keep sessions where at least ONE binge's max of
            # the variable falls in the range. Sessions without a binge (or
            # whose binges have no value for the variable) drop — the range
            # is a criterion on binges, and they have none satisfying it.
            emax = sessions_data.episode_vmax()
            if emax is not None and varmax_col in emax.columns:
                vals = pd.to_numeric(emax[varmax_col], errors="coerce")
                ok = vals.notna()
                if varmax_lo is not None:
                    ok &= vals >= varmax_lo
                if varmax_hi is not None:
                    ok &= vals <= varmax_hi
                passing = pd.MultiIndex.from_frame(emax.loc[ok, ["collection_id", "session_id"]])
                keys = pd.MultiIndex.from_arrays([index["collection_id"], index["session_id"]])
                mask &= keys.isin(passing)
                binge_scoped = True
        if not binge_scoped:
            # Session scope — or the binge scope's data isn't available (no
            # episodes artifact / unknown variable), which degrades to the
            # session-max semantics rather than silently dropping the filter.
            col = f"{sessions_data.VARMAX_PREFIX}{varmax_col}"
            if col in index.columns:
                vals = pd.to_numeric(index[col], errors="coerce")
                if varmax_lo is not None:
                    mask &= _np_mask(vals >= varmax_lo)
                if varmax_hi is not None:
                    mask &= _np_mask(vals <= varmax_hi)
    # Free-text search over the baked per-session blob (lowercased at build);
    # every whitespace-separated term must match. Ignored on an old artifact.
    blobs = sessions_data.search_blob(index)
    search_available = blobs is not None
    if search_q and search_available:
        for term in search_q.lower().split():
            mask &= blobs.str.contains(term, regex=False).fillna(False).to_numpy(dtype=bool)
    df = index[mask].copy()
    total_matching = int(len(df))
    # Rides through the sort and the page slice with its row.
    df["_in_study"] = in_study[mask]

    # Directed-binge counts join BEFORE the sort so the column is sortable —
    # ranking sessions by it is how a researcher hunts rabbit holes.
    directed = sessions_data.directed_counts()
    if directed is not None:
        keys = pd.MultiIndex.from_arrays([df["collection_id"], df["session_id"]])
        df["n_directed_episodes"] = directed.reindex(keys).fillna(0).astype("int32").to_numpy()
    if sort not in df.columns:
        # e.g. sorting by directed binges against an artifact that has none.
        sort = "min_window_cosdist"
    df = df.sort_values(sort, ascending=ascending, na_position="last")
    # Pagination: clamp the requested page so a filter change that shrinks the
    # result set never returns an empty page while matches exist.
    if limit > 0:
        page = min(page, max((total_matching - 1) // limit, 0))
        df = df.iloc[page * limit : (page + 1) * limit]
    else:
        page = 0

    display = load_display_id_map()
    sessions = []
    for _, row in df.iterrows():
        rec = {col: sessions_data.clean(row.get(col)) for col in _OVERVIEW_COLS}
        rec["collection_label"] = display.get(rec["collection_id"], rec["collection_id"])
        rec["in_study"] = bool(row.get("_in_study"))
        # None (not 0) when the artifact predates direction_p: the client must
        # be able to tell "no directed binges" from "never measured".
        rec["n_directed_episodes"] = (
            sessions_data.clean(row.get("n_directed_episodes")) if directed is not None else None
        )
        sessions.append(rec)

    meta = sessions_data.load_meta()
    return jsonify(
        {
            "sessions": sessions,
            "scope": "all" if all_scope else "study",
            "total_in_study": total_in_study,
            # Under scope=all the population is the artifact; this stays the
            # study's own count so the status line can name both.
            "study_total": int(in_study.sum()),
            "total_above_floors": total_above_floors,
            "total_matching": total_matching,
            "returned": len(sessions),
            "page": page,
            "page_size": limit,
            "ranges": ranges,
            "search_available": search_available,
            "meta": meta,
            "params": sessions_data.display_params(meta),
            "floors": {
                "min_plays": min_plays,
                "min_session_minutes": min_minutes,
                "min_coverage": min_coverage,
            },
            "defaults": {
                "min_emb_plays": DEFAULT_MIN_EMB_PLAYS,
                "min_plays": floors["min_plays"],
                "min_session_minutes": floors["min_session_minutes"],
                "min_coverage": floors["min_coverage"],
            },
        }
    )


@sessions_bp.route("/api/sessions/detail", methods=["GET"])
@permission_required("tab.sessions")
def api_sessions_detail():
    """One session's full play sequence + focus episodes + per-item context.

    Query params: ``study``, ``collection_id``, ``session_id`` (all required),
    ``scope=all`` (admins only; see :func:`_admin_all_scope`).
    The session must belong to the (accessible) study on all three scoping
    axes — its collection, the study's date window and, for a sampled study,
    an admitted day — so a bookmarked link into a session the study no longer
    contains is refused rather than rendered. An admin's ``scope=all`` lifts
    that: any session in the artifact opens, and the payload's
    ``session.in_study`` says whether the study contains it. Each play carries
    enrichment flags, ``in_study`` (the item appears in the study's viewer
    frame) and a ``streamable`` verdict — an item is streamable when it is in
    the frame AND its media was downloaded, which is exactly what the
    ``/api/video/<study>/<item_id>`` gate will accept. For an admin the gate
    accepts any downloaded item, and so does the verdict.
    """
    from .api_viewer_routes import _study_item_ids

    study = (request.args.get("study") or "").strip()
    collection_id = (request.args.get("collection_id") or "").strip()
    session_id = (request.args.get("session_id") or "").strip()
    if not study or not collection_id or not session_id:
        return jsonify({"error": "study, collection_id and session_id are required"}), 400
    denied = study_access_error(study)
    if denied is not None:
        return denied
    all_scope = _admin_all_scope()
    admin_play = _admin_playback()
    if not all_scope and collection_id not in sessions_data.study_collection_ids(study):
        return jsonify({"error": "Collection not found in this study"}), 403

    # The verdicts differ per audience (admin playback, admin scope), so the
    # cache never hands one audience's payload to another.
    cache_key = (
        study,
        collection_id,
        session_id,
        all_scope,
        admin_play,
        _detail_cache_version(study),
    )
    with _detail_response_lock:
        cached_payload = _DETAIL_RESPONSE_CACHE.get(cache_key)
    if cached_payload is not None:
        return jsonify(cached_payload)

    index = sessions_data.load_index()
    if index is None:
        return jsonify({"error": "The sessions index has not been built yet."}), 404
    match = index[(index["collection_id"] == collection_id) & (index["session_id"] == session_id)]
    # The other scoping axes: a session the collection recorded outside the
    # study's date window, or on a day the sample dropped, is not this
    # study's session. An admin's all-scope view still reports the verdict.
    session_in_study = False
    if not match.empty:
        scoped = (
            sessions_data.in_study_window(match, study).to_numpy()
            & sessions_data.in_study_cells(match, study).to_numpy()
        )
        session_in_study = bool(scoped[0]) and collection_id in sessions_data.study_collection_ids(
            study
        )
        if not all_scope:
            match = match[scoped]
    if match.empty:
        return jsonify({"error": "Session not found"}), 404
    session_row = match.iloc[0]

    plays = sessions_data.session_plays(collection_id, session_row)
    episodes = sessions_data.session_episodes(collection_id, session_id)
    windows = sessions_data.session_windows(collection_id, session_id)
    feat = sessions_data.features()
    flags = sessions_data.flag_sets()
    study_ids = _study_item_ids(study) or frozenset()
    session_item_ids = {str(i) for i in plays["item_id"]}
    embedded_ids = sessions_data.embedded_ids(session_item_ids, flags)
    # A plays artifact built with baked-in text answers story/desc/hashtags
    # directly; only the fallback path (activity file / pre-upgrade artifact)
    # still needs the per-request pushdown reads — which decode the whole
    # text column of the corpus parquets, the tab's dominant per-click cost.
    if "story" in plays.columns:
        stories, scrape_text = sessions_data.play_text_maps(plays)
    else:
        stories = sessions_data.story_map(session_item_ids)
        scrape_text = sessions_data.scrape_text_map(session_item_ids)

    # A play belongs to an episode when its timestamp falls inside the
    # episode's span and its item is one of the episode's members.
    ep_spans = [
        (
            ep["episode_idx"],
            pd.Timestamp(ep["start_ts"]),
            pd.Timestamp(ep["end_ts"]),
            {m["item_id"] for m in ep["members"]},
        )
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
        story = (
            stories.get(iid) or (None if f is None else sessions_data.clean(f.get("story"))) or None
        )
        if isinstance(story, str) and len(story) > _STORY_CAP:
            story = story[:_STORY_CAP] + "…"
        text = scrape_text.get(iid) or {}
        desc = text.get("desc")
        if isinstance(desc, str) and len(desc) > _STORY_CAP:
            desc = desc[:_STORY_CAP] + "…"
        play_rows.append(
            {
                "seq": seq,
                "item_id": iid,
                "ts": ts.isoformat(),
                "dwell_s": sessions_data.clean(row.get("play_duration")),
                "duration_s": None if f is None else sessions_data.clean(f.get("duration")),
                "platform": sessions_data.clean(row.get("source_platform")),
                "annotated": iid in flags["annotated"],
                "embedded": iid in embedded_ids,
                "in_study": iid in study_ids,
                "streamable": ((admin_play or iid in study_ids) and (iid in flags["downloaded"])),
                "niche_name": None if f is None else sessions_data.clean(f.get("niche_name")),
                "category": None if f is None else sessions_data.clean(f.get("category")),
                "story": story,
                "desc": desc,
                "hashtags": text.get("hashtags"),
                "author": None if f is None else sessions_data.clean(f.get("author")),
                "political_score": None
                if f is None
                else sessions_data.clean(f.get("political_score")),
                "sensitivity_score": None
                if f is None
                else sessions_data.clean(f.get("sensitivity_score")),
                "episode_idx": episode_idx,
            }
        )

    # Distances of the just-outside context plays to each binge/sequence's
    # member centroid — the "why wasn't this one included" signal. Best-effort:
    # a missing dense store simply leaves the payload without them.
    try:
        sessions_data.attach_context_distances(
            episodes + windows, play_rows, sessions_data.context_plays()
        )
    except Exception:
        pass

    # Per-run creator counts and the within-binge trend scan, both computed
    # here rather than baked into the artifact: they need no embedding
    # vectors, so they stay live and a change needs no rebuild.
    trend_feat = sessions_data.trend_frame(session_item_ids)
    min_n = sessions_data.trend_min_videos()
    for ep in episodes:
        ids = [m["item_id"] for m in ep["members"]]
        ep["creators"] = sessions_stats.creator_count(ids, feat)
        ep["trend_scan"] = sessions_stats.scan_trend(ep["members"], trend_feat, min_n)
    for w in windows:
        w["creators"] = sessions_stats.creator_count([m["item_id"] for m in w["members"]], feat)

    # Session-level observed min/max of the same variables the binge cards
    # show — live-computed from the current video_map, so it can differ
    # slightly from the index's baked ``vmax_``/``vmin_`` columns after a map
    # rebuild (both are honest; they describe different build moments).
    session_series: dict[str, np.ndarray] = (
        {col: trend_feat[col].to_numpy(dtype=float) for col in trend_feat.columns}
        if not trend_feat.empty
        else {}
    )
    dwell_vals = pd.to_numeric(plays["play_duration"], errors="coerce")
    session_series["dwell_s"] = dwell_vals.to_numpy(dtype=float)
    session_ranges = sessions_stats.min_max_ranges(session_series)

    # Per-play values of the same numeric variables, aligned with ``plays``
    # order — the session line plot's data. (``dwell_s`` already rides on each
    # play row.) Variables with no finite value in this session are omitted.
    play_variables: dict[str, list] = {}
    if not trend_feat.empty and play_rows:
        aligned = trend_feat.reindex([p["item_id"] for p in play_rows])
        for col in trend_feat.columns:
            vals = aligned[col].to_numpy(dtype=float)
            if np.isfinite(vals).any():
                play_variables[col] = [round(float(v), 4) if np.isfinite(v) else None for v in vals]

    display = load_display_id_map()
    session = {col: sessions_data.clean(session_row.get(col)) for col in _OVERVIEW_COLS}
    session["collection_label"] = display.get(collection_id, collection_id)
    session["in_study"] = session_in_study
    payload = {
        "session": session,
        "plays": play_rows,
        "episodes": episodes,
        "windows": windows,
        "session_ranges": session_ranges,
        "play_variables": play_variables,
        "params": sessions_data.display_params(sessions_data.load_meta()),
    }
    with _detail_response_lock:
        while len(_DETAIL_RESPONSE_CACHE) >= _DETAIL_RESPONSE_MAX:
            _DETAIL_RESPONSE_CACHE.pop(next(iter(_DETAIL_RESPONSE_CACHE)))
        _DETAIL_RESPONSE_CACHE[cache_key] = payload
    return jsonify(payload)


@sessions_bp.route("/api/sessions/status", methods=["GET"])
@permission_required("tab.sessions")
def api_sessions_status():
    """Lightweight freshness signal for the Sessions tab.

    Reports artifact existence/provenance, whether the ``sessions_refresh`` (or
    upstream embeddings) worker is currently running, and whether the artifact
    was built by a different embedding model than the active backend's.
    """
    from web_interface.services.worker_status import is_worker_running
    from web_interface.tasks.process_manager import load_process_stats

    if is_cloud_run():
        load_process_stats()

    meta = sessions_data.load_meta()
    exists = sessions_data.artifact_fingerprint(session_explorer.SESSIONS_FILE) is not None
    active_model = None
    try:
        active_model = embeddings.active_embedding_backend().model_id()
    except Exception:
        pass
    built_model = (meta or {}).get("embedding_model")
    model_mismatch = bool(built_model) and bool(active_model) and built_model != active_model

    return jsonify(
        {
            "artifact_exists": bool(exists),
            "built_at": (meta or {}).get("built_at"),
            "meta": meta,
            "active_embedding_model": active_model,
            "model_mismatch": model_mismatch,
            "refresh_running": is_worker_running("sessions_refresh"),
            "embeddings_updating": is_worker_running("embeddings_refresh"),
        }
    )
