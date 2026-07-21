"""Persisted, admin-controlled site settings (e.g. signup approval gating).

Stored as a single JSON file under the existing ``users`` storage location so it
inherits the same local/GCS backend as the per-user files. Schema is intentionally
open: keys may be added over time without migrations.
"""

import fyp.data_io as data_io
from fyp.analysis.embedding_backends.settings import (
    EMBEDDING_BACKEND_KEY,
    get_embedding_backend as get_embedding_backend,  # re-export (read side lives in fyp)
)
from fyp.annotation.backends.settings import (
    ANNOTATION_BACKEND_KEY,
    get_annotation_backend as get_annotation_backend,  # re-export (read side lives in fyp)
)


SETTINGS_FILENAME = "admin_settings.json"

# The [machine] model/generation parameters are deliberately NOT admin
# settings: they live in config/config.toml only and changing them requires a
# rebuild/redeploy. Only the backend selectors are runtime-editable.
DEFAULTS: dict = {
    "new_user_admin_approval_required": False,
    "default_new_user_role": "viewer",
    ANNOTATION_BACKEND_KEY: "gemini",
    EMBEDDING_BACKEND_KEY: "gemini",
}


# Per-key type for /api/admin/settings PUT validation. Keys not listed here
# default to bool (legacy behaviour). Add an entry whenever a non-bool setting
# is introduced so the route can validate it without growing a switch statement.
SETTING_TYPES: dict = {
    "new_user_admin_approval_required": bool,
    "default_new_user_role": str,
    ANNOTATION_BACKEND_KEY: str,
    EMBEDDING_BACKEND_KEY: str,
}






def validate_setting_value(key: str, value) -> str | None:
    """Validate one settings value beyond its base type.

    Args:
        key: Settings key (already checked against the allowed set).
        value: The proposed value (already checked against ``SETTING_TYPES``).

    Returns:
        A user-facing error message, or ``None`` when the value is valid.
    """
    if value == "" or value is None:
        return None

    if key == ANNOTATION_BACKEND_KEY:
        from fyp.annotation.backends import BACKEND_IDS
        if value not in BACKEND_IDS:
            return f"Unknown annotation backend: {value!r} (known: {list(BACKEND_IDS)})"
    elif key == EMBEDDING_BACKEND_KEY:
        from fyp.analysis.embedding_backends import BACKEND_IDS
        if value not in BACKEND_IDS:
            return f"Unknown embedding backend: {value!r} (known: {list(BACKEND_IDS)})"
    return None






def load_admin_settings() -> dict:
    """Load admin settings from disk, returning ``{}`` if the file is missing.

    Returns:
        Parsed JSON object, or an empty dict if not present / unreadable.
    """
    # A missing file is the normal first-run state — check exists() first so
    # every fresh boot doesn't print a [DATA_IO] ERROR for it.
    if not data_io.exists(storage_location="users", filename=SETTINGS_FILENAME):
        return {}
    data = data_io.load_json(storage_location="users", filename=SETTINGS_FILENAME)
    return data if isinstance(data, dict) else {}




def save_admin_settings(settings: dict) -> None:
    """Persist admin settings, replacing any existing file.

    Args:
        settings: Full settings dict to write.
    """
    data_io.save_json(data=settings, storage_location="users", filename=SETTINGS_FILENAME)




def get_setting(key: str):
    """Return a single setting, falling back to the module-level default.

    Args:
        key: Setting key.

    Returns:
        The persisted value if set, else the default from ``DEFAULTS``,
        else ``None``.
    """
    return load_admin_settings().get(key, DEFAULTS.get(key))




def get_new_user_approval_required() -> bool:
    """Whether new signups must be approved by an admin before activation."""
    return bool(get_setting("new_user_admin_approval_required"))




def get_default_new_user_role() -> str:
    """Role assigned to newly-signed-up users.

    Falls back to ``"viewer"`` if the configured role no longer exists in
    ``roles.json`` (e.g. it was deleted via the admin UI). Importing the
    ``role_manager`` lazily so this module stays free of auth-side imports.
    """
    name = get_setting("default_new_user_role") or "viewer"
    try:
        from web_interface.auth import role_manager
        if not role_manager.role_exists(name):
            return "viewer"
    except Exception:
        # If role_manager isn't initialised yet (very early boot), trust the
        # stored value — the caller already wraps add_user which validates.
        pass
    return name
