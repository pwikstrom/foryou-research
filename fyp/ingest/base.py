#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import functools
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

import fyp.data_io as data_io
from fyp import activity_contract as _activity_contract
from fyp import activity_versioning as _activity_versioning
from fyp import scrape_contract as _scrape_contract
from fyp import scrape_versioning as _scrape_versioning
from fyp import structure_sentinel as _structure_sentinel
from fyp.donations import generate_collection_metadata
from fyp.logging_setup import get_logger
from fyp.polars_ops import fast_vertical_concat
from fyp.recode_variables import infer_timezone_offset
from fyp.types import convert_dtypes_to_pyarrow
from fyp.utils import ACTIVITY_TYPE_MAP

logger = get_logger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf




def _collections_label() -> str:
    """Lazy accessor for the config-derived collections label."""
    from fyp.organize_datasets import COLLECTIONS_LABEL

    return COLLECTIONS_LABEL




WEEKDAY_MAPPER = { 1:"monday", 2:"tuesday",3:"wednesday",4:"thursday",5:"friday",6:"saturday",7:"sunday"}

# Maps a collection's standard donated-metadata scratch columns to the canonical
# scrape base fields (config/scrape_contract.toml) written by save_enrichment_seed.
_SEED_TO_CANONICAL = {
    "seed_desc": "desc",
    "seed_author_id": "author_id",
    "seed_author_name": "author_name",
    "seed_create_time": "create_time",
}

# Scratch column carrying the per-file donor timezone from the ingestion manifest
# (an IANA name like "Asia/Kolkata" or a fixed "+05:30" offset). Stamped in
# load_raw, consumed by the timezone resolvers, dropped by process()'s filter.
_MANIFEST_TZ_COLUMN = "manifest_tz"

_FIXED_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")


def parse_donor_timezone(tz_str: str | None):
    """Parse a manifest timezone string to a ``tzinfo``, or ``None`` if unusable.

    Accepts an IANA zone name (``"Australia/Brisbane"``, ``"Asia/Kolkata"`` —
    preferred, since it carries DST history) or a fixed UTC offset
    (``"+05:30"``, ``"-8"``, ``"+1000"``).

    Args:
        tz_str: The manifest timezone value.

    Returns:
        A ``ZoneInfo`` or fixed-offset ``timezone``, or ``None`` when the value
        is empty or not a recognisable timezone.
    """
    if not tz_str or not isinstance(tz_str, str):
        return None
    tz_str = tz_str.strip()
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    match = _FIXED_OFFSET_RE.match(tz_str)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    return None


def _zone_offset_hours(utc_timestamps: pd.Series, tz) -> pd.Series:
    """Return the per-row UTC offset in hours of ``tz`` at each UTC instant.

    Vectorised and DST-correct: converts the tz-aware UTC series into ``tz`` and
    measures the wall-clock difference. ``NaT`` rows stay ``NaN``.
    """
    converted = utc_timestamps.dt.tz_convert(tz)
    return (converted.dt.tz_localize(None) - utc_timestamps.dt.tz_localize(None)) / pd.Timedelta(hours=1)


def _first_manifest_tz(df: pd.DataFrame):
    """Return the parsed manifest timezone for a per-file frame, or ``None``.

    A frame handled by ``process_single`` holds one raw file's rows, so the
    manifest timezone is constant; the first non-null value is taken.
    """
    if _MANIFEST_TZ_COLUMN not in df.columns:
        return None
    values = df[_MANIFEST_TZ_COLUMN].dropna()
    if len(values) == 0:
        return None
    return parse_donor_timezone(str(values.iloc[0]))


# The activity schema is owned by config/activity_contract.toml (the REQUIRED_COLUMNS /
# additional_columns analogue). Loaded once at import; falls back to a literal schema
# only if the contract cannot be read, so ingestion never hard-fails on a contract error.
try:
    _ACTIVITY_CONTRACT = _activity_contract.load_contract()
    _ACTIVITY_REQUIRED_COLUMNS = _activity_contract.required_columns(_ACTIVITY_CONTRACT)
    _ACTIVITY_REQUIRED_CORE = _activity_contract.required_core_fields(_ACTIVITY_CONTRACT)
except Exception:
    _ACTIVITY_CONTRACT = None
    _ACTIVITY_REQUIRED_COLUMNS = {
        "collection_id": "string[pyarrow]",
        "raw_file": "string[pyarrow]",
        "source_platform": "string[pyarrow]",
        "data_source": "string[pyarrow]",
        "activity_type": "string[pyarrow]",
        "utc_timestamp": "timestamp[ns][pyarrow]",
        "tz_offset": "int64[pyarrow]",
        "item_id": "string[pyarrow]",
        "ts_added_to_dataset": "timestamp[ns][pyarrow]",
        "extra_data": "string[pyarrow]",
    }
    _ACTIVITY_REQUIRED_CORE = [
        "activity_type", "utc_timestamp", "collection_id", "data_source", "tz_offset",
    ]




def _day_segment_from_hour(hour: int) -> str:
    if not (0 <= hour <= 23):
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if hour <= 5:
        return "night"
    if hour <= 11:
        return "morning"
    if hour <= 17:
        return "afternoon"
    return "evening"




COLLECTION_TAGS_FILENAME = "collections_tags.json"
STUDIES_FILENAME = "studies.json"


# Per-file ingestion ledger. Records the outcome of every raw_file ever scanned
# so that the next ingest run can skip files that have a "do not re-include"
# outcome without rescanning, loading, processing, and re-deduping them.
INGESTION_LEDGER_FILENAME = "ingestion_ledger.json"

# Legacy flat list of "discarded" filenames. Read once on first load to seed
# the ledger, then ignored. Not deleted from disk. It is a bare list of names
# with no counts, timestamps, provenance or reason, so its entries get their
# own outcome rather than being reported as something the ledger never recorded.
LEGACY_DISCARDED_FILENAME = "discarded_collection_files.json"

# Marks a ledger entry seeded from LEGACY_DISCARDED_FILENAME. Ledgers written
# before ``skipped_legacy`` existed stamped those entries ``discarded_at_load``
# with a fabricated 0-row count; this note is what identifies them for the
# in-place upgrade in _load_ledger.
LEGACY_MIGRATION_NOTE = "migrated from legacy discarded_collection_files.json"

# Outcomes whose files must NOT be reloaded on the next ingest. Stored on the
# ledger entry. Membership in this set is the single source of truth for the
# "skip next run" filter used by load_raw.
LEDGER_SKIP_OUTCOMES: set[str] = {
    "fully_deduped",
    "discarded_at_load",
    "manually_excluded",
    "quarantined_structure",
    "skipped_legacy",
}




def apply_cid_remap_to_metadata(
    cid_remap: dict[str, str],
    storage_location: str = "recoded",
    save: bool = True,
    verbose: bool = False,
) -> dict:
    """Propagate a ``{old_collection_id: new_collection_id}`` remap to the
    JSON artifacts that key on ``collection_id`` outside the main parquet:
    ``collections_tags.json`` and ``studies.json``.

    Tag-merging policy: when both old and new ids have an entry in
    ``collections_tags.json``, ``annotation_tags`` are unioned;
    ``display_collection_id`` and ``hidden`` prefer the *new* entry's value,
    falling back to the *old* entry's value if the new one is missing or
    empty. Single-entry cases just rename the key.

    Studies: every study's ``SELECTED_COLLECTIONS`` list has each old id
    replaced with the new id, then deduped while preserving order.

    Args:
        cid_remap: ``{old: new}`` mapping. Empty dict is a no-op.
        storage_location: Where to read/write the JSON files (defaults to
            ``"recoded"``, matching the rest of the ingest pipeline).
        save: If True, persist the updates. If False, computes the changes
            but does not write — useful for dry-runs.
        verbose: Print summary of changes.

    Returns:
        A summary dict with keys:
          - ``tag_keys_renamed``: list of (old, new) pairs renamed in tags.
          - ``tag_keys_merged``: list of (old, new) pairs whose tags were
            merged into an existing new entry.
          - ``studies_updated``: list of study names whose
            ``SELECTED_COLLECTIONS`` changed.
          - ``unmapped_old_keys``: subset of cid_remap keys that didn't appear
            in tags (informational, not an error).
    """
    summary = {
        "tag_keys_renamed": [],
        "tag_keys_merged": [],
        "studies_updated": [],
        "unmapped_old_keys": [],
    }

    if not cid_remap:
        return summary

    # --- collections_tags.json ---
    tags = {}
    if data_io.exists(storage_location=storage_location, filename=COLLECTION_TAGS_FILENAME):
        tags = data_io.load_json(storage_location=storage_location, filename=COLLECTION_TAGS_FILENAME) or {}

    tags_changed = False
    for old_cid, new_cid in cid_remap.items():
        if old_cid not in tags:
            summary["unmapped_old_keys"].append(old_cid)
            continue
        old_entry = tags.pop(old_cid)
        if new_cid in tags:
            new_entry = tags[new_cid]
            old_atags = list(old_entry.get("annotation_tags") or [])
            new_atags = list(new_entry.get("annotation_tags") or [])
            seen = set()
            merged_atags = []
            for t in new_atags + old_atags:
                if t not in seen:
                    seen.add(t)
                    merged_atags.append(t)
            new_entry["annotation_tags"] = merged_atags
            new_display = new_entry.get("display_collection_id") or ""
            old_display = old_entry.get("display_collection_id") or ""
            if not new_display.strip() and old_display.strip():
                new_entry["display_collection_id"] = old_display
            if "hidden" not in new_entry and "hidden" in old_entry:
                new_entry["hidden"] = old_entry["hidden"]
            tags[new_cid] = new_entry
            summary["tag_keys_merged"].append((old_cid, new_cid))
        else:
            tags[new_cid] = old_entry
            summary["tag_keys_renamed"].append((old_cid, new_cid))
        tags_changed = True

    if tags_changed and save:
        data_io.save_json(data=tags, storage_location=storage_location, filename=COLLECTION_TAGS_FILENAME)

    # --- studies.json ---
    studies = {}
    if data_io.exists(storage_location=storage_location, filename=STUDIES_FILENAME):
        studies = data_io.load_json(storage_location=storage_location, filename=STUDIES_FILENAME) or {}

    studies_changed = False
    for sname, sdata in studies.items():
        sc = sdata.get("SELECTED_COLLECTIONS")
        if not isinstance(sc, list):
            continue
        new_sc = []
        seen = set()
        changed = False
        for cid in sc:
            mapped = cid_remap.get(cid, cid)
            if mapped != cid:
                changed = True
            if mapped not in seen:
                seen.add(mapped)
                new_sc.append(mapped)
        if changed:
            sdata["SELECTED_COLLECTIONS"] = new_sc
            summary["studies_updated"].append(sname)
            studies_changed = True

    if studies_changed and save:
        data_io.save_json(data=studies, storage_location=storage_location, filename=STUDIES_FILENAME)

    if verbose:
        logger.info(
            f"cid_remap propagated: tags renamed={len(summary['tag_keys_renamed'])}, "
            f"merged={len(summary['tag_keys_merged'])}; studies updated="
            f"{len(summary['studies_updated'])}; unmapped old keys="
            f"{len(summary['unmapped_old_keys'])}"
        )

    return summary










def assign_session_ids(df: pd.DataFrame, gap_threshold_s: int | None = None) -> pd.DataFrame:
    """Assign a persistent, globally-unique ``session_id`` to every activity.

    A *session* (a "phone sitting") is a maximal run of one collection's
    activities separated by gaps no larger than ``gap_threshold_s``. Every
    activity row gets the id of the sitting it belongs to, formatted as
    ``"{collection_id}__{n}"`` so ids are unique across collections.

    This is deliberately distinct from the transient, per-raw-file 180s grouping
    used inside ``TikTokDDPCollection.process_single`` for comment item_id
    backfill — that one is dropped immediately and never persisted. This
    session_id is computed on the full per-collection sequence (after migration,
    before any study sampling) and is meant to be used anywhere downstream.

    Args:
        df: Activity dataframe with ``collection_id`` and ``utc_timestamp``.
        gap_threshold_s: Maximum within-sitting gap in seconds. ``None`` reads
            ``[sessions] session_gap_s`` from the config (default 900 = 15 min).

    Returns:
        The same dataframe with a ``session_id`` column added; original row
        order is preserved.
    """
    if gap_threshold_s is None:
        gap_threshold_s = int(_cf().get("sessions", {}).get("session_gap_s", 900))
    if df.empty:
        df["session_id"] = pd.Series(dtype="string[pyarrow]")
        return df

    order = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort").index
    ordered = df.loc[order]
    gap = ordered.groupby("collection_id")["utc_timestamp"].diff().dt.total_seconds()
    session_break = gap.isna() | (gap > gap_threshold_s)
    session_num = session_break.groupby(ordered["collection_id"]).cumsum().astype("int64")
    session_id = ordered["collection_id"].astype(str) + "__" + session_num.astype(str)

    df["session_id"] = session_id.reindex(df.index)
    df["session_id"] = df["session_id"].convert_dtypes(dtype_backend="pyarrow")
    return df




def _engagement_token(atype: str, edata) -> str:
    """Build one folded ``extra_data`` token: ``"<atype>"`` or ``"<atype>:context"``."""
    if edata is not pd.NA and pd.notna(edata):
        edata_clean = re.sub(r"[\s,]+", " ", str(edata)).strip()
        if edata_clean:
            return f"{atype}:{edata_clean}"
    return str(atype)




def derive_play_duration(df: pd.DataFrame, cap_seconds: int = 600) -> pd.DataFrame:
    """Derive per-play dwell time from forward time-deltas between activities.

    ``play_duration`` is assigned only to ``play`` activities: the time elapsed
    until the *next* recorded event serves as a proxy for how long the user
    spent on the item. When a play is directly followed by other activities on
    the same ``item_id`` (e.g. a fave or comment on the same video), those
    deltas represent time spent on the same item and are attributed to the
    first play in the run; the non-lead activity types are folded into the lead
    play's ``extra_data``. Non-play activities always get NA, as does the last
    activity of the frame (no forward delta) and anything above ``cap_seconds``.

    Engagement activities (fave/comment/share/follow/save) that are *not*
    chronologically adjacent to a play of the same item still get linked: their
    token is folded into the nearest-in-time play row with the same ``item_id``
    anywhere in the frame. This matters on platforms whose exports log a view
    only once per item (e.g. Instagram's ``videos_watched``), so a later like
    of that item can be days away from its logged play. Only ``extra_data`` is
    affected — ``play_duration`` stays a strictly adjacency-based measure.

    Args:
        df: A single-donor activity frame in chronological order with
            ``utc_timestamp``, ``activity_type`` and ``item_id`` columns
            (every platform's ``process_single`` frame qualifies).
        cap_seconds: Durations above this are considered idle time → NA.

    Returns:
        The same frame with a ``play_duration`` [int64[pyarrow]] column added.
    """
    df = df.reset_index(drop=True)
    if "extra_data" not in df.columns:
        df["extra_data"] = pd.NA
    elif isinstance(df["extra_data"].dtype, pd.ArrowDtype) and df["extra_data"].isna().all():
        # An all-NA pyarrow column may carry the null type, which rejects the
        # string tokens the folds below write into it.
        df["extra_data"] = df["extra_data"].astype("string[pyarrow]")

    if df.empty:
        df["play_duration"] = pd.Series([], dtype="int64[pyarrow]")
        return df

    # 1. Forward delta on the full frame: for each row, the time until the *next*
    # event. This is the correct attribution of dwell time to an activity.
    delta = df["utc_timestamp"].diff().dt.total_seconds()
    forward_delta = delta.shift(-1)

    # Default assignment: play activities get forward_delta, everything else gets NA.
    df["play_duration"] = forward_delta.where(df["activity_type"] == "play")

    # 2. Detect consecutive same-item_id runs of length > 1. A row is a non-first member
    # of a run when its item_id equals the previous row's item_id (and item_id is not null).
    # Such runs are vanishingly rare (~1/10,000 activities are non-play), so we iterate.
    is_continuation = df["item_id"].notna() & (df["item_id"] == df["item_id"].shift(1))

    # Non-lead rows whose token was folded into an adjacent lead play. Rows in
    # here are excluded from the same-item fallback fold below.
    folded_rows: set[int] = set()

    if is_continuation.any():
        # Walk each continuation backward to find the full run, then aggregate.
        continuation_idxs = df.index[is_continuation].tolist()
        visited: set[int] = set()
        for idx in continuation_idxs:
            if idx in visited:
                continue
            # Find the start of this run by walking back
            run_item = df.at[idx, "item_id"]
            run_start = idx
            while run_start - 1 in df.index and pd.notna(df.at[run_start - 1, "item_id"]) and df.at[run_start - 1, "item_id"] == run_item:
                run_start -= 1
            # Find the end of the run by walking forward
            run_end = idx
            while run_end + 1 in df.index and pd.notna(df.at[run_end + 1, "item_id"]) and df.at[run_end + 1, "item_id"] == run_item:
                run_end += 1
            run_slice = list(range(run_start, run_end + 1))
            visited.update(run_slice)

            # Find the first play activity in the run
            play_rows = [i for i in run_slice if df.at[i, "activity_type"] is not pd.NA and df.at[i, "activity_type"] == "play"]
            if not play_rows:
                df.loc[run_slice, "play_duration"] = pd.NA
                continue

            # Sum forward_delta across all rows in the run using the full-df precomputed
            # series, so the last row's contribution (gap to the row after the run) is
            # correctly included — slicing before shifting would lose it.
            first_play = play_rows[0]
            total_delta = forward_delta.loc[run_slice].sum()
            df.loc[run_slice, "play_duration"] = pd.NA
            df.at[first_play, "play_duration"] = total_delta

            # Record the activity types of the non-lead rows in the run on the lead play's
            # extra_data column, as a comma-separated string (e.g. "fave" or "fave,comment").
            other_parts = []
            for i in run_slice:
                if i == first_play:
                    continue
                atype = df.at[i, "activity_type"]
                if atype is pd.NA:
                    continue
                other_parts.append(_engagement_token(atype, df.at[i, "extra_data"]))
                folded_rows.add(i)
            if other_parts:
                df.at[first_play, "extra_data"] = ",".join(other_parts)

    # 3. Same-item fallback fold: engagement rows that did not fold via
    # adjacency but whose item was played somewhere in the frame get their
    # token appended to the nearest-in-time play of that item.
    is_engagement = df["activity_type"].isin(list(ACTIVITY_TYPE_MAP.keys()))
    pending = df.index[is_engagement & df["item_id"].notna() & ~df.index.isin(list(folded_rows))]
    if len(pending) > 0:
        is_play = df["activity_type"] == "play"
        plays = df.loc[is_play & df["item_id"].notna(), "item_id"]
        play_rows_by_item = {k: list(v) for k, v in plays.groupby(plays).groups.items()}
        for i in pending:
            candidates = play_rows_by_item.get(df.at[i, "item_id"], [])
            if not candidates:
                continue
            ts = df.at[i, "utc_timestamp"]
            target = min(candidates, key=lambda p: abs(df.at[p, "utc_timestamp"] - ts))
            token = _engagement_token(df.at[i, "activity_type"], df.at[i, "extra_data"])
            existing = df.at[target, "extra_data"]
            if existing is not pd.NA and pd.notna(existing):
                df.at[target, "extra_data"] = f"{existing},{token}"
            else:
                df.at[target, "extra_data"] = token

    # 4. Cap play_duration at cap_seconds and cast to the project dtype.
    df["play_duration"] = df["play_duration"].map(
        lambda x: x if pd.notna(x) and x <= cap_seconds else pd.NA
    ).astype("int64[pyarrow]")

    return df





class ForYouBaseCollection(ABC):

    platform_url_template: str | None = None
    # Class attributes so registries (e.g. the viewer's platform URL map, the
    # raw-upload location list) can read platform facts without instantiating;
    # __init__ mirrors them per instance.
    source_platform: str | None = None
    raw_path: str | None = None
    ingestion_mode: str = "upload"
    _registry: list[type] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__name__ != "ForYouCollection":
            ForYouBaseCollection._registry.append(cls)
            cls._register_class_raw_location()





    @classmethod
    def _register_class_raw_location(cls) -> None:
        """Register this class's raw-upload storage location by convention.

        Resolves ``activity_data/{source_platform}/{raw_path}`` and registers it
        through :func:`fyp.data_io.register_location`, so adding a platform needs
        no static ``fyp_config`` edit. Runs at class definition (import time) so
        the location exists in every process before any request touches it —
        upload routes must not depend on a collection having been instantiated
        first. Locations already present in config (the built-in ddp/aio/
        zeeschuimer ones) are left untouched; failures are printed loudly but
        never break the import.
        """
        raw_path = cls.__dict__.get("raw_path") or getattr(cls, "raw_path", None)
        source_platform = getattr(cls, "source_platform", None)
        if not raw_path or not source_platform:
            return
        try:
            abs_path = os.path.join(_cf()["paths"]["activity_data"], source_platform, raw_path)
            data_io.register_location(raw_path, abs_path)
        except Exception as exc:
            logger.warning(f"WARNING: could not register raw location '{raw_path}' for {cls.__name__}: {exc}")

    # The canonical required columns come from config/activity_contract.toml.
    REQUIRED_COLUMNS = _ACTIVITY_REQUIRED_COLUMNS

    # Standard donated-metadata scratch columns a subclass may populate in
    # load_single_raw. They are dropped from the activity rows by process()'s
    # column filter; save_enrichment_seed persists them separately as a scrape
    # enrichment seed. Keys of _SEED_TO_CANONICAL.
    SEED_SCRATCH_COLUMNS = list(_SEED_TO_CANONICAL.keys())




    def __init__(self, collection_id: str = None, verbose: bool = False):
        self.collection_id = collection_id
        self.verbose = verbose
        self.data = pd.DataFrame()
        self.state: Literal["empty", "raw", "processed"] = "empty"
        self.additional_columns = {}
        self.raw_path = getattr(type(self), "raw_path", None)
        self.processed_storage_location = "recoded"
        self.min_required_rows_per_raw_file = 10
        self.discarded_raw_files = []
        self.discarded_collections_filename = "discarded_collection_files.json"
        self.source_platform = getattr(type(self), "source_platform", None)
        self.data_source = None
        self.collections = []
        # Donor timezone for the file currently being loaded (from the manifest);
        # set per-file in load_raw so load_single_raw can honour it.
        self._current_file_tz = None
        # Structure-drift detector for this run (fyp.structure_sentinel.
        # StructureSentinel), injected by run_ingest_refresh. When None, no
        # structure checks run and load_raw behaves exactly as before.
        self.sentinel = None
        # Files withheld from this run because their structure deviated from
        # the learned baseline: {filename: verdict dict}.
        self.quarantined_this_run: dict[str, dict] = {}
        # Files whose load_single_raw raised this run: {filename: error message}.
        # They stay pending (retried next refresh); tracked so the refresh
        # summary can tell the user why a file was not ingested.
        self.load_failed_this_run: dict[str, str] = {}
        # Per-file intake stats for this run:
        # {filename: {"raw_rows": int, "dropped": {reason: count}}}.
        # raw_rows is recorded for every file load_single_raw returned —
        # including files later discarded for too few rows, so the ledger can
        # report the true count instead of 0. Drop reasons are accumulated by
        # process()/_standardize() via _record_file_drops().
        self.file_stats_this_run: dict[str, dict] = {}


    def clear(self):
        self.data = pd.DataFrame()
        self.state = "empty"



    def load_processed(
        self, 
        processed_fn: str, 
        drop_similar_activity_sequences: bool = True):
        
        if self.verbose:
            logger.info(f"Loading processed data from {processed_fn}. Data source: {self.source_platform}_{self.data_source}")

        new_processed_data = data_io.load_parquet(
            storage_location=self.processed_storage_location,
            filename=processed_fn,
            verbose=False#self.verbose
        )

        if len(self.data) > 0:
            if self.state != "processed":
                if self.verbose:
                    logger.warning(f"Warning: There is data in this collection but the state is '{self.state}'. Existing data must be processed. Cannot load new data.")
                return
            if self.verbose:
                logger.info(f"Adding {len( new_processed_data):,} new processed activities to existing {len(self.data):,} activities.")
            # Vertical concat via polars: parallel, avoids pandas' O(n) copy
            # on accumulating appends. Matters at events-scale (tens of millions
            # of rows). See fyp/polars_ops.py.
            self.data = fast_vertical_concat([self.data, new_processed_data])
        else:
            if self.verbose:
                logger.info(f"Loading {len(new_processed_data):,} processed activities.")
            self.data = new_processed_data.copy()

        self.state = "processed"

        if drop_similar_activity_sequences:
            if self.verbose:
                logger.info("Dropping activities from files with overlapping/similar activity sequences")
            cid_remap = self.identify_similar_file_content(drop_them=True)
            if cid_remap:
                apply_cid_remap_to_metadata(cid_remap, verbose=self.verbose)

        if self.verbose:
            logger.info(f"There are now {len(self.data):,} activities in the collection.")




    def save_processed(self):

        if self.state != "processed":
            logger.warning(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first.")
            return
        
        fn = f"{self.source_platform}_{self.data_source}_processed_activities.parquet"

        if len(self.data) > 0:
            local_time_cols = [c for c in self.data.columns if c.startswith("local_")]
            if len(local_time_cols) > 0:
                logger.info("This dataset seem to have 'local time features' added. I am dropping these columns when saving.")
                self.data.drop(local_time_cols, axis=1, inplace=True)
                
            _ = data_io.save_parquet(
                df=self.data,
                storage_location=self.processed_storage_location,
                filename=fn)

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))


        data_io.save_json(
            data=self.discarded_raw_files,
            storage_location=self.processed_storage_location,
            filename=self.discarded_collections_filename,
            verbose=False#self.verbose
        )





    @staticmethod
    def _finalize_activity_frame(df: pd.DataFrame) -> pd.DataFrame:
        """Apply the shared post-conversion tail every platform's rows go through.

        Drops rows whose ``utc_timestamp`` could not be parsed, sets the donor's
        ``tz_offset`` (from an explicit manifest timezone when one was supplied,
        else inferred from the UTC series), and returns the frame in stable
        chronological order. Called at the end of each platform's
        ``process_single`` so the tail cannot drift between platforms.
        """
        df = df[df["utc_timestamp"].notna()].copy()
        if len(df) > 0:
            tz = _first_manifest_tz(df)
            if tz is not None:
                df["tz_offset"] = _zone_offset_hours(df["utc_timestamp"], tz)
            else:
                df["tz_offset"] = infer_timezone_offset(df["utc_timestamp"])
        df.sort_values("utc_timestamp", inplace=True, kind="mergesort")
        df.reset_index(drop=True, inplace=True)
        return df





    def save_enrichment_seed(self) -> None:
        """Persist donated item metadata as an enrichment seed (platform-agnostic).

        Reads the standard ``seed_*`` scratch columns from this collection's raw
        data (present before ``process()`` drops them) and merges them into a
        per-platform parquet whose columns match the canonical scrape base schema
        (``config/scrape_contract.toml``), keyed by ``(source_platform, item_id)``
        with ``scrape_status="donated"`` and a per-row ``scrape_contract_version``
        stamp. Existing seed rows from earlier ingest runs are preserved — new
        rows only win a key collision when they carry a caption the stored row
        lacks. A later scrape/consolidation can merge the seed as a
        lowest-precedence fallback for items that cannot be scraped. This is a
        no-op for collections that populate no seed columns (e.g. TikTok);
        platform classes only supply values.
        """
        if self.state != "raw" or len(self.data) == 0:
            return
        present = [c for c in self.SEED_SCRATCH_COLUMNS if c in self.data.columns]
        if not present or "item_id" not in self.data.columns:
            return

        contract = _scrape_contract.load_contract()
        base_cols = _scrape_contract.base_field_names(contract)
        base_dtypes = _scrape_contract.field_dtypes(contract)

        src = self.data
        seed = pd.DataFrame({"item_id": src["item_id"].values})
        for col in base_cols:
            seed[col] = pd.NA

        seed["source_platform"] = self.source_platform
        seed["scrape_status"] = "donated"
        seed["scrape_ts"] = datetime.now(timezone.utc).replace(tzinfo=None)

        for scratch, canonical in _SEED_TO_CANONICAL.items():
            if scratch in src.columns and canonical in seed.columns:
                seed[canonical] = src[scratch].values

        seed = seed[seed["item_id"].notna()].copy()
        if len(seed) == 0:
            return

        seed = _scrape_versioning.stamp_version(seed)

        for col, dtype in base_dtypes.items():
            if col in seed.columns:
                seed[col] = seed[col].astype(dtype)
        seed = convert_dtypes_to_pyarrow(seed, verbose=False)

        # Merge with the stored seed so earlier donations survive incremental
        # ingests. New rows are placed first: with the caption-presence sort
        # below, a fresh captioned row beats a stored one, and a stored
        # captioned row beats a fresh caption-less duplicate.
        fn = f"{self.source_platform}_{self.data_source}_enrichment_seed.parquet"
        if data_io.exists(storage_location=self.processed_storage_location, filename=fn):
            existing = data_io.load_parquet(
                storage_location=self.processed_storage_location, filename=fn
            )
            if existing is not None and len(existing) > 0:
                seed = fast_vertical_concat([seed, existing])

        # One row per item, preferring rows that carry a caption/title. Sorting
        # on the null mask (stable) keeps insertion order within each group
        # instead of ordering arbitrarily by caption text.
        seed = seed.sort_values(by="desc", key=lambda s: s.isna(), kind="mergesort")
        seed = seed.drop_duplicates(subset=["source_platform", "item_id"], keep="first")

        data_io.save_parquet(
            df=seed,
            storage_location=self.processed_storage_location,
            filename=fn,
        )
        if self.verbose:
            logger.info(f"Saved {len(seed):,} donated enrichment-seed rows to {fn}.")






    def load_raw(self, skip_these_raw_files: list[str] = []):
        if self.verbose:
            logger.info(f"Loading raw data for collection '{self.source_platform}_{self.data_source}'.")

        if self.state != "empty":
            logger.info(f"This collection '{self.source_platform}_{self.data_source}' is not empty. The current data will be replaced.")

        if self.raw_path is None:
            raise ValueError("No raw path has been set for this collection.")

        self.load_failed_this_run = {}
        self.file_stats_this_run = {}

        MANIFEST_FILENAME = "ingestion_manifest.json"

        all_the_files = [fn for fn in data_io.listdir(self.raw_path)
                         if not fn.startswith(".") and fn != MANIFEST_FILENAME]

        all_the_files = [fn for fn in all_the_files if fn not in skip_these_raw_files+self.discarded_raw_files]

        # Load ingestion manifest (written at upload time with collection_id / tags per file)
        manifest: dict = {}
        if data_io.exists(storage_location=self.raw_path, filename=MANIFEST_FILENAME):
            manifest = data_io.load_json(
                storage_location=self.raw_path,
                filename=MANIFEST_FILENAME,
                verbose=False
            ) or {}




        many_dfs = []
        # load all files in the directory
        for fn in all_the_files:
            # The donor timezone from the manifest (if any) is exposed on the
            # instance so a platform's load_single_raw can use it when producing
            # UTC (YouTube needs it to interpret local wall-clock times), and is
            # also stamped as a column below for the offset resolver.
            file_meta = manifest.get(fn, {}) or {}
            self._current_file_tz = file_meta.get("tz") or None

            # A parse error is not the same as a legitimately-small donation:
            # errored files are skipped THIS run but stay pending (not added to
            # discarded_raw_files, so no ledger skip-outcome is stamped) and are
            # retried on the next refresh — e.g. after a parser fix for a new
            # export-format variant.
            try:
                one_df = self.load_single_raw(fn)
            except Exception as exc:
                logger.error(
                    f"ERROR: failed to load raw file '{fn}' for "
                    f"'{self.source_platform}_{self.data_source}': {exc}. "
                    f"Leaving it pending for retry."
                )
                self.load_failed_this_run[fn] = str(exc)
                continue

            self.file_stats_this_run[fn] = {"raw_rows": int(len(one_df)), "dropped": {}}

            if len(one_df) > 0:
                mtime = data_io.getmtime(storage_location=self.raw_path, filename = fn)
                one_df["ts_added_to_dataset"] = pd.to_datetime(mtime, unit="s")
                one_df["raw_file"] = fn

                # Apply manifest-based collection_id if available. Must be written
                # directly to `collection_id` (not a scratch column) because
                # `process()` filters columns down to REQUIRED_COLUMNS before
                # `_standardize()` runs — any scratch column would be dropped.
                if file_meta.get("collection_id"):
                    one_df["collection_id"] = file_meta["collection_id"]

                # Per-file donor timezone for the offset resolver (scratch column,
                # dropped by process()'s filter before _standardize()).
                one_df[_MANIFEST_TZ_COLUMN] = self._current_file_tz if self._current_file_tz else pd.NA

                if self.verbose: logger.info(f"Loaded file: {fn}. Number of rows: {len(one_df):,}")

            # I will keep data from this file if there are at least 10 activities. (just an arbitrary number)
            if len(one_df) >= self.min_required_rows_per_raw_file:
                # Structure-drift check (Phase A): a quarantined verdict
                # withholds the file's rows from this run; the file is reviewed
                # in the Data Management UI. A sentinel failure must never
                # block ingestion, so it degrades to ingest-with-warning.
                verdict = None
                if self.sentinel is not None:
                    try:
                        verdict = self.sentinel.check_raw(self, fn, one_df)
                    except Exception as exc:
                        logger.warning(f"WARNING: structure check failed for '{fn}': {exc}. Ingesting anyway.")
                if verdict is not None and verdict["status"] == "quarantined":
                    self.quarantined_this_run[fn] = verdict
                    if self.verbose: logger.info(f"Quarantining file: {fn} (structure drift).")
                else:
                    many_dfs.append(one_df)
            else:
                if self.verbose: logger.info(f"Discarding file: {fn}. Too few rows: {len(one_df):,}")
                self.discarded_raw_files.append(fn)


        if len(many_dfs) > 1:
            # Vertical concat via polars — fast multi-frame stack of per-file
            # raw DataFrames. See fyp/polars_ops.py.
            self.data = fast_vertical_concat(many_dfs)
            self.state = "raw"

        elif len(many_dfs) == 1:
            self.data = many_dfs[0]
            self.state = "raw"
        else:
            self.data = pd.DataFrame()
            self.state = "empty"



    @abstractmethod
    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Subclasses must implement this logic."""




    def _record_file_drops(self, counts, reason: str) -> None:
        """Accumulate per-file dropped-row counts under a reason key.

        Args:
            counts: A ``{raw_file: n_dropped}`` mapping (or a pandas Series
                indexed by raw_file). Zero/negative entries are ignored.
            reason: The drop-reason key (e.g. ``"not_parseable"``,
                ``"missing_required"``).
        """
        for fn, n in dict(counts).items():
            n = int(n)
            if n <= 0:
                continue
            entry = self.file_stats_this_run.setdefault(str(fn), {"raw_rows": None, "dropped": {}})
            dropped = entry.setdefault("dropped", {})
            dropped[reason] = dropped.get(reason, 0) + n



    @classmethod
    def accepted_upload_suffixes(cls) -> list[str]:
        """File suffixes this platform's ``load_single_raw`` can actually parse.

        The upload endpoint rejects mismatched files with a clear message
        instead of letting them fail cryptically (and retry forever) at
        ingest time. An empty list means no restriction.

        Returns:
            Lower-case suffixes including the dot (e.g. ``[".json"]``), or an
            empty list when any file type is accepted.
        """
        return []



    @classmethod
    def zip_member_suffixes(cls) -> list[str]:
        """Zip-member suffixes the ingester needs from an uploaded donation zip.

        Matched with the same path-suffix semantics as
        :func:`fyp.utils.read_zip_members`. The web upload UI uses this list to
        slim large donation zips client-side before upload, keeping only the
        listed members. Empty for platforms whose uploads are consumed whole.

        Returns:
            Member-name suffixes, or an empty list when client-side slimming
            does not apply to this platform.
        """
        return []




    def fingerprint_raw(self, filename: str) -> dict:
        """Extract a structure fingerprint from one raw upload (drift detection).

        Generic default dispatching on the file extension: ``.json`` files are
        fingerprinted as a single JSON document (TikTok DDP/AIO), ``.ndjson``
        as sampled NDJSON records (Zeeschuimer), and ``.zip`` via the class's
        :meth:`zip_member_suffixes` members (Instagram, YouTube — HTML members
        get structural-marker fingerprints). Unknown extensions return a
        minimal fingerprint that disables the structure layer for the file.

        Args:
            filename: The raw file's name within ``self.raw_path``.

        Returns:
            A fingerprint dict (see :mod:`fyp.structure_sentinel`).
        """
        lowered = filename.lower()
        if lowered.endswith(".json"):
            payload = data_io.load_json(storage_location=self.raw_path, filename=filename)
            return _structure_sentinel.fingerprint_json_payload(payload)
        if lowered.endswith(".ndjson"):
            records = data_io.read_ndjson_file(storage_location=self.raw_path, filename=filename)
            return _structure_sentinel.fingerprint_ndjson_lines(records or [])
        if lowered.endswith(".zip") and type(self).zip_member_suffixes():
            local_path = data_io.local_copy(storage_location=self.raw_path, filename=filename)
            if not local_path:
                raise ValueError(f"could not fetch '{filename}' from '{self.raw_path}'")
            try:
                return _structure_sentinel.fingerprint_zip(local_path, type(self).zip_member_suffixes())
            finally:
                data_io.release_local_copy(local_path)
        # Extensionless uploads (e.g. AIO donations fetched from S3 are bare
        # UUIDs holding DDP JSON) — try JSON before giving up on the layer.
        try:
            payload = data_io.load_json(storage_location=self.raw_path, filename=filename)
            if payload is not None:
                return _structure_sentinel.fingerprint_json_payload(payload)
        except Exception:
            pass
        return {"kind": "unknown", "member_paths": [], "key_paths": [], "stats": {}}




    def process(self):

        if self.state == "empty":
            if self.verbose:
                logger.info(f"There is no data from platform/data_source '{self.source_platform}_{self.data_source}'. Nothing for me to do.")
            return


        if self.state != "raw":
            if self.verbose:
                logger.warning(f"Platform/data_source '{self.source_platform}_{self.data_source}' is not in raw state. Cannot process. Please load raw data first.")
            return

        if self.verbose:
            logger.info(f"Processing {len(self.data):,} raw rows for platform/data_source '{self.source_platform}_{self.data_source}'...")

        # Per-file row counts before/after the platform's process_single pass:
        # the difference is rows the platform could not turn into activities
        # (unreadable timestamp, missing item reference, ...). Counted here —
        # generically, per raw_file — because each platform drops these rows
        # inside its own process_single.
        _before = {str(k): int(v) for k, v in self.data.groupby("raw_file").size().items()}

        self.data = self.data.groupby("raw_file", group_keys=False)[self.data.columns].apply(self.process_single)

        _after: dict[str, int] = {}
        if "raw_file" in self.data.columns and len(self.data) > 0:
            _after = {str(k): int(v) for k, v in self.data.groupby("raw_file").size().items()}
        self._record_file_drops(
            {fn: n - _after.get(fn, 0) for fn, n in _before.items()},
            "not_parseable",
        )

        # Platform-specific extras (the additional_columns analogue) come from the
        # activity contract, keyed on this collection's platform. Merged over any
        # subclass-set columns so a contract-load failure still degrades gracefully.
        if _ACTIVITY_CONTRACT is not None:
            self.additional_columns = {
                **self.additional_columns,
                **_activity_contract.platform_columns(_ACTIVITY_CONTRACT, self.source_platform),
            }

        good_columns = list((set(self.additional_columns.keys()) | set(list(self.REQUIRED_COLUMNS.keys()))) & set(self.data.columns))
        
        self.data = self.data[good_columns].copy()
        self._standardize()
        self.state = "processed"

        if self.verbose:
            logger.info(f"Raw data from platform/data_source '{self.source_platform}_{self.data_source}' is now processed. Number of rows: {len(self.data):,}")        


    @abstractmethod
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Subclasses must implement this logic."""






    def identify_similar_file_content(
        self,
        overlap_threshold: float = 0.2,
        drop_them: bool = True,
    ) -> dict[str, str]:
        """Cluster raw_files by timestamp-sequence similarity and dedupe within
        clusters.

        The platform receives multiple donations from the same source over
        time, often with overlapping time windows. Two raw_files are inferred
        to come from the same source if their per-second timestamp sets overlap
        by more than ``overlap_threshold`` (relative to the smaller set) — the
        assumption is that an activity-timestamp sequence is statistically
        unique to a source. Such raw_files are merged into a single
        ``collection_id`` cluster, the per-row union is taken, and overlapping
        rows are deduped within the cluster.

        Behaviour:
          1. Build per-raw_file timestamp sets (utc_timestamp truncated to
             whole seconds).
          2. For every pair of raw_files with overlap > threshold, union them
             via union-find. Connected components = inferred sources.
          3. For each multi-file cluster, choose a canonical ``collection_id``
             from the raw_file with the latest ``ts_added_to_dataset.max()``.
             Restamp ``collection_id`` on every row in that cluster.
          4. Sort the dataset by ``ts_added_to_dataset`` ascending and
             ``drop_duplicates(subset=[collection_id, item_id, utc_timestamp,
             activity_type, tz_offset], keep='last')`` so the newest donation's
             row wins on overlapping events.

        Notes:
          - Single-file clusters are untouched: their ``collection_id`` stays,
            and dedupe collapses any internal repeats.
          - The dedupe key includes ``collection_id``, so unrelated sources
            that happen to coincide on (item_id, timestamp, activity_type,
            tz_offset) are never collapsed.
          - This function does NOT add to ``self.discarded_raw_files``.
            Re-donations are merged, not blacklisted, so re-running ingest is
            idempotent.

        Args:
            overlap_threshold: Per-second timestamp-set overlap (relative to
                the smaller set) above which two raw_files are clustered.
            drop_them: Retained for caller compatibility. The function always
                mutates ``self.data`` in place.

        Returns:
            ``{old_collection_id: new_collection_id}`` for every raw_file
            whose ``collection_id`` was restamped by clustering. Callers can
            use this to propagate the change to downstream artifacts that key
            on ``collection_id`` (``collections_tags.json``, ``studies.json``).
            Empty dict if no clusters were formed.
        """
        del drop_them  # always treated as True; kept for caller compatibility

        if self.state != "processed":
            logger.warning(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot identify similar file content. Please process data first.")
            return {}

        if len(self.data) == 0:
            return {}

        # 1. Per-raw_file timestamp sets at second resolution.
        seconds = (self.data["utc_timestamp"].astype("int64[pyarrow]") // 1_000_000_000).astype("int64")
        ts_sets: dict[str, set[int]] = (
            self.data.assign(_sec=seconds)
            .groupby("raw_file", observed=True)["_sec"]
            .apply(lambda s: set(s.tolist()))
            .to_dict()
        )
        raw_files = list(ts_sets.keys())

        # 2. Union-find over pairs with overlap > threshold.
        parent = {f: f for f in raw_files}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i, a in enumerate(raw_files):
            ts_a = ts_sets[a]
            if len(ts_a) == 0:
                continue
            for b in raw_files[i + 1:]:
                ts_b = ts_sets[b]
                denom = min(len(ts_a), len(ts_b))
                if denom == 0:
                    continue
                if len(ts_a & ts_b) / denom > overlap_threshold:
                    union(a, b)

        clusters: dict[str, list[str]] = {}
        for f in raw_files:
            clusters.setdefault(find(f), []).append(f)

        multi_clusters = [files for files in clusters.values() if len(files) > 1]

        # 3. For each multi-file cluster, pick canonical collection_id from
        # the raw_file with the latest ts_added_to_dataset.
        canonical_map: dict[str, str] = {}  # raw_file -> canonical collection_id
        cid_remap: dict[str, str] = {}      # old_collection_id -> new_collection_id
        if multi_clusters:
            latest_per_file = (
                self.data.groupby("raw_file", observed=True)["ts_added_to_dataset"].max()
            )
            collection_id_per_file = (
                self.data.groupby("raw_file", observed=True)["collection_id"].first()
            )
            for files in multi_clusters:
                latest_file = max(files, key=lambda f: latest_per_file[f])
                canonical_collection_id = collection_id_per_file[latest_file]
                # The latest raw_file in the cluster may itself have a NA
                # collection_id (legacy rows that predate the manifest-based
                # ingest). Fall back to any non-NA cid in the cluster.
                if pd.isna(canonical_collection_id):
                    for f in files:
                        cid = collection_id_per_file[f]
                        if pd.notna(cid):
                            canonical_collection_id = cid
                            break
                if pd.isna(canonical_collection_id):
                    # No usable collection_id anywhere in the cluster — skip
                    # restamping so we don't overwrite real ids with NA.
                    continue
                for f in files:
                    canonical_map[f] = canonical_collection_id
                    old_cid = collection_id_per_file[f]
                    if pd.notna(old_cid) and old_cid != canonical_collection_id:
                        cid_remap[str(old_cid)] = str(canonical_collection_id)

            # Restamp collection_id on rows in multi-file clusters.
            mask = self.data["raw_file"].isin(canonical_map)
            new_ids = self.data.loc[mask, "raw_file"].map(canonical_map)
            self.data.loc[mask, "collection_id"] = new_ids.astype(self.data["collection_id"].dtype)

            if self.verbose:
                merged_files = sum(len(c) for c in multi_clusters)
                logger.info(
                    f"Clustered {merged_files} raw_files into {len(multi_clusters)} "
                    f"merged collection(s)."
                )

        # 4. Dedupe within cluster (collection_id is now canonical for clusters).
        # Sort ascending by ts_added_to_dataset so keep='last' picks the newest row.
        rows_before = len(self.data)
        self.data = (
            self.data.sort_values("ts_added_to_dataset", kind="mergesort")
            .drop_duplicates(
                subset=["collection_id", "item_id", "utc_timestamp", "activity_type", "tz_offset"],
                keep="last",
            )
            .copy()
        )
        if self.verbose and rows_before > len(self.data):
            logger.info(f"Deduped {rows_before - len(self.data):,} overlapping rows within clusters.")

        return cid_remap





    def add_local_time_features(self) -> None:
        df = self.data

        # A refresh with nothing ingested (fresh install, all files pending)
        # leaves an empty frame with no columns — nothing to derive.
        if len(df) == 0 or 'tz_offset' not in df.columns:
            return

        offset_timedelta = pd.to_timedelta(df['tz_offset'], unit='h')
        df["local_timestamp"] = df["utc_timestamp"] + offset_timedelta


        ts = df["local_timestamp"]
        
        iso = ts.dt.isocalendar()  # DataFrame: year, week, day
        iso["day"] = iso["day"].map(WEEKDAY_MAPPER)
        iso["year_week"] = iso["year"].astype(str) + "-" + iso["week"].astype(str)

        # these dtype fixes feel stupid, but I cannot seem to get around it any other way
        df["local_weekday"] = iso["day"].to_list()
        df["local_weekday"] = df["local_weekday"].convert_dtypes(dtype_backend="pyarrow")
        df["local_week"] = iso["year_week"].to_list()
        df["local_week"] = df["local_week"].convert_dtypes(dtype_backend="pyarrow")

        local_hour = ts.dt.hour.astype("uint8[pyarrow]")

        df["local_day_segment"] = local_hour.map(_day_segment_from_hour).convert_dtypes(dtype_backend="pyarrow")

        df["local_date"] = ts.dt.date.astype("date32[pyarrow]")




    def add_session_ids(self, gap_threshold_s: int | None = None) -> None:
        """Assign a persistent sitting-level ``session_id`` to every activity.

        Thin wrapper around :func:`assign_session_ids` (``None`` gap reads
        ``[sessions] session_gap_s`` from the config). Call this *after*
        sub-collections are migrated, so the full per-collection sequence is in
        ``self.data`` (a sitting may span multiple raw files). Persisted by
        ``save_processed`` alongside the local-time features.
        """
        self.data = assign_session_ids(self.data, gap_threshold_s=gap_threshold_s)






    def _standardize(self):
        """
        Ensures the dataframe has all required columns and correct dtypes.
        """
        df = self.data.copy()

        df['source_platform'] = self.source_platform
        df['data_source'] = self.data_source

        if "collection_id" not in df.columns:
            if self.collection_id is not None:
                df['collection_id'] = self.collection_id
            elif "raw_file" in df.columns:
                df["collection_id"] = df["raw_file"]
            else:
                df["collection_id"] = pd.NA


        # 1. Ensure all required columns exist
        for col, dtype in self.REQUIRED_COLUMNS.items():
            if col not in df.columns:
                if self.verbose:
                    logger.warning(f"Warning: Missing column {col}, filling with NA.")
                df[col] = pd.NA

        # 2. Enforce dtypes
        # We try to use the dictionary to cast, but pandas/pyarrow can be finicky with dictionary casting
        # so we iterate.
        for col, dtype in self.REQUIRED_COLUMNS.items():
            if col in df.columns:
                try:
                    df[col] = df[col].astype(dtype)
                except Exception as e:
                    if self.verbose:
                        logger.warning(f"Error casting {col} to {dtype}: {e}. Trying fyp.types.convert_dtypes_to_pyarrow.")
                    # Fallback to the robust converter
                    # converting specific column to pyarrow backed using the helper
                    # Note: convert_dtypes_to_pyarrow works on DF, but we can try to apply it to the column or the whole DF later
        
        # Use the robust converter for the whole DF for good measure to ensure everything is pyarrow backed where possible
        # and specifically fixing complex types if any
        try:
             df = convert_dtypes_to_pyarrow(df, verbose=False)
        except Exception as e:
             if self.verbose: logger.warning(f"Warning: convert_dtypes_to_pyarrow failed: {e}")

        # Hard-drop integrity gate: a row missing any required-core STRUCTURAL field
        # is malformed and dropped. Column presence is already ensured above; this
        # checks VALUES. item_id (null for login/search/follow) and extra_data (the
        # folded-engagement payload, null for ~92% of rows) are intentionally NOT
        # required and stay nullable.
        core = [c for c in _ACTIVITY_REQUIRED_CORE if c in df.columns]
        if core:
            invalid = df[core].isna().any(axis=1)
            n_bad = int(invalid.sum())
            if n_bad:
                logger.info(
                    f"Activity ingest: hard-dropping {n_bad:,} row(s) with a null "
                    f"required-core field ({', '.join(core)})."
                )
                if "raw_file" in df.columns:
                    self._record_file_drops(
                        df.loc[invalid].groupby("raw_file").size(), "missing_required"
                    )
                df = df[~invalid].copy()

        # Stamp per-row activity-contract provenance. This is a derived field, so it
        # is not part of REQUIRED_COLUMNS / the good_columns filter, but it persists
        # like the rest (save_processed only drops the transient local_* columns).
        df = _activity_versioning.stamp_version(df)

        # -----------------------------------------------------
        # It's important to sort by time
        df.sort_values("utc_timestamp", inplace=True, kind='mergesort')
        df.reset_index(drop=True, inplace=True)

        self.data = df.copy()












class ForYouCollection(ForYouBaseCollection):
    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "all"
        self.data_source = "foryou"
        self.collections = []
        self.ledger_filename = INGESTION_LEDGER_FILENAME
        self.ledger: dict = {"schema_version": 1, "files": {}}
        self._load_ledger()




    def _load_ledger(self) -> None:
        """Load the per-file ingestion ledger from disk. If absent, fall back
        to seeding from the legacy flat ``discarded_collection_files.json``
        (every entry becomes ``skipped_legacy``). The ``discarded_raw_files``
        attribute is rebuilt as a derived view over the ledger so the rest of
        the pipeline (which still reads the flat list) continues to work.

        Ledgers seeded before ``skipped_legacy`` existed are upgraded in place:
        those entries claimed ``discarded_at_load`` ("too few rows") with a
        0-row count, none of which the legacy file actually recorded. The
        rewrite is in memory and reaches disk on the next ``save_ledger``.
        """
        ledger = None
        if data_io.exists(
            storage_location=self.processed_storage_location,
            filename=self.ledger_filename,
        ):
            ledger = data_io.load_json(
                storage_location=self.processed_storage_location,
                filename=self.ledger_filename,
                verbose=False,
            )

        if not isinstance(ledger, dict) or "files" not in ledger:
            legacy_list: list = []
            if data_io.exists(
                storage_location=self.processed_storage_location,
                filename=LEGACY_DISCARDED_FILENAME,
            ):
                loaded = data_io.load_json(
                    storage_location=self.processed_storage_location,
                    filename=LEGACY_DISCARDED_FILENAME,
                    verbose=False,
                )
                if isinstance(loaded, list):
                    legacy_list = loaded
            files = {
                fn: {
                    "outcome": "skipped_legacy",
                    # None, not 0: the legacy file recorded no counts at all,
                    # and a zero here reads as "we read the file and found
                    # nothing in it".
                    "raw_rows": None,
                    "kept_rows": None,
                    "collection_id": None,
                    "merged_with_siblings": [],
                    "platform": None,
                    "source": None,
                    "ts_first_seen": None,
                    "ts_last_seen": None,
                    "notes": LEGACY_MIGRATION_NOTE,
                }
                for fn in legacy_list
            }
            ledger = {"schema_version": 1, "files": files}

        self.ledger = ledger
        self._upgrade_legacy_ledger_entries()
        self._refresh_discarded_from_ledger()




    def _upgrade_legacy_ledger_entries(self) -> None:
        """Re-stamp entries an older migration mislabelled ``discarded_at_load``.

        They came from the legacy flat list, which carried no reason and no
        counts — so "Skipped — too few rows / 0 rows read" was a claim the data
        never supported. Matched on the migration note, which is the only thing
        that distinguishes them from a real too-few-rows discard. Skip
        behaviour is unchanged: both outcomes are in LEDGER_SKIP_OUTCOMES.
        """
        for entry in (self.ledger.get("files") or {}).values():
            if not isinstance(entry, dict):
                continue
            if entry.get("notes") != LEGACY_MIGRATION_NOTE:
                continue
            if entry.get("outcome") != "discarded_at_load":
                continue
            entry["outcome"] = "skipped_legacy"
            entry["raw_rows"] = None
            entry["kept_rows"] = None




    def _refresh_discarded_from_ledger(self) -> None:
        """Rebuild ``self.discarded_raw_files`` from the ledger, preserving any
        filenames already in the list (e.g. too-few-rows entries a sub-collection
        appended during this run that haven't been written into the ledger
        yet). Mutates in place so sub-collections that share this list via
        ``register_collection_class`` see the update.
        """
        files = self.ledger.get("files", {})
        ledger_skips = [
            fn for fn, meta in files.items()
            if (meta or {}).get("outcome") in LEDGER_SKIP_OUTCOMES
        ]
        merged = list(dict.fromkeys(ledger_skips + list(self.discarded_raw_files)))
        self.discarded_raw_files[:] = merged




    def update_ledger(self, per_file_summary: list[dict]) -> None:
        """Update the in-memory ledger with the outcomes from a freshly
        completed ingestion. Preserves ``ts_first_seen`` for previously known
        files and stamps ``ts_last_seen`` on every entry touched.

        Args:
            per_file_summary: list of dicts from
                ``run_ingest_refresh._build_per_file_summary``.
        """
        now = datetime.now(timezone.utc).isoformat()
        files = self.ledger.setdefault("files", {})
        for entry in per_file_summary:
            fn = entry.get("filename")
            if not fn:
                continue
            existing = files.get(fn) or {}
            files[fn] = {
                "outcome": entry.get("outcome"),
                "raw_rows": int(entry.get("raw_rows") or 0),
                "processed_rows": int(entry.get("processed_rows") or 0),
                "kept_rows": int(entry.get("final_rows") or 0),
                "deduped_rows": int(entry.get("deduped_rows") or 0),
                "dropped": entry.get("dropped") or {},
                "collection_id": entry.get("canonical_collection_id"),
                "merged_with_siblings": entry.get("merged_with_siblings") or [],
                "platform": entry.get("platform"),
                "source": entry.get("source"),
                "ts_first_seen": existing.get("ts_first_seen") or now,
                "ts_last_seen": now,
                "notes": entry.get("notes") or existing.get("notes"),
            }
        self._refresh_discarded_from_ledger()




    def save_ledger(self) -> None:
        """Persist the ledger to its JSON file."""
        data_io.save_json(
            data=self.ledger,
            storage_location=self.processed_storage_location,
            filename=self.ledger_filename,
            verbose=False,
        )




    def remove_from_ledger(self, filename: str) -> bool:
        """Drop a single filename from the ledger so it will be rescanned on
        the next ingestion run. Returns True if the entry existed and was
        removed, False otherwise. Caller is responsible for calling
        ``save_ledger`` to persist the change.
        """
        files = self.ledger.setdefault("files", {})
        if filename in files:
            del files[filename]
            self._refresh_discarded_from_ledger()
            return True
        return False




    def set_ledger_outcome(self, filename: str, outcome: str, note: str | None = None) -> bool:
        """Overwrite a single file's ledger outcome (e.g. a structure-review
        reject rewrites ``quarantined_structure`` → ``manually_excluded``).
        Returns True if the entry existed, False otherwise. Caller is
        responsible for calling ``save_ledger`` to persist the change.
        """
        files = self.ledger.setdefault("files", {})
        entry = files.get(filename)
        if entry is None:
            return False
        entry["outcome"] = outcome
        entry["ts_last_seen"] = datetime.now(timezone.utc).isoformat()
        if note:
            entry["notes"] = note
        self._refresh_discarded_from_ledger()
        return True


    def load_single_raw(self, fn: str) -> pd.DataFrame:
        raise ValueError("Don't use this class to load raw data")
    
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        raise ValueError("Don't use this class to process raw data")



    def register_collection_class(self, collection_class: type[ForYouBaseCollection]):
        if not issubclass(collection_class, ForYouBaseCollection):
            raise ValueError(f"{collection_class} is not a subclass of ForYouBaseCollection")
        if collection_class in [type(x) for x in self.collections]:
            if self.verbose:
                logger.info(f"{collection_class} is already registered.")
            return
        self.collections.append(collection_class(verbose=self.verbose))
        self.collections[-1].discarded_raw_files = self.discarded_raw_files
        if self.verbose:
            logger.info(f"Registered collection class: {collection_class}")



    def load_processed(self):

        fn = f"{_collections_label()}_recoded.parquet"
        if not data_io.exists(storage_location=self.processed_storage_location, filename=fn):
            if self.verbose:
                logger.info("No processed collection file found.")
            return

        self.data = data_io.load_parquet(
            storage_location=self.processed_storage_location,
            filename=fn,
            verbose=False
        )

        stale_cols = [c for c in self.data.columns if c.startswith("__")]
        if stale_cols:
            self.data.drop(columns=stale_cols, inplace=True)

        if len(self.data) > 0:
            # Self-healing backfills for pre-column history; both are no-ops on
            # healed data and are persisted by the next save_processed().
            self._backfill_source_platform()
            self._backfill_play_duration()
            self.state = "processed"
            if self.verbose:
                logger.info(f"Loaded {len(self.data):,} processed activities from {fn}.")
        else:
            if self.verbose:
                logger.info("Processed collection file was empty.")




    def _backfill_source_platform(self) -> None:
        """Fill missing ``source_platform`` with the default platform (self-heal).

        Rows ingested before the column existed carry NA, which silently breaks
        the composite ``(source_platform, item_id)`` activity↔enrichment join and
        drops the rows from the per-platform enrichment-status filters. All
        pre-column history is TikTok by definition (same argument as the
        scrape-side backfill in ``fyp.scrape.consolidate_and_save_scrape_data``).
        """
        default_platform = _scrape_contract.default_platform(_scrape_contract.load_contract()) or "tiktok"
        if "source_platform" not in self.data.columns:
            self.data["source_platform"] = pd.NA
        n_missing = int(self.data["source_platform"].isna().sum())
        if n_missing:
            logger.info(f"Backfilling source_platform='{default_platform}' on {n_missing:,} pre-column activity row(s).")
        self.data["source_platform"] = (
            self.data["source_platform"].fillna(default_platform).astype("string[pyarrow]")
        )




    def _backfill_play_duration(self) -> None:
        """Recompute ``play_duration`` for platforms ingested before it went base (self-heal).

        IG/YT rows ingested while ``play_duration`` was TikTok-only carry all-NA
        values, yet the forward-delta derivation needs nothing beyond the
        persisted ``utc_timestamp`` / ``activity_type`` / ``item_id`` per
        ``raw_file`` (dedup drops whole files, never rows, so per-file sequences
        are intact). Recomputes only platform groups whose play rows are ALL NA —
        already-derived platforms (TikTok) are untouched and repeat runs are
        no-ops.
        """
        if "raw_file" not in self.data.columns or "activity_type" not in self.data.columns:
            return
        if "play_duration" not in self.data.columns:
            self.data["play_duration"] = pd.Series(pd.NA, index=self.data.index, dtype="int64[pyarrow]")

        for platform, grp in self.data.groupby("source_platform", dropna=False):
            grp_plays = grp[grp["activity_type"] == "play"]
            if len(grp_plays) == 0 or grp_plays["play_duration"].notna().any():
                continue
            logger.info(f"Backfilling play_duration for {len(grp):,} '{platform}' activity row(s).")
            for _, file_grp in grp.groupby("raw_file", dropna=False):
                ordered = file_grp.sort_values("utc_timestamp", kind="mergesort")
                recomputed = derive_play_duration(ordered)
                self.data.loc[ordered.index, "play_duration"] = (
                    recomputed["play_duration"].set_axis(ordered.index)
                )
                self.data.loc[ordered.index, "extra_data"] = (
                    recomputed["extra_data"].set_axis(ordered.index)
                )
        self.data["play_duration"] = self.data["play_duration"].astype("int64[pyarrow]")




    def process(self):
        if len(self.collections) == 0:
            logger.warning("This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            logger.info("Processing the registered sub collections...")

        for collection in self.collections:
            collection.process()

        if self.verbose:
            logger.info("Done processing the registered sub collections.")




    def load_raw(self):
        if len(self.collections) == 0:
            if self.verbose:
                logger.warning("This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            logger.info("Loading new raw data for the registered sub collections...")

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        if len(self.data) > 0:
            skip_these_raw_files = self.data['raw_file'].unique().tolist() + self.discarded_raw_files
            if self.verbose:
                logger.info(f"Skipping {len(skip_these_raw_files):,} raw files that are already discarded or already in the collection.")
        else:
            skip_these_raw_files = self.discarded_raw_files

        for collection in self.collections:
            collection.load_raw(skip_these_raw_files=skip_these_raw_files)
        
        if self.verbose:
            logger.info(f"Done loading raw {sum([len(collection.data) for collection in self.collections]):,} rows for the registered sub collections.")




    def migrate_sub_collections(self):

        processed_collections = [collection for collection in self.collections if collection.state == "processed"]

        if len(processed_collections) == 0:
            if self.verbose:
                logger.info("No processed sub collections to migrate. Nothing for me to do.")
            return

        if self.verbose:
            logger.info(f"Migrating {len(processed_collections):,} processed sub collections to the top...")
            logger.info(f"There are {len(self.data):,} rows in the top collection already.")

        # Vertical concat via polars — stacks all processed sub-collections
        # into the top-level collection in a single parallel pass.
        # See fyp/polars_ops.py.
        if len(self.data) > 0:
            self.data = fast_vertical_concat(
                [self.data] + [collection.data for collection in processed_collections]
            )
        else:
            self.data = fast_vertical_concat(
                [collection.data for collection in processed_collections]
            )

        self.state = "processed"
        cid_remap = self.identify_similar_file_content(drop_them=True)
        if cid_remap:
            apply_cid_remap_to_metadata(cid_remap, verbose=self.verbose)

        for collection in processed_collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))


        for collection in processed_collections:
            logger.info(f"Migrated {len(collection.data):,} activities from '{collection.source_platform}_{collection.data_source}'.")
            collection.data = pd.DataFrame()
            collection.state = "empty"

        if self.verbose:
            logger.info(f"Done migrating the sub collections. There are now {len(self.data):,} activities in the top collection. Sub collections are empty.")







    def save_processed(self):

        if self.state != "processed":
            logger.warning(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first.")
            return


        # metadata (needs local_* columns present in self.data).
        # Load the existing metadata ourselves so we can (a) regenerate stats
        # for *every* collection in self.data — generate_collection_metadata's
        # "load_from_disk=True" path short-circuits when no collection_ids are
        # new, which leaves counts stale whenever events are appended to an
        # existing collection — and (b) restore columns set outside the
        # generator (e.g. ('other','accepted') flipped during acceptance).
        # A refresh that ingested nothing (fresh install, all files pending)
        # has no rows to save and no stats to compute — skip the parquet
        # writes (never clobber existing files with empties) but still fall
        # through to the ledger + manifest bookkeeping below.
        if len(self.data) > 0:
            old_metadata = None
            if data_io.exists(
                storage_location=self.processed_storage_location,
                filename=f"{_collections_label()}_metadata.parquet"):
                old_metadata = data_io.load_parquet(
                    storage_location=self.processed_storage_location,
                    filename=f"{_collections_label()}_metadata.parquet",
                    verbose=False)

            self.stats = generate_collection_metadata(
                self.data,
                update_col=None,
                sort_by=None,
                verbose=True,
                save_to_disk_ok=False,
                load_from_disk=False)

            if old_metadata is not None and not old_metadata.empty:
                preserved_cols = [c for c in old_metadata.columns if c not in self.stats.columns]
                if preserved_cols:
                    self.stats = pd.merge(
                        self.stats, old_metadata[preserved_cols],
                        left_index=True, right_index=True, how='left')

            self.stats[('other','accepted')] = True
            self.stats[('participants', 'date')] = self.stats[('other', 'ts_added_to_dataset')]

            data_io.save_parquet(
                df=self.stats,
                storage_location=self.processed_storage_location,
                filename=f"{_collections_label()}_metadata.parquet",
                asyncronous=False)


            # activity data
            data_io.save_parquet(
                df=self.data,
                storage_location=self.processed_storage_location,
                filename=f"{_collections_label()}_recoded.parquet",
                asyncronous=False)


        # Make sure every too-few-rows filename appended by a sub-collection
        # during this run is reflected in the ledger as ``discarded_at_load``.
        # update_ledger (called by the worker before save_processed with the
        # full per-file summary) is the normal path; this loop is a safety net
        # for any flat-list entries the summary missed.
        ledger_files = self.ledger.setdefault("files", {})
        now = datetime.now(timezone.utc).isoformat()
        for collection in self.collections:
            for fn in collection.discarded_raw_files:
                if fn in ledger_files:
                    continue
                ledger_files[fn] = {
                    "outcome": "discarded_at_load",
                    "raw_rows": 0,
                    "kept_rows": 0,
                    "collection_id": None,
                    "merged_with_siblings": [],
                    "platform": collection.source_platform,
                    "source": collection.data_source,
                    "ts_first_seen": now,
                    "ts_last_seen": now,
                    "notes": None,
                }
        self._refresh_discarded_from_ledger()
        self.save_ledger()

        # Clean up ingestion manifests: remove entries for files now in the dataset
        processed_files = set(self.discarded_raw_files)
        if 'raw_file' in self.data.columns:
            processed_files |= set(self.data['raw_file'].unique().tolist())
        MANIFEST_FILENAME = "ingestion_manifest.json"
        for collection in self.collections:
            if collection.raw_path is None:
                continue
            if not data_io.exists(storage_location=collection.raw_path, filename=MANIFEST_FILENAME):
                continue
            manifest = data_io.load_json(
                storage_location=collection.raw_path,
                filename=MANIFEST_FILENAME,
                verbose=False
            ) or {}
            trimmed = {fn: meta for fn, meta in manifest.items() if fn not in processed_files}
            if len(trimmed) < len(manifest):
                data_io.save_json(
                    data=trimmed,
                    storage_location=collection.raw_path,
                    filename=MANIFEST_FILENAME,
                    verbose=False
                )
                if self.verbose:
                    logger.info(f"Cleaned {len(manifest) - len(trimmed)} processed entries from {collection.raw_path}/{MANIFEST_FILENAME}")




    def refresh_collection(self):
        self.load_processed()
        self.load_raw()
        self.process()
        self.migrate_sub_collections()
        self.add_local_time_features()
        self.save_processed()








@functools.lru_cache(maxsize=1)
def _config_timezone_offset() -> float:
    """Return the project timezone's current UTC offset in hours (fallback only).

    Used when a YouTube Takeout timestamp carries an unrecognised timezone
    abbreviation; the row is still converted to UTC using this offset, and the
    per-donor ``tz_offset`` is re-inferred downstream from the UTC series.
    Cached — the offset cannot meaningfully change within one process run.
    """
    tzname = _cf()["misc"].get("TIME_ZONE", "UTC")
    try:
        now = datetime.now(ZoneInfo(tzname))
    except ZoneInfoNotFoundError:
        return 0.0
    off = now.utcoffset()
    return off.total_seconds() / 3600 if off is not None else 0.0





def get_main_collection(verbose: bool = False) -> ForYouCollection:
    """Factory function to initialize and configure the main collection.

    Collection classes are auto-registered via __init_subclass__ on
    ForYouBaseCollection. Adding a new subclass is sufficient to include
    it in the ingestion pipeline — no changes here are needed.
    """
    main_collection = ForYouCollection(verbose=verbose)
    for cls in ForYouBaseCollection._registry:
        main_collection.register_collection_class(cls)
    return main_collection





def registered_raw_locations() -> tuple[str, ...]:
    """Return every registered collection class's raw-upload storage location.

    Read from class attributes (no instantiation), in registry order. This is
    the single source for code that must probe all upload locations (e.g.
    collection deletion), so a new platform class is covered automatically.
    """
    locations: list[str] = []
    for cls in ForYouBaseCollection._registry:
        raw_path = getattr(cls, "raw_path", None)
        if isinstance(raw_path, str) and raw_path and raw_path not in locations:
            locations.append(raw_path)
    return tuple(locations)





def platform_url_templates() -> dict[str, str]:
    """Return each registered platform's "open on platform" URL template.

    Built from class attributes (no instantiation): every registered collection
    class declaring both a ``source_platform`` and a ``platform_url_template``
    contributes one entry, so adding a platform needs no edit here or in the web
    layer. Templates take a single ``{item_id}`` placeholder.

    Returns:
        Dict source_platform → URL template.
    """
    templates: dict[str, str] = {}
    for cls in ForYouBaseCollection._registry:
        platform = getattr(cls, "source_platform", None)
        template = getattr(cls, "platform_url_template", None)
        if platform and template:
            templates.setdefault(platform, template)
    return templates
