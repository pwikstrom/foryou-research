"""Read-side accessor for the embedding-backend setting in the admin store.

The store itself (``users/admin_settings.json``) is owned and written by
``web_interface/admin_settings.py``; this module is the read-only view the
``fyp`` core uses so the dependency keeps pointing web_interface → fyp.
The key name defined here is imported by the web layer's validation, so the
two sides cannot drift. The filename constant is shared with the annotation
settings read side for the same reason.
"""

import fyp.data_io as data_io
from fyp.annotation.backends.settings import SETTINGS_FILENAME

EMBEDDING_BACKEND_KEY = "embedding_backend"

__all__ = ["EMBEDDING_BACKEND_KEY", "SETTINGS_FILENAME", "get_embedding_backend"]






def get_embedding_backend() -> str:
    """The admin-selected embedding backend id (default ``"gemini"``).

    Returns:
        The stored backend id, or ``"gemini"`` when unset/unreadable.
    """
    try:
        # A missing file is the normal first-run state — check exists() first
        # so a fresh boot doesn't log a data_io error for it.
        if not data_io.exists(storage_location="users", filename=SETTINGS_FILENAME):
            return "gemini"
        data = data_io.load_json(storage_location="users", filename=SETTINGS_FILENAME)
        value = data.get(EMBEDDING_BACKEND_KEY) if isinstance(data, dict) else None
    except Exception:
        return "gemini"
    return value if isinstance(value, str) and value else "gemini"
