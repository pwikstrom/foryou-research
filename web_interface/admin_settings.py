"""Persisted, admin-controlled site settings (e.g. signup approval gating).

Stored as a single JSON file under the existing ``users`` storage location so it
inherits the same local/GCS backend as the per-user files. Schema is intentionally
open: keys may be added over time without migrations.
"""

import fyp.data_io as data_io
from fyp.annotation.backends.settings import (
    ANNOTATION_BACKEND_KEY,
    MACHINE_OVERRIDE_KEYS,
    get_annotation_backend as get_annotation_backend,  # re-export (read side lives in fyp)
    get_machine_overrides as get_machine_overrides,  # re-export
)


SETTINGS_FILENAME = "admin_settings.json"

# Annotation settings: the backend selector plus runtime overrides of the five
# [machine] config values. Empty string = "no override, use config.toml".
# Key names are owned by fyp.annotation.backends.settings (the read side);
# get_annotation_backend / get_machine_overrides are re-exported from there.
_ANNOTATION_DEFAULTS = {ANNOTATION_BACKEND_KEY: "gemini",
                        **{key: "" for key in MACHINE_OVERRIDE_KEYS}}

DEFAULTS: dict = {
    "new_user_admin_approval_required": False,
    "default_new_user_role": "viewer",
    **_ANNOTATION_DEFAULTS,
}


# Per-key type for /api/admin/settings PUT validation. Keys not listed here
# default to bool (legacy behaviour). Add an entry whenever a non-bool setting
# is introduced so the route can validate it without growing a switch statement.
# The machine_* numeric keys accept int/float or "" (cleared); the route layers
# range/enum checks on top of these base types.
SETTING_TYPES: dict = {
    "new_user_admin_approval_required": bool,
    "default_new_user_role": str,
    ANNOTATION_BACKEND_KEY: str,
    "machine_model": str,
    "machine_temperature": (int, float, str),
    "machine_thinking_budget": (int, str),
    "machine_media_resolution": str,
    "machine_max_output_tokens": (int, str),
}




MEDIA_RESOLUTION_VALUES = ("", "LOW", "MEDIUM", "HIGH")






def validate_setting_value(key: str, value) -> str | None:
    """Validate one settings value beyond its base type.

    Args:
        key: Settings key (already checked against the allowed set).
        value: The proposed value (already checked against ``SETTING_TYPES``).

    Returns:
        A user-facing error message, or ``None`` when the value is valid.
    """
    numeric_keys = ("machine_temperature", "machine_thinking_budget",
                    "machine_max_output_tokens")
    # bool is an int subclass — reject it explicitly for the numeric keys.
    if key in numeric_keys and isinstance(value, bool):
        return f"Setting '{key}' must be a number"
    if value == "" or value is None:
        return None  # cleared override — always valid
    # str is allowed in SETTING_TYPES only as the "" cleared sentinel.
    if key in numeric_keys and isinstance(value, str):
        return f"Setting '{key}' must be a number (or \"\" to clear the override)"

    if key == ANNOTATION_BACKEND_KEY:
        from fyp.annotation.backends import BACKEND_IDS
        if value not in BACKEND_IDS:
            return f"Unknown annotation backend: {value!r} (known: {list(BACKEND_IDS)})"
    elif key == "machine_media_resolution":
        if str(value).upper() not in MEDIA_RESOLUTION_VALUES:
            return f"machine_media_resolution must be one of {MEDIA_RESOLUTION_VALUES}"
    elif key == "machine_temperature":
        if not 0.0 <= float(value) <= 2.0:
            return "machine_temperature must be between 0.0 and 2.0"
    elif key == "machine_thinking_budget":
        if int(value) < -1:
            return "machine_thinking_budget must be >= -1"
    elif key == "machine_max_output_tokens":
        if int(value) <= 0:
            return "machine_max_output_tokens must be positive"
    elif key == "machine_model":
        if not str(value).strip():
            return "machine_model must be a non-empty model id"
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
