"""Semantic Space tab API: serve the global video embedding map.

Unlike the study-scoped analysis tabs, this map covers every annotated video
in the corpus (it is built from ``recoded/video_map.parquet`` +
``recoded/video_niches.json`` by the ``video_map_refresh`` worker, not from a
study cache). The payload is cached in-process and only rebuilt when the map
file changes on disk, so repeated tab opens are instant.
"""

import pandas as pd
from flask import Blueprint, jsonify

import fyp.data_io as data_io
import fyp.embeddings as embeddings
import fyp.video_map as video_map
from web_interface.permissions import permission_required

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
        _MAP_CACHE["payload"] = _build_payload()
        _MAP_CACHE["fingerprint"] = key

    return jsonify(_MAP_CACHE["payload"])
