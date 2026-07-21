"""Read-side accessors for annotation settings in the admin settings store.

The store itself (``users/admin_settings.json``) is owned and written by
``web_interface/admin_settings.py``; this module is the read-only view the
``fyp`` core uses so the dependency keeps pointing web_interface → fyp.
The key names defined here are imported by the web layer's validation, so the
two sides cannot drift.
"""

import fyp.data_io as data_io

# Must match web_interface.admin_settings.SETTINGS_FILENAME (web imports the
# admin-settings machinery; fyp only reads the same file via data_io).
SETTINGS_FILENAME = "admin_settings.json"

ANNOTATION_BACKEND_KEY = "annotation_backend"






def _load_settings() -> dict:
    """Load the admin settings JSON; empty dict on any failure."""
    try:
        # A missing file is the normal first-run state — check exists() first
        # so a fresh boot doesn't log a data_io error for it.
        if not data_io.exists(storage_location="users", filename=SETTINGS_FILENAME):
            return {}
        data = data_io.load_json(storage_location="users", filename=SETTINGS_FILENAME)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}






def get_annotation_backend() -> str:
    """The admin-selected annotation backend id (default ``"gemini"``).

    Returns:
        The stored backend id, or ``"gemini"`` when unset/unreadable.
    """
    value = _load_settings().get(ANNOTATION_BACKEND_KEY)
    return value if isinstance(value, str) and value else "gemini"
