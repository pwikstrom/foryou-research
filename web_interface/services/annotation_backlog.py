"""The annotation backlog: videos a machine's own annotator works through.

The hosted Hub annotates with the admin-selected backend from
``cache/to_annotate.json``. A second machine (a Mac running a local model)
annotates the large remainder from a separate list, the backlog, so the two
never share a queue. Both write to the same storage; the hosted Hub alone
consolidates.

Three small files in ``cache``:

``annotate_backlog.json``
    ``{"lanes": {"urgent": [...], "coverage": [...], "rest": [...]}, "failed":
    [...], "retry": [...], "paused": bool}``. The ids still to annotate. A
    worker empties a more urgent lane before it takes from the next, and
    takes a lane from its front. An id leaves its lane only when it is
    finished (annotated, failed, or found to need no annotation any more).

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


# Lanes, most urgent first. A worker empties a lane before it takes from the next.
LANES = ("urgent", "coverage", "rest")


def _norm(data) -> dict:
    """A stored backlog (either format, or nothing) as lanes / failed / retry / paused."""
    data = data if isinstance(data, dict) else {}
    stored = data.get("lanes") if isinstance(data.get("lanes"), dict) else {}
    lanes = {lane: [str(i) for i in stored.get(lane) or []] for lane in LANES}
    # The first format had one list, ``items``: it is the last lane.
    lanes["rest"] += [str(i) for i in data.get("items") or []]
    return {
        "version": 2,
        "created_at": data.get("created_at") or _now().isoformat(),
        "updated_at": data.get("updated_at"),
        "lanes": lanes,
        "failed": [str(i) for i in data.get("failed") or []],
        "retry": [str(i) for i in data.get("retry") or []],
        "paused": bool(data.get("paused")),
    }


def _ordered(lanes: dict) -> list[str]:
    return list(dict.fromkeys(i for lane in LANES for i in lanes[lane]))


def load_backlog() -> dict:
    """The backlog: ``lanes`` (ids per lane), ``items`` (all ids in the order they
    are taken), ``failed``, ``retry`` (failed ids being tried again) and ``paused``."""
    backlog = _norm(_load(BACKLOG_FILENAME))
    return {**backlog, "items": _ordered(backlog["lanes"])}


def _update(change) -> dict:
    """Apply ``change(backlog)`` (in place, on the normalised form) atomically."""
    result: dict = {}

    def _mutate(current):
        backlog = _norm(current)
        change(backlog)
        backlog["updated_at"] = _now().isoformat()
        result["backlog"] = backlog
        return backlog

    data_io.update_json(storage_location=LOCATION, filename=BACKLOG_FILENAME, mutate=_mutate)
    return result["backlog"]


def eligible_ids(item_ids=None, retry_ids=()) -> list[str]:
    """Ids that may be annotated, by the Hub's one eligibility rule.

    Args:
        item_ids: Candidates in the order to keep; ``None`` means every item
            in enrichment status, in its stored order.
        retry_ids: Candidates whose earlier failed annotation is to be ignored.

    Returns:
        The eligible ids (scraped, media downloaded, not annotated, and not
        failed unless listed in ``retry_ids``).
    """
    from web_interface.services import collection_enrichment as ce

    status = ce.load_status(item_ids)
    if status is None:
        return []
    candidates = list(status.index.astype(str)) if item_ids is None else [str(i) for i in item_ids]
    keep = set(ce.annotation_eligible(candidates, status))
    again = [i for i in candidates if i in set(retry_ids)]
    if again:
        keep |= set(ce.annotation_eligible(again, status, retry_failed=True))
    return [i for i in candidates if i in keep]


def add(item_ids, lane: str = "rest", front: bool = False) -> dict:
    """Put ids in a lane (at its end, or at its front).

    An id already in a more urgent lane stays there; one in a less urgent lane
    moves up. Ids recorded as failed are left out (see :func:`retry_failed`).
    The ids are taken as given: pass them through :func:`eligible_ids` first.

    Returns:
        ``{"added": n, "lanes": {lane: size}}``.
    """
    if lane not in LANES:
        raise ValueError(f"Unknown lane {lane!r} (lanes: {', '.join(LANES)})")
    wanted = list(dict.fromkeys(str(i) for i in item_ids))
    out: dict = {}

    def _change(backlog):
        lanes = backlog["lanes"]
        above = {i for name in LANES[: LANES.index(lane)] for i in lanes[name]}
        skip = above | set(backlog["failed"]) - set(backlog["retry"])
        new = [i for i in wanted if i not in skip]
        moving = set(new)
        for name in LANES[LANES.index(lane) + 1 :]:
            lanes[name] = [i for i in lanes[name] if i not in moving]
        if front:
            lanes[lane] = new + [i for i in lanes[lane] if i not in moving]
        else:
            here = set(lanes[lane])
            lanes[lane] += [i for i in new if i not in here]
        out["added"] = len(new)

    backlog = _update(_change)
    return {"added": out["added"], "lanes": {k: len(v) for k, v in backlog["lanes"].items()}}


def set_backlog(item_ids, append: bool = False) -> int:
    """Write the last lane from an ordered id list (replacing it unless ``append``).

    The ids are taken as given: pass them through :func:`eligible_ids` first.
    The more urgent lanes are left as they are; ids recorded as failed stay
    recorded and are not listed again.

    Returns:
        The number of ids in the backlog after the write.
    """
    if not append:
        _update(lambda backlog: backlog["lanes"].__setitem__("rest", []))
    add(item_ids, lane="rest")
    return len(load_backlog()["items"])


def remove(item_ids) -> int:
    """Take ids out of every lane (they are not recorded as failed). Returns ids left."""
    gone = {str(i) for i in item_ids}

    def _change(backlog):
        for name in LANES:
            backlog["lanes"][name] = [i for i in backlog["lanes"][name] if i not in gone]

    return len(_ordered(_update(_change)["lanes"]))


def empty(lane: str | None = None) -> None:
    """Empty one lane, or every lane when ``lane`` is None. Failures stay recorded."""
    if lane is not None and lane not in LANES:
        raise ValueError(f"Unknown lane {lane!r} (lanes: {', '.join(LANES)})")

    def _change(backlog):
        for name in LANES if lane is None else (lane,):
            backlog["lanes"][name] = []

    _update(_change)


def set_paused(paused: bool) -> None:
    """Ask workers to stop taking new chunks (or to carry on)."""
    _update(lambda backlog: backlog.__setitem__("paused", bool(paused)))


def retry_failed(lane: str = "rest") -> int:
    """List the failed ids again; their earlier failure is ignored when they are claimed.

    Returns:
        The number of ids listed again.
    """
    if lane not in LANES:
        raise ValueError(f"Unknown lane {lane!r} (lanes: {', '.join(LANES)})")
    out: dict = {}

    def _change(backlog):
        again = list(backlog["failed"])
        out["n"] = len(again)
        backlog["retry"] = list(dict.fromkeys(backlog["retry"] + again))
        backlog["failed"] = []
        listed = {i for name in LANES for i in backlog["lanes"][name]}
        backlog["lanes"][lane] += [i for i in again if i not in listed]

    _update(_change)
    return out["n"]


def study_item_ids(study_name: str) -> list[str]:
    """Every video of a study's frame (its cached dataset; built if missing)."""
    filename = f"{study_name}_recoded.parquet"
    frame = None
    if data_io.exists(storage_location=LOCATION, filename=filename):
        frame = data_io.load_parquet_selective(
            storage_location=LOCATION, filename=filename, columns=["item_id"]
        )
    if frame is None or frame.empty:
        from fyp.analysis.organize_datasets import create_study_recoded_dataset

        frame = create_study_recoded_dataset(
            study_name=study_name, save_to_cache=True, verbose=False
        )
    if frame is None or frame.empty:
        return []
    return list(dict.fromkeys(frame["item_id"].dropna().astype(str)))


def collection_item_ids(collection_id: str) -> list[str]:
    """Every video a collection's donor viewed, in viewing order."""
    from web_interface.services import collection_enrichment as ce

    activity = ce.load_activity(collection_id)
    if activity is None or activity.empty:
        return []
    return list(dict.fromkeys(activity.sort_values("day")["item_id"].astype(str)))


def add_from(
    study: str | None = None,
    collection: str | None = None,
    item_ids=None,
    lane: str = "urgent",
    front: bool = False,
) -> dict:
    """Put a study's, a collection's or a list's annotatable videos in a lane.

    Exactly one source is given. Only videos that may be annotated are added
    (scraped, media downloaded, not annotated, not failed).

    Returns:
        ``{"source", "candidates", "eligible", "added", "lanes"}``.
    """
    given = [x for x in (study, collection, item_ids) if x is not None]
    if len(given) != 1:
        raise ValueError("Give exactly one of study, collection or item_ids")
    if study is not None:
        source, candidates = f"study {study}", study_item_ids(study)
    elif collection is not None:
        source, candidates = f"collection {collection}", collection_item_ids(collection)
    else:
        source, candidates = "list", list(dict.fromkeys(str(i) for i in item_ids))
    wanted = eligible_ids(candidates) if candidates else []
    result = add(wanted, lane=lane, front=front)
    return {"source": source, "candidates": len(candidates), "eligible": len(wanted), **result}


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
    backlog = load_backlog()
    items = backlog["items"]
    if not items or n <= 0 or backlog["paused"]:
        return None
    taken = claimed_ids() | hosted_annotator_ids()
    candidates = [i for i in items[: n * _SCAN_FACTOR + len(taken)] if i not in taken]
    candidates = candidates[: n * _SCAN_FACTOR]
    dropped: list[str] = []
    if check_eligibility and candidates:
        wanted = set(eligible_ids(candidates, retry_ids=backlog["retry"]))
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

    def _change(backlog):
        for name in LANES:
            backlog["lanes"][name] = [i for i in backlog["lanes"][name] if i not in gone]
        backlog["retry"] = [i for i in backlog["retry"] if i not in gone]
        seen = set(backlog["failed"])
        backlog["failed"] += [str(i) for i in failed if str(i) not in seen]

    return len(_ordered(_update(_change)["lanes"]))


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
        ``{"exists", "remaining", "lanes", "paused", "failed", "claimed",
        "running", "worker"}``.
        ``worker`` is the last status the worker wrote (or ``None``);
        ``running`` is True while that status is fresh and not marked stopped.
    """
    backlog = load_backlog()
    worker = worker_status()
    return {
        "exists": bool(backlog["items"] or backlog["failed"] or worker),
        "remaining": len(backlog["items"]),
        "lanes": {lane: len(ids) for lane, ids in backlog["lanes"].items()},
        "paused": backlog["paused"],
        "failed": len(backlog["failed"]),
        "claimed": len(claimed_ids()),
        "running": bool(worker and worker["running"]),
        "worker": worker,
    }
