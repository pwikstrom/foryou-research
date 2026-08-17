"""Semantic Space tab API: serve the global video embedding map.

Unlike the study-scoped analysis tabs, this map covers every annotated video
in the corpus (it is built from ``recoded/video_map.parquet`` +
``recoded/video_niches.json`` by the ``video_map_refresh`` worker, not from a
study cache). The payload is cached in-process and only rebuilt when the map
file changes on disk, so repeated tab opens are instant.
"""

import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user

import fyp.data_io as data_io
import fyp.embeddings as embeddings
import fyp.video_map as video_map
import web_interface.semantic_trajectory as semantic_trajectory
from web_interface.data_service import (
    get_accessible_studies,
    get_study_collections,
    load_display_id_map,
)
from web_interface.permissions import permission_required
from web_interface.task_status import is_cloud_run

semantic_space_bp = Blueprint('semantic_space_bp', __name__)

# In-process cache: rebuilt only when the map file's mtime changes.
_MAP_CACHE: dict = {"fingerprint": None, "payload": None}

# "Colour by" overlays advertised to the frontend. ``field`` is the column in
# video_map.parquet; ``kind`` is "numeric" (continuous colourscale) or
# "categorical" (discrete legend). Only overlays whose column is actually
# present in the map file are returned, so older maps degrade gracefully.
# ``decimals`` overrides the payload rounding (default 4) for fields whose
# values live well below 1, e.g. per-play engagement rates.
_OVERLAYS = [
    {"key": "category", "label": "Content category", "kind": "categorical", "field": "category"},
    {"key": "typicality", "label": "Typicality", "kind": "numeric", "field": "typicality"},
    {"key": "platform", "label": "Platform", "kind": "categorical", "field": "source_platform"},
    {"key": "popularity", "label": "Popularity (plays)", "kind": "numeric", "field": "log_plays"},
    {"key": "faves_per_K_play", "label": "Faves per 1K plays", "kind": "numeric", "field": "faves_per_K_play", "decimals": 3},
    {"key": "comments_per_K_play", "label": "Comments per 1K plays", "kind": "numeric", "field": "comments_per_K_play", "decimals": 3},
    {"key": "shares_per_K_play", "label": "Shares per 1K plays", "kind": "numeric", "field": "shares_per_K_play", "decimals": 3},
    {"key": "saves_per_K_play", "label": "Saves per 1K plays", "kind": "numeric", "field": "saves_per_K_play", "decimals": 3},
    {"key": "sensitivity_score", "label": "Sensitivity", "kind": "numeric", "field": "sensitivity_score"},
    {"key": "political_score", "label": "Political content", "kind": "numeric", "field": "political_score"},
    {"key": "speech_vs_music", "label": "Speech vs music", "kind": "numeric", "field": "speech_vs_music"},
    {"key": "faces_age_estimate", "label": "Face age estimate", "kind": "numeric", "field": "faces_age_estimate"},
    {"key": "australian_relevance", "label": "Australian relevance", "kind": "categorical", "field": "australian_relevance"},
    {"key": "tiktok_native", "label": "TikTok-native", "kind": "categorical", "field": "tiktok_native"},
    {"key": "trend", "label": "Trend", "kind": "categorical", "field": "trend"},
    {"key": "advertising", "label": "Advertising", "kind": "categorical", "field": "advertising"},
    {"key": "aigc", "label": "AI-generated", "kind": "categorical", "field": "aigc"},
    {"key": "main_gender", "label": "Main gender", "kind": "categorical", "field": "main_gender"},
    {"key": "main_ethnicity", "label": "Main ethnicity", "kind": "categorical", "field": "main_ethnicity"},
]






def _niche_category_shares(df: pd.DataFrame, top_n: int = 3) -> dict[int, list]:
    """Top content-category shares per niche, as ``{label, pct}`` entries.

    Derived here rather than baked into the niches file at build time, so the
    percentages appear on maps that already exist without a rebuild. Shares are
    taken over every video in the niche — not just the mapped sample — so they
    describe the same population as the niche's ``size``.

    ``content_category`` is deliberately excluded from the embedded text, which
    is what makes these shares an independent check on a niche rather than a
    restatement of its own inputs.

    Args:
        df: The full map frame (every clustered video, mapped or not).
        top_n: How many categories to keep per niche.

    Returns:
        Dict niche id → list of ``{"label", "pct"}``, most common first.
    """
    if "category" not in df.columns or "niche" not in df.columns:
        return {}
    counts = df.groupby(["niche", "category"], observed=True).size().rename("n").reset_index()
    counts["pct"] = 100.0 * counts["n"] / counts.groupby("niche")["n"].transform("sum")
    counts = counts.sort_values(["niche", "n"], ascending=[True, False])
    return {
        int(niche): [
            {"label": str(row.category), "pct": round(float(row.pct), 1)}
            for row in grp.head(top_n).itertuples()
        ]
        for niche, grp in counts.groupby("niche", observed=True)
    }






def _build_payload() -> dict:
    """Assemble the columnar map payload from the enriched map file + niches.

    Returns:
        Dict with columnar ``points`` arrays (always item_id/x/y/niche/
        niche_name/story, plus a column per available overlay), the ``niches``
        lookup, the ``overlays`` manifest, and corpus counts.
    """
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE,
    )
    total_videos = int(len(df))

    # Mapped points carry 2D coordinates; the rest are clustered-only.
    mapped = df[df["x"].notna()].copy()
    mapped["item_id"] = mapped["item_id"].astype("string")

    niches_meta = data_io.load_json(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.NICHES_FILE,
    ) or {}

    points = {
        "item_id": mapped["item_id"].tolist(),
        "x": [round(float(v), 3) for v in mapped["x"].tolist()],
        "y": [round(float(v), 3) for v in mapped["y"].tolist()],
        "niche": [int(v) for v in mapped["niche"].tolist()],
        "niche_name": mapped["niche_name"].astype("string").fillna("").tolist(),
        "story": mapped["story"].astype("string").fillna("").tolist(),
    }

    overlays = []
    for ov in _OVERLAYS:
        field = ov["field"]
        if field not in mapped.columns:
            continue
        col = mapped[field]
        if ov["kind"] == "numeric":
            decimals = ov.get("decimals", 4)
            points[field] = [round(float(v), decimals) if pd.notna(v) else None for v in col.tolist()]
        else:
            points[field] = col.astype("string").fillna("unknown").tolist()
        overlays.append({"key": ov["key"], "label": ov["label"], "kind": ov["kind"], "field": field})

    cat_shares = _niche_category_shares(df)

    # A missing/blank name falls back to the same "Niche N" label the frontend
    # uses, so an unnamed niche reads identically wherever it surfaces.
    # terms/top_categories have always been in the niches file; typicality only
    # appears in maps built after it was added, so both stay optional and the
    # frontend omits whatever is absent.
    niches = {
        str(k): {
            "name": v.get("name") or f"Niche {k}",
            "size": int(v.get("size", 0)),
            "terms": [str(t) for t in (v.get("terms") or [])],
            "top_categories": cat_shares.get(int(k)) or [
                {"label": str(c), "pct": None} for c in (v.get("top_categories") or [])
            ],
            "typicality": v.get("typicality"),
            "typicality_pct": v.get("typicality_pct"),
            "isolation_pct": v.get("isolation_pct"),
            "nearest_ids": [int(n) for n in (v.get("nearest") or [])],
        }
        for k, v in niches_meta.items()
    }

    # Resolve neighbour ids to names in a second pass — a neighbour's name lives
    # in another entry, and the build stores ids precisely so a renamed niche
    # still resolves correctly here.
    for meta in niches.values():
        meta["nearest"] = [
            niches[str(n)]["name"] for n in meta.pop("nearest_ids") if str(n) in niches
        ]

    # Build provenance rides along so the tab can publish the projection's own
    # accuracy rather than asking the reader to take the layout on trust. Absent
    # on maps built before the score existed; the frontend then omits it.
    build_meta = {}
    if data_io.exists(storage_location=embeddings.STORE_LOCATION,
                      filename=video_map.MAP_META_FILE):
        build_meta = data_io.load_json(
            storage_location=embeddings.STORE_LOCATION,
            filename=video_map.MAP_META_FILE,
        ) or {}

    return {
        "points": points,
        "niches": niches,
        "overlays": overlays,
        "total_mapped": int(len(mapped)),
        "total_videos": total_videos,
        "n_niches": len(niches_meta),
        "neighbour_preservation": build_meta.get("neighbour_preservation") or None,
    }






@semantic_space_bp.route('/api/semantic_space/map', methods=['GET'])
@permission_required('tab.semantic_space')
def api_semantic_space_map():
    """Return the global video map (mapped points + niche metadata + overlays).

    Served from an in-process cache keyed on the map file's mtime/size, so the
    heavy parquet read happens only after a ``video_map_refresh``.
    """
    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE):
        return jsonify({
            "error": "The video map has not been built yet. Run the "
                     "'video_map_refresh' task to generate it."
        }), 404

    fingerprint = data_io.stat(storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE)
    key = None if fingerprint is None else f"{fingerprint['size']}:{fingerprint['mtime']}"

    if _MAP_CACHE["payload"] is None or _MAP_CACHE["fingerprint"] != key:
        payload = _build_payload()
        # Stamp the map file's mtime so the tab can detect a fresher rebuild
        # (via /api/semantic_space/status) and offer a reload.
        payload["map_built_at"] = None if fingerprint is None else fingerprint.get("mtime")
        _MAP_CACHE["payload"] = payload
        _MAP_CACHE["fingerprint"] = key

    return jsonify(_MAP_CACHE["payload"])




@semantic_space_bp.route('/api/semantic_space/status', methods=['GET'])
@permission_required('tab.semantic_space')
def api_semantic_space_status():
    """Lightweight freshness signal for the Semantic Space tab.

    Reports whether the embedding store / map are currently being rebuilt and
    whether the existing map is stale — i.e. new embeddings exist that the
    current map does not yet cover. The map keeps rendering throughout; the
    frontend uses this only to drive an informational banner, so the checks
    are deliberately cheap (process_stats counters + one file stat, no parquet
    reads).
    """
    from web_interface.process_manager import load_process_stats, process_stats
    from web_interface.routes.management_routes import _is_worker_running

    # Cross-service stats live on GCS; reload so we see task-runner writes.
    if is_cloud_run():
        load_process_stats()

    emb_running = _is_worker_running("embeddings_refresh")
    map_running = _is_worker_running("video_map_refresh")
    pipeline_in_flight = bool(
        process_stats.get("consolidate_enrichment", {}).get("pipeline_in_flight")
    )

    embedded = process_stats.get("embeddings_refresh", {}).get("embeddings_total")
    map_built_from = process_stats.get("video_map_refresh", {}).get("map_videos")

    map_exists = data_io.exists(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE
    )
    fingerprint = (
        data_io.stat(storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE)
        if map_exists else None
    )
    map_built_at = fingerprint.get("mtime") if fingerprint else None

    # Stale = the store holds more embeddings than the map was last built from.
    # Missing counters default to "fresh" so the banner never cries wolf.
    behind = 0
    if isinstance(embedded, (int, float)) and isinstance(map_built_from, (int, float)):
        behind = max(0, int(embedded) - int(map_built_from))

    # A map built by a different embedding model than the active backend's is
    # explicitly stale — a backend switch requires an embeddings refresh + map
    # rebuild before the tab reflects the new model. Older maps predate the
    # meta file; they can't be attributed, so no mismatch is claimed.
    map_meta = None
    model_mismatch = False
    if map_exists and data_io.exists(
            storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_META_FILE):
        try:
            map_meta = data_io.load_json(
                storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_META_FILE)
        except Exception:
            map_meta = None
    active_model = None
    try:
        active_model = embeddings.active_embedding_backend().model_id()
    except Exception:
        pass
    if isinstance(map_meta, dict) and active_model:
        built_model = map_meta.get("embedding_model")
        model_mismatch = bool(built_model) and built_model != active_model

    if map_running:
        phase = "mapping"
    elif emb_running:
        phase = "embedding"
    elif pipeline_in_flight:
        phase = "consolidating"
    else:
        phase = None

    return jsonify({
        # The cascade is topping up the embedding store (map unchanged).
        "embeddings_updating": bool(emb_running or pipeline_in_flight),
        # A new map is actively being calculated.
        "map_rebuilding": bool(map_running),
        # The visible map is behind the embedding store, or was built by a
        # different embedding model than the active backend's.
        "map_stale": behind > 0 or model_mismatch,
        "behind": behind,
        "phase": phase,
        "map_exists": bool(map_exists),
        "map_built_at": map_built_at,
        "embedded": int(embedded) if isinstance(embedded, (int, float)) else None,
        "map_built_from": int(map_built_from) if isinstance(map_built_from, (int, float)) else None,
        # Build provenance (None for maps predating the meta file).
        "map_meta": map_meta if isinstance(map_meta, dict) else None,
        "active_embedding_model": active_model,
        "model_mismatch": model_mismatch,
    })




def _user_ctx() -> tuple:
    """Return ``(username, role, is_admin)`` for the current user."""
    username = getattr(current_user, "username", current_user.id)
    role = getattr(current_user, "role", None) or getattr(current_user, "user_role", "viewer")
    attr = getattr(current_user, "is_admin", False)
    is_admin = attr() if callable(attr) else bool(attr)
    if role == "admin":
        is_admin = True
    return username, role, is_admin




def _accessible_collection_ids() -> set:
    """Collection ids the current user may project onto the map.

    Mirrors the study-access gate used by the Collections tab: admins see every
    collection in every study, others only those in studies they can access.
    """
    username, role, is_admin = _user_ctx()
    ids: set = set()
    for study in get_accessible_studies(username, role, is_admin):
        for d in get_study_collections(study):
            cid = d.get("collection_id")
            if cid:
                ids.add(str(cid))
    return ids




@semantic_space_bp.route('/api/semantic_space/collections', methods=['GET'])
@permission_required('tab.semantic_space')
def api_semantic_space_collections():
    """List the collections the user can overlay, scoped to the selected study.

    Each entry is ``{"id": <collection_id>, "label": <display id>}`` (the label
    falls back to the raw id when no display id is set). With a ``study`` query
    param the list is restricted to that study's collections (and the user must
    have access to it); without one it falls back to every accessible collection.
    """
    study = (request.args.get('study') or '').strip()
    username, role, is_admin = _user_ctx()
    accessible_studies = get_accessible_studies(username, role, is_admin)

    if study:
        if study not in accessible_studies:
            return jsonify({"collections": []})
        cids = [str(d.get("collection_id")) for d in get_study_collections(study)
                if d.get("collection_id")]
    else:
        cids = sorted({str(d.get("collection_id"))
                       for s in accessible_studies for d in get_study_collections(s)
                       if d.get("collection_id")})

    display = load_display_id_map()
    seen: set = set()
    collections = []
    for cid in cids:
        if cid in seen:
            continue
        seen.add(cid)
        collections.append({"id": cid, "label": display.get(cid, cid)})
    collections.sort(key=lambda c: (c["label"] or "").lower())
    return jsonify({"collections": collections})




@semantic_space_bp.route('/api/semantic_space/trajectory', methods=['GET'])
@permission_required('tab.semantic_space')
def api_semantic_space_trajectory():
    """Centre-of-gravity / entropy / daily trajectory for one collection.

    Projects the collection's play activity onto the global map via its
    embedding-derived niche labels (see :mod:`web_interface.semantic_trajectory`)
    and returns the per-day + all-time metrics the overlay renders.
    """
    collection_id = (request.args.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({"error": "collection_id is required"}), 400
    if collection_id not in _accessible_collection_ids():
        return jsonify({"error": "Collection not found or not accessible"}), 403

    if not data_io.exists(storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE):
        return jsonify({
            "error": "The video map has not been built yet. Run the "
                     "'video_map_refresh' task to generate it."
        }), 404

    interval = request.args.get('interval', 'month')
    if interval not in ('day', 'week', 'month', 'all'):
        interval = 'month'
    start = (request.args.get('start') or '').strip() or None
    end = (request.args.get('end') or '').strip() or None

    try:
        payload = semantic_trajectory.build_trajectory(
            collection_id, interval=interval, start=start, end=end,
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to build trajectory: {exc}"}), 500

    return jsonify(payload)
