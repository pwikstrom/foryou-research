"""Enrichment history: one durable, high-level journal of what happened.

The enrichment machinery already keeps several records — per-process run logs,
the plan ledger, task status files, the refresh-run record, the admin activity
log — and none of them tells the story of a cycle. Reconstructing the night of
2026-09-04 took all five plus two parquet joins, and still missed that the last
annotation batch had landed after its plan was parked and was never folded in.

This journal is the story, one line per event, at the altitude an operator
reads: a plan armed, a queue built or emptied, a queue handed to a worker (and
how much of it was the plan's own work), a scrape or annotation finished with
its totals, a consolidation and what it changed, a plan parked and why. Every
writer records the one fact it alone knows, at the moment it knows it. The
Edit Collections panel shows a collection's slice; Dataset Assembly shows the
whole.

Rules, shared with ``run_logs``:

* **Nothing here raises.** A journal write must never turn a working task into
  a failed one — every public function swallows and logs.
* **Writes are compare-and-set** (``data_io.update_json``), so the web
  service, the task runner and the workers can never clobber each other.
* **One ring, bounded.** The newest ``MAX_EVENTS`` events are kept; older
  ones fall off the front. Roughly a few weeks of nightly cycles.
"""

from datetime import UTC, datetime

import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

JOURNAL_LOCATION = "cache"
JOURNAL_FILENAME = "enrichment_journal.json"
VERSION = 1

# Events retained. A four-cycle night is ~25 events.
MAX_EVENTS = 600

# Collection ids carried on an event that touched many collections (a
# consolidation, a refresh). Enough for any real impact; bounds the document.
MAX_COLLECTION_IDS = 200

# Families drive the row colour in both UIs.
FAMILY_PLAN = "plan"
FAMILY_QUEUE = "queue"
FAMILY_WORKER = "worker"
FAMILY_CONSOLIDATE = "consolidate"
FAMILY_REFRESH = "refresh"
FAMILY_ATTENTION = "attention"

# kind -> (label, family). The one vocabulary both surfaces render; a kind
# missing here still records (with its raw name as the label) rather than
# failing, but every writer in the codebase should use a listed kind.
KINDS: dict[str, tuple[str, str]] = {
    "plan.armed": ("Armed", FAMILY_PLAN),
    "plan.paused": ("Paused", FAMILY_PLAN),
    "plan.resumed": ("Resumed", FAMILY_PLAN),
    "plan.settings": ("Settings changed", FAMILY_PLAN),
    "plan.tick": ("Cycle requested", FAMILY_PLAN),
    "plan.done": ("Idle", FAMILY_PLAN),
    "plan.blocked": ("Needs attention", FAMILY_ATTENTION),
    "tick.busy": ("Cycle waited", FAMILY_PLAN),
    "queue.built": ("Queue built", FAMILY_QUEUE),
    "queue.emptied": ("Queue emptied", FAMILY_QUEUE),
    "queue.drained": ("Queue handed to a worker", FAMILY_QUEUE),
    "slice.queued": ("Next slice queued", FAMILY_QUEUE),
    "handoff": ("Handed to annotation", FAMILY_QUEUE),
    "worker.started": ("Worker started", FAMILY_WORKER),
    "scrape.finished": ("Scrape finished", FAMILY_WORKER),
    "annotate.finished": ("Annotation finished", FAMILY_WORKER),
    "consolidate.finished": ("Consolidated", FAMILY_CONSOLIDATE),
    "results.settled": ("Results folded in", FAMILY_CONSOLIDATE),
    "finalize.dispatched": ("Analysis refresh started", FAMILY_REFRESH),
    "refresh.finished": ("Analyses refreshed", FAMILY_REFRESH),
}


def _now_iso() -> str:
    """An offset-aware UTC instant, so the UI renders it in the viewer's zone."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty_doc() -> dict:
    return {"version": VERSION, "events": []}


def _coerce(doc) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        return _empty_doc()
    return doc


def label_for(kind: str) -> str:
    return KINDS.get(kind, (kind, FAMILY_PLAN))[0]


def family_for(kind: str) -> str:
    return KINDS.get(kind, (kind, FAMILY_PLAN))[1]


def record(kind: str, message: str, *, collection_id: str | None = None,
           platform: str | None = None, actor: str | None = None,
           collection_ids=None, **detail) -> None:
    """Append one event. Never raises.

    Args:
        kind: A key of :data:`KINDS`.
        message: The one-line, operator-facing statement of what happened.
        collection_id: The collection this event is about, when it is about
            exactly one (a plan's own events).
        platform: The scrape platform involved, for platform-lane events with
            no single collection (a scraper run, a platform queue).
        actor: Who caused it — a username, ``"enrichment_supervisor"`` for the
            loop, or None for the system.
        collection_ids: Every collection an event touched, when it touched
            many (a consolidation's affected collections). Capped.
        **detail: The numbers behind the message, kept for tooltips and for
            anyone reading the file.
    """
    try:
        ids = None
        if collection_ids:
            ids = [str(i) for i in list(collection_ids)[:MAX_COLLECTION_IDS]]
        event = {
            "ts": _now_iso(),
            "kind": str(kind),
            "message": str(message),
            "collection_id": str(collection_id) if collection_id else None,
            "platform": str(platform) if platform else None,
            "actor": str(actor) if actor else None,
            "collection_ids": ids,
            "detail": {k: v for k, v in detail.items() if v is not None},
        }

        def _mutate(doc):
            doc = _coerce(doc)
            doc["events"] = [e for e in doc["events"] if isinstance(e, dict)]
            doc["events"].append(event)
            doc["events"] = doc["events"][-MAX_EVENTS:]
            doc["version"] = VERSION
            return doc

        data_io.update_json(storage_location=JOURNAL_LOCATION, filename=JOURNAL_FILENAME,
                            mutate=_mutate, default=_empty_doc())
    except Exception as exc:
        logger.warning(f"enrichment_journal: could not record {kind!r}: {exc}")


def _matches(event: dict, collection_id: str | None, platform: str | None) -> bool:
    """Whether an event belongs in one collection's view.

    A collection's history is its own events, the many-collection events that
    name it, and the platform-lane events with no collection of their own (a
    scraper run on its platform, the shared annotator, a consolidation that
    changed nothing) — the machinery its plan shares with everyone else.
    """
    if not collection_id:
        return True
    if event.get("collection_id") == collection_id:
        return True
    tagged = event.get("collection_ids")
    if tagged:
        return collection_id in tagged
    if event.get("collection_id"):
        return False
    ev_platform = event.get("platform")
    return ev_platform is None or platform is None or ev_platform == platform


def read(*, collection_id: str | None = None, platform: str | None = None,
         limit: int = 100) -> list[dict]:
    """The newest events, newest first, each with its ``label`` and ``family``.

    Args:
        collection_id: Restrict to one collection's view (see :func:`_matches`).
        platform: That collection's platform, to keep only its scrape lane.
        limit: Maximum events returned.
    """
    try:
        doc = _coerce(data_io.load_json(storage_location=JOURNAL_LOCATION,
                                        filename=JOURNAL_FILENAME))
    except Exception as exc:
        logger.warning(f"enrichment_journal: could not read the journal: {exc}")
        return []
    cid = str(collection_id) if collection_id else None
    out = []
    for event in reversed(doc["events"]):
        if not isinstance(event, dict):
            continue
        if not _matches(event, cid, platform):
            continue
        kind = str(event.get("kind") or "")
        out.append({**event, "label": label_for(kind), "family": family_for(kind)})
        if len(out) >= max(1, int(limit)):
            break
    return out


def collection_ids_present(limit_events: int = MAX_EVENTS) -> list[str]:
    """Every collection id named by any retained event (for a filter picker)."""
    try:
        doc = _coerce(data_io.load_json(storage_location=JOURNAL_LOCATION,
                                        filename=JOURNAL_FILENAME))
    except Exception:
        return []
    seen: dict[str, None] = {}
    for event in reversed(doc["events"][-limit_events:]):
        if not isinstance(event, dict):
            continue
        if event.get("collection_id"):
            seen.setdefault(str(event["collection_id"]), None)
        for cid in event.get("collection_ids") or []:
            seen.setdefault(str(cid), None)
    return list(seen)


# The platforms' own spellings; anything unknown is capitalised.
_PLATFORM_LABELS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}


def platform_label(platform: str | None) -> str:
    """How a platform reads in a message ("TikTok", not "Tiktok")."""
    key = str(platform or "").strip().lower()
    return _PLATFORM_LABELS.get(key, key.capitalize() if key else "")


def actor_label(actor: str | None) -> str:
    """How an actor reads in a message: a person, the loop, or the system."""
    if not actor:
        return "the system"
    if actor == "enrichment_supervisor":
        return "the enrichment loop"
    return str(actor)
