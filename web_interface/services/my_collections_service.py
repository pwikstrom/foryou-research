"""Donated-data-only stats for the "My Collections" My-stuff page.

Everything here is computed from the participant's own rows of
``recoded/collections_recoded.parquet`` (plus donated-status enrichment-seed
rows and the corpus-wide ``('personas', …)`` columns of
``collections_metadata.parquet`` for population percentiles). No scrape data,
no annotations — a collection is fully explorable the moment it is ingested.

The personality bundle degrades gracefully: every section is independently
nullable and a ``capabilities`` object tells the frontend what the donation
actually contains (Instagram DDPs have plays+faves only, zeeschuimer captures
no play durations, watch-history-only donations have no engagement rows).
"""

import os
import re
import time

import pandas as pd

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

from ..collection_accounts import collections_for_user
from .study_data import get_collection_tags

RECODED_FILENAME = f"{COLLECTIONS_LABEL}_recoded.parquet"
METADATA_FILENAME = f"{COLLECTIONS_LABEL}_metadata.parquet"
MANIFEST_FILENAME = "ingestion_manifest.json"


class PendingPreviewError(Exception):
    """A pending upload could not be turned into a personality preview.

    The message is participant-facing. Raised by ``build_pending_personality``
    when the platform parser rejects the file — the same parser the pipeline
    would use, so this doubles as the QA gate for self-serve donations.
    """

_VIEW_TYPES = ["play", "observe"]
_LIKE_TYPES = ["fave", "like", "fave_item"]

_ACTIVITY_COLUMNS = [
    "collection_id", "activity_type", "item_id", "play_duration", "session_id",
    "local_timestamp", "local_date", "local_weekday", "local_week",
    "local_day_segment", "extra_data", "source_platform", "data_source",
]

# ('personas', …) columns used for the picker list and corpus percentiles.
_PERSONA_FIELDS = [
    "total_events", "active_days", "first_event_ts", "last_event_ts",
    "total_watch_time_s", "daily_watch_time_s", "videos_per_day",
    "num_watches", "num_comments", "num_likes", "likes_per_video",
    "median_watch_time_s",
]

_WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]

# local_day_segment value -> persona archetype (segments from
# fyp.ingest.base._day_segment_from_hour: night 0-5, morning 6-11,
# afternoon 12-17, evening 18-23).
_ARCHETYPES = {
    "morning": "Morning Person",
    "afternoon": "Afternoon Ace",
    "evening": "Night Owl",
    "night": "Overnighter",
}

# Eight-word ladders per axis, indexed by floor(score / 12.5). Second person,
# kind at both extremes — nobody gets shamed for how they scroll.
_LADDERS = {
    "patience": ["Lightning-Scrolling", "Quick-Flicking", "Speed-Sampling",
                 "Curiously Skimming", "Steady-Viewing", "Attentive",
                 "Deep-Diving", "Deep-Soaking"],
    "enthusiasm": ["Cool-Handed", "Reserved", "Selective", "Warm-Hearted",
                   "Cheerful", "Generous", "Wholehearted", "Double-Tap-Devoted"],
    "consistency": ["Free-Range", "Spontaneous", "Wandering", "Flexible",
                    "Rhythmic", "Habitual", "Clockwork", "Metronomic"],
    "binge": ["Snack-Size", "Quick-Dipping", "Casual", "Session-Sipping",
              "Steady-Streaming", "Marathon-Curious", "Binge-Artist",
              "Full-Season"],
    "chattiness": ["Silently Observing", "Quietly Lurking", "Occasionally Commenting",
                   "Measured", "Conversational", "Chatty", "Talkative",
                   "Comment-Section-Regular"],
}

# Preferred axis per statement slot, with fallbacks so the sentence never has
# holes when a donation lacks durations or engagement rows.
_STATEMENT_SLOTS = [
    ["patience", "binge"],
    ["enthusiasm", "chattiness", "binge"],
    ["consistency", "binge", "chattiness"],
]

# Pragmatic emoji matcher: pictographs, symbols and flags — deliberately NOT
# \p{Emoji}, which also matches plain digits and #/* (an old-app bug that made
# "3" many people's favourite emoji).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"   # arrows/stars block (⭐ etc.)
    "\U00002700-\U000027BF"
    "]"
)

_CACHE_TTL_S = 600
_bundle_cache: dict[tuple, tuple[float, dict]] = {}
_corpus_cache: dict[str, tuple[float, dict]] = {}
# Per-user (platforms, coverage) maps from the owned-collections scan — the
# scan now carries item_id/activity_type for the coverage column, so it is
# worth memoising. {username: (ts, platforms, coverage)}.
_coverage_cache: dict[str, tuple[float, dict, dict]] = {}

# The Persona checkboxes make _bundle_cache keys combinatorial (any subset of
# a user's collections), so the cache needs bounds: expired entries are swept
# on every write and the total is capped by dropping the oldest.
_BUNDLE_CACHE_MAX = 32


def _evict_bundle_cache(now: float) -> None:
    """Called before every cache write: drop expired entries, then oldest
    entries until the incoming write will fit under the cap."""
    for k in [k for k, (ts, _) in _bundle_cache.items() if now - ts >= _CACHE_TTL_S]:
        _bundle_cache.pop(k, None)
    while len(_bundle_cache) >= _BUNDLE_CACHE_MAX:
        oldest = min(_bundle_cache, key=lambda k: _bundle_cache[k][0])
        _bundle_cache.pop(oldest, None)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _load_metadata_personas(collection_ids: list[str] | None = None) -> pd.DataFrame | None:
    """Load the ('personas', …) columns of collections_metadata.parquet.

    Requests both the stringified-tuple and plain column names (the on-disk
    form depends on how the frame was last written — same defensive pattern
    as ``stats_service._load_collection_event_windows``).
    """
    if not data_io.exists(storage_location="recoded", filename=METADATA_FILENAME):
        return None
    columns = []
    for f in _PERSONA_FIELDS:
        columns.append(f"('personas', '{f}')")
        columns.append(f)
    columns.append("('other', 'ts_added_to_dataset')")
    columns.append("ts_added_to_dataset")
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=METADATA_FILENAME,
            columns=columns,
            set_index="collection_id",
        )
    except Exception as e:
        print(f"[my_collections] failed to load collections_metadata: {e}")
        return None
    if df is None or df.empty:
        return None
    # Normalize to flat single-level column names.
    flat = {}
    for f in _PERSONA_FIELDS:
        for candidate in (("personas", f), f):
            if candidate in df.columns:
                flat[f] = df[candidate]
                break
    for candidate in (("other", "ts_added_to_dataset"), "ts_added_to_dataset"):
        if candidate in df.columns:
            flat["ts_added_to_dataset"] = df[candidate]
            break
    if not flat:
        return None
    out = pd.DataFrame(flat)
    out.index = out.index.map(str)
    if collection_ids is not None:
        out = out[out.index.isin([str(c) for c in collection_ids])]
    return out


def _load_activities(collection_ids: list[str]) -> pd.DataFrame | None:
    """Load the donated activity rows for ``collection_ids`` (base columns only)."""
    if not collection_ids:
        return None
    if not data_io.exists(storage_location="recoded", filename=RECODED_FILENAME):
        return None
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=_ACTIVITY_COLUMNS,
            filters=[("collection_id", "in", [str(c) for c in collection_ids])],
        )
    except Exception as e:
        print(f"[my_collections] failed to load activities: {e}")
        return None
    if df is None or df.empty:
        return None
    df["local_timestamp"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    # local_hour is contract-derived but not persisted in the master parquet.
    df["local_hour"] = df["local_timestamp"].dt.hour
    return df


def _load_donated_seed_row(platform: str, data_source: str, item_id: str) -> dict | None:
    """Look up a donated (never scraped) enrichment-seed row for one item.

    YouTube DDPs carry titles/channels and Instagram DDPs carry captions/owners
    without any scraping; TikTok supplies nothing. Rows with any other
    ``scrape_status`` are ignored — this page must never surface scrape data.
    """
    filename = f"{platform}_{data_source}_enrichment_seed.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return None
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=["item_id", "desc", "author_name", "scrape_status"],
            filters=[("item_id", "==", str(item_id))],
        )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df[df["scrape_status"] == "donated"]
    if df.empty:
        return None
    row = df.iloc[0]
    desc = row.get("desc")
    author = row.get("author_name")
    return {
        "desc": None if pd.isna(desc) else str(desc),
        "author_name": None if pd.isna(author) else str(author),
    }


# ---------------------------------------------------------------------------
# Corpus percentiles
# ---------------------------------------------------------------------------

def corpus_percentile_frame(force: bool = False) -> pd.DataFrame | None:
    """The full corpus personas frame, cached for ``_CACHE_TTL_S`` seconds."""
    now = time.time()
    hit = _corpus_cache.get("frame")
    if not force and hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    df = _load_metadata_personas(None)
    _corpus_cache["frame"] = (now, df)
    return df


def _percentile(series: pd.Series | None, value: float | None) -> float | None:
    """Share of corpus collections with a strictly smaller value, as 0-100."""
    if value is None or series is None:
        return None
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 3:  # a percentile against 1-2 peers is noise, not honesty
        return None
    return round(float((s < value).mean()) * 100, 1)


# ---------------------------------------------------------------------------
# Self-serve donation uploads
# ---------------------------------------------------------------------------

def donation_upload_sources() -> list[dict]:
    """One entry per participant-uploadable donation ingester.

    Enumerated from the registered ingest classes — a new platform class shows
    up here (and as an upload card) automatically. Machine sources (aio fetch,
    zeeschuimer captures) are excluded: participants upload platform DDP
    exports only.
    """
    from fyp.ingest import get_main_collection
    out = []
    for col in get_main_collection(verbose=False).collections:
        if getattr(col, "ingestion_mode", "upload") != "upload":
            continue
        if col.data_source != "ddp":
            continue
        out.append({
            "source_platform": col.source_platform,
            "data_source": col.data_source,
            "raw_path": col.raw_path,
            "class_name": col.__class__.__name__,
            "accepted_upload_suffixes": col.accepted_upload_suffixes(),
            "zip_member_suffixes": col.zip_member_suffixes(),
            "review": col.review_manifest(),
        })
    return out


def _pending_uploads_for_user(username: str) -> dict[str, dict]:
    """{collection_id: {raw_path, filename, source_platform, tz}} for this
    user's manifest entries across all donation raw locations."""
    pending: dict[str, dict] = {}
    for src in donation_upload_sources():
        raw_path = src["raw_path"]
        if not data_io.exists(storage_location=raw_path, filename=MANIFEST_FILENAME):
            continue
        manifest = data_io.load_json(
            storage_location=raw_path, filename=MANIFEST_FILENAME, verbose=False) or {}
        for fn, entry in manifest.items():
            if not isinstance(entry, dict) or entry.get("user_id") != username:
                continue
            cid = entry.get("collection_id") or os.path.splitext(fn)[0]
            pending[str(cid)] = {
                "raw_path": raw_path,
                "filename": fn,
                "source_platform": src["source_platform"],
                "data_source": src["data_source"],
                "tz": entry.get("tz"),
            }
    return pending


def _fresh_ingester(raw_path: str):
    """A fresh instance of the registered class serving ``raw_path``, or None.

    Fresh (not the shared ``get_main_collection`` singletons) so a preview's
    ``data``/``state`` mutations can never leak into other requests.
    """
    from fyp.ingest.base import ForYouBaseCollection
    for cls in ForYouBaseCollection._registry:
        if getattr(cls, "raw_path", None) == raw_path:
            inst = cls()
            if getattr(inst, "ingestion_mode", "upload") == "upload" and inst.data_source == "ddp":
                return inst
    return None


def build_pending_personality(raw_path: str, filename: str) -> dict:
    """Personality bundle computed straight from an uploaded raw file.

    Replicates the per-file portion of the pipeline's ``load_raw`` →
    ``process`` → local-time → sessions path on a fresh single-file instance —
    the exact same parser and transforms ``ingest_refresh`` will run, but
    entirely in memory: nothing is written, no corpus state is touched.
    Raises :class:`PendingPreviewError` (participant-facing message) when the
    file cannot be parsed into enough activities.
    """
    key = ("pending", raw_path, filename)
    now = time.time()
    hit = _bundle_cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]

    from fyp.ingest.base import _MANIFEST_TZ_COLUMN

    inst = _fresh_ingester(raw_path)
    if inst is None:
        raise PendingPreviewError("Unknown donation platform.")
    label = platform_display_label(inst.source_platform)

    manifest = {}
    if data_io.exists(storage_location=raw_path, filename=MANIFEST_FILENAME):
        manifest = data_io.load_json(
            storage_location=raw_path, filename=MANIFEST_FILENAME, verbose=False) or {}
    entry = manifest.get(filename) or {}
    cid = entry.get("collection_id") or os.path.splitext(filename)[0]

    inst._current_file_tz = entry.get("tz") or None
    try:
        one_df = inst.load_single_raw(filename)
    except Exception as exc:
        raise PendingPreviewError(
            f"This doesn't look like a valid {label} export "
            f"(we couldn't read it: {exc}). Please check the file and try again."
        )
    if len(one_df) < inst.min_required_rows_per_raw_file:
        raise PendingPreviewError(
            f"We could read the file, but it holds almost no {label} activity "
            f"({len(one_df)} events). Is this the right export?"
        )

    mtime = data_io.getmtime(storage_location=raw_path, filename=filename)
    one_df["ts_added_to_dataset"] = pd.to_datetime(mtime, unit="s")
    one_df["raw_file"] = filename
    one_df["collection_id"] = cid
    one_df[_MANIFEST_TZ_COLUMN] = inst._current_file_tz if inst._current_file_tz else pd.NA

    inst.file_stats_this_run = {filename: {"raw_rows": int(len(one_df)), "dropped": {}}}
    inst.data = one_df
    inst.state = "raw"
    try:
        inst.process()
        inst.add_local_time_features()
        inst.add_session_ids()
    except Exception as exc:
        raise PendingPreviewError(
            f"We couldn't make sense of the activities in this {label} export "
            f"({exc}). Please check the file and try again."
        )
    df = inst.data
    if df is None or len(df) < inst.min_required_rows_per_raw_file:
        raise PendingPreviewError(
            f"We could read the file, but almost none of it turned into usable "
            f"{label} activity. Is this the right export?"
        )

    df = df.copy()
    df["local_timestamp"] = pd.to_datetime(df["local_timestamp"], errors="coerce")
    df["local_hour"] = df["local_timestamp"].dt.hour

    bundle = _compute_bundle(df, [str(cid)])
    bundle["pending"] = True
    _evict_bundle_cache(now)
    _bundle_cache[key] = (now, bundle)
    return bundle


def platform_display_label(platform: str | None) -> str:
    labels = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}
    return labels.get(str(platform), str(platform or "donation"))


def discard_pending_upload(raw_path: str, filename: str) -> None:
    """Remove a rejected pending upload: the raw file, its manifest entry, and
    its tags-sidecar link (only when the collection isn't in the dataset)."""
    from ..collection_accounts import drop_collection_entry

    manifest = {}
    if data_io.exists(storage_location=raw_path, filename=MANIFEST_FILENAME):
        manifest = data_io.load_json(
            storage_location=raw_path, filename=MANIFEST_FILENAME, verbose=False) or {}
    entry = manifest.pop(filename, None) or {}
    data_io.save_json(data=manifest, storage_location=raw_path,
                      filename=MANIFEST_FILENAME, verbose=False)
    if data_io.exists(storage_location=raw_path, filename=filename):
        data_io.remove(storage_location=raw_path, filename=filename)

    cid = entry.get("collection_id") or os.path.splitext(filename)[0]
    meta = _load_metadata_personas([str(cid)])
    in_dataset = meta is not None and str(cid) in meta.index
    if not in_dataset:
        drop_collection_entry(str(cid))
    _bundle_cache.pop(("pending", raw_path, filename), None)


# ---------------------------------------------------------------------------
# Participant withdrawals (delete with a 30-day restore window)
# ---------------------------------------------------------------------------

WITHDRAWALS_FILENAME = "withdrawals.json"
WITHDRAWAL_RETENTION_DAYS = 30


def _load_withdrawals_raw() -> dict:
    if data_io.exists(storage_location="recoded", filename=WITHDRAWALS_FILENAME):
        return data_io.load_json(
            storage_location="recoded", filename=WITHDRAWALS_FILENAME, verbose=False) or {}
    return {}


def _save_withdrawals(w: dict) -> None:
    data_io.save_json(data=w, storage_location="recoded",
                      filename=WITHDRAWALS_FILENAME, verbose=False)


def _utc_now() -> pd.Timestamp:
    """Now as an offset-aware UTC instant — these stamps reach the browser."""
    return pd.Timestamp.now(tz="UTC")


def _as_utc(value) -> pd.Timestamp | None:
    """Parse a stored stamp to offset-aware UTC, or ``None`` if unusable.

    Ledger entries written before the stamps became offset-aware are naive
    UTC, so a zone-less value is localised rather than rejected.
    """
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def load_withdrawals(purge: bool = True) -> dict:
    """The withdrawal ledger: {cid: {user_id, deleted_at, restorable_until,
    display_id, source_platform, raw_path, files: [filename, ...]}}.

    With ``purge`` (the default), entries past their restore window are
    removed lazily on read: their archived raw files are deleted for good and
    the record dropped. No scheduler needed — the ledger is read on every
    My Collections page load, and restores are date-checked independently.
    """
    w = _load_withdrawals_raw()
    if not purge or not w:
        return w
    now = _utc_now()
    expired = []
    for cid, entry in w.items():
        until = _as_utc(entry.get("restorable_until"))
        if until is None or now <= until:
            continue
        expired.append(cid)
        for fn in entry.get("files") or []:
            try:
                if data_io.exists(storage_location="archive", filename=fn):
                    data_io.remove(storage_location="archive", filename=fn)
            except Exception as exc:
                print(f"[my_collections] purge of archived '{fn}' failed: {exc}")
    if expired:
        for cid in expired:
            w.pop(cid, None)
        _save_withdrawals(w)
    return w


def record_withdrawal(cid: str, username: str, files: list[str],
                      raw_path: str | None, display_id: str | None,
                      source_platform: str | None) -> dict:
    """Write the ledger entry for a just-requested withdrawal."""
    now = _utc_now()
    entry = {
        "user_id": username,
        "deleted_at": now.isoformat(timespec="seconds"),
        "restorable_until": (now + pd.Timedelta(days=WITHDRAWAL_RETENTION_DAYS)).isoformat(timespec="seconds"),
        "display_id": display_id,
        "source_platform": source_platform,
        "raw_path": raw_path,
        "files": list(files),
    }
    w = _load_withdrawals_raw()
    w[str(cid)] = entry
    _save_withdrawals(w)
    return entry


def drop_withdrawal(cid: str) -> None:
    w = _load_withdrawals_raw()
    if str(cid) in w:
        w.pop(str(cid))
        _save_withdrawals(w)


class RestoreError(Exception):
    """A withdrawal could not be restored; the message is participant-facing."""


def restore_withdrawal(cid: str) -> dict:
    """Bring a withdrawn donation back: move the archived raw file(s) into the
    platform's upload location and relink the account — the collection becomes
    a normal pending upload (instant preview, re-added on the next process
    run). Raises :class:`RestoreError` past the window or when the archived
    file is not available (e.g. the delete worker hasn't archived it yet)."""
    from ..collection_accounts import set_collection_owner

    w = _load_withdrawals_raw()
    entry = w.get(str(cid))
    if not isinstance(entry, dict):
        raise RestoreError("No withdrawal record found for this collection.")
    until = _as_utc(entry.get("restorable_until"))
    if until is None or _utc_now() > until:
        raise RestoreError("The restore window for this collection has closed.")

    raw_path = entry.get("raw_path")
    files = entry.get("files") or []
    if not raw_path or not files:
        raise RestoreError("This withdrawal has no restorable file on record.")

    missing = [fn for fn in files
               if not data_io.exists(storage_location="archive", filename=fn)]
    if missing:
        raise RestoreError(
            "The archived file isn't available yet — the removal may still be "
            "processing. Try again in a few minutes.")

    manifest = {}
    if data_io.exists(storage_location=raw_path, filename=MANIFEST_FILENAME):
        manifest = data_io.load_json(
            storage_location=raw_path, filename=MANIFEST_FILENAME, verbose=False) or {}
    for fn in files:
        data_io.move(src_storage_location="archive", dst_storage_location=raw_path,
                     filename=fn, verbose=False)
        if not data_io.exists(storage_location=raw_path, filename=fn):
            raise RestoreError("Restoring the file did not persist. Try again.")
        manifest[fn] = {"collection_id": str(cid), "tags": [],
                        "user_id": entry.get("user_id")}
    data_io.save_json(data=manifest, storage_location=raw_path,
                      filename=MANIFEST_FILENAME, verbose=False)
    set_collection_owner(str(cid), entry.get("user_id"))
    drop_withdrawal(str(cid))
    invalidate_cache()
    return entry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _platforms_and_coverage(username: str, cids: list[str]) -> tuple[dict, dict]:
    """Per-collection platform/source plus scraped/annotated coverage, TTL-cached.

    One selective scan of the recoded parquet over the user's collections.
    Coverage is the share of a collection's VIEW activities (play/observe —
    never ``total_events``, which counts likes/searches/follows that have no
    scrapeable item) whose item is scraped / annotated in
    ``enrichment_status.parquet``. Missing status table or no view rows →
    the collection simply has no coverage entry (UI shows an em-dash).
    """
    now = time.time()
    hit = _coverage_cache.get(username)
    if hit and (now - hit[0]) < _CACHE_TTL_S:
        return hit[1], hit[2]

    platforms: dict[str, dict] = {}
    coverage: dict[str, dict] = {}
    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=RECODED_FILENAME,
            columns=["collection_id", "source_platform", "data_source",
                     "item_id", "activity_type"],
            filters=[("collection_id", "in", cids)],
        )
        if df is not None and not df.empty:
            for cid, grp in df.groupby("collection_id", observed=True):
                platforms[str(cid)] = {
                    "source_platform": str(grp["source_platform"].mode().iloc[0]),
                    "data_source": str(grp["data_source"].mode().iloc[0]),
                }
            from . import preview_cache
            status = preview_cache.get_enrichment_status_cached()
            views = df[df["activity_type"].astype(str).isin(_VIEW_TYPES)]
            if status is not None and len(views):
                iid_keys = views["item_id"].astype(str).to_numpy()
                scraped, annotated = preview_cache.status_flags(iid_keys, status)
                flags = pd.DataFrame({
                    "collection_id": views["collection_id"].astype(str).to_numpy(),
                    "scraped": scraped,
                    "annotated": annotated,
                })
                for cid, grp in flags.groupby("collection_id", observed=True):
                    coverage[str(cid)] = {
                        "pct_scraped": round(float(grp["scraped"].mean()), 4),
                        "pct_annotated": round(float(grp["annotated"].mean()), 4),
                    }
    except Exception as e:
        print(f"[my_collections] platform/coverage lookup failed: {e}")
        # Fall through with whatever was collected; do not cache a failure
        # for the full TTL.
        return platforms, coverage

    _coverage_cache[username] = (now, platforms, coverage)
    return platforms, coverage


def list_owned_collections(username: str) -> list[dict]:
    """Light per-collection metadata for the picker cards.

    Includes uploaded-but-not-yet-processed donations (status "pending") —
    the account link is written at upload time, so pending collections are
    already owned; they just have no corpus metadata yet.
    """
    withdrawals = {cid: e for cid, e in load_withdrawals().items()
                   if isinstance(e, dict) and e.get("user_id") == username}
    cids = collections_for_user(username)
    if not cids and not withdrawals:
        return []
    tags = get_collection_tags() or {}
    meta = _load_metadata_personas(cids)
    pending = _pending_uploads_for_user(username)

    # One selective scan for platform/source (metadata doesn't carry it) and
    # enrichment coverage. Skipped for a withdrawn-only listing: an empty id
    # list makes the pyarrow filter throw.
    platforms: dict[str, dict] = {}
    coverage: dict[str, dict] = {}
    if cids:
        platforms, coverage = _platforms_and_coverage(username, cids)

    out = []
    for cid in cids:
        if cid in withdrawals:
            continue  # rendered from the withdrawal ledger below
        entry = tags.get(cid) if isinstance(tags.get(cid), dict) else {}
        in_dataset = meta is not None and cid in meta.index
        pend = pending.get(cid) if not in_dataset else None
        item = {
            "collection_id": cid,
            "display_id": entry.get("display_collection_id") or cid,
            "source_platform": (pend or platforms.get(cid, {})).get("source_platform"),
            "data_source": (pend or platforms.get(cid, {})).get("data_source"),
            "status": "pending" if pend else "ready",
            "raw_path": pend.get("raw_path") if pend else None,
            "filename": pend.get("filename") if pend else None,
            "total_events": None,
            "active_days": None,
            "first_event_ts": None,
            "last_event_ts": None,
            "ts_added_to_dataset": None,
            "total_watch_time_s": None,
            "pct_scraped": (coverage.get(cid) or {}).get("pct_scraped"),
            "pct_annotated": (coverage.get(cid) or {}).get("pct_annotated"),
        }
        if in_dataset:
            row = meta.loc[cid]
            for f in ("total_events", "active_days", "total_watch_time_s"):
                v = row.get(f)
                item[f] = None if pd.isna(v) else float(v)
            for f in ("first_event_ts", "last_event_ts", "ts_added_to_dataset"):
                v = row.get(f)
                item[f] = None if (v is None or pd.isna(v)) else str(v)
        elif not pend:
            # Linked but neither in the dataset nor pending (e.g. mid-delete):
            # keep the card with null stats rather than hiding it.
            pass
        out.append(item)

    # Withdrawn collections: deleted from the dataset, restorable from the
    # archive until their date.
    for cid, e in withdrawals.items():
        out.append({
            "collection_id": cid,
            "display_id": e.get("display_id") or cid,
            "source_platform": e.get("source_platform"),
            "data_source": None,
            "status": "withdrawn",
            "raw_path": None,
            "filename": None,
            "deleted_at": e.get("deleted_at"),
            "restorable_until": e.get("restorable_until"),
            "total_events": None,
            "active_days": None,
            "first_event_ts": None,
            "last_event_ts": None,
            "ts_added_to_dataset": None,
            "total_watch_time_s": None,
            "pct_scraped": None,
            "pct_annotated": None,
        })
    return out


def build_personality(collection_ids: list[str]) -> dict | None:
    """The full "My Short-Video Personality" bundle for one or more collections.

    Returns None when no donated activity rows exist for the ids.
    """
    key = tuple(sorted(str(c) for c in collection_ids))
    now = time.time()
    hit = _bundle_cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]

    df = _load_activities(list(key))
    if df is None or df.empty:
        return None

    bundle = _compute_bundle(df, list(key))
    _evict_bundle_cache(now)
    _bundle_cache[key] = (now, bundle)
    return bundle


def invalidate_cache() -> None:
    _bundle_cache.clear()
    _corpus_cache.clear()
    _coverage_cache.clear()


# ---------------------------------------------------------------------------
# Bundle computation
# ---------------------------------------------------------------------------

def _trim_to_watch_window(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop each collection's rows that predate its first viewing event.

    Platform exports keep engagement (like lists especially) from the
    beginning of time while the watch history only reaches back months —
    counting those old likes would make a donor look far more engaged than
    they are *per video watched*. The corpus personas stats
    (``calc_collection_stats.process_single_collection``) apply the same
    first-play cut, so trimming here keeps every cohort comparison
    apples-to-apples. A collection with no viewing events at all is kept
    whole (there is nothing to anchor the cut to).

    Returns the trimmed frame and the number of dropped rows.
    """
    keep_masks = []
    for _cid, grp in df.groupby("collection_id", sort=False):
        first_play = grp.loc[grp["activity_type"].isin(_VIEW_TYPES), "local_timestamp"].min()
        if pd.isna(first_play):
            keep_masks.append(pd.Series(True, index=grp.index))
        else:
            keep_masks.append(grp["local_timestamp"] >= first_play)
    keep = pd.concat(keep_masks).reindex(df.index, fill_value=True)
    dropped = int((~keep).sum())
    return df[keep], dropped


def _compute_bundle(df: pd.DataFrame, collection_ids: list[str]) -> dict:
    df, pre_play_dropped = _trim_to_watch_window(df)
    plays = df[df["activity_type"].isin(_VIEW_TYPES)]
    likes = df[df["activity_type"].isin(_LIKE_TYPES)]
    comments = df[df["activity_type"] == "comment"]
    posts = df[df["activity_type"] == "post"]
    searches = df[df["activity_type"] == "search"]

    durations = pd.to_numeric(plays["play_duration"], errors="coerce").dropna() \
        if "play_duration" in plays.columns else pd.Series(dtype=float)
    has_durations = len(plays) > 0 and (len(durations) / len(plays)) >= 0.05

    capabilities = {
        "has_watch_durations": bool(has_durations),
        "has_likes": len(likes) > 0,
        "has_comments": len(comments) > 0,
        "has_posts": len(posts) > 0,
        "has_searches": len(searches) > 0,
        "has_sessions": bool("session_id" in df.columns and df["session_id"].notna().any()),
    }

    sessions = _session_stats(df) if capabilities["has_sessions"] else None
    corpus = corpus_percentile_frame()
    n_corpus = int(len(corpus)) if corpus is not None else 0

    axes = _persona_axes(plays, likes, comments, durations, sessions, corpus)
    persona = _persona_statement(axes, plays)
    persona["n_corpus"] = n_corpus

    platforms = sorted(str(p) for p in df["source_platform"].dropna().unique())

    bundle = {
        "collection_ids": collection_ids,
        "platforms": platforms,
        "capabilities": capabilities,
        "pre_play_engagement_dropped": pre_play_dropped,
        "persona": persona,
        "comparisons": _cohort_comparisons(plays, likes, comments, durations, corpus),
        "platform_habits": _platform_habits(plays) if len(platforms) > 1 else None,
        "hour_of_day": _hour_of_day(plays),
        "weekday": _weekday(plays),
        "weekly": _weekly(plays),
        "calendar": _calendar(plays),
        "doomscroll": _doomscroll(durations) if has_durations else None,
        "rewatch": _rewatch(plays),
        "searches": _search_terms(searches) if capabilities["has_searches"] else None,
        "emoji": _favourite_emoji(comments) if capabilities["has_comments"] else None,
        "stats": _stat_strip(df, plays, likes, comments, posts, durations,
                             sessions, has_durations, corpus),
    }
    return bundle


def _session_stats(df: pd.DataFrame) -> dict:
    grp = df.dropna(subset=["session_id"]).groupby("session_id")["local_timestamp"]
    spans = (grp.max() - grp.min()).dt.total_seconds()
    if spans.empty:
        return {"n_sessions": 0, "longest_s": 0.0, "binge_share": None}
    return {
        "n_sessions": int(len(spans)),
        "longest_s": float(spans.max()),
        "binge_share": float((spans > 1200).mean()),
    }


def _persona_axes(plays, likes, comments, durations, sessions, corpus) -> dict:
    """The five axes: score 0-100 (fractions as-is, ratios as corpus percentiles)."""
    n_plays = max(1, len(plays))
    axes: dict[str, dict] = {}

    # Patience — share of watches held for 30s or more (fraction, no norm).
    if len(durations) > 0:
        axes["patience"] = {"score": round(float((durations >= 30).mean()) * 100, 1),
                            "percentile": None}
    else:
        axes["patience"] = {"score": None, "percentile": None}

    # Binge — share of sessions longer than 20 minutes (fraction).
    if sessions and sessions["n_sessions"] > 0 and sessions["binge_share"] is not None:
        axes["binge"] = {"score": round(sessions["binge_share"] * 100, 1),
                         "percentile": None}
    else:
        axes["binge"] = {"score": None, "percentile": None}

    # Consistency — share of views inside the two favourite hours (fraction).
    if len(plays) > 0:
        hour_counts = plays["local_hour"].value_counts()
        axes["consistency"] = {
            "score": round(float(hour_counts.nlargest(2).sum() / len(plays)) * 100, 1),
            "percentile": None,
        }
    else:
        axes["consistency"] = {"score": None, "percentile": None}

    # Chattiness — comments per play, ranked against the corpus.
    if len(comments) > 0:
        own = len(comments) / n_plays
        corpus_series = None
        if corpus is not None and {"num_comments", "num_watches"} <= set(corpus.columns):
            watches = pd.to_numeric(corpus["num_watches"], errors="coerce")
            corpus_series = pd.to_numeric(corpus["num_comments"], errors="coerce") / watches.clip(lower=1)
        pct = _percentile(corpus_series, own)
        axes["chattiness"] = {"score": pct if pct is not None else min(100.0, round(own * 100, 1)),
                              "percentile": pct}
    else:
        axes["chattiness"] = {"score": None, "percentile": None}

    # Enthusiasm — likes per play, ranked against the corpus likes_per_video.
    if len(likes) > 0:
        own = len(likes) / n_plays
        corpus_series = corpus["likes_per_video"] if corpus is not None and "likes_per_video" in corpus.columns else None
        pct = _percentile(corpus_series, own)
        axes["enthusiasm"] = {"score": pct if pct is not None else min(100.0, round(own * 100, 1)),
                              "percentile": pct}
    else:
        axes["enthusiasm"] = {"score": None, "percentile": None}

    return axes


def _persona_statement(axes: dict, plays: pd.DataFrame) -> dict:
    """Pick three ladder words + the time-of-day archetype, backfilling gaps."""
    used: set[str] = set()
    words = []
    for slot in _STATEMENT_SLOTS:
        picked = None
        for axis in slot:
            if axis in used:
                continue
            score = axes.get(axis, {}).get("score")
            if score is not None:
                idx = min(7, int(score // 12.5))
                picked = {"axis": axis, "word": _LADDERS[axis][idx]}
                used.add(axis)
                break
        words.append(picked)
    words = [w for w in words if w]

    # Archetype from the day-segment shares of the viewing events.
    archetype = None
    segment_shares = {}
    if len(plays) > 0 and "local_day_segment" in plays.columns:
        shares = plays["local_day_segment"].value_counts(normalize=True)
        segment_shares = {str(k): round(float(v), 4) for k, v in shares.items()}
        top = shares.idxmax()
        archetype = _ARCHETYPES.get(str(top))

    statement = None
    if words and archetype:
        adjectives = [w["word"] for w in words]
        article = "an" if adjectives[0][0].upper() in "AEIOU" else "a"
        if len(adjectives) == 1:
            joined = adjectives[0]
        else:
            joined = ", ".join(adjectives[:-1]) + f", {adjectives[-1]}"
        statement = f"You are {article} {joined} {archetype}!"

    return {
        "statement": statement,
        "words": words,
        "archetype": archetype,
        "segment_shares": segment_shares,
        "axes": axes,
    }


def _cohort_comparisons(plays, likes, comments, durations, corpus) -> list[dict] | None:
    """Rate-normalized "you vs the cohort" rows.

    Donation lengths vary wildly, so every comparison is a rate — per active
    day or per 1,000 videos watched (the corpus-wide per-play convention) —
    never an absolute count. Cohort values come from the live personas
    columns, which are computed with the same first-play cut this bundle
    applies, so the rates are directly comparable.
    """
    if corpus is None or plays.empty:
        return None
    n_plays = len(plays)
    active_days = max(1, plays["local_timestamp"].dt.date.nunique())

    def _corpus_num(col):
        return pd.to_numeric(corpus[col], errors="coerce") if col in corpus.columns else None

    watches = _corpus_num("num_watches")
    rows_spec = [
        {
            "key": "videos_per_day",
            "label": "videos per active day",
            "own": n_plays / active_days,
            "series": _corpus_num("videos_per_day"),
        },
        {
            "key": "watch_time_per_day",
            "label": "minutes watched per active day",
            "own": (float(durations.sum()) / active_days / 60) if len(durations) else None,
            "series": (_corpus_num("daily_watch_time_s") / 60) if _corpus_num("daily_watch_time_s") is not None else None,
        },
        {
            "key": "median_watch_time",
            "label": "seconds per video before scrolling on",
            "own": float(durations.median()) if len(durations) else None,
            "series": _corpus_num("median_watch_time_s"),
        },
        {
            "key": "likes_per_1k",
            "label": "likes per 1,000 videos",
            "own": (len(likes) / n_plays * 1000) if len(likes) else None,
            "series": (_corpus_num("num_likes") / watches.clip(lower=1) * 1000)
                      if watches is not None and _corpus_num("num_likes") is not None else None,
        },
        {
            "key": "comments_per_1k",
            "label": "comments per 1,000 videos",
            "own": (len(comments) / n_plays * 1000) if len(comments) else None,
            "series": (_corpus_num("num_comments") / watches.clip(lower=1) * 1000)
                      if watches is not None and _corpus_num("num_comments") is not None else None,
        },
    ]

    rows = []
    for spec in rows_spec:
        own, series = spec["own"], spec["series"]
        if own is None or series is None:
            continue
        series = series.dropna()
        series = series[series > 0] if spec["key"] != "videos_per_day" else series
        if len(series) < 3:
            continue
        median = float(series.median())
        pct = _percentile(series, own)
        rows.append({
            "key": spec["key"],
            "label": spec["label"],
            "own": round(float(own), 1),
            "cohort_median": round(median, 1),
            "ratio": round(float(own) / median, 2) if median > 0 else None,
            "percentile": pct,
        })
    return rows or None


def _platform_habits(plays: pd.DataFrame) -> list[dict] | None:
    """Per-platform time-of-day signature, for multi-platform donors —
    the "Instagram by day, TikTok after dark" comparison."""
    if plays.empty or "local_day_segment" not in plays.columns:
        return None
    out = []
    for plat, grp in plays.groupby("source_platform"):
        if grp.empty:
            continue
        shares = grp["local_day_segment"].value_counts(normalize=True)
        top = str(shares.idxmax())
        peak_hour = int(grp["local_hour"].value_counts().idxmax())
        out.append({
            "platform": str(plat),
            "top_segment": top,
            "top_segment_share": round(float(shares.max()), 3),
            "peak_hour": peak_hour,
            "peak_label": _friendly_hour(peak_hour),
            "n_plays": int(len(grp)),
        })
    out.sort(key=lambda d: -d["n_plays"])
    return out if len(out) > 1 else None


def _hour_of_day(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays["local_hour"].value_counts()
    full = [int(counts.get(h, 0)) for h in range(24)]
    peak = int(counts.idxmax())
    result = {"counts": full, "peak_hour": peak, "peak_label": _friendly_hour(peak)}

    # Per-platform overlay for multi-platform donors: each platform's curve is
    # a SHARE of that platform's plays, so a small YouTube donation is still
    # visible next to a huge TikTok one.
    if plays["source_platform"].nunique() > 1:
        by_platform = {}
        for plat, grp in plays.groupby("source_platform"):
            c = grp["local_hour"].value_counts()
            total = max(1, len(grp))
            by_platform[str(plat)] = [round(float(c.get(h, 0)) / total, 4) for h in range(24)]
        result["by_platform"] = by_platform
    return result


def _friendly_hour(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    if hour < 12:
        return f"{hour} AM"
    return f"{hour - 12} PM"


def _weekday(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays["local_weekday"].astype(str).str.lower().value_counts()
    ordered = {d: int(counts.get(d, 0)) for d in _WEEKDAY_ORDER}
    top = max(ordered, key=ordered.get)
    result = {"counts": ordered, "top": top}
    if plays["source_platform"].nunique() > 1:
        by_platform = {}
        for plat, grp in plays.groupby("source_platform"):
            c = grp["local_weekday"].astype(str).str.lower().value_counts()
            by_platform[str(plat)] = {d: int(c.get(d, 0)) for d in _WEEKDAY_ORDER}
        result["by_platform"] = by_platform
    return result


def _weekly(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    counts = plays.groupby("local_week").size()

    def _week_key(w: str):
        try:
            y, wk = str(w).split("-")
            return (int(y), int(wk))
        except Exception:
            return (0, 0)

    weeks = sorted((str(w) for w in counts.index), key=_week_key)
    series = [{"week": w, "count": int(counts[w])} for w in weeks]
    top = max(series, key=lambda d: d["count"])
    return {"series": series, "top_week": top}


def _calendar(plays: pd.DataFrame) -> dict | None:
    if plays.empty:
        return None
    dates = pd.to_datetime(plays["local_date"].astype(str), errors="coerce").dropna()
    if dates.empty:
        return None
    counts = dates.dt.date.value_counts().sort_index()
    days = [{"date": d.isoformat(), "count": int(c)} for d, c in counts.items()]

    # Longest run of consecutive active days.
    longest = streak = 1
    prev = None
    for d in counts.index:
        if prev is not None and (d - prev).days == 1:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 1
        prev = d
    return {"days": days, "longest_streak": int(longest)}


def _doomscroll(durations: pd.Series) -> dict | None:
    if durations.empty:
        return None
    buckets = {
        "under_3s": int((durations < 3).sum()),
        "3_10s": int(((durations >= 3) & (durations < 10)).sum()),
        "10_30s": int(((durations >= 10) & (durations < 30)).sum()),
        "30_60s": int(((durations >= 30) & (durations < 60)).sum()),
        "over_60s": int((durations >= 60).sum()),
    }
    return {
        "buckets": buckets,
        "n_watches": int(len(durations)),
        "median_s": float(durations.median()),
    }


def _rewatch(plays: pd.DataFrame) -> dict | None:
    """The most-rewatched item, if anything was watched 10+ times."""
    ids = plays["item_id"].dropna()
    ids = ids[ids.astype(str).str.len() > 0]
    if ids.empty:
        return None
    counts = ids.value_counts()
    top_id = str(counts.index[0])
    n = int(counts.iloc[0])
    if n < 10:
        return None
    row = plays[plays["item_id"] == counts.index[0]].iloc[0]
    platform = str(row.get("source_platform") or "")
    data_source = str(row.get("data_source") or "")

    url = None
    if platform == "youtube":
        url = f"https://www.youtube.com/watch?v={top_id}"
    elif platform == "tiktok" and top_id.isdigit():
        url = f"https://www.tiktok.com/@_/video/{top_id}"
    # Instagram item ids are internal media pks — no public URL can be built.

    seed = _load_donated_seed_row(platform, data_source, top_id) or {}
    return {
        "item_id": top_id,
        "count": n,
        "platform": platform,
        "url": url,
        "desc": seed.get("desc"),
        "author_name": seed.get("author_name"),
    }


def _search_terms(searches: pd.DataFrame) -> dict | None:
    terms = searches["extra_data"].dropna().astype(str).str.strip().str.lower()
    terms = terms[terms.str.len() > 0]
    if terms.empty:
        return None
    top = terms.value_counts().head(10)
    return {
        "n_searches": int(len(terms)),
        "top_terms": [{"term": str(t), "count": int(c)} for t, c in top.items()],
    }


def _favourite_emoji(comments: pd.DataFrame) -> dict | None:
    text = " ".join(comments["extra_data"].dropna().astype(str).tolist())
    found = _EMOJI_RE.findall(text)
    if not found:
        return None
    counts = pd.Series(found).value_counts()
    return {"top": str(counts.index[0]), "count": int(counts.iloc[0])}


def _stat_strip(df, plays, likes, comments, posts, durations, sessions,
                has_durations, corpus) -> dict:
    total_watch_s = float(durations.sum()) if has_durations else None
    watch_pct = None
    if total_watch_s is not None and corpus is not None and "total_watch_time_s" in corpus.columns:
        watch_pct = _percentile(corpus["total_watch_time_s"], total_watch_s)

    # Date range and active days follow the VIEWING events, matching the
    # charts — a TikTok like-list can reach years further back than the
    # donated watch history, and "you spent X watching between A and B"
    # must not stretch over that gap.
    ts = plays["local_timestamp"].dropna()
    if ts.empty:
        ts = df["local_timestamp"].dropna()
    return {
        "n_videos": int(len(plays)),
        "n_likes": int(len(likes)),
        "n_comments": int(len(comments)),
        "n_posts": int(len(posts)),
        "under_3s": int((durations < 3).sum()) if has_durations else None,
        "over_60s": int((durations >= 60).sum()) if has_durations else None,
        "total_watch_time_s": total_watch_s,
        "watch_time_percentile": watch_pct,
        "n_sessions": sessions["n_sessions"] if sessions else None,
        "longest_session_s": sessions["longest_s"] if sessions else None,
        "active_days": int(ts.dt.date.nunique()) if not ts.empty else 0,
        "first_date": ts.min().date().isoformat() if not ts.empty else None,
        "last_date": ts.max().date().isoformat() if not ts.empty else None,
    }
