"""Durable per-process run logs (``proc_logs/<key>.json`` in ``cache``).

Before this module a background process's log lived in two places, both of them
transient: an in-memory ``deque`` in the web service (gone on restart, and
invisible to a task running in the *other* Cloud Run service), and a ``logs``
array inside the task's status file that was emptied on every
``GCSStatusReporter.start()``, on every ``stamp_task_status`` fan-out, and by
the dispatch placeholder the instant an admin pressed Start. Reading a log
meant catching it live; a minute later it was gone.

This store is the durable replacement. One JSON document per status key holds a
ring of the last ``MAX_RUNS`` runs, each with its own banner, timestamped lines,
and metadata (who started it, with which arguments, how it ended). It lives in
``cache`` — GCS in production — so the web service, the task runner and every
admin see the same log, and it survives a restart or a scale-to-zero.

Two rules the rest of the codebase depends on:

* **``append`` is the only place a line is timestamped.** Subprocess-mode lines
  travel ``LocalStatusReporter.log`` → ``print`` → the parent's
  ``enqueue_output``; only the last of those hops writes here, so a line is
  never stamped twice. ``append`` also refuses to re-stamp a line that already
  carries one.
* **Nothing here raises.** Bookkeeping must never turn a working task into a
  failed one, so every public function swallows its exceptions and logs a
  warning instead.

Writes go through ``data_io.update_json`` (compare-and-swap), so the web
service and the task runner can never clobber each other, and are batched by a
per-key flusher thread on ``FLUSH_INTERVAL`` — matching the status file's own
write throttle.
"""

import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

LOG_LOCATION = "cache"
LOG_PREFIX = "proc_logs"

# Runs retained per process. Older runs fall off the front of the ring.
MAX_RUNS = 10

# Line budget for the live run, and the harder cap applied when a run is
# archived — only the newest run is allowed to be large, so the document a
# flush rewrites every few seconds stays small.
MAX_LINES_CURRENT = 1500
MAX_LINES_ARCHIVED = 200

MAX_LINE_CHARS = 2000

# Seconds between background flushes. Matches task_status.THROTTLE_INTERVAL so
# a running task performs roughly the same number of writes it always did.
FLUSH_INTERVAL = 5.0

# Force a flush when a burst outruns the timer, bounding in-memory growth.
MAX_PENDING_LINES = 200

# Status keys become filenames. data_io joins the filename onto the storage
# root without normalising it, so an unvalidated key containing "../" would
# write outside `cache`. Validation is mandatory, not defensive.
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_STAMP_RE = re.compile(r"^\[\d\d:\d\d:\d\d\] ")

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INTERRUPTED = "interrupted"

# task_args keys that are plumbing rather than user intent — they would only
# clutter the "Args:" banner line.
_ARG_SKIP_KEYS = {
    "log_run_id", "started_by", "launched_by", "phase", "chunk_index",
    "next_task", "pipeline_remaining", "pipeline_stage_index",
    "pipeline_stage_total", "pipeline_fanout", "pipeline_leaves",
    "pipeline_fork_ts",
}

# Bulky or user-identifying values — summarised rather than printed.
_ARG_SUMMARISE_KEYS = {"arms_spec", "item_ids"}

# Re-entrancy guard. Flushing calls data_io, which logs; with the fyp logger
# sink attached that log line comes straight back into append(). Without this
# a single contended write would recurse until the stack blew.
_guard = threading.local()




def _cf() -> dict:
    """Lazy fyp_config accessor (keeps importing this module config-free)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf




def now_stamp() -> str:
    """Return the current local time as ``HH:MM:SS`` in the project timezone."""
    try:
        tz_name = _cf().get("misc", {}).get("TIME_ZONE", "UTC")
        return datetime.now(tz=ZoneInfo(tz_name)).strftime("%H:%M:%S")
    except Exception:
        return datetime.now(UTC).strftime("%H:%M:%S")




def _now_iso() -> str:
    """Return the current local time as a tz-aware ISO-8601 string."""
    try:
        tz_name = _cf().get("misc", {}).get("TIME_ZONE", "UTC")
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz=tz).isoformat(timespec="seconds")




def new_run_id() -> str:
    """Return a fresh run identifier: compact UTC timestamp plus a random tail."""
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"




def valid_key(key: str) -> bool:
    """Return True when ``key`` is safe to use as a log filename."""
    return bool(key) and bool(_KEY_RE.match(key))




def log_filename(key: str) -> str:
    """Return the storage filename for a status key.

    Args:
        key: The task's status key, e.g. ``"pca_refresh"``.

    Returns:
        The path relative to the ``cache`` location.

    Raises:
        ValueError: when the key contains anything but ``[A-Za-z0-9_.-]``.
    """
    if not valid_key(key):
        raise ValueError(f"Unsafe process-log key: {key!r}")
    return f"{LOG_PREFIX}/{key}.json"




class _KeyState:
    """In-memory buffer and flusher thread for one status key."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.run_id = ""
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


_states: dict[str, _KeyState] = {}
_states_lock = threading.Lock()




def _state(key: str) -> _KeyState:
    """Return (creating if needed) the buffer state for a status key."""
    with _states_lock:
        state = _states.get(key)
        if state is None:
            state = _KeyState(key)
            _states[key] = state
        return state




def _empty_doc(key: str) -> dict:
    """Return an empty store document for a status key."""
    return {"version": 1, "key": key, "runs": []}




def _coerce(doc, key: str) -> dict:
    """Return ``doc`` as a well-formed store document (repairing junk)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
        return _empty_doc(key)
    doc.setdefault("version", 1)
    doc.setdefault("key", key)
    return doc




def _find_run(doc: dict, run_id: str) -> dict | None:
    """Return the run record with ``run_id``, or None."""
    for run in doc.get("runs", []):
        if isinstance(run, dict) and run.get("run_id") == run_id:
            return run
    return None




def _trim_run(run: dict, cap: int) -> None:
    """Trim a run's lines to ``cap``, counting what was dropped."""
    lines = run.get("lines")
    if not isinstance(lines, list):
        run["lines"] = []
        return
    excess = len(lines) - cap
    if excess > 0:
        run["dropped"] = int(run.get("dropped", 0)) + excess
        run["lines"] = lines[-cap:]




def _summarise_args(task_args: dict | None) -> str:
    """Render a task's arguments as a short, log-safe ``k=v`` string."""
    if not isinstance(task_args, dict):
        return ""
    parts = []
    for key in sorted(task_args):
        if key in _ARG_SKIP_KEYS:
            continue
        value = task_args[key]
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple, dict)):
            parts.append(f"{key}=<{len(value)} items>")
            continue
        if key in _ARG_SUMMARISE_KEYS:
            parts.append(f"{key}=<set>")
            continue
        text = str(value)
        if len(text) > 80:
            text = text[:80] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)




def _banner(started_by: str, task_args: dict | None, mode: str) -> list[str]:
    """Return the opening lines written at the top of every run."""
    lines = [
        f"{'═' * 8} Run started · {_now_iso()} · {mode} {'═' * 8}",
        f"Started by {started_by or 'system'}",
    ]
    args = _summarise_args(task_args)
    if args:
        lines.append(f"Args: {args}")
    return lines




def _start_flusher(state: _KeyState) -> None:
    """Start the background flusher thread for a key (idempotent)."""
    if state.thread and state.thread.is_alive():
        return
    state.stop.clear()
    state.thread = threading.Thread(
        target=_flush_loop, args=(state,), daemon=True,
        name=f"run-log-flush-{state.key}",
    )
    state.thread.start()




def _stop_flusher(state: _KeyState) -> None:
    """Stop the background flusher thread for a key."""
    state.stop.set()
    thread = state.thread
    state.thread = None
    if thread and thread.is_alive():
        thread.join(timeout=5)




def _flush_loop(state: _KeyState) -> None:
    """Flush a key's pending lines on a fixed interval until stopped."""
    while not state.stop.wait(FLUSH_INTERVAL):
        flush(state.key)




def open_run(key: str, run_id: str = "", started_by: str = "",
             task_args: dict | None = None, mode: str = "cloud") -> str:
    """Begin a new run for a process, archiving whatever came before it.

    Args:
        key: The task's status key (``study_refresh__<study>`` for keyed tasks).
        run_id: The run's identifier; generated when omitted.
        started_by: Username of the admin who launched it, or "" for system.
        task_args: The task's arguments, summarised into the banner.
        mode: ``"cloud"``, ``"subprocess"`` or ``"thread"``.

    Returns:
        The run id, or "" when the store could not be written.
    """
    run_id = run_id or new_run_id()
    if not valid_key(key):
        logger.warning(f"Refusing to open a process log for unsafe key {key!r}.")
        return ""

    record = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "started_by": started_by or "",
        "mode": mode,
        "state": STATE_RUNNING,
        "ended_at": None,
        "dropped": 0,
        "lines": [f"[{now_stamp()}] {line}"
                  for line in _banner(started_by, task_args, mode)],
    }

    def _mutate(doc):
        doc = _coerce(doc, key)
        for run in doc["runs"]:
            if not isinstance(run, dict):
                continue
            if run.get("state") == STATE_RUNNING:
                run["state"] = STATE_INTERRUPTED
                run["ended_at"] = _now_iso()
            _trim_run(run, MAX_LINES_ARCHIVED)
        doc["runs"] = [r for r in doc["runs"] if isinstance(r, dict)]
        doc["runs"].append(record)
        doc["runs"] = doc["runs"][-MAX_RUNS:]
        return doc

    state = _state(key)
    with state.lock:
        state.run_id = run_id
        state.pending = []
    try:
        data_io.update_json(storage_location=LOG_LOCATION, filename=log_filename(key),
                            mutate=_mutate, default=_empty_doc(key))
    except Exception as e:
        logger.warning(f"Could not open process log for '{key}': {e}")
        return ""
    _start_flusher(state)
    return run_id




def attach_run(key: str, run_id: str = "", started_by: str = "",
               task_args: dict | None = None, mode: str = "cloud") -> str:
    """Adopt an already-open run, or open a new one.

    The Cloud Tasks path opens a run at dispatch time (in the web service, which
    is the only place that knows *who* clicked Start) and passes its id to the
    worker in ``task_args``. The worker calls this so one click produces one
    run, rather than a dispatch run plus a worker run. It is also what keeps a
    self-chaining scraper's many batches inside a single continuous log.

    Args:
        key: The task's status key.
        run_id: The run id to adopt; a new run is opened when it is missing or
            no longer the newest running run.
        started_by: Username used only when a new run has to be opened.
        task_args: Task arguments used only when a new run has to be opened.
        mode: Execution mode used only when a new run has to be opened.

    Returns:
        The run id in effect, or "" on failure.
    """
    if not valid_key(key):
        return ""
    if run_id:
        try:
            doc = _load(key)
            runs = doc.get("runs", [])
            newest = runs[-1] if runs else None
            if (isinstance(newest, dict) and newest.get("run_id") == run_id
                    and newest.get("state") == STATE_RUNNING):
                state = _state(key)
                with state.lock:
                    state.run_id = run_id
                _start_flusher(state)
                return run_id
        except Exception as e:
            logger.warning(f"Could not attach to process log for '{key}': {e}")
    return open_run(key, run_id=run_id, started_by=started_by,
                    task_args=task_args, mode=mode)




def append(key: str, message: str) -> None:
    """Buffer one log line for a run, timestamping it. Never raises.

    This is the single point at which a line acquires its ``[HH:MM:SS]``
    prefix; a message that already carries one is left alone.

    Args:
        key: The task's status key.
        message: The raw log line.
    """
    try:
        if not valid_key(key) or message is None:
            return
        text = str(message).rstrip("\n")
        if not text.strip():
            return
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "… (truncated)"
        if not _STAMP_RE.match(text):
            text = f"[{now_stamp()}] {text}"

        state = _state(key)
        with state.lock:
            if not state.run_id:
                return
            state.pending.append(text)
            overflowing = len(state.pending) >= MAX_PENDING_LINES
        # Never write from inside a write: a data_io warning logged during a
        # flush arrives here, and forcing a nested flush would recurse.
        if overflowing and not getattr(_guard, "writing", False):
            flush(key)
    except Exception as e:
        logger.warning(f"Could not append to process log for '{key}': {e}")




def flush(key: str) -> None:
    """Write a key's buffered lines to storage. Never raises.

    Args:
        key: The task's status key.
    """
    try:
        state = _state(key)
        with state.lock:
            run_id = state.run_id
            pending = list(state.pending)
        if not run_id or not pending:
            return

        def _mutate(doc):
            doc = _coerce(doc, key)
            run = _find_run(doc, run_id)
            if run is None:
                return None  # evicted from the ring — nothing to append to
            lines = run.get("lines")
            run["lines"] = (lines if isinstance(lines, list) else []) + pending
            _trim_run(run, MAX_LINES_CURRENT)
            return doc

        _guard.writing = True
        try:
            data_io.update_json(storage_location=LOG_LOCATION,
                                filename=log_filename(key), mutate=_mutate,
                                default=_empty_doc(key))
        finally:
            _guard.writing = False

        # Drop exactly what was persisted; anything appended meanwhile stays.
        with state.lock:
            state.pending = state.pending[len(pending):]
    except Exception as e:
        logger.warning(f"Could not flush process log for '{key}': {e}")




def detach(key: str) -> None:
    """Flush and stop writing, leaving the run open. Never raises.

    Used at a self-chain hop: this Cloud Task is done but the run is not, and
    the next link will ``attach_run`` to the same run id. Without the flush,
    every hop silently dropped whatever it logged since its last write.

    Args:
        key: The task's status key.
    """
    try:
        flush(key)
        _stop_flusher(_state(key))
    except Exception as e:
        logger.warning(f"Could not detach process log for '{key}': {e}")




def finalize(key: str, state_name: str = STATE_COMPLETED) -> None:
    """Close out a run with a terminal state and a footer. Never raises.

    Falls back to the newest still-running run in storage when this process
    holds no buffer for the key — cancelling a stuck Cloud Run task happens in
    the web service, while the run was opened by the task runner.

    Args:
        key: The task's status key.
        state_name: One of ``completed`` / ``failed`` / ``cancelled``.
    """
    try:
        if not valid_key(key):
            return
        state = _state(key)
        with state.lock:
            run_id = state.run_id
        local = bool(run_id)
        if not run_id:
            runs = _load(key).get("runs", [])
            newest = runs[-1] if runs else None
            if not isinstance(newest, dict) or newest.get("state") != STATE_RUNNING:
                return
            run_id = newest.get("run_id", "")
            if not run_id:
                return

        ended_at = _now_iso()
        if local:
            append(key, f"{'═' * 8} Run {state_name} · {ended_at} {'═' * 8}")
            flush(key)

        def _mutate(doc):
            doc = _coerce(doc, key)
            run = _find_run(doc, run_id)
            if run is None:
                return None
            run["state"] = state_name
            run["ended_at"] = ended_at
            if not local:
                # No local buffer to append the footer through.
                run.setdefault("lines", []).append(
                    f"[{now_stamp()}] {'═' * 8} Run {state_name} · {ended_at} {'═' * 8}")
            return doc

        _guard.writing = True
        try:
            data_io.update_json(storage_location=LOG_LOCATION,
                                filename=log_filename(key), mutate=_mutate,
                                default=_empty_doc(key))
        finally:
            _guard.writing = False

        _stop_flusher(state)
        with state.lock:
            state.run_id = ""
    except Exception as e:
        _guard.writing = False
        logger.warning(f"Could not finalize process log for '{key}': {e}")




def _load(key: str) -> dict:
    """Read a key's store document, returning an empty one when absent."""
    filename = log_filename(key)
    if not data_io.exists(storage_location=LOG_LOCATION, filename=filename):
        return _empty_doc(key)
    return _coerce(data_io.load_json(storage_location=LOG_LOCATION,
                                     filename=filename), key)




def _run_meta(run: dict) -> dict:
    """Return a run record without its lines, for the run picker."""
    lines = run.get("lines") or []
    return {
        "run_id": run.get("run_id", ""),
        "started_at": run.get("started_at"),
        "started_by": run.get("started_by", ""),
        "mode": run.get("mode", ""),
        "state": run.get("state", ""),
        "ended_at": run.get("ended_at"),
        "line_count": len(lines) + int(run.get("dropped", 0)),
    }




def read(key: str, run_id: str = "", since: int = 0) -> dict:
    """Return a run's log lines plus the run list for the picker. Never raises.

    Args:
        key: The task's status key.
        run_id: The run to read; the newest run when omitted.
        since: The ``next_since`` cursor from a previous call. Only lines
            written after it are returned, so a polling client appends rather
            than re-downloading the whole log every second.

    Returns:
        ``{"lines": [...], "next_since": int, "reset": bool, "run": {...} | None,
        "runs": [{...}]}``. ``reset`` tells the client to replace rather than
        append — the cursor was stale, or lines it had not yet seen were
        trimmed off the front.
    """
    empty = {"lines": [], "next_since": 0, "reset": True, "run": None, "runs": []}
    try:
        if not valid_key(key):
            return empty
        doc = _load(key)
        runs = [r for r in doc.get("runs", []) if isinstance(r, dict)]
        if not runs:
            return empty

        target = _find_run(doc, run_id) if run_id else runs[-1]
        if target is None:
            return {**empty, "runs": [_run_meta(r) for r in reversed(runs)]}

        lines = target.get("lines") or []
        dropped = int(target.get("dropped", 0))
        total = dropped + len(lines)

        new_count = total - int(since or 0)
        reset = False
        if since <= 0 or new_count > len(lines) or new_count < 0:
            # First read, a stale cursor, or the client's next line has already
            # been trimmed away — send everything we still hold.
            out = list(lines)
            reset = True
        elif new_count == 0:
            out = []
        else:
            out = lines[-new_count:]

        return {
            "lines": out,
            "next_since": total,
            "reset": reset,
            "run": _run_meta(target),
            "runs": [_run_meta(r) for r in reversed(runs)],
        }
    except Exception as e:
        logger.warning(f"Could not read process log for '{key}': {e}")
        return empty




def clear(key: str) -> bool:
    """Delete a process's whole run history. Never raises.

    Args:
        key: The task's status key.

    Returns:
        True when the history was removed.
    """
    try:
        if not valid_key(key):
            return False
        state = _state(key)
        _stop_flusher(state)
        with state.lock:
            state.run_id = ""
            state.pending = []
        filename = log_filename(key)
        if data_io.exists(storage_location=LOG_LOCATION, filename=filename):
            data_io.remove(storage_location=LOG_LOCATION, filename=filename)
        return True
    except Exception as e:
        logger.warning(f"Could not clear process log for '{key}': {e}")
        return False




class ReporterLogHandler(logging.Handler):
    """Tees ``fyp`` package log records into a task's durable run log.

    On Cloud Run the task runner is nobody's subprocess: its stdout goes to
    Cloud Logging, so the UI used to show only the worker's explicit
    ``reporter.log`` calls — four lines for a consolidation that narrates
    hundreds. ``logging_setup.add_sink`` attaches this to every ``fyp`` logger
    for the duration of a task so that narration reaches the log modal.

    Records emitted *while* the handler is writing are dropped rather than
    queued: the write path itself logs, and forwarding those would recurse.
    """

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "handling", False):
            return
        _guard.handling = True
        try:
            append(self.key, record.getMessage())
        except Exception:
            pass
        finally:
            _guard.handling = False
