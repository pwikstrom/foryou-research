"""Durable background-task failure ledger (``task_failures.json`` in ``cache``).

Cloud Tasks HTTP queues have no dead-letter topic, and a task's own status file
is overwritten by the next run (``GCSStatusReporter.resume`` even clears the
previous chain link's error). Before this module the only trace of a failure was
``last_run_outcome="Fail"`` on one process_stats key — no history, no attempt
count, no cross-task view. This ledger is that missing audit trail: every failed
attempt is appended here, and entries whose disposition is ``dead`` are the
dead-letter record an admin must look at.

Two dispositions:
    * ``retrying`` — the handler returned 5xx and Cloud Tasks will try again.
    * ``dead``     — terminal: either the task is not retry-safe, or the retries
      are exhausted. Surfaces as a System Health failure until acknowledged.

Writes go through ``data_io.update_json`` (compare-and-swap) so the web service
and the task runner can never clobber each other's entries. Every function is
non-raising by design: bookkeeping must never turn a task failure into a crash.
"""

import uuid
from datetime import UTC, datetime, timedelta

import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

FAILURES_LOCATION = "cache"
FAILURES_FILENAME = "task_failures.json"

# Ledger cap — oldest entries are trimmed on append (same policy as
# ``activity_log.MAX_ENTRIES``).
MAX_ENTRIES = 200

# How much of a traceback to keep per entry. Enough to identify the failure,
# bounded so the ledger stays a small JSON object.
MAX_ERROR_CHARS = 2000

DISPOSITION_RETRYING = "retrying"
DISPOSITION_DEAD = "dead"

# task_args keys that may hold user-identifying or bulky values; the ledger is
# an operational record, not a data store.
_REDACT_ARG_KEYS = {"launched_by", "item_ids", "collections", "arms_spec"}






def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()






def _redact_args(task_args: dict | None) -> dict:
    """Return a compact, log-safe copy of a task's arguments."""
    if not isinstance(task_args, dict):
        return {}
    out = {}
    for key, value in task_args.items():
        if key in _REDACT_ARG_KEYS:
            out[key] = "<redacted>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = f"<{type(value).__name__}>"
    return out






def load_failures() -> list:
    """Return the ledger entries, newest last (never raises)."""
    try:
        # A missing file is the normal no-failures state — check exists() first
        # so every health poll doesn't log a [DATA_IO] load error for it.
        if not data_io.exists(storage_location=FAILURES_LOCATION,
                              filename=FAILURES_FILENAME):
            return []
        entries = data_io.load_json(storage_location=FAILURES_LOCATION,
                                    filename=FAILURES_FILENAME)
        return entries if isinstance(entries, list) else []
    except Exception as e:
        logger.warning(f"Could not load task failures: {e}")
        return []






def record_failure(task: str, error: str, status_key: str = "",
                   retry_count: int = 0, disposition: str = DISPOSITION_DEAD,
                   task_args: dict | None = None, phase: str = "run") -> None:
    """Append one failed attempt to the ledger. Never raises.

    Args:
        task: task name, e.g. ``"pca_refresh"``.
        error: exception text / traceback (truncated to ``MAX_ERROR_CHARS``).
        status_key: the task's status-file key (differs from ``task`` for
            per-study refreshes).
        retry_count: Cloud Tasks' ``X-CloudTasks-TaskRetryCount`` for this
            attempt (0 on the first try).
        disposition: ``DISPOSITION_RETRYING`` or ``DISPOSITION_DEAD``.
        task_args: the task's arguments; redacted before storing.
        phase: where it failed — ``"run"``, ``"chain_dispatch"``, ``"fork"``,
            or ``"subprocess"``.
    """
    text = str(error or "")
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "… (truncated)"

    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "task": task,
        "status_key": status_key or task,
        "retry_count": int(retry_count or 0),
        "disposition": disposition,
        "phase": phase,
        "error": text,
        "task_args": _redact_args(task_args),
        "acknowledged": False,
    }

    def _mutate(entries):
        entries = entries if isinstance(entries, list) else []
        entries.append(entry)
        return entries[-MAX_ENTRIES:]

    try:
        data_io.update_json(storage_location=FAILURES_LOCATION,
                            filename=FAILURES_FILENAME, mutate=_mutate, default=[])
        logger.warning(f"  [tasks] Recorded {disposition} failure for '{task}' "
                       f"(attempt {entry['retry_count']}, phase={phase}).")
    except Exception as e:
        logger.warning(f"Could not record task failure for '{task}': {e}")






def acknowledge(entry_id: str = "") -> int:
    """Mark ledger entries as acknowledged. Never raises.

    Args:
        entry_id: a single entry's id, or ``""`` to acknowledge every entry.

    Returns:
        The number of entries changed (0 on any failure).
    """
    changed = [0]

    def _mutate(entries):
        entries = entries if isinstance(entries, list) else []
        for item in entries:
            if not isinstance(item, dict) or item.get("acknowledged"):
                continue
            if entry_id and item.get("id") != entry_id:
                continue
            item["acknowledged"] = True
            changed[0] += 1
        if not changed[0]:
            return None  # nothing to write
        return entries

    try:
        data_io.update_json(storage_location=FAILURES_LOCATION,
                            filename=FAILURES_FILENAME, mutate=_mutate, default=[])
    except Exception as e:
        logger.warning(f"Could not acknowledge task failures: {e}")
        return 0
    return changed[0]






def unacknowledged_dead(within_hours: int = 48) -> list:
    """Return recent unacknowledged dead-letter entries (never raises).

    Args:
        within_hours: only entries newer than this are returned.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=within_hours)
    out = []
    for item in load_failures():
        if not isinstance(item, dict):
            continue
        if item.get("acknowledged") or item.get("disposition") != DISPOSITION_DEAD:
            continue
        try:
            if datetime.fromisoformat(item.get("ts", "")) < cutoff:
                continue
        except (TypeError, ValueError):
            pass  # unparseable timestamp — surface it rather than hide it
        out.append(item)
    return out
