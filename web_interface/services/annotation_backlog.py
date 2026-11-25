"""The annotation backlog: videos a machine's own annotator works through.

The hosted Hub annotates with the admin-selected backend from
``cache/to_annotate.json``. A second machine (a Mac running a local model)
annotates the large remainder from a separate list, the backlog, so the two
never share a queue. Both write to the same storage; the hosted Hub alone
consolidates.

Three small files in ``cache``:

``annotate_backlog.json``
    ``{"items": [...], "failed": [...]}``. The ids still to annotate, in the
    order to take them. An id leaves ``items`` only when it is finished
    (annotated, failed, or found to need no annotation any more).

``annotate_backlog_claims.json``
    ``{"claims": {claim_id: {...}}}``. A claim marks the ids one worker is
    annotating right now, with a heartbeat. A claim never removes ids from the
    backlog, so a worker that dies loses nothing: its claim goes stale after
    :data:`CLAIM_STALE_S` and the ids are taken again. Claims are written with
    ``data_io.update_json`` (compare-and-swap), so two workers claiming at the
    same moment cannot take the same id.

``annotate_backlog_status.json``
    What the worker last reported (progress, speed, heartbeat), for the Hub.

The backlog gives way to the hosted Hub. A claim skips ids that are queued for
or in flight with the hosted annotator, and drops ids that enrichment status
shows as annotated since they were listed. Where both still annotate a video,
the views show the other annotator's row (``annotation_versioning.newest_row_mask``).
"""

import getpass
import os
import socket
import threading
import uuid
from datetime import UTC, datetime

import fyp.core.data_io as data_io
from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)

LOCATION = "cache"
BACKLOG_FILENAME = "annotate_backlog.json"
CLAIMS_FILENAME = "annotate_backlog_claims.json"
STATUS_FILENAME = "annotate_backlog_status.json"

# A claim whose heartbeat is older than this is treated as abandoned.
CLAIM_STALE_S = 900
HEARTBEAT_INTERVAL_S = 60
# A worker that has not reported for this long is shown as not running.
STATUS_STALE_S = 900
# How far past the chunk size a claim looks for ids that are still wanted.
_SCAN_FACTOR = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _age_s(stamp, now: datetime | None = None) -> float | None:
    try:
        return ((now or _now()) - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return None


def _load(filename: str):
    try:
        return data_io.load_json_optional(storage_location=LOCATION, filename=filename)
    except Exception as exc:
        logger.warning(f"[BACKLOG] could not read {filename}: {exc}")
        return None


# ---- the backlog ------------------------------------------------------------


def load_backlog() -> dict:
    """The backlog as ``{"items": [...], "failed": [...]}`` (empty when absent)."""
    data = _load(BACKLOG_FILENAME)
    if not isinstance(data, dict):
        return {"items": [], "failed": []}
    return {
        **data,
        "items": [str(i) for i in data.get("items") or []],
        "failed": [str(i) for i in data.get("failed") or []],
    }


def eligible_ids(item_ids=None) -> list[str]:
    """Ids that may be annotated, by the Hub's one eligibility rule.

    Args:
        item_ids: Candidates in the order to keep; ``None`` means every item
            in enrichment status, in its stored order.

    Returns:
        The eligible ids (scraped, media downloaded, not annotated, not failed).
    """
    from web_interface.services import collection_enrichment as ce

    status = ce.load_status(item_ids)
    if status is None:
        return []
    candidates = list(status.index.astype(str)) if item_ids is None else item_ids
    return ce.annotation_eligible(candidates, status)


def set_backlog(item_ids, append: bool = False) -> int:
    """Write the backlog from an ordered id list (replacing it unless ``append``).

    The ids are taken as given: pass them through :func:`eligible_ids` first.
    Ids recorded as failed stay recorded and are not listed again.

    Returns:
        The number of ids listed after the write.
    """
    wanted = list(dict.fromkeys(str(i) for i in item_ids))
    result: dict = {}

    def _mutate(current):
        current = current if isinstance(current, dict) else {}
        failed = [str(i) for i in current.get("failed") or []]
        base = [str(i) for i in current.get("items") or []] if append else []
        skip = set(failed) | set(base)
        items = base + [i for i in wanted if i not in skip]
        result["n"] = len(items)
        return {
            "version": 1,
            "created_at": current.get("created_at") or _now().isoformat(),
            "updated_at": _now().isoformat(),
            "items": items,
            "failed": failed,
        }

    data_io.update_json(storage_location=LOCATION, filename=BACKLOG_FILENAME, mutate=_mutate)
    return result.get("n", 0)


# ---- claims -----------------------------------------------------------------


def _fresh_claims(claims_doc, now: datetime | None = None) -> dict:
    claims = (claims_doc or {}).get("claims") if isinstance(claims_doc, dict) else None
    out = {}
    for claim_id, claim in (claims or {}).items():
        age = _age_s((claim or {}).get("heartbeat_at"), now)
        if age is not None and age <= CLAIM_STALE_S:
            out[claim_id] = claim
    return out


def claimed_ids(now: datetime | None = None) -> set[str]:
    """Ids held by a claim whose heartbeat is fresh."""
    return {
        str(i)
        for claim in _fresh_claims(_load(CLAIMS_FILENAME), now).values()
        for i in claim["items"]
    }


def hosted_annotator_ids() -> set[str]:
    """Ids the hosted annotator has queued or in flight (the backlog gives way)."""
    from web_interface.services import collection_enrichment as ce

    ids: set[str] = set()
    queue = _load(ce.ANNOTATE_QUEUE_FILENAME)
    if isinstance(queue, list):
        ids.update(str(i) for i in queue)
    state = _load("annotate_batch_job.json")
    if isinstance(state, dict):
        jobs = state.get("jobs")
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict):
                    ids.update(str(i) for i in job.get("submitted_ids") or [])
        ids.update(str(i) for i in state.get("submitted_ids") or [])
    return ids


def _holder() -> dict:
    return {"host": socket.gethostname(), "user": getpass.getuser(), "pid": os.getpid()}


def claim(n: int, check_eligibility: bool = True) -> dict | None:
    """Claim up to ``n`` ids from the head of the backlog.

    Ids already claimed by a live worker, or queued or in flight with the
    hosted annotator, are passed over. Ids that no longer need annotating (or
    cannot be annotated: never scraped, no media) are removed from the backlog.

    Args:
        n: The most ids to claim.
        check_eligibility: Check candidates against enrichment status. Off only
            in tests that have no status table.

    Returns:
        ``{"claim_id", "items", "dropped"}``, or ``None`` when nothing is left
        to claim right now.
    """
    items = load_backlog()["items"]
    if not items or n <= 0:
        return None
    taken = claimed_ids() | hosted_annotator_ids()
    candidates = [i for i in items[: n * _SCAN_FACTOR + len(taken)] if i not in taken]
    candidates = candidates[: n * _SCAN_FACTOR]
    dropped: list[str] = []
    if check_eligibility and candidates:
        wanted = set(eligible_ids(candidates))
        dropped = [i for i in candidates if i not in wanted]
        candidates = [i for i in candidates if i in wanted]
        if dropped:
            _remove_from_backlog(dropped, failed=[])
    candidates = candidates[:n]
    if not candidates:
        # Everything scanned was dropped or is spoken for; the caller may try again.
        return {"claim_id": None, "items": [], "dropped": dropped} if dropped else None

    claim_id = uuid.uuid4().hex[:12]
    stamp = _now().isoformat()
    granted: dict = {}

    def _mutate(current):
        fresh = _fresh_claims(current)
        held = {str(i) for c in fresh.values() for i in c["items"]}
        mine = [i for i in candidates if i not in held]
        granted["items"] = mine
        if not mine:
            return None if fresh == ((current or {}).get("claims") or {}) else {"claims": fresh}
        fresh[claim_id] = {**_holder(), "items": mine, "claimed_at": stamp, "heartbeat_at": stamp}
        return {"claims": fresh}

    data_io.update_json(storage_location=LOCATION, filename=CLAIMS_FILENAME, mutate=_mutate)
    if not granted.get("items"):
        return {"claim_id": None, "items": [], "dropped": dropped}
    return {"claim_id": claim_id, "items": granted["items"], "dropped": dropped}


def heartbeat(claim_id: str) -> bool:
    """Refresh a claim's heartbeat. False when the claim no longer exists."""
    alive: dict = {}

    def _mutate(current):
        claims = dict((current or {}).get("claims") or {})
        if claim_id not in claims:
            alive["ok"] = False
            return None
        alive["ok"] = True
        claims[claim_id] = {**claims[claim_id], "heartbeat_at": _now().isoformat()}
        return {"claims": claims}

    try:
        data_io.update_json(storage_location=LOCATION, filename=CLAIMS_FILENAME, mutate=_mutate)
    except Exception as exc:
        logger.warning(f"[BACKLOG] heartbeat failed (continuing): {exc}")
        return True
    return bool(alive.get("ok"))


def release(claim_id: str) -> None:
    """Drop a claim without finishing its ids (they stay in the backlog)."""

    def _mutate(current):
        claims = dict((current or {}).get("claims") or {})
        if claim_id not in claims:
            return None
        del claims[claim_id]
        return {"claims": claims}

    data_io.update_json(storage_location=LOCATION, filename=CLAIMS_FILENAME, mutate=_mutate)


def _remove_from_backlog(done, failed) -> int:
    gone = {str(i) for i in done} | {str(i) for i in failed}
    left: dict = {}

    def _mutate(current):
        if not isinstance(current, dict):
            return None
        items = [str(i) for i in current.get("items") or [] if str(i) not in gone]
        known = [str(i) for i in current.get("failed") or []]
        seen = set(known)
        known += [str(i) for i in failed if str(i) not in seen]
        left["n"] = len(items)
        return {**current, "items": items, "failed": known, "updated_at": _now().isoformat()}

    data_io.update_json(storage_location=LOCATION, filename=BACKLOG_FILENAME, mutate=_mutate)
    return left.get("n", 0)


def complete(claim_id: str, ok_ids, fail_ids) -> int:
    """Finish a claim: its annotated and failed ids leave the backlog.

    Ids of the claim that are in neither list (a chunk whose results could not
    be read back) stay in the backlog and are taken again later.

    Returns:
        The number of ids left in the backlog.
    """
    remaining = _remove_from_backlog(ok_ids, fail_ids)
    release(claim_id)
    return remaining


class ClaimHeartbeat:
    """Keeps a claim's heartbeat fresh while its chunk is annotated.

    Use as a context manager around the annotation call. ``on_beat`` (optional)
    runs after each heartbeat, for the status file.
    """

    def __init__(self, claim_id: str, on_beat=None, interval_s: float = HEARTBEAT_INTERVAL_S):
        self.claim_id = claim_id
        self._on_beat = on_beat
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            heartbeat(self.claim_id)
            if self._on_beat is not None:
                try:
                    self._on_beat()
                except Exception as exc:
                    logger.warning(f"[BACKLOG] status update failed (continuing): {exc}")

    def __enter__(self) -> "ClaimHeartbeat":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ---- status -----------------------------------------------------------------


def write_status(**fields) -> None:
    """Record what the worker is doing (best effort; never raises)."""
    try:
        data_io.save_json(
            data={**_holder(), **fields, "heartbeat_at": _now().isoformat()},
            storage_location=LOCATION,
            filename=STATUS_FILENAME,
        )
    except Exception as exc:
        logger.warning(f"[BACKLOG] status write failed (continuing): {exc}")


def worker_status() -> dict | None:
    """The worker's last report with a ``running`` flag, or ``None`` if it never ran.

    Reads only the small status file (not the backlog), so it is cheap enough
    for the Data Management status poll.
    """
    worker = _load(STATUS_FILENAME)
    if not isinstance(worker, dict):
        return None
    age = _age_s(worker.get("heartbeat_at"))
    running = worker.get("state") == "running" and age is not None and age <= STATUS_STALE_S
    return {**worker, "running": bool(running)}


def read_status() -> dict:
    """The backlog as the Hub shows it.

    Returns:
        ``{"exists", "remaining", "failed", "claimed", "running", "worker"}``.
        ``worker`` is the last status the worker wrote (or ``None``);
        ``running`` is True while that status is fresh and not marked stopped.
    """
    backlog = load_backlog()
    worker = worker_status()
    return {
        "exists": bool(backlog["items"] or backlog["failed"] or worker),
        "remaining": len(backlog["items"]),
        "failed": len(backlog["failed"]),
        "claimed": len(claimed_ids()),
        "running": bool(worker and worker["running"]),
        "worker": worker,
    }
