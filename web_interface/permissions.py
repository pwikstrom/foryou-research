"""Tab + sub-page permission catalog and Flask decorator.

Permissions are persisted per role in ``users/roles.json`` (see
``auth.RoleManager``). The catalog defined here is the single source of truth
for the admin permission-matrix UI and for the ``permission_required``
decorator used to gate Flask routes.

Permission keys are strings of the form ``tab.<tab_id>`` or
``tab.<tab_id>.<sub_page_id>``. Hierarchy is logical only — granting
``tab.data_management`` does NOT auto-grant its sub-pages. Each box on the
matrix is independent so admins can hide specific sub-pages.

The admin role bypasses all checks (see ``role_required`` in ``auth.py``); a
role with ``"*"`` in its permission list also has implicit full access.
"""

from functools import wraps

from flask import abort, current_app
from flask_login import current_user




PERMISSION_CATALOG: list[dict] = [
    {"key": "tab.explore",                          "label": "Explore"},
    {"key": "tab.timelines",                        "label": "Timelines"},
    {"key": "tab.video_analysis",                   "label": "Video Analysis"},
    {"key": "tab.correlations",                     "label": "Correlations"},
    {"key": "tab.my_studies",                       "label": "My Studies"},
    {"key": "tab.data_management",                  "label": "Data Management"},
    {"key": "tab.data_management.ingestion",        "label": "  • Ingest Collections"},
    {"key": "tab.data_management.edit_collections", "label": "  • Edit Collections"},
    {"key": "tab.data_management.studies",          "label": "  • Define Studies"},
    {"key": "tab.data_management.enrichment",       "label": "  • Scrape & Annotate"},
    {"key": "tab.data_management.refresh",          "label": "  • Refresh Caches"},
    {"key": "tab.admin",                            "label": "Admin"},
    {"key": "tab.admin.new_users",                  "label": "  • New Users"},
    {"key": "tab.admin.active_users",               "label": "  • Active Users"},
    {"key": "tab.admin.roles",                      "label": "  • User Roles"},
    {"key": "tab.admin.annotations",                "label": "  • User Annotations"},
    {"key": "tab.admin.reliability",                "label": "  • Inter-coder Reliability"},
    {"key": "tab.admin.general",                    "label": "  • General"},
]


ALL_PERMISSION_KEYS: set[str] = {entry["key"] for entry in PERMISSION_CATALOG}


# Default permission set assigned to any non-admin role created in legacy
# (list-format) roles.json. Preserves the historical "viewer" experience —
# the four always-on view tabs plus My Studies.
DEFAULT_NON_ADMIN_PERMISSIONS: list[str] = [
    "tab.explore",
    "tab.timelines",
    "tab.video_analysis",
    "tab.correlations",
    "tab.my_studies",
]




def user_has_permission(user, perm_key: str) -> bool:
    """Return True if ``user`` is allowed to access ``perm_key``.

    Admin role bypasses all checks. A role whose stored permissions include
    ``"*"`` also has implicit full access.

    Args:
        user: A ``User`` instance (or anything with ``is_admin()`` and ``role``).
        perm_key: A key from ``ALL_PERMISSION_KEYS``.

    Returns:
        True if the user can access the permission, False otherwise.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if hasattr(user, "is_admin") and user.is_admin():
        return True

    # Imported lazily to avoid a circular import with auth.py at module load.
    from web_interface.auth import role_manager

    perms = role_manager.get_role_permissions(getattr(user, "role", None))
    if "*" in perms:
        return True
    return perm_key in perms




def get_user_permissions(user) -> list[str]:
    """Return the effective permission list for ``user``.

    Admin and ``"*"`` roles expand to the full catalog. Used by the template
    to inject ``window.USER_PERMS`` for client-side defensive checks.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    if hasattr(user, "is_admin") and user.is_admin():
        return sorted(ALL_PERMISSION_KEYS)

    from web_interface.auth import role_manager

    perms = role_manager.get_role_permissions(getattr(user, "role", None))
    if "*" in perms:
        return sorted(ALL_PERMISSION_KEYS)
    return [p for p in perms if p in ALL_PERMISSION_KEYS]




def permission_required(*perm_keys: str):
    """Decorator that gates a Flask route on one or more permission keys.

    Mirrors ``auth.role_required``: redirects unauthenticated users to the
    login flow, lets admins through unconditionally, and otherwise enforces
    that the user holds **at least one** of the listed permissions. Pass a
    single key for the common case; pass several to cover an endpoint that
    serves multiple sub-pages (e.g. ``/api/admin/users`` powers both
    "New Users" and "Active Users").

    Args:
        *perm_keys: One or more permission keys from the catalog.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            if not any(user_has_permission(current_user, key) for key in perm_keys):
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator
