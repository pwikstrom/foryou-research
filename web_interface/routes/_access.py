"""Shared access-control helpers for the API route modules.

Every analysis-tab endpoint that takes a study (or collection) parameter must
verify the current user can access it — the permission decorator alone only
gates the tab, not the data. These helpers centralise the check so the route
modules don't each re-implement it (the pattern originated in
``api_correlations_routes.py`` during the Correlations Phase 0 audit).
"""

from flask import jsonify
from flask_login import current_user

from ..data_service import get_accessible_studies, get_study_collections


def current_user_ctx() -> tuple[str, str | None, bool]:
    """Return ``(username, role, is_admin)`` for the current request's user.

    Tolerates both the real ``User`` (``is_admin`` is a method) and test
    doubles where it is a plain attribute.
    """
    username = getattr(current_user, "username", current_user.id)
    role = getattr(current_user, "role", None)
    is_admin_attr = getattr(current_user, "is_admin", False)
    is_admin = is_admin_attr() if callable(is_admin_attr) else bool(is_admin_attr)
    return username, role, is_admin






def accessible_study_names() -> list[str]:
    """Return the list of study names the current user can access."""
    username, role, is_admin = current_user_ctx()
    return get_accessible_studies(username, role, is_admin)






def study_access_error(study: str):
    """Return a 403 response tuple if the user cannot access ``study``, else None."""
    if study in accessible_study_names():
        return None
    return jsonify({"error": "Access denied to this study"}), 403






def collection_access_error(collection_id: str):
    """Return a 403 response tuple unless ``collection_id`` belongs to at
    least one study the current user can access, else None."""
    wanted = str(collection_id)
    for study in accessible_study_names():
        for d in get_study_collections(study):
            if str(d.get('collection_id')) == wanted:
                return None
    return jsonify({"error": "Access denied to this collection"}), 403
