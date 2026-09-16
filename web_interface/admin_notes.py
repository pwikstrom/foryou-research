"""Admin's log: free-text notes an admin attaches to a user account.

A small notebook per user. Each note records who wrote it and when, and is
stored in ``{username}_notes.json`` under the ``users`` storage location,
next to the account record and its activity-log sidecar. Unlike the activity
log (which is keyed by the *actor*), notes are keyed by the *subject*: the
account the note is about.

Notes are admin-only working memory ("called about consent form", "second
donation expected in October") and are never shown to the account holder.
A storage failure must never break the request — reads fall back to an
empty list, writes report failure through their return value.
"""

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf

logger = logging.getLogger(__name__)


MAX_NOTE_CHARS = 4000
STORAGE_LOCATION = "users"


def _filename(username: str) -> str:
    return f"{username}_notes.json"


def _now_iso() -> str:
    tz_name = fyp_cf.get("misc", {}).get("TIME_ZONE", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz=tz).isoformat(timespec="seconds")


def _load(username: str) -> list:
    """Return the stored notes in insertion (oldest-first) order. Never raises."""
    try:
        filename = _filename(username)
        if not data_io.exists(storage_location=STORAGE_LOCATION, filename=filename):
            return []
        data = data_io.load_json(storage_location=STORAGE_LOCATION, filename=filename)
        notes = data.get("notes") if isinstance(data, dict) else None
        return [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []
    except Exception as e:
        logger.error(f"admin_notes load failed for {username!r}: {e}")
        return []


def _save(username: str, notes: list) -> bool:
    try:
        data_io.save_json(
            data={"notes": notes},
            storage_location=STORAGE_LOCATION,
            filename=_filename(username),
        )
        return True
    except Exception as e:
        logger.error(f"admin_notes save failed for {username!r}: {e}")
        return False


def read(username: str) -> list:
    """Return the user's notes, newest first."""
    if not username:
        return []
    return list(reversed(_load(username)))


def add(username: str, author: str, text: str) -> tuple[dict | None, str | None]:
    """Append a note about ``username`` written by ``author``.

    Returns ``(note, None)`` on success or ``(None, error_message)``.
    """
    if not username:
        return None, "Missing username"
    if not author:
        return None, "Missing author"
    text = (text or "").strip()
    if not text:
        return None, "Note text is empty"
    if len(text) > MAX_NOTE_CHARS:
        return None, f"Note is too long (max {MAX_NOTE_CHARS} characters)"

    note = {
        "id": uuid.uuid4().hex,
        "timestamp": _now_iso(),
        "author": author,
        "text": text,
    }
    notes = _load(username)
    notes.append(note)
    if not _save(username, notes):
        return None, "Failed to save note"
    return note, None


def delete(username: str, note_id: str) -> tuple[dict | None, str | None]:
    """Remove one note by id. Returns ``(removed_note, None)`` or ``(None, error)``."""
    if not username or not note_id:
        return None, "Missing username or note id"
    notes = _load(username)
    remaining = [n for n in notes if n.get("id") != note_id]
    if len(remaining) == len(notes):
        return None, "Note not found"
    removed = next(n for n in notes if n.get("id") == note_id)
    if not _save(username, remaining):
        return None, "Failed to save notes"
    return removed, None


def remove_all(username: str) -> None:
    """Delete the notes sidecar (used when the account itself is removed). Never raises."""
    if not username:
        return
    try:
        filename = _filename(username)
        if data_io.exists(storage_location=STORAGE_LOCATION, filename=filename):
            data_io.remove(storage_location=STORAGE_LOCATION, filename=filename)
    except Exception as e:
        logger.error(f"admin_notes remove_all failed for {username!r}: {e}")
