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
    {"key": "tab.sessions",                         "label": "Sessions"},
    {"key": "tab.my_stuff.my_studies",              "label": "My stuff — My Studies"},
    {"key": "tab.my_stuff.tasks",                   "label": "My stuff — My Tasks"},
    {"key": "tab.my_stuff.preferences",             "label": "My stuff — Preferences"},
    {"key": "tab.my_stuff.video_tags",              "label": "My stuff — My Video Tags"},
    {"key": "tab.my_stuff.profile",                 "label": "My stuff — Profile"},
    {"key": "tab.data_management.ingestion",        "label": "Data Management — Ingest Collections"},
    {"key": "tab.data_management.edit_collections", "label": "Data Management — Edit Collections"},
    {"key": "tab.data_management.studies",          "label": "Data Management — Define Studies"},
    {"key": "tab.data_management.scrape",           "label": "Data Management — Scrape"},
    {"key": "tab.data_management.annotation",       "label": "Data Management — Annotation"},
    {"key": "tab.data_management.refresh",          "label": "Data Management — Dataset Assembly"},
    {"key": "tab.admin.new_users",                  "label": "Admin — New Users"},
    {"key": "tab.admin.active_users",               "label": "Admin — Active Users"},
    {"key": "tab.admin.roles",                      "label": "Admin — User Roles"},
    {"key": "tab.admin.annotations",                "label": "Admin — User Annotations"},
    {"key": "tab.admin.backends",                   "label": "Admin — Backends"},
    {"key": "tab.admin.versions",                   "label": "Admin — Versions"},
    {"key": "tab.admin.ab_eval",                    "label": "Admin — Contracts"},
    {"key": "tab.admin.human_eval",                 "label": "Admin — Reliability Control"},
    {"key": "tab.admin.schema",                     "label": "Admin — Variable Visibility"},
    {"key": "tab.admin.data_contracts",             "label": "Admin — Data Contracts"},
    {"key": "tab.admin.stoplist",                   "label": "Admin — Hashtag Stoplist"},
    {"key": "tab.admin.scrapers",                   "label": "Admin — Scrapers"},
    {"key": "tab.admin.general",                    "label": "Admin — Site Settings"},
    {"key": "tab.admin.system_info",                "label": "Admin — System Information"},
    {"key": "feature.annotation_votes",             "label": "Voting — annotation demand signals"},
]


ALL_PERMISSION_KEYS: set[str] = {entry["key"] for entry in PERMISSION_CATALOG}


# Parent-tab keys are implicitly granted when any of their sub-pages is granted —
# the matrix UI hides them and only stores sub-page permissions, but server-side
# checks (Jinja, decorators) still ask about the parent. Keep this list in sync
# with the sub-page prefixes in PERMISSION_CATALOG above.
PARENT_TAB_KEYS: set[str] = {"tab.data_management", "tab.admin", "tab.my_stuff"}


# Default permission set assigned to any non-admin role created in legacy
# (list-format) roles.json. Preserves the historical "viewer" experience —
# the view tabs plus the personal "My stuff" pages.
DEFAULT_NON_ADMIN_PERMISSIONS: list[str] = [
    "tab.explore",
    "tab.timelines",
    "tab.video_analysis",
    "tab.correlations",
    "tab.semantic_space",
    "tab.sessions",
    "tab.my_stuff.my_studies",
    "tab.my_stuff.tasks",
    "tab.my_stuff.preferences",
    "tab.my_stuff.video_tags",
    "tab.my_stuff.profile",
]


# Permission set for the built-in read-only "student" role: the analysis tabs
# plus the personal My-stuff pages. Deliberately excluded: tab.semantic_space
# (the embedding map is corpus-global, so any holder sees the *real* corpus,
# not just shared studies — grant per-installation if that is acceptable),
# tab.sessions (chronological session sequences are the Hub's most
# re-identifying view of a donor), every tab.data_management.* / tab.admin.*
# key, and feature.annotation_votes (votes are demand signals feeding the paid
# annotation queue).
STUDENT_PERMISSIONS: list[str] = [
    "tab.explore",
    "tab.timelines",
    "tab.video_analysis",
    "tab.correlations",
    "tab.my_stuff.my_studies",
    "tab.my_stuff.tasks",
    "tab.my_stuff.preferences",
    "tab.my_stuff.video_tags",
    "tab.my_stuff.profile",
]


# Roles the boot-time roles.json migration must never touch. The grant-all
# list below is re-applied on every boot, which would silently hand the
# student role the voting key each restart even after an admin removed it.
# Skipping the role entirely is safe because migrations only append keys —
# admin matrix edits to a skipped role therefore persist.
PERMISSION_MIGRATION_SKIP_ROLES: set[str] = {"student"}


# Boot-time roles.json migration data (see RoleManager._migrate_permission_keys):
# renamed keys map old→new; the grant-all list is added to every non-"*" role
# because those pages were previously ungated (Settings, Coding) for all users.
PERMISSION_KEY_RENAMES: dict[str, str] = {
    "tab.my_studies": "tab.my_stuff.my_studies",
}
PERMISSION_KEYS_GRANT_ALL: list[str] = [
    "tab.my_stuff.tasks",
    "tab.my_stuff.preferences",
    "tab.my_stuff.video_tags",
    "tab.my_stuff.profile",
    # 2026-07 (S4): the vote endpoints used to be ungated for any logged-in
    # user; existing roles keep voting, the student role (skip-listed) does not.
    # Note: grant-all keys are re-appended every boot, so they cannot be
    # durably revoked from a non-skipped role via the admin matrix.
    "feature.annotation_votes",
]

# Implied grants for the 2026-07 Admin-tab restructure: pages that used to live
# inside a broader sub-page (General, Variable Visibility) became their own
# sidebar entries with their own keys. Any role that held the old umbrella key
# gets the split-out keys, so existing roles keep seeing exactly what they saw.
# (The new "tab.admin.scrapers" page is deliberately NOT implied — it is new
# functionality, granted explicitly or via the admin role.)
PERMISSION_KEY_IMPLIED_GRANTS: dict[str, list[str]] = {
    "tab.admin.general": ["tab.admin.backends", "tab.admin.stoplist"],
    "tab.admin.schema": ["tab.admin.versions", "tab.admin.ab_eval"],
    # 2026-07 read-only Data Contracts page: version history for the scrape /
    # activity contracts, so it rides with the annotation Versions key.
    "tab.admin.versions": ["tab.admin.data_contracts"],
    # 2026-07 Data Management restructure: "Scrape & Annotate" split into a
    # Scrape page and an Annotation page. Roles that held the old enrichment
    # key gain both new keys; the stale key stays in roles.json harmlessly.
    "tab.data_management.enrichment": [
        "tab.data_management.scrape",
        "tab.data_management.annotation",
    ],
}




# --- Pipeline tour (home pane / public guide) ---------------------------------

# The Hub presents itself as a five-stage pipeline (Ingest -> Enrich -> Annotate
# -> Analyse -> Share). The logged-in home pane is a user guide, so it shows a
# stage only when the user actually holds a page inside it: an analysis-only
# account gets the Analyse stage and nothing else. The public /thehub page is a
# description of the whole system and deliberately shows all five.
#
# Order matters — it is the order the stepper renders in.
PIPELINE_STEPS: list[str] = ["ingest", "enrich", "annotate", "analyse", "share"]

# A stage is visible when the user holds ANY of its keys.
PIPELINE_STEP_PERMISSIONS: dict[str, list[str]] = {
    "ingest": [
        "tab.data_management.ingestion",
        "tab.data_management.edit_collections",
        "tab.data_management.studies",
    ],
    "enrich": [
        "tab.data_management.scrape",
    ],
    "annotate": [
        "tab.data_management.annotation",
        "tab.admin.ab_eval",
        "tab.admin.versions",
        "tab.admin.human_eval",
    ],
    "analyse": [
        "tab.explore",
        "tab.timelines",
        "tab.video_analysis",
        "tab.sessions",
        "tab.correlations",
        "tab.semantic_space",
    ],
    "share": [
        "tab.admin.roles",
        "tab.admin.active_users",
        "tab.admin.new_users",
    ],
}


def visible_pipeline_steps(user) -> list[str]:
    """Return the pipeline stages ``user`` has at least one page inside.

    Args:
        user: A ``User`` instance (or anything ``user_has_permission`` accepts).

    Returns:
        A subset of ``PIPELINE_STEPS`` in pipeline order — possibly empty for a
        user with no gated pages at all, in which case the caller drops the
        tour entirely rather than rendering an empty stepper.
    """
    return [
        step for step in PIPELINE_STEPS
        if any(user_has_permission(user, key) for key in PIPELINE_STEP_PERMISSIONS[step])
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
