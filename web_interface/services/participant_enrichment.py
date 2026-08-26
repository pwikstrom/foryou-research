"""First-batch enrichment for participant collections.

The recruitment funnel promises a participant that a small first batch of
their collection is scraped and annotated soon after upload, and that they get
an email when it is ready. Three stages, all called from workers:

* ``enqueue_first_batches`` — called at the end of an ingest run for the
  collections that were ingested and are owned by a user account. Picks a
  bounded, most-recent slice of each collection's not-yet-enriched view items,
  appends them to the per-platform SCRAPE queue only (annotation needs the
  scraped media, so those items must not enter the annotation queue yet), and
  records the batch in the ``cache/participant_first_batches.json`` ledger.

* Scrape → annotate handoff — done inside ``check_first_batch_completions``:
  ledger items that enrichment status now shows as scraped but not annotated
  are appended to ``cache/to_annotate.json`` at that point. Consolidation is
  the moment scrape results become visible, so items enter the annotation
  queue only once their media provably exists.

* Completion — also in ``check_first_batch_completions``: an un-notified
  ledger entry whose items are (mostly) annotated emails the collection owner
  and arms the real-data tour re-offer. The workers themselves stay
  operator-launched; this module only queues and notifies.

MASTER SWITCH: ``AUTO_ENQUEUE_ENABLED`` below. While False (the shipped
value), ``enqueue_first_batches`` is a no-op — no collection is auto-queued
into ANY queue at ingest. The ledger/handoff/notification machinery stays in
place (it only acts on ledger entries, which the switch prevents from being
created), so flipping the switch re-arms the whole flow.

Everything here is fire-and-forget from the callers' perspective: any failure
is logged and never propagates into the ingest/consolidation run.
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

# Master switch for the automatic first batch. Disabled 2026-08-26 pending a
# decision on how participant enrichment should be scheduled — while False,
# ingest queues NOTHING automatically and no ledger entries are created.
AUTO_ENQUEUE_ENABLED = False

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
    """Queue a first SCRAPE batch for each owned new collection.

    The items go to the scrape queue only; they reach the annotation queue via
    the consolidation-time handoff in ``check_first_batch_completions``, once
    their media has actually been scraped.

    Args:
        collection_ids: Collections touched by the just-finished ingest run.
        log: Line logger (the ingest reporter's ``log``).

    Returns:
        ``{collection_id: n_queued}`` for the collections that got a batch.
    """
    queued: dict[str, int] = {}
    if not AUTO_ENQUEUE_ENABLED:
        return queued
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
            flags = _status_lookup(items)
            if flags is not None:
                items = [i for i in items if not bool(flags["annotated"].get(i, False))]
            items = items[:FIRST_BATCH_SIZE]
            if not items:
                continue
            by_platform.setdefault(platform, []).extend(items)
            new_entries[cid] = {
                "owner": owner_map[cid],
                "item_ids": items,
                # Items already handed to to_annotate.json by the
                # consolidation-time handoff; starts empty — nothing is
                # annotation-queued until its media is scraped.
                "annotate_queued": [],
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
            filename=LEDGER_FILENAME,
            mutate=lambda current: {**(current if isinstance(current, dict) else {}), **new_entries},
            default={},
        )
        for cid, n in queued.items():
            log(f"First batch queued for {cid}: {n} items to scrape "
                f"(annotation follows automatically once they are scraped).")
    except Exception as exc:  # never fail the caller's run
        logger.error(f"participant_enrichment.enqueue_first_batches failed: {exc}")
    return queued


def _status_lookup(item_ids: list[str]) -> dict[str, pd.Series] | None:
    """``scraped`` / ``annotated`` flags per item id, from enrichment status.

    Returns:
        ``{"scraped": Series, "annotated": Series}`` indexed by item id, or
        None when ``recoded/enrichment_status.parquet`` is unavailable.
    """
    try:
        from web_interface.services import preview_cache
        status = preview_cache.get_enrichment_status_cached()
        if status is None:
            return None
        import numpy as np
        ids = [str(i) for i in item_ids]
        scraped, annotated = preview_cache.status_flags(np.asarray(ids), status)
        return {"scraped": pd.Series(scraped, index=ids),
                "annotated": pd.Series(annotated, index=ids)}
    except Exception as exc:
        logger.error(f"participant_enrichment: enrichment status lookup failed: {exc}")
        return None


def check_first_batch_completions() -> list[str]:
    """Advance open first batches: hand scraped items to annotation, then
    notify owners whose batch is now annotated.

    Called by the consolidation worker after enrichment results persist (the
    moment scrape/annotation outcomes become visible in enrichment status).
    Never raises.

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
            flags = _status_lookup(items)
            if flags is None:
                return notified  # no status table yet; try again next run

            # --- Scrape → annotate handoff. Only items whose media is now
            # scraped enter the annotation queue: an unscraped item there
            # would burn as "file not found" and be pruned as failed.
            already = {str(i) for i in (entry.get("annotate_queued") or [])}
            ready = [i for i in items
                     if bool(flags["scraped"].get(i, False))
                     and not bool(flags["annotated"].get(i, False))
                     and i not in already]
            if ready:
                data_io.update_json(
                    storage_location="cache",
                    filename="to_annotate.json",
                    mutate=lambda current, ready=ready: list(
                        set(current if isinstance(current, list) else []) | set(ready)
                    ),
                    default=[],
                )

                def _record_handoff(current, cid=cid, ready=ready):
                    current = current if isinstance(current, dict) else {}
                    if cid in current and isinstance(current[cid], dict):
                        prev = {str(i) for i in (current[cid].get("annotate_queued") or [])}
                        current[cid] = {**current[cid],
                                        "annotate_queued": sorted(prev | set(ready))}
                    return current

                data_io.update_json(storage_location="cache", filename=LEDGER_FILENAME,
                                    mutate=_record_handoff, default={})
                logger.info(f"First batch for {cid}: {len(ready)} scraped item(s) "
                            f"handed to the annotation queue.")

            # --- Completion check.
            n_done = int(flags["annotated"].sum())
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
