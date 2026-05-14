"""Per-user activity log for Data Management and User Management mutations.

Each entry is appended to ``{username}_log.json`` under the ``users`` storage
location. Entries are written under the **actor's** file (the user who
performed the action), not the target user. The list is capped at
``MAX_ENTRIES``; the oldest entries are trimmed on append.

A logging failure must never break the underlying action — every read/write
is wrapped in a broad try/except that only emits a log line.
"""

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf

logger = logging.getLogger(__name__)


MAX_ENTRIES = 500


CATEGORY_DATA_MANAGEMENT = "data_management"
CATEGORY_USER_MANAGEMENT = "user_management"


def _log_filename(username: str) -> str:
    return f"{username}_log.json"


def _now_iso() -> str:
    tz_name = fyp_cf.get("misc", {}).get("TIME_ZONE", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz=tz).isoformat(timespec="seconds")


def record(
    actor: str,
    category: str,
    action: str,
    target: str = "",
    details: Optional[dict] = None,
) -> None:
    """Append an event to the actor's log file. Never raises."""
    if not actor:
        return
    try:
        filename = _log_filename(actor)
        existing = None
        if data_io.exists(storage_location="users", filename=filename):
            existing = data_io.load_json(storage_location="users", filename=filename)

        if isinstance(existing, dict) and isinstance(existing.get("entries"), list):
            entries = existing["entries"]
        else:
            entries = []

        entries.append({
            "timestamp": _now_iso(),
            "category": category,
            "action": action,
            "target": target or "",
            "details": details or {},
        })

        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]

        data_io.save_json(
            data={"entries": entries},
            storage_location="users",
            filename=filename,
        )
    except Exception as e:
        logger.error(f"activity_log.record failed for actor={actor!r} action={action!r}: {e}")


def read(username: str) -> list:
    """Return the user's activity entries, newest first. Never raises."""
    if not username:
        return []
    try:
        filename = _log_filename(username)
        if not data_io.exists(storage_location="users", filename=filename):
            return []
        data = data_io.load_json(storage_location="users", filename=filename)
        if not isinstance(data, dict):
            return []
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            return []
        return list(reversed(entries))
    except Exception as e:
        logger.error(f"activity_log.read failed for username={username!r}: {e}")
        return []
