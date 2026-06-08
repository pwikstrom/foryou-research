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
from web_interface.data_service import get_accessible_studies, get_study_collections
from web_interface.permissions import permission_required
from web_interface.task_status import is_cloud_run

semantic_space_bp = Blueprint('semantic_space_bp', __name__)

# In-process cache: rebuilt only when the map file's mtime changes.
_MAP_CACHE: dict = {"fingerprint": None, "payload": None}

# "Colour by" overlays advertised to the frontend. ``field`` is the column in
# video_map.parquet; ``kind`` is "numeric" (continuous colourscale) or
# "categorical" (discrete legend). Only overlays whose column is actually
# present in the map file are returned, so older maps degrade gracefully.
_OVERLAYS = [
    {"key": "category", "label": "Content category", "kind": "categorical", "field": "category"},
    {"key": "popularity", "label": "Popularity (plays)", "kind": "numeric", "field": "log_plays"},
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
            points[field] = [round(float(v), 4) if pd.notna(v) else None for v in col.tolist()]
        else:
            points[field] = col.astype("string").fillna("unknown").tolist()
        overlays.append({"key": ov["key"], "label": ov["label"], "kind": ov["kind"], "field": field})

    niches = {
        str(k): {"name": v.get("name", str(k)), "size": int(v.get("size", 0))}
        for k, v in niches_meta.items()
    }

    return {
        "points": points,
        "niches": niches,
        "overlays": overlays,
        "total_mapped": int(len(mapped)),
        "total_videos": total_videos,
        "n_niches": len(niches_meta),
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
        # The visible map is behind the embedding store.
        "map_stale": behind > 0,
        "behind": behind,
        "phase": phase,
        "map_exists": bool(map_exists),
        "map_built_at": map_built_at,
        "embedded": int(embedded) if isinstance(embedded, (int, float)) else None,
        "map_built_from": int(map_built_from) if isinstance(map_built_from, (int, float)) else None,
    })




def _accessible_collection_ids() -> set:
    """Collection ids the current user may project onto the map.

    Mirrors the study-access gate used by the Collections tab: admins see every
    collection in every study, others only those in studies they can access.
    """
    username = getattr(current_user, "username", current_user.id)
    role = getattr(current_user, "role", None) or getattr(current_user, "user_role", "viewer")
    attr = getattr(current_user, "is_admin", False)
    is_admin = attr() if callable(attr) else bool(attr)
    if role == "admin":
        is_admin = True

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
    """List the collection ids the user can overlay on the Semantic Space map."""
    return jsonify({"collections": sorted(_accessible_collection_ids())})




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
