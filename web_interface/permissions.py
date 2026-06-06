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
    {"key": "tab.semantic_space",                   "label": "Semantic Space"},
    {"key": "tab.my_studies",                       "label": "My Studies"},
    {"key": "tab.data_management.ingestion",        "label": "Data Management — Ingest Collections"},
    {"key": "tab.data_management.edit_collections", "label": "Data Management — Edit Collections"},
    {"key": "tab.data_management.studies",          "label": "Data Management — Define Studies"},
    {"key": "tab.data_management.enrichment",       "label": "Data Management — Scrape & Annotate"},
    {"key": "tab.data_management.refresh",          "label": "Data Management — Refresh Caches"},
    {"key": "tab.admin.new_users",                  "label": "Admin — New Users"},
    {"key": "tab.admin.active_users",               "label": "Admin — Active Users"},
    {"key": "tab.admin.roles",                      "label": "Admin — User Roles"},
    {"key": "tab.admin.annotations",                "label": "Admin — User Annotations"},
    {"key": "tab.admin.reliability",                "label": "Admin — Inter-coder Reliability"},
    {"key": "tab.admin.general",                    "label": "Admin — General"},
    {"key": "tab.admin.schema",                     "label": "Admin — Variable Schema"},
]


ALL_PERMISSION_KEYS: set[str] = {entry["key"] for entry in PERMISSION_CATALOG}


# Parent-tab keys are implicitly granted when any of their sub-pages is granted —
# the matrix UI hides them and only stores sub-page permissions, but server-side
# checks (Jinja, decorators) still ask about the parent. Keep this list in sync
# with the sub-page prefixes in PERMISSION_CATALOG above.
PARENT_TAB_KEYS: set[str] = {"tab.data_management", "tab.admin"}


# Default permission set assigned to any non-admin role created in legacy
# (list-format) roles.json. Preserves the historical "viewer" experience —
# the four always-on view tabs plus My Studies.
DEFAULT_NON_ADMIN_PERMISSIONS: list[str] = [
    "tab.explore",
    "tab.timelines",
    "tab.video_analysis",
    "tab.correlations",
    "tab.semantic_space",
    "tab.my_studies",
]




def user_has_permission(user, perm_key: str) -> bool:
    """Return True if ``user`` is allowed to access ``perm_key``.

    Admin role bypasses all checks. A role whose stored permissions include
    ``"*"`` also has implicit full access. Parent-tab keys (``tab.admin`` and
    ``tab.data_management``) are implicitly granted whenever any of their
    sub-pages is granted, so the matrix UI only needs to expose sub-page rows.

    Args:
        user: A ``User`` instance (or anything with ``is_admin()`` and ``role``).
        perm_key: A key from ``ALL_PERMISSION_KEYS`` (or a parent-tab key).

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
    if perm_key in perms:
        return True
    if perm_key in PARENT_TAB_KEYS:
        prefix = perm_key + "."
        return any(p.startswith(prefix) for p in perms)
    return False




def get_user_permissions(user) -> list[str]:
    """Return the effective permission list for ``user``.

    Admin and ``"*"`` roles expand to the full catalog plus the parent-tab
    keys. For regular roles, returns each sub-page they hold plus the parent
    tab key for every parent that has at least one sub-page granted — so JS
    checks against ``window.USER_PERMS`` work without needing to know the
    implicit-grant rule.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    if hasattr(user, "is_admin") and user.is_admin():
        return sorted(ALL_PERMISSION_KEYS | PARENT_TAB_KEYS)

    from web_interface.auth import role_manager

    perms = role_manager.get_role_permissions(getattr(user, "role", None))
    if "*" in perms:
        return sorted(ALL_PERMISSION_KEYS | PARENT_TAB_KEYS)

    effective = {p for p in perms if p in ALL_PERMISSION_KEYS}
    for parent in PARENT_TAB_KEYS:
        prefix = parent + "."
        if any(p.startswith(prefix) for p in effective):
            effective.add(parent)
    return sorted(effective)




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
