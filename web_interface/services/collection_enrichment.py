"""Automatic per-collection enrichment: the plan ledger and the slice cutter.

A donated collection is fully explorable on the My Collections page the moment it
is ingested (that page reads donated activity only), but every analysis tab needs
its videos scraped and annotated. This module owns the *decision* half of doing
that automatically: which videos of an armed collection to enrich next. The
*execution* half stays entirely with the existing global machinery — the
per-platform scrape queues, ``to_annotate.json``, the queue workers and the
consolidation pipeline. Nothing here scrapes, annotates or consolidates;
:mod:`web_interface.run_enrichment_supervisor` drives those.

TWO PROCESSES, AND WHY BOTH BUY WHOLE DAYS
------------------------------------------
The Hub's analyses are floored on the **collection-day**, so a thin
few-videos-on-every-day spread is worth almost nothing:

* Correlations groups by (collection_id, local_date), drops any day with fewer
  than ``correlations.minimum_group_size`` (10) enriched rows, and refuses to run
  at all below 10 surviving days (``fyp/analysis/pca.py``).
* Sessions segments binges from *contiguous* plays in one sitting and needs at
  least 4 embedded videos over a 6-member window (``fyp/analysis/session_explorer``).
* Timelines admits only rows that are scraped AND annotated AND have a play
  duration, then needs >= 14 active days with real per-day denominators.

So both processes buy days; they differ only in *which* days:

* **Process B (deep dive)** — every view item of consecutive days, most recent
  first, uncapped. The only thing that buys Sessions, and it gives Timelines and
  Correlations honest denominators on a recent window.
* **Process A (spread)** — a couple of *whole* days per calendar month, each
  enriched up to a cap, walking backwards month by month. Buys the long arc for
  Timelines, breadth for Semantic Space, ellipsed periods for the participant's
  own trajectory, and extra qualifying cells for Correlations.

``sample_share`` splits each cycle's item budget between them.

THE LEDGER
----------
``cache/collection_enrichment.json``, one entry per collection, holding settings,
the two cursors, the spend counter, and the plan's ``in_flight`` item ids (what it
has queued for scraping and not yet seen resolve — the handoff's scope, bounded by
cycle size). No other per-item state: what has actually been scraped or annotated
is already in ``enrichment_status.parquet``, and a second copy would only drift. Every write goes through
``data_io.update_json`` (compare-and-set) because the supervisor runs on both the
hub and the task-runner.

Related: :mod:`web_interface.services.participant_enrichment` owns the one-shot
*welcome* batch, which is a different job and is currently switched off.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

logger = logging.getLogger(__name__)

RECODED_FILENAME = f"{COLLECTIONS_LABEL}_recoded.parquet"
STATUS_FILENAME = "enrichment_status.parquet"
LEDGER_FILENAME = "collection_enrichment.json"
ANNOTATE_QUEUE_FILENAME = "to_annotate.json"
# The worker whose task-status file `last_tick` reads.
SUPERVISOR_TASK = "enrichment_supervisor"

_VIEW_TYPES = ("play", "observe")

# Status columns the planner and the handoff need. A superset of
# preview_cache's three-column projection: the planner must also skip items whose
# scrape permanently failed, or it would re-queue TikTok's ip_blocked tail every
# cycle forever.
_STATUS_COLUMNS = ["item_id", "scraped_ok", "scrape_fail", "video_downloaded",
                   "annotated_ok", "annotated_fail"]

# Defaults for a freshly armed collection. The day-shaped ones come straight from
# the shipped study-sampler defaults (organize_datasets.simple_sample_collection_events),
# which encode what a useful study looks like: 30-50 videos/day over >= 20 days.
DEFAULT_SETTINGS = {
    # The state to reach: keep enriching until this many of the collection's
    # unique videos are annotated. 0 = no target set, which the engine reads as
    # "nothing to do" — an armed plan must state its goal, or it would run to
    # 100%. A target is idempotent: annotation done by any other means counts
    # toward it, and re-opening a finished plan is just raising the number.
    "annotation_target": 0,
    "cycle_items": 400,       # items enqueued per cycle (ignored when auto)
    # Auto: the supervisor sizes each cycle itself — min(target headroom, one
    # full set of concurrent annotation jobs) — so a cycle's annotation is
    # ~one batch-job turnaround. False here (not True) because save_plan
    # re-seeds these defaults into every stored entry: a True default would
    # retroactively flip pre-existing manual plans. The PANEL pre-checks the
    # box for collections with no plan yet, which is where the default lives.
    "cycle_items_auto": False,
    "sample_share": 0.5,      # fraction of the cycle given to Process A
    "a_days_per_month": 2,    # A: whole days sampled per calendar month
    "a_day_cap": 50,          # A: max items enriched on one sampled day
    "min_day_items": 10,      # skip days below this (the Correlations floor)
    "earliest_date": None,    # optional floor; None = the whole history
}

# A collection becomes analytically alive somewhere around here: Timelines gates
# at 14 active days and Correlations needs 10 qualifying collection-days.
MILESTONE_DAYS = 14

# Consecutive cycles that enqueue work but yield no newly scraped item before the
# plan parks itself. Stops a permanently unscrapeable tail burning cycles.
MAX_STALLS = 3

STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_BLOCKED = "blocked"


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def load_plans() -> dict:
    """Every enrichment plan, keyed by collection id. Never raises."""
    try:
        plans = data_io.load_json(storage_location="cache", filename=LEDGER_FILENAME)
        return plans if isinstance(plans, dict) else {}
    except Exception:
        return {}


def get_plan(collection_id: str) -> dict | None:
    """One plan, or None when the collection has never been armed."""
    cid = str(collection_id)
    if cid == META_KEY:
        return None
    entry = load_plans().get(cid)
    return entry if isinstance(entry, dict) else None


def save_plan(collection_id: str, patch: dict) -> dict:
    """Merge ``patch`` into one plan entry atomically.

    Nested ``settings`` are merged key-by-key so a partial settings update does
    not drop the keys it omits. Everything else is a shallow overwrite.

    Args:
        collection_id: The collection whose plan to update.
        patch: Keys to set. ``{"__delete__": True}`` removes the entry.

    Returns:
        The whole ledger as written.
    """
    cid = str(collection_id)

    def _mutate(current):
        current = current if isinstance(current, dict) else {}
        if patch.get("__delete__"):
            current.pop(cid, None)
            return current
        entry = dict(current.get(cid) or {})
        settings = dict(entry.get("settings") or {})
        for key, value in patch.items():
            if key == "settings" and isinstance(value, dict):
                settings.update(value)
            else:
                entry[key] = value
        entry["settings"] = {**DEFAULT_SETTINGS, **settings}
        current[cid] = entry
        return current

    return data_io.update_json(storage_location="cache", filename=LEDGER_FILENAME,
                               mutate=_mutate, default={})


def drop_plan(collection_id: str) -> None:
    """Forget a collection's plan — called when it is deleted or withdrawn."""
    try:
        save_plan(collection_id, {"__delete__": True})
    except Exception as exc:
        logger.error(f"collection_enrichment: could not drop plan for {collection_id}: {exc}")


def armed_plans() -> dict:
    """Plans in the running state, i.e. the ones the supervisor should serve."""
    return {cid: e for cid, e in load_plans().items()
            if cid != META_KEY and isinstance(e, dict)
            and e.get("state") == STATE_RUNNING}


# Reserved ledger key for supervisor bookkeeping that belongs to no single
# collection (currently the annotate-stall guard). Filtered out of armed_plans
# by name, and no collection id can collide with it.
META_KEY = "__meta__"


def get_meta(key: str, default=None):
    entry = load_plans().get(META_KEY)
    if isinstance(entry, dict):
        return entry.get(key, default)
    return default


def set_meta(key: str, value) -> None:
    def _mutate(current):
        current = current if isinstance(current, dict) else {}
        meta = dict(current.get(META_KEY) or {})
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
        if meta:
            current[META_KEY] = meta
        else:
            current.pop(META_KEY, None)
        return current

    data_io.update_json(storage_location="cache", filename=LEDGER_FILENAME,
                        mutate=_mutate, default={})


def normalize_settings(raw: dict | None) -> dict:
    """Coerce a settings payload from the UI into safe, bounded values.

    Every knob is clamped rather than rejected: this runs unattended, so a
    nonsensical value must degrade to something harmless instead of raising
    somewhere in the middle of a cycle.
    """
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)

    def _int(key, lo, hi):
        try:
            out[key] = max(lo, min(hi, int(raw.get(key, DEFAULT_SETTINGS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULT_SETTINGS[key]

    _int("annotation_target", 0, 10_000_000)
    _int("cycle_items", 1, 20_000)
    out["cycle_items_auto"] = bool(raw.get("cycle_items_auto",
                                           DEFAULT_SETTINGS["cycle_items_auto"]))
    _int("a_days_per_month", 0, 31)
    # Floor 10: a cap under the min_day_items analysis floor would buy spread
    # days that can never qualify. Ceiling 1,000: one day's cap, not a budget.
    _int("a_day_cap", 10, 1_000)
    _int("min_day_items", 1, 10_000)

    try:
        out["sample_share"] = max(0.0, min(1.0, float(
            raw.get("sample_share", DEFAULT_SETTINGS["sample_share"]))))
    except (TypeError, ValueError):
        out["sample_share"] = DEFAULT_SETTINGS["sample_share"]

    earliest = raw.get("earliest_date") or None
    if earliest:
        try:
            out["earliest_date"] = pd.Timestamp(earliest).normalize().strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            out["earliest_date"] = None
    else:
        out["earliest_date"] = None
    return out


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #

def load_status(item_ids=None) -> pd.DataFrame | None:
    """Enrichment status projected to the columns the planner and handoff need.

    Args:
        item_ids: Optional ids to restrict the read to.

    Returns:
        A DataFrame indexed by item_id with boolean columns, or None when
        ``enrichment_status.parquet`` does not exist yet (a corpus that has never
        been consolidated — every item then reads as unenriched).
    """
    try:
        if not data_io.exists(storage_location="recoded", filename=STATUS_FILENAME):
            return None
        filters = None
        if item_ids is not None:
            ids = [str(i) for i in item_ids]
            if not ids:
                return None
            filters = [("item_id", "in", ids)]
        df = data_io.load_parquet_selective(
            storage_location="recoded", filename=STATUS_FILENAME,
            columns=_STATUS_COLUMNS, filters=filters,
        )
        if df is None or df.empty:
            return None
        for col in _STATUS_COLUMNS[1:]:
            if col not in df.columns:
                df[col] = False
            df[col] = df[col].fillna(False).astype(bool)
        df["item_id"] = df["item_id"].astype(str)
        return df.drop_duplicates(subset=["item_id"]).set_index("item_id")
    except Exception as exc:
        logger.error(f"collection_enrichment: enrichment status read failed: {exc}")
        return None


def load_activity(collection_id: str) -> pd.DataFrame | None:
    """One collection's viewing activity, one row per (item, day).

    Deliberately does not go through ``preview_cache._prepare_preview_frame``:
    that cache is keyed by collection *set* and holds only two frames in memory,
    so driving it per-collection would evict the study-preview frames the admin
    UI depends on. A single filtered four-column read is cheap enough.

    Returns:
        A DataFrame with columns item_id, day (normalised Timestamp) and
        source_platform, deduplicated per (item_id, day), or None.
    """
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded", filename=RECODED_FILENAME,
            columns=["collection_id", "item_id", "activity_type",
                     "local_timestamp", "source_platform"],
            filters=[("collection_id", "in", [str(collection_id)])],
        )
        if df is None or df.empty:
            return None
        df = df[df["activity_type"].astype(str).isin(_VIEW_TYPES)]
        if df.empty:
            return None
        day = pd.to_datetime(df["local_timestamp"], errors="coerce").dt.normalize()
        out = pd.DataFrame({
            "item_id": df["item_id"].astype(str).to_numpy(),
            "day": day.to_numpy(),
            "source_platform": df["source_platform"].astype(str).to_numpy(),
        })
        out = out[out["day"].notna() & (out["item_id"] != "")]
        if out.empty:
            return None
        # One row per (item, day): the same video replayed twice in a day is one
        # unit of enrichment work, not two.
        return out.drop_duplicates(subset=["item_id", "day"]).reset_index(drop=True)
    except Exception as exc:
        logger.error(f"collection_enrichment: activity read for {collection_id} failed: {exc}")
        return None


def collection_platform(activity: pd.DataFrame) -> str:
    """The collection's modal platform — which scrape queue its items belong in."""
    try:
        return str(activity["source_platform"].mode().iloc[0])
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #

def annotation_eligible(item_ids, df_status, durations=None,
                        retry_failed: bool = False, max_duration=None) -> list[str]:
    """The ids among ``item_ids`` that may be put in the annotation queue.

    The single definition of that predicate. An item is annotatable when it is
    scraped, its media actually downloaded, and it has not already succeeded or
    permanently failed annotation:

        scraped_ok & video_downloaded & !annotated_ok & !annotated_fail

    This must not be duplicated anywhere. An unscraped id in ``to_annotate.json``
    resolves no media, is refined as ``annotated_fail``, and is then pruned as
    permanently failed — the item is burnt and no queue builder will ever pick it
    up again. That is exactly the 2026-08-26 hazard that switched the participant
    first-batch auto-queue off.

    Args:
        item_ids: Candidate ids.
        df_status: Enrichment status indexed by item_id (see :func:`load_status`),
            or a frame carrying an ``item_id`` column, or None.
        durations: Optional {item_id: seconds} for the secondary duration guard.
            ``video_downloaded`` already encodes the platform media cap, so this
            only matters where a duration is at hand.
        retry_failed: Ignore ``annotated_fail`` so past failures are re-queued.
        max_duration: Override for ``machine.max_duration_for_annotation``.

    Returns:
        Eligible ids, order-preserving and deduplicated.
    """
    ids = list(dict.fromkeys(str(i) for i in item_ids if str(i)))
    if not ids or df_status is None or df_status.empty:
        return []

    # Accept the status table however it arrives: item_id as a named index, as
    # a column, or (a legacy load_parquet shape) as an unnamed index.
    status = df_status
    if status.index.name != "item_id":
        if "item_id" not in status.columns:
            status = status.reset_index()
            if "index" in status.columns and "item_id" not in status.columns:
                status = status.rename(columns={"index": "item_id"})
            if "item_id" not in status.columns:
                return []
        status = status.copy()
        status["item_id"] = status["item_id"].astype(str)
        status = status.drop_duplicates(subset=["item_id"]).set_index("item_id")

    if max_duration is None:
        try:
            from fyp.fyp_config import fyp_cf
            max_duration = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
        except Exception:
            max_duration = 600

    # Vectorised: one reindex per flag (unknown ids read as False, so an item
    # absent from enrichment status is never annotatable). Order-preserving.
    idx = pd.Index(ids)

    def _flag(name: str) -> np.ndarray:
        if name not in status.columns:
            return np.zeros(len(ids), dtype=bool)
        return status[name].reindex(idx).fillna(False).to_numpy(dtype=bool)

    keep = _flag("scraped_ok") & ~_flag("annotated_ok")
    if "video_downloaded" in status.columns:
        # Annotation needs an mp4: metadata-only items (e.g. YouTube long-form
        # past the media duration cap) are not annotatable.
        keep &= _flag("video_downloaded")
    if not retry_failed:
        keep &= ~_flag("annotated_fail")
    if durations:
        # Coerce rather than construct as float64: study frames load with the
        # pyarrow dtype backend, so a missing duration arrives as ``pd.NA``,
        # which a float64 Series constructor refuses outright ("float()
        # argument must be ... not 'NAType'"). Unparseable values read as NaN
        # and are kept, matching the unknown-duration branch below.
        dur = pd.to_numeric(
            pd.Series([durations.get(i) for i in ids], index=idx, dtype="object"),
            errors="coerce",
        )
        keep &= (dur.isna() | (dur < float(max_duration))).to_numpy()

    return [iid for iid, ok in zip(ids, keep) if ok]


def _scrapeable_mask(item_ids: np.ndarray, status: pd.DataFrame | None) -> np.ndarray:
    """True where an item still needs (and can still get) a scrape.

    Excludes both the already-scraped and the permanently failed, so a cycle never
    re-queues a video the scraper has given up on.
    """
    if status is None or status.empty:
        return np.ones(len(item_ids), dtype=bool)
    scraped = status["scraped_ok"].reindex(item_ids).fillna(False).to_numpy(dtype=bool)
    failed = status["scrape_fail"].reindex(item_ids).fillna(False).to_numpy(dtype=bool)
    return ~(scraped | failed)


def _annotated_unique(activity: pd.DataFrame, status: pd.DataFrame | None) -> int:
    """How many of the collection's unique videos are annotated — the number an
    annotation target is measured against."""
    if status is None or status.empty or "annotated_ok" not in status.columns:
        return 0
    unique_ids = pd.Index(pd.unique(activity["item_id"].to_numpy()))
    return int(status["annotated_ok"].reindex(unique_ids).fillna(False).sum())


# --------------------------------------------------------------------------- #
# Deterministic sampling
# --------------------------------------------------------------------------- #

def stable_sample(keys, k: int, salt: str = "") -> list:
    """Pick ``k`` of ``keys`` deterministically, independent of input order.

    Sorts by a blake2b digest of ``salt + key`` and takes the head. Order
    independence is the point: a cycle re-plans from a persisted cursor, and the
    same cursor must always yield the same slice however the rows came back from
    parquet.

    Deliberately NOT ``organize_datasets.simple_sample_collection_events``, whose
    thresholds come from ``fyp_cf["study_defs"][study_name]`` (there is no study
    here) and whose ``RandomState(42)`` is consumed sequentially by
    ``groupby.apply``, making its output depend on row order. Do not "fix" this
    by switching to that sampler.
    """
    keys = list(keys)
    if k <= 0:
        return []
    if k >= len(keys):
        return keys
    salted = salt.encode("utf-8")
    ranked = sorted(
        keys,
        key=lambda key: hashlib.blake2b(salted + str(key).encode("utf-8"),
                                        digest_size=8).digest(),
    )
    return ranked[:k]


# --------------------------------------------------------------------------- #
# The slice cutter
# --------------------------------------------------------------------------- #

def _day_key(day) -> str:
    return pd.Timestamp(day).strftime("%Y-%m-%d")


def _month_key(day) -> str:
    return pd.Timestamp(day).strftime("%Y-%m")


def plan_cycle(collection_id: str, entry: dict,
               activity: pd.DataFrame | None = None,
               status: pd.DataFrame | None = None) -> dict:
    """Cut the next slice of work for one collection.

    Runs Process B and Process A against the collection's own budget share, then
    reports the item ids to scrape and where the two cursors now stand. Pure: it
    reads data and returns a decision, it does not touch a queue or the ledger.

    Args:
        collection_id: The collection being served.
        entry: Its ledger entry.
        activity: Pre-loaded activity (see :func:`load_activity`), else loaded here.
        status: Pre-loaded enrichment status, else loaded here.

    Returns:
        ``{"item_ids", "a_cursor", "b_cursor", "a", "b", "exhausted", "platform"}``.
        ``exhausted`` is True when both processes have walked off the end of the
        collection's history and there is nothing left to buy.
    """
    settings = {**DEFAULT_SETTINGS, **(entry.get("settings") or {})}
    empty = {"item_ids": [], "a_cursor": entry.get("a_cursor"),
             "b_cursor": entry.get("b_cursor"), "a": 0, "b": 0,
             "exhausted": False, "platform": ""}

    if activity is None:
        activity = load_activity(collection_id)
    if activity is None or activity.empty:
        return {**empty, "exhausted": True}
    if status is None:
        status = load_status(activity["item_id"].unique())

    platform = collection_platform(activity)

    # The cycle is clamped by how far the collection still is from its
    # annotation target. Measured against the truth (unique annotated videos in
    # enrichment status), not a spend meter, so annotation done by any other
    # means moves the plan closer to done rather than being paid for twice.
    # Ticks run one at a time and PLAN only fires with the queues drained and
    # results consolidated, so the count is current to within one cycle — the
    # documented worst-case overshoot.
    budget = int(settings["cycle_items"])
    target = int(settings.get("annotation_target") or 0)
    remaining_target = max(0, target - _annotated_unique(activity, status))
    budget = min(budget, remaining_target)
    if budget <= 0:
        return {**empty, "platform": platform, "exhausted": True}

    a_budget = int(round(budget * float(settings["sample_share"])))
    b_budget = budget - a_budget

    # Per-day view: total size (for the min_day_items floor) and the ids still
    # worth scraping. Computed once and shared by both processes.
    items = activity["item_id"].to_numpy()
    need_scrape = _scrapeable_mask(items, status)
    activity = activity.assign(_need=need_scrape)

    floor_ts = None
    if settings.get("earliest_date"):
        try:
            floor_ts = pd.Timestamp(settings["earliest_date"]).normalize()
        except (TypeError, ValueError):
            floor_ts = None

    by_day: dict[pd.Timestamp, dict] = {}
    for day, grp in activity.groupby("day", observed=True):
        day = pd.Timestamp(day)
        if floor_ts is not None and day < floor_ts:
            continue
        by_day[day] = {
            "total": int(len(grp)),
            "scraped": int((~grp["_need"]).sum()),
            # Sorted so a re-plan from the same cursor yields a byte-identical
            # slice regardless of parquet row order.
            "need": sorted(str(i) for i in grp.loc[grp["_need"], "item_id"]),
        }
    if not by_day:
        return {**empty, "platform": platform, "exhausted": True}

    all_days = sorted(by_day, reverse=True)
    min_day = int(settings["min_day_items"])

    # ---- Process B: whole days, uncapped, newest first ---------------------
    b_cursor = entry.get("b_cursor")
    picked_b: list[str] = []
    b_days: list[pd.Timestamp] = []
    # A zero share disables the process outright; it then counts as "done" so
    # exhaustion is decided by the other process alone.
    b_done = b_budget <= 0
    for day in all_days if b_budget > 0 else []:
        if b_cursor and _day_key(day) >= str(b_cursor):
            continue  # already walked past this day in an earlier cycle
        info = by_day[day]
        if info["total"] < min_day or not info["need"]:
            b_days.append(day)  # nothing to buy here; the cursor still advances
            continue
        # Never split a day: Sessions must not see a half-covered sitting. A day
        # too big for what is left of the budget waits for the next cycle.
        if picked_b and len(picked_b) + len(info["need"]) > b_budget:
            break
        picked_b.extend(info["need"])
        b_days.append(day)
        if len(picked_b) >= b_budget:
            break
    else:
        b_done = True
    if b_days:
        b_cursor = _day_key(b_days[-1])

    b_covered = {_day_key(d) for d in b_days}

    # ---- Process A: whole sampled days, newest month first -----------------
    a_cursor = entry.get("a_cursor")
    picked_a: list[str] = []
    a_months: list[str] = []
    a_done = a_budget <= 0
    a_full = False
    months = sorted({_month_key(d) for d in all_days}, reverse=True)
    for month in months if a_budget > 0 else []:
        if a_full:
            break
        if a_cursor and month >= str(a_cursor):
            continue
        # A only ever buys days that clear the Correlations floor, and never a day
        # B has already taken whole — B's day is complete, so there is nothing to
        # add and a smaller sample would be pure waste.
        eligible = [d for d in all_days
                    if _month_key(d) == month
                    and by_day[d]["total"] >= min_day
                    and by_day[d]["need"]
                    and _day_key(d) not in b_covered]
        # Seeded draw among qualifying days rather than "the busiest days":
        # picking the busiest would bias Timelines' trends toward heavy-usage days.
        for day in stable_sample(eligible, int(settings["a_days_per_month"]),
                                 salt=f"{collection_id}:{month}"):
            info = by_day[day]
            quota = int(settings["a_day_cap"]) - info["scraped"]
            if quota <= 0:
                continue
            picked_a.extend(stable_sample(info["need"], quota,
                                          salt=f"{collection_id}:{_day_key(day)}"))
            if len(picked_a) >= a_budget:
                a_full = True
                break
        a_months.append(month)
    else:
        a_done = True
    if a_months:
        a_cursor = a_months[-1]

    # Neither process's contribution is trimmed to the cycle budget: the DAY is
    # the unit of value (a half-covered sitting is worth nothing to Sessions; a
    # thinned A-day can fall under the Correlations floor), so a cycle may
    # overshoot cycle_items by at most one B day plus one A day. The
    # annotation target still clamps the handoff, so overshoot is bounded.
    seen = set(picked_b)
    extra_a = [i for i in picked_a if i not in seen and not seen.add(i)]

    picked = picked_b + extra_a
    return {
        "item_ids": picked,
        "a_cursor": a_cursor,
        "b_cursor": b_cursor,
        "a": len(extra_a),
        "b": len(picked_b),
        "exhausted": bool(a_done and b_done and not picked),
        "platform": platform,
    }


# --------------------------------------------------------------------------- #
# Scrape -> annotate handoff
# --------------------------------------------------------------------------- #

def handoff_scraped(collection_id: str, entry: dict,
                    activity: pd.DataFrame | None = None,
                    status: pd.DataFrame | None = None) -> dict:
    """What of this collection may enter annotation, and the surviving in-flight set.

    Called only after a consolidation, which is the moment scrape outcomes become
    visible in ``enrichment_status.parquet``. Never at plan time — see
    :func:`annotation_eligible` for why an unscraped id in the annotation queue is
    unrecoverable.

    Scope: every scraped-but-unannotated video in the collection, bounded by
    the plan's annotation target. Annotating an already-scraped video is the
    cheapest step toward the target, so the loop always clears that backlog
    before any new scraping — and because the handoff outranks the plan step
    in the tick, that ordering needs no extra machinery. (Until 2026-08-31
    this sweep was the ``annotate_existing`` opt-in; the target now bounds it,
    which is the protection the opt-in existed to provide. Stored plans may
    still carry that key — nothing reads it.) The ``in_flight`` set no longer
    scopes the handoff; it remains the plan's record of queued scrapes, which
    is what stall detection reads.

    Returns:
        ``{"ready": [ids to queue now], "in_flight": [ids still awaiting a
        scrape outcome]}``. Resolved ids (annotated, permanently failed either
        way) leave in_flight so it cannot grow without bound.
    """
    settings = {**DEFAULT_SETTINGS, **(entry.get("settings") or {})}
    in_flight = [str(i) for i in (entry.get("in_flight") or [])]
    target = int(settings.get("annotation_target") or 0)

    if activity is None:
        activity = load_activity(collection_id)
    if activity is None or activity.empty:
        return {"ready": [], "in_flight": in_flight}
    ids = list(dict.fromkeys(activity["item_id"].astype(str)))

    if status is None:
        status = load_status(activity["item_id"].unique())

    eligible = annotation_eligible(ids, status)
    # Clamp to what the target still needs. No target = nothing may be handed
    # off: the plan's goal has been unset, so it must not keep spending on the
    # strength of items queued under an earlier goal.
    room = max(0, target - _annotated_unique(activity, status))
    eligible = eligible[:room]

    # Prune in_flight: an id leaves once its outcome is known — handed off now,
    # already annotated (ok or fail), or its scrape permanently failed. What
    # remains is still genuinely awaiting a scrape.
    resolved = set(eligible)
    if status is not None and not status.empty:
        for iid in in_flight:
            if iid in resolved:
                continue
            if iid in status.index:
                row = status.loc[iid]
                if bool(row.get("annotated_ok")) or bool(row.get("annotated_fail"))                         or bool(row.get("scrape_fail")):
                    resolved.add(iid)
    remaining = [i for i in in_flight if i not in resolved]
    return {"ready": eligible, "in_flight": remaining}


def queue_for_annotation(item_ids: list[str]) -> int:
    """Append ids to the global annotation queue atomically. Returns how many."""
    ids = [str(i) for i in item_ids if str(i)]
    if not ids:
        return 0
    data_io.update_json(
        storage_location="cache", filename=ANNOTATE_QUEUE_FILENAME,
        mutate=lambda current: sorted(
            {str(v) for v in (current if isinstance(current, list) else [])} | set(ids)
        ),
        default=[],
    )
    return len(ids)


# --------------------------------------------------------------------------- #
# Progress, for the modal
# --------------------------------------------------------------------------- #

def progress(collection_id: str, entry: dict | None = None) -> dict:
    """Coverage and milestone figures for one collection's enrichment panel.

    Reads the truth (activity + enrichment status) rather than the ledger, so the
    numbers stay right even if a plan is re-armed, edited or hand-queued around.

    Two different denominators come back, and mixing them up is the easy mistake:

    * ``total_items`` counts **video-days** — one row per (video, day), because a
      video replayed twice in one day is one unit of work but the same video seen
      on three days is three days' worth of viewing to explain.
    * ``unique_items`` counts **videos**. This is what the target is measured
      in: a video is scraped and annotated once, however often it was watched.

    ``target_floor`` / ``target_ceiling`` bound the useful annotation target:
    at or below the floor (what is already annotated) the target is already
    met, and above the ceiling (everything not permanently failed) it can never
    be reached.
    """
    entry = entry if isinstance(entry, dict) else (get_plan(collection_id) or {})
    settings = {**DEFAULT_SETTINGS, **(entry.get("settings") or {})}
    out = {
        "state": entry.get("state"),
        "cycles": int(entry.get("cycles") or 0),
        "spent_items": int(entry.get("spent_items") or 0),
        "annotation_target": int(settings.get("annotation_target") or 0),
        "a_cursor": entry.get("a_cursor"),
        "b_cursor": entry.get("b_cursor"),
        "stall_count": int(entry.get("stall_count") or 0),
        "last_error": entry.get("last_error"),
        "last_cycle_at": entry.get("last_cycle_at"),
        "last_batch": entry.get("last_batch"),
        # What Auto resolved cycle_items to last cycle (None in manual mode) —
        # the panel's disabled input displays it.
        "last_auto_cycle_items": entry.get("last_auto_cycle_items"),
        "milestone_days": MILESTONE_DAYS,
        "total_items": 0, "scraped_items": 0, "annotated_items": 0,
        "unique_items": 0, "unique_scraped": 0, "unique_annotated": 0,
        "unique_failed": 0,
        "total_days": 0, "qualifying_days": 0, "milestone_pct": 0.0,
        "oldest_day": None, "newest_day": None,
        "target_floor": 0, "target_ceiling": 0,
    }
    try:
        activity = load_activity(collection_id)
        if activity is None or activity.empty:
            return out
        status = load_status(activity["item_id"].unique())
        items = activity["item_id"].to_numpy()
        if status is not None and not status.empty:
            scraped = status["scraped_ok"].reindex(items).fillna(False).to_numpy(dtype=bool)
            annotated = status["annotated_ok"].reindex(items).fillna(False).to_numpy(dtype=bool)
        else:
            scraped = np.zeros(len(items), dtype=bool)
            annotated = np.zeros(len(items), dtype=bool)

        # Per-video (not per video-day) coverage: what the budget actually pays
        # for. `u_failed` is the permanently unprocessable tail — annotation
        # failed for good, or the scrape failed and will never be retried — so
        # it belongs in neither the "done" nor the "still to do" figure.
        unique_ids = pd.Index(pd.unique(items))
        if status is not None and not status.empty:
            def _uflag(name):
                if name not in status.columns:
                    return np.zeros(len(unique_ids), dtype=bool)
                return status[name].reindex(unique_ids).fillna(False).to_numpy(dtype=bool)
            u_scraped = _uflag("scraped_ok")
            u_annotated = _uflag("annotated_ok")
            u_failed = (~u_annotated
                        & (_uflag("annotated_fail") | (_uflag("scrape_fail") & ~u_scraped)))
        else:
            u_scraped = np.zeros(len(unique_ids), dtype=bool)
            u_annotated = np.zeros(len(unique_ids), dtype=bool)
            u_failed = np.zeros(len(unique_ids), dtype=bool)

        # Per-row failed mask, day-aligned like `scraped`/`annotated` above.
        if status is not None and not status.empty:
            def _rflag(name):
                if name not in status.columns:
                    return np.zeros(len(items), dtype=bool)
                return status[name].reindex(items).fillna(False).to_numpy(dtype=bool)
            row_failed = (~annotated
                          & (_rflag("annotated_fail") | (_rflag("scrape_fail") & ~scraped)))
        else:
            row_failed = np.zeros(len(items), dtype=bool)

        frame = activity.assign(_ann=annotated,
                                _await=scraped & ~annotated,
                                _fail=row_failed)
        grouped = frame.groupby("day", observed=True)
        per_day = grouped["_ann"].sum()
        min_day = int(settings["min_day_items"])
        # A "qualifying" day is one Correlations would actually keep: at least
        # min_day_items of it annotated.
        qualifying = int((per_day >= min_day).sum())

        # The stacked daily series for the panel's activity chart: per active
        # day, how the day's video-days split by enrichment state. One row per
        # active day (~a few hundred) — small enough to ride on every load.
        agg = grouped.agg(_n=("_ann", "size"), _a=("_ann", "sum"),
                          _w=("_await", "sum"), _f=("_fail", "sum"))
        agg = agg.sort_index()
        daily = {
            "dates": [_day_key(d) for d in agg.index],
            "annotated": [int(v) for v in agg["_a"]],
            "awaiting": [int(v) for v in agg["_w"]],
            "failed": [int(v) for v in agg["_f"]],
            "total": [int(v) for v in agg["_n"]],
        }

        out.update({
            "total_items": int(len(items)),
            "scraped_items": int(scraped.sum()),
            "annotated_items": int(annotated.sum()),
            "unique_items": int(len(unique_ids)),
            "unique_scraped": int(u_scraped.sum()),
            "unique_annotated": int(u_annotated.sum()),
            "unique_failed": int(u_failed.sum()),
            # The window of targets that do anything: below what is already
            # annotated the plan is instantly complete, above everything that
            # has not permanently failed it can never finish. Still a ceiling,
            # not a promise — videos can keep failing on the way there.
            "target_floor": int(u_annotated.sum()),
            "target_ceiling": int(len(unique_ids) - int(u_failed.sum())),
            "total_days": int(frame["day"].nunique()),
            "qualifying_days": qualifying,
            "milestone_pct": round(min(1.0, qualifying / MILESTONE_DAYS) * 100, 1),
            "oldest_day": _day_key(frame["day"].min()),
            "newest_day": _day_key(frame["day"].max()),
            "daily": daily,
            "min_day_items": min_day,
        })
    except Exception as exc:
        logger.error(f"collection_enrichment.progress({collection_id}) failed: {exc}")
    return out


def last_tick() -> dict:
    """The supervisor's most recent tick, as the modal needs to report it.

    On Cloud Run a tick is a dispatched Cloud Task, so the POST that starts one
    can only say it was dispatched — it cannot say what the tick decided. Without
    this the panel reads the same whether the loop queued a slice or found the
    budget already spent, which is exactly how a plan that is silently complete
    looks like a broken loop.

    Returns:
        ``{"state", "action", "message", "error", "start_time", "updated_at"}``,
        or ``{}`` when the supervisor has never run (or the status is unreadable).
    """
    try:
        from web_interface.task_status import read_task_status
        status = read_task_status(SUPERVISOR_TASK) or {}
    except Exception as exc:
        logger.error(f"collection_enrichment.last_tick failed: {exc}")
        return {}
    if not isinstance(status, dict) or not status:
        return {}
    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    progress_msg = (status.get("progress") or {}).get("message") \
        if isinstance(status.get("progress"), dict) else None
    return {
        "state": status.get("state"),
        "action": data.get("action"),
        "message": data.get("message") or progress_msg,
        "error": status.get("error"),
        "start_time": status.get("start_time"),
        "updated_at": status.get("updated_at"),
    }


def activity(platform: str | None = None) -> dict:
    """What the enrichment machinery is doing right now, for the panel's
    status strip.

    Answers "what is actually happening" from the worker task statuses — the
    same files the supervisor's busy gate reads — rather than from anything
    the client remembers, so the strip can never disagree with the workers.
    One running worker is enough: the machinery is serial by design, and the
    strip is a summary, not a scheduler view.

    Args:
        platform: The plan's platform, to name the right scraper queue. When
            unknown (no plan yet), only the shared annotator / consolidation
            workers are reported.

    Returns:
        ``{"kind", "worker", "message", "started_at"}`` where ``kind`` is one
        of ``"scraping"``, ``"annotating"``, ``"consolidating"`` or
        ``"waiting"``; ``message`` is the running worker's own latest progress
        line (None when it has not reported one).
    """
    candidates = []
    if platform:
        candidates.append((f"queue_scraper_{platform}", "scraping"))
    candidates += [("queue_annotator_batch", "annotating"),
                   ("queue_annotator", "annotating"),
                   ("consolidate_enrichment", "consolidating")]
    try:
        from web_interface.services.worker_status import _is_worker_running
        from web_interface.task_status import read_task_status
        for name, kind in candidates:
            if not _is_worker_running(name):
                continue
            status = read_task_status(name) or {}
            progress_msg = (status.get("progress") or {}).get("message") \
                if isinstance(status.get("progress"), dict) else None
            return {"kind": kind, "worker": name, "message": progress_msg,
                    "started_at": status.get("start_time")}
    except Exception as exc:
        logger.error(f"collection_enrichment.activity failed: {exc}")
    return {"kind": "waiting", "worker": None, "message": None,
            "started_at": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
