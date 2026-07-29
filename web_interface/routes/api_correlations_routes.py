"""Correlations tab API: scatter + correlation-matrix views over PCA scores.

Serves the precomputed group-level PCA artifacts (``{study}_PCA.parquet`` and
``{study}_comp_interpretations.json``, written by the ``pca_refresh`` worker).
Every endpoint is gated on the ``tab.correlations`` permission and on the
user's study access (same gate as the other analysis tabs). The payload
building lives in ``services/correlations_service.py`` — these routes are a
thin auth + request-parsing layer.
"""

from flask import Blueprint, jsonify, request
from flask_login import current_user

from fyp.logging_setup import get_logger

from ..data_service import get_accessible_studies, get_pca_df
from ..permissions import permission_required
from ..services import correlations_service

logger = get_logger(__name__)

correlations_bp = Blueprint('correlations_bp', __name__)






def _study_access_error(study):
    """Return a 403 response tuple if the user cannot access ``study``, else None."""
    username = getattr(current_user, "username", current_user.id)
    role = getattr(current_user, "role", None)
    is_admin_attr = getattr(current_user, "is_admin", False)
    is_admin = is_admin_attr() if callable(is_admin_attr) else bool(is_admin_attr)
    if study in get_accessible_studies(username, role, is_admin):
        return None
    return jsonify({"error": "Access denied to this study"}), 403






@correlations_bp.route('/api/correlations/metadata', methods=['POST'])
@permission_required('tab.correlations')
def api_pca_metadata():
    data = request.json or {}
    study = data.get("study")
    if not study:
        return jsonify({"error": "No study"}), 400

    denied = _study_access_error(study)
    if denied is not None:
        return denied

    df = get_pca_df(study)
    if df is None:
        return jsonify({"error": "PCA data not found"}), 404

    payload = correlations_service.build_metadata_payload(df, study)
    if payload is None:
        logger.error(f"No factors found in var_schema for study {study}")
        return jsonify({"error": "No factors found in var_schema"}), 500

    return jsonify(payload)






@correlations_bp.route('/api/correlations/data', methods=['POST'])
@permission_required('tab.correlations')
def api_pca_data():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})
    x_col = data.get("x_col")
    y_col = data.get("y_col")
    color_col = data.get("color_col")

    if not study or not x_col or not y_col:
        return jsonify({"error": "Missing params"}), 400

    denied = _study_access_error(study)
    if denied is not None:
        return denied

    df = get_pca_df(study)
    if df is None:
        return jsonify({"error": "PCA data not found"}), 404

    if x_col not in df.columns or y_col not in df.columns:
        return jsonify({"error": "Unknown axis column"}), 400

    payload = correlations_service.build_scatter_payload(
        df, filters, x_col, y_col, color_col, center=bool(data.get("center")))
    return jsonify(payload)






@correlations_bp.route('/api/correlations/correlation_matrix', methods=['POST'])
@permission_required('tab.correlations')
def api_pca_correlation_matrix():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})

    if not study:
        return jsonify({"error": "No study"}), 400

    denied = _study_access_error(study)
    if denied is not None:
        return denied

    df = get_pca_df(study)
    if df is None:
        return jsonify({"error": "PCA data not found"}), 404

    payload, error = correlations_service.build_matrix_payload(
        df, filters, study,
        method=data.get("method"),
        center=bool(data.get("center")))
    if payload is None:
        return jsonify({"error": error}), 400

    return jsonify(payload)






@correlations_bp.route('/api/correlations/group_stats', methods=['POST'])
@permission_required('tab.correlations')
def api_correlations_group_stats():
    """Serve the worker-precomputed group-differences artifact for a study.

    The ANOVA/Kruskal–Wallis sweep + per-family PERMANOVA are computed by the
    ``pca_refresh`` worker over the whole study (filters do not apply) and
    stored as ``{study}_corr_stats.json``.
    """
    data = request.json or {}
    study = data.get("study")
    if not study:
        return jsonify({"error": "No study"}), 400

    denied = _study_access_error(study)
    if denied is not None:
        return denied

    payload = correlations_service.load_group_stats(study)
    if payload is None:
        return jsonify({
            "error": "Group statistics not computed yet for this study",
            "hint": "Run Data Pipeline → Refresh Caches (PCA / Correlations), then reload.",
        }), 404

    return jsonify(payload)






@correlations_bp.route('/api/correlations/status', methods=['GET'])
@permission_required('tab.correlations')
def api_correlations_status():
    """Lightweight freshness signal: is the PCA artifact behind the study data?

    Informational only (drives a banner); the tab keeps rendering regardless.
    """
    study = (request.args.get('study') or '').strip()
    if not study:
        return jsonify({"error": "No study"}), 400

    denied = _study_access_error(study)
    if denied is not None:
        return denied

    return jsonify(correlations_service.build_status_payload(study))
