"""Persisted, admin-controlled site settings (e.g. signup approval gating).

Stored as a single JSON file under the existing ``users`` storage location so it
inherits the same local/GCS backend as the per-user files. Schema is intentionally
open: keys may be added over time without migrations.
"""

import threading
import time

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
    # Cost guardrails: the most items a single queue-build request from a
    # NON-admin user may add to the annotation / scrape queues (0 = unlimited).
    # Admins always bypass. Server-side clamp — the UI shows when it applied.
    "queue_cap_annotation_items": 5000,
    "queue_cap_scrape_items": 10000,
    # Sessions-tab list floors. These entries are the LAST-RESORT fallbacks —
    # resolution is admin setting > [sessions] config > here (see
    # get_session_floors), so an instance that never opens the admin page keeps
    # its committed config values. Deliberately the structurally safe pair plus
    # no coverage floor: nothing findable is hidden at these values.
    "sessions_min_plays": 4,
    "sessions_min_minutes": 0.0,
    "sessions_min_coverage_pct": 0.0,
}


# Sessions-tab floors, in (settings key, [sessions] config key) pairs.
SESSION_FLOOR_KEYS: dict = {
    "sessions_min_plays": "min_session_plays",
    "sessions_min_minutes": "min_session_minutes",
    "sessions_min_coverage_pct": "min_session_coverage_pct",
}


# Per-key type for /api/admin/settings PUT validation. Keys not listed here
# default to bool (legacy behaviour). Add an entry whenever a non-bool setting
# is introduced so the route can validate it without growing a switch statement.
SETTING_TYPES: dict = {
    "new_user_admin_approval_required": bool,
    "default_new_user_role": str,
    ANNOTATION_BACKEND_KEY: str,
    EMBEDDING_BACKEND_KEY: str,
    "queue_cap_annotation_items": int,
    "queue_cap_scrape_items": int,
    "sessions_min_plays": int,
    "sessions_min_minutes": (int, float),
    "sessions_min_coverage_pct": (int, float),
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
        from fyp.annotation.backends import variants
        known = variants.selection_ids()
        if value not in known:
            return f"Unknown annotation backend: {value!r} (known: {list(known)})"
    elif key == EMBEDDING_BACKEND_KEY:
        from fyp.analysis.embedding_backends import BACKEND_IDS
        if value not in BACKEND_IDS:
            return f"Unknown embedding backend: {value!r} (known: {list(BACKEND_IDS)})"
    elif key in ("queue_cap_annotation_items", "queue_cap_scrape_items"):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return f"{key} must be a non-negative integer (0 = unlimited)"
    elif key in SESSION_FLOOR_KEYS:
        # bool is an int subclass — reject it explicitly or True becomes 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return f"{key} must be a non-negative number (0 = no floor)"
        if key == "sessions_min_coverage_pct" and value > 100:
            return "sessions_min_coverage_pct must be a percentage between 0 and 100"
    return None






def get_queue_cap(queue_kind: str) -> int:
    """Return the non-admin per-request queue-build cap for a queue kind.

    Args:
        queue_kind: ``"annotation"`` or ``"scrape"``.

    Returns:
        The cap as an int; 0 means unlimited.
    """
    key = f"queue_cap_{queue_kind}_items"
    try:
        value = int(get_setting(key) or 0)
    except (TypeError, ValueError):
        value = int(DEFAULTS.get(key, 0))
    return max(0, value)






def get_session_floors() -> dict:
    """Effective Sessions-tab list floors, keyed by settings key.

    Resolution order per key: the admin setting if an admin has ever saved one,
    else the ``[sessions]`` config seed, else :data:`DEFAULTS`. Config stays the
    committed default so an instance that never opens the admin page keeps its
    deployed behaviour; the admin store is the runtime override, so changing a
    floor needs no rebuild.

    Values are floors, never negative; the coverage entry is a percentage
    (0-100), which is what the admin types — callers convert to the 0-1
    fraction the index stores.

    Returns:
        ``{"sessions_min_plays": int, "sessions_min_minutes": float,
        "sessions_min_coverage_pct": float}``.
    """
    from fyp.fyp_config import fyp_cf

    stored = load_admin_settings()
    cfg = fyp_cf.get("sessions", {})
    if not isinstance(cfg, dict):
        cfg = {}

    out: dict = {}
    for key, cfg_key in SESSION_FLOOR_KEYS.items():
        raw = stored.get(key, cfg.get(cfg_key, DEFAULTS.get(key)))
        caster = int if key == "sessions_min_plays" else float
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            value = caster(DEFAULTS.get(key, 0))
        out[key] = max(value, caster(0))
    out["sessions_min_coverage_pct"] = min(out["sessions_min_coverage_pct"], 100.0)
    return out




# Short-TTL read cache: the settings ride on hot request paths (session
# floors, queue caps), and on GCS each uncached read is two network
# round-trips. The web service is the only writer, and save_admin_settings
# invalidates, so a stale read can only come from another service's write —
# bounded by the TTL.
_SETTINGS_CACHE: dict = {"ts": 0.0, "data": None}
_SETTINGS_TTL_S = 15.0
_settings_lock = threading.Lock()




def load_admin_settings() -> dict:
    """Load admin settings, returning ``{}`` if the file is missing.

    Returns:
        Parsed JSON object (a copy — callers may mutate before saving), or an
        empty dict if not present / unreadable.
    """
    now = time.monotonic()
    if _SETTINGS_CACHE["data"] is not None and now - _SETTINGS_CACHE["ts"] < _SETTINGS_TTL_S:
        return dict(_SETTINGS_CACHE["data"])
    with _settings_lock:
        if _SETTINGS_CACHE["data"] is not None and now - _SETTINGS_CACHE["ts"] < _SETTINGS_TTL_S:
            return dict(_SETTINGS_CACHE["data"])
        # A missing file is the normal first-run state — check exists() first
        # so every fresh boot doesn't print a [DATA_IO] ERROR for it.
        if not data_io.exists(storage_location="users", filename=SETTINGS_FILENAME):
            data = {}
        else:
            loaded = data_io.load_json(storage_location="users", filename=SETTINGS_FILENAME)
            data = loaded if isinstance(loaded, dict) else {}
        _SETTINGS_CACHE.update({"ts": now, "data": data})
    return dict(data)




def save_admin_settings(settings: dict) -> None:
    """Persist admin settings, replacing any existing file.

    Args:
        settings: Full settings dict to write.
    """
    data_io.save_json(data=settings, storage_location="users", filename=SETTINGS_FILENAME)
    _SETTINGS_CACHE.update({"ts": 0.0, "data": None})




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
