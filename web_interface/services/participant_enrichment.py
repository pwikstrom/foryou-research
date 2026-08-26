"""First-batch enrichment for participant collections.

The recruitment funnel promises a participant that a small first batch of
their collection is scraped and annotated soon after upload, and that they get
an email when it is ready. Two halves, both called from workers:

* ``enqueue_first_batches`` — called at the end of an ingest run for the
  collections that were ingested and are owned by a user account. Picks a
  bounded, most-recent slice of each collection's not-yet-enriched view items,
  appends them to the per-platform scrape queue and ``cache/to_annotate.json``
  (the same queues the admin Scrape & Annotate page fills), and records the
  batch in the ``cache/participant_first_batches.json`` ledger.

* ``check_first_batch_completions`` — called by the annotation workers after
  results persist. For each un-notified ledger entry whose items are now
  (mostly) annotated, emails the collection owner and marks the entry
  notified. The workers themselves stay operator-launched; this module only
  queues and notifies.

Everything here is fire-and-forget from the callers' perspective: any failure
is logged and never propagates into the ingest/annotation run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

logger = logging.getLogger(__name__)

RECODED_FILENAME = f"{COLLECTIONS_LABEL}_recoded.parquet"
LEDGER_FILENAME = "participant_first_batches.json"

# Cost bound for the prioritised first slice of one participant collection —
# deliberately small: the point is a quick taste, not coverage (the admin
# queue pages own the rest, under their own queue caps).
FIRST_BATCH_SIZE = 50

# The "ready" rule: notify once at least this share of the batch's items is
# annotated AND at least this many items are. The share threshold (rather
# than all-items) absorbs items that can never complete — permanently
# ip-blocked videos, deleted posts — without stranding the notification.
COMPLETION_MIN_SHARE = 0.5
COMPLETION_MIN_ITEMS = 5

_VIEW_TYPES = ["play", "observe"]


def _load_ledger() -> dict:
    try:
        ledger = data_io.load_json(storage_location="cache", filename=LEDGER_FILENAME)
        return ledger if isinstance(ledger, dict) else {}
    except Exception:
        return {}


def enqueue_first_batches(collection_ids: list[str], log=print) -> dict:
    """Queue a first scrape/annotation batch for each owned new collection.

    Args:
        collection_ids: Collections touched by the just-finished ingest run.
        log: Line logger (the ingest reporter's ``log``).

    Returns:
        ``{collection_id: n_queued}`` for the collections that got a batch.
    """
    queued: dict[str, int] = {}
    try:
        from web_interface.collection_accounts import load_owner_map

        owner_map = load_owner_map(fresh=True)
        ledger = _load_ledger()
        candidates = [
            str(c) for c in collection_ids
            if owner_map.get(str(c)) and str(c) not in ledger
        ]
        if not candidates:
            return queued

        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=["collection_id", "source_platform", "item_id",
                     "activity_type", "utc_timestamp"],
            filters=[("collection_id", "in", candidates)],
        )
        if df is None or df.empty:
            return queued

        from fyp.scrape import scrape_queues
        scrapeable = set(scrape_queues.registered_platforms())

        views = df[df["activity_type"].astype(str).isin(_VIEW_TYPES)]
        new_entries: dict[str, dict] = {}
        to_annotate_all: list[str] = []
        by_platform: dict[str, list[str]] = {}

        for cid, grp in views.groupby("collection_id", observed=True):
            cid = str(cid)
            platform = str(grp["source_platform"].mode().iloc[0])
            if platform not in scrapeable:
                log(f"First batch for {cid}: no scraper registered for '{platform}', skipped.")
                continue
            # Most recent first: the participant recognises those videos, and
            # recency also maximises the odds the posts are still up.
            grp = grp.dropna(subset=["item_id"]).sort_values("utc_timestamp", ascending=False)
            items = list(dict.fromkeys(grp["item_id"].astype(str)))
            # A merged re-donation may already carry enrichment; only queue
            # what still needs it.
            flags = _annotated_flags(items)
            if flags is not None:
                items = [i for i in items if not bool(flags.get(i, False))]
            items = items[:FIRST_BATCH_SIZE]
            if not items:
                continue
            by_platform.setdefault(platform, []).extend(items)
            to_annotate_all.extend(items)
            new_entries[cid] = {
                "owner": owner_map[cid],
                "item_ids": items,
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
                "notified": False,
            }
            queued[cid] = len(items)

        if not new_entries:
            return queued

        for platform, items in by_platform.items():
            scrape_queues.append_to_scrape_queue(platform, items)
        data_io.update_json(
            storage_location="cache",
            filename="to_annotate.json",
            mutate=lambda current: list(
                set(current if isinstance(current, list) else []) | set(to_annotate_all)
            ),
            default=[],
        )
        data_io.update_json(
            storage_location="cache",
            filename=LEDGER_FILENAME,
            mutate=lambda current: {**(current if isinstance(current, dict) else {}), **new_entries},
            default={},
        )
        for cid, n in queued.items():
            log(f"First batch queued for {cid}: {n} items to scrape + annotate.")
    except Exception as exc:  # never fail the caller's run
        logger.error(f"participant_enrichment.enqueue_first_batches failed: {exc}")
    return queued


def _annotated_flags(item_ids: list[str]) -> pd.Series | None:
    """``annotated_ok`` per item id, from ``recoded/enrichment_status.parquet``."""
    try:
        from web_interface.services import preview_cache
        status = preview_cache.get_enrichment_status_cached()
        if status is None:
            return None
        import numpy as np
        _, annotated = preview_cache.status_flags(np.asarray([str(i) for i in item_ids]), status)
        return pd.Series(annotated, index=[str(i) for i in item_ids])
    except Exception as exc:
        logger.error(f"participant_enrichment: enrichment status lookup failed: {exc}")
        return None


def check_first_batch_completions() -> list[str]:
    """Email owners whose first batch is now annotated; mark them notified.

    Called by the annotation workers after persisting results. Never raises.

    Returns:
        The collection ids notified this call.
    """
    notified: list[str] = []
    try:
        ledger = _load_ledger()
        open_entries = {cid: e for cid, e in ledger.items()
                        if isinstance(e, dict) and not e.get("notified")}
        if not open_entries:
            return notified

        from web_interface import mail_utils
        from web_interface.security import user_manager

        for cid, entry in open_entries.items():
            items = [str(i) for i in (entry.get("item_ids") or [])]
            if not items:
                continue
            flags = _annotated_flags(items)
            if flags is None:
                return notified  # no status table yet; try again next batch
            n_done = int(flags.sum())
            if n_done < COMPLETION_MIN_ITEMS or (n_done / len(items)) < COMPLETION_MIN_SHARE:
                continue

            owner = str(entry.get("owner") or "")
            user = user_manager.get_user(owner) if owner else None
            # consent_to_contact is the participant's own switch; an unset
            # value reads as consent absent → no email, but the ledger entry
            # is still closed so the tour re-offer works on their next login.
            wants_email = bool(user and (user.profile or {}).get("consent_to_contact"))
            if user and wants_email and mail_utils.is_email(owner):
                mail_utils.send_first_batch_ready_email_async(owner, cid, n_done)
            if user:
                user_manager.update_user_settings(owner, {"hub_tour_real_data_pending": True})

            def _mark(current, cid=cid):
                current = current if isinstance(current, dict) else {}
                if cid in current and isinstance(current[cid], dict):
                    current[cid] = {**current[cid], "notified": True,
                                    "notified_at": datetime.now(timezone.utc).isoformat(),
                                    "n_annotated": n_done,
                                    "emailed": bool(user and wants_email)}
                return current

            data_io.update_json(storage_location="cache", filename=LEDGER_FILENAME,
                                mutate=_mark, default={})
            notified.append(cid)
            logger.info(f"First batch complete for {cid}: {n_done}/{len(items)} annotated"
                        f" (owner {owner}, emailed={bool(user and wants_email)})")
    except Exception as exc:
        logger.error(f"participant_enrichment.check_first_batch_completions failed: {exc}")
    return notified
