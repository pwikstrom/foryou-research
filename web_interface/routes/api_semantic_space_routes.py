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






def _build_payload() -> dict:
    """Assemble the columnar map payload from the enriched map file + niches.

    Returns:
        Dict with columnar ``points`` arrays, the ``niches`` lookup, the sorted
        ``categories`` list, and corpus counts.
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

    categories = sorted(
        c for c in mapped["category"].astype("string").fillna("none").unique() if c
    )

    points = {
        "item_id": mapped["item_id"].tolist(),
        "x": [round(float(v), 3) for v in mapped["x"].tolist()],
        "y": [round(float(v), 3) for v in mapped["y"].tolist()],
        "niche": [int(v) for v in mapped["niche"].tolist()],
        "niche_name": mapped["niche_name"].astype("string").fillna("").tolist(),
        "category": mapped["category"].astype("string").fillna("none").tolist(),
        "log_plays": [round(float(v), 3) if pd.notna(v) else 0.0 for v in mapped["log_plays"].tolist()],
        "story": mapped["story"].astype("string").fillna("").tolist(),
    }

    niches = {
        str(k): {"name": v.get("name", str(k)), "size": int(v.get("size", 0))}
        for k, v in niches_meta.items()
    }

    return {
        "points": points,
        "niches": niches,
        "categories": categories,
        "total_mapped": int(len(mapped)),
        "total_videos": total_videos,
        "n_niches": len(niches_meta),
    }






@semantic_space_bp.route('/api/semantic_space/map', methods=['GET'])
@permission_required('tab.semantic_space')
def api_semantic_space_map():
    """Return the global video map (mapped points + niche metadata).

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
