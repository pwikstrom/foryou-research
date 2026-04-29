"""Persisted, admin-controlled site settings (e.g. signup approval gating).

Stored as a single JSON file under the existing ``users`` storage location so it
inherits the same local/GCS backend as the per-user files. Schema is intentionally
open: keys may be added over time without migrations.
"""

import fyp.data_io as data_io


SETTINGS_FILENAME = "admin_settings.json"

DEFAULTS: dict = {
    "new_user_admin_approval_required": False,
}




def load_admin_settings() -> dict:
    """Load admin settings from disk, returning ``{}`` if the file is missing.

    Returns:
        Parsed JSON object, or an empty dict if not present / unreadable.
    """
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
