#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import functools
import html
import json
import os
import re
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

import fyp.data_io as data_io
from fyp import activity_contract as _activity_contract
from fyp import activity_versioning as _activity_versioning
from fyp import scrape_contract as _scrape_contract
from fyp import scrape_versioning as _scrape_versioning
from fyp import structure_sentinel as _structure_sentinel
from fyp.donations import generate_collection_metadata
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL
from fyp.polars_ops import fast_vertical_concat
from fyp.recode_variables import infer_timezone_offset
from fyp.types import convert_dtypes_to_pyarrow
from fyp.utils import clean_url, read_zip_members, repair_mojibake

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

# Legacy flat list of "discarded" filenames (too-few-rows only). Read once on
# first load to seed the ledger, then ignored. Not deleted from disk.
LEGACY_DISCARDED_FILENAME = "discarded_collection_files.json"

# Outcomes whose files must NOT be reloaded on the next ingest. Stored on the
# ledger entry. Membership in this set is the single source of truth for the
# "skip next run" filter used by load_raw.
LEDGER_SKIP_OUTCOMES: set[str] = {
    "fully_deduped",
    "discarded_at_load",
    "manually_excluded",
    "quarantined_structure",
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
        print(
            f"cid_remap propagated: tags renamed={len(summary['tag_keys_renamed'])}, "
            f"merged={len(summary['tag_keys_merged'])}; studies updated="
            f"{len(summary['studies_updated'])}; unmapped old keys="
            f"{len(summary['unmapped_old_keys'])}"
        )

    return summary










def assign_session_ids(df: pd.DataFrame, gap_threshold_s: int = 900) -> pd.DataFrame:
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
        gap_threshold_s: Maximum within-sitting gap in seconds (default 900 = 15 min).

    Returns:
        The same dataframe with a ``session_id`` column added; original row
        order is preserved.
    """
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
                edata = df.at[i, "extra_data"]
                if edata is not pd.NA:
                    edata_clean = re.sub(r"[\s,]+", " ", str(edata)).strip()
                    other_parts.append(f"{atype}:{edata_clean}")
                else:
                    other_parts.append(atype)
            if other_parts:
                df.at[first_play, "extra_data"] = ",".join(other_parts)

    # 3. Cap play_duration at cap_seconds and cast to the project dtype.
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
            abs_path = os.path.join(fyp_cf["paths"]["activity_data"], source_platform, raw_path)
            data_io.register_location(raw_path, abs_path)
        except Exception as exc:
            print(f"WARNING: could not register raw location '{raw_path}' for {cls.__name__}: {exc}")

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


    def clear(self):
        self.data = pd.DataFrame()
        self.state = "empty"



    def load_processed(
        self, 
        processed_fn: str, 
        drop_similar_activity_sequences: bool = True):
        
        if self.verbose:
            print(f"Loading processed data from {processed_fn}. Data source: {self.source_platform}_{self.data_source}")

        new_processed_data = data_io.load_parquet(
            storage_location=self.processed_storage_location,
            filename=processed_fn,
            verbose=False#self.verbose
        )

        if len(self.data) > 0:
            if self.state != "processed":
                if self.verbose:
                    print(f"Warning: There is data in this collection but the state is '{self.state}'. Existing data must be processed. Cannot load new data.")
                return
            if self.verbose:
                print(f"Adding {len( new_processed_data):,} new processed activities to existing {len(self.data):,} activities.")
            # Vertical concat via polars: parallel, avoids pandas' O(n) copy
            # on accumulating appends. Matters at events-scale (tens of millions
            # of rows). See fyp/polars_ops.py.
            self.data = fast_vertical_concat([self.data, new_processed_data])
        else:
            if self.verbose:
                print(f"Loading {len(new_processed_data):,} processed activities.")
            self.data = new_processed_data.copy()

        self.state = "processed"

        if drop_similar_activity_sequences:
            if self.verbose:
                print("Dropping activities from files with overlapping/similar activity sequences")
            cid_remap = self.identify_similar_file_content(drop_them=True)
            if cid_remap:
                apply_cid_remap_to_metadata(cid_remap, verbose=self.verbose)

        if self.verbose:
            print(f"There are now {len(self.data):,} activities in the collection.")




    def save_processed(self):

        if self.state != "processed":
            print(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first.")
            return
        
        fn = f"{self.source_platform}_{self.data_source}_processed_activities.parquet"

        if len(self.data) > 0:
            local_time_cols = [c for c in self.data.columns if c.startswith("local_")]
            if len(local_time_cols) > 0:
                print("This dataset seem to have 'local time features' added. I am dropping these columns when saving.")
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
            print(f"Saved {len(seed):,} donated enrichment-seed rows to {fn}.")






    def load_raw(self, skip_these_raw_files: list[str] = []):
        if self.verbose:
            print(f"Loading raw data for collection '{self.source_platform}_{self.data_source}'.")

        if self.state != "empty":
            print(f"This collection '{self.source_platform}_{self.data_source}' is not empty. The current data will be replaced.")

        if self.raw_path is None:
            raise ValueError("No raw path has been set for this collection.")



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
                print(
                    f"ERROR: failed to load raw file '{fn}' for "
                    f"'{self.source_platform}_{self.data_source}': {exc}. "
                    f"Leaving it pending for retry."
                )
                continue

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

                if self.verbose: print(f"Loaded file: {fn}. Number of rows: {len(one_df):,}")

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
                        print(f"WARNING: structure check failed for '{fn}': {exc}. Ingesting anyway.")
                if verdict is not None and verdict["status"] == "quarantined":
                    self.quarantined_this_run[fn] = verdict
                    if self.verbose: print(f"Quarantining file: {fn} (structure drift).")
                else:
                    many_dfs.append(one_df)
            else:
                if self.verbose: print(f"Discarding file: {fn}. Too few rows: {len(one_df):,}")
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
                print(f"There is no data from platform/data_source '{self.source_platform}_{self.data_source}'. Nothing for me to do.")
            return


        if self.state != "raw":
            if self.verbose:
                print(f"Platform/data_source '{self.source_platform}_{self.data_source}' is not in raw state. Cannot process. Please load raw data first.")
            return

        if self.verbose:
            print(f"Processing {len(self.data):,} raw rows for platform/data_source '{self.source_platform}_{self.data_source}'...")

        self.data = self.data.groupby("raw_file", group_keys=False)[self.data.columns].apply(self.process_single)

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
            print(f"Raw data from platform/data_source '{self.source_platform}_{self.data_source}' is now processed. Number of rows: {len(self.data):,}")        


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
            print(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot identify similar file content. Please process data first.")
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
                print(
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
            print(f"Deduped {rows_before - len(self.data):,} overlapping rows within clusters.")

        return cid_remap





    def add_local_time_features(self) -> None:
        df = self.data

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




    def add_session_ids(self, gap_threshold_s: int = 900) -> None:
        """Assign a persistent sitting-level ``session_id`` to every activity.

        Thin wrapper around :func:`assign_session_ids`. Call this *after*
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
                    print(f"Warning: Missing column {col}, filling with NA.")
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
                        print(f"Error casting {col} to {dtype}: {e}. Trying fyp.types.convert_dtypes_to_pyarrow.")
                    # Fallback to the robust converter
                    # converting specific column to pyarrow backed using the helper
                    # Note: convert_dtypes_to_pyarrow works on DF, but we can try to apply it to the column or the whole DF later
        
        # Use the robust converter for the whole DF for good measure to ensure everything is pyarrow backed where possible
        # and specifically fixing complex types if any
        try:
             df = convert_dtypes_to_pyarrow(df, verbose=False)
        except Exception as e:
             if self.verbose: print(f"Warning: convert_dtypes_to_pyarrow failed: {e}")

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
                print(
                    f"Activity ingest: hard-dropping {n_bad:,} row(s) with a null "
                    f"required-core field ({', '.join(core)})."
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
        (every entry becomes ``discarded_at_load``). The ``discarded_raw_files``
        attribute is rebuilt as a derived view over the ledger so the rest of
        the pipeline (which still reads the flat list) continues to work.
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
                    "outcome": "discarded_at_load",
                    "raw_rows": 0,
                    "kept_rows": 0,
                    "collection_id": None,
                    "merged_with_siblings": [],
                    "platform": None,
                    "source": None,
                    "ts_first_seen": None,
                    "ts_last_seen": None,
                    "notes": "migrated from legacy discarded_collection_files.json",
                }
                for fn in legacy_list
            }
            ledger = {"schema_version": 1, "files": files}

        self.ledger = ledger
        self._refresh_discarded_from_ledger()




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
                "kept_rows": int(entry.get("final_rows") or 0),
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
                print(f"{collection_class} is already registered.")
            return
        self.collections.append(collection_class(verbose=self.verbose))
        self.collections[-1].discarded_raw_files = self.discarded_raw_files
        if self.verbose:
            print(f"Registered collection class: {collection_class}")



    def load_processed(self):

        fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
        if not data_io.exists(storage_location=self.processed_storage_location, filename=fn):
            if self.verbose:
                print("No processed collection file found.")
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
                print(f"Loaded {len(self.data):,} processed activities from {fn}.")
        else:
            if self.verbose:
                print("Processed collection file was empty.")




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
            print(f"Backfilling source_platform='{default_platform}' on {n_missing:,} pre-column activity row(s).")
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
            print(f"Backfilling play_duration for {len(grp):,} '{platform}' activity row(s).")
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
            print("This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            print("Processing the registered sub collections...")

        for collection in self.collections:
            collection.process()

        if self.verbose:
            print("Done processing the registered sub collections.")




    def load_raw(self):
        if len(self.collections) == 0:
            if self.verbose:
                print("This ForYouCollection does not have any sub collections. You need to register a collection class first.")
            return
        if self.verbose:
            print("Loading new raw data for the registered sub collections...")

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        if len(self.data) > 0:
            skip_these_raw_files = self.data['raw_file'].unique().tolist() + self.discarded_raw_files
            if self.verbose:
                print(f"Skipping {len(skip_these_raw_files):,} raw files that are already discarded or already in the collection.")
        else:
            skip_these_raw_files = self.discarded_raw_files

        for collection in self.collections:
            collection.load_raw(skip_these_raw_files=skip_these_raw_files)
        
        if self.verbose:
            print(f"Done loading raw {sum([len(collection.data) for collection in self.collections]):,} rows for the registered sub collections.")




    def migrate_sub_collections(self):

        processed_collections = [collection for collection in self.collections if collection.state == "processed"]

        if len(processed_collections) == 0:
            if self.verbose:
                print("No processed sub collections to migrate. Nothing for me to do.")
            return

        if self.verbose:
            print(f"Migrating {len(processed_collections):,} processed sub collections to the top...")
            print(f"There are {len(self.data):,} rows in the top collection already.")

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
            print(f"Migrated {len(collection.data):,} activities from '{collection.source_platform}_{collection.data_source}'.")
            collection.data = pd.DataFrame()
            collection.state = "empty"

        if self.verbose:
            print(f"Done migrating the sub collections. There are now {len(self.data):,} activities in the top collection. Sub collections are empty.")







    def save_processed(self):

        if self.state != "processed":
            print(f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first.")
            return


        # metadata (needs local_* columns present in self.data).
        # Load the existing metadata ourselves so we can (a) regenerate stats
        # for *every* collection in self.data — generate_collection_metadata's
        # "load_from_disk=True" path short-circuits when no collection_ids are
        # new, which leaves counts stale whenever events are appended to an
        # existing collection — and (b) restore columns set outside the
        # generator (e.g. ('other','accepted') flipped during acceptance).
        old_metadata = None
        if data_io.exists(
            storage_location=self.processed_storage_location,
            filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            old_metadata = data_io.load_parquet(
                storage_location=self.processed_storage_location,
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
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
            filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
            asyncronous=False)


        # activity data
        data_io.save_parquet(
            df=self.data,
            storage_location=self.processed_storage_location,
            filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
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
        processed_files = set(self.data['raw_file'].unique().tolist()) | set(self.discarded_raw_files)
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
                    print(f"Cleaned {len(manifest) - len(trimmed)} processed entries from {collection.raw_path}/{MANIFEST_FILENAME}")




    def refresh_collection(self):
        self.load_processed()
        self.load_raw()
        self.process()
        self.migrate_sub_collections()
        self.add_local_time_features()
        self.save_processed()








class TikTokDDPCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"
    raw_path = "ddp_raw"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the comment string, the account name that was just followed, etc...
        # play_duration is a base activity-contract column (derive_play_duration), no extras needed here.
        super().__init__(collection_id, verbose)
        self.source_platform = "tiktok"
        self.data_source = "ddp"
        self.raw_path = "ddp_raw" #"/Users/<user>/fyp_local/activity_data/ddp/ddp_raw"
        self.min_required_rows_per_raw_file = 10




    def load_single_raw(self, filename: str) -> pd.DataFrame:

        donation_dict = data_io.load_json(storage_location = self.raw_path, filename = filename)

        #with open(filename, "r") as f:
        #    donation_dict = json.load(f)

        # find list of dicts
        donation_items = []
        
        stack = deque([(None, donation_dict)])
        while stack:
            feature, obj = stack.pop()
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and item:
                        donation_items.append({
                            "activity_type": (feature or '').lower(),
                            "variable_list": [k.lower() for k in item.keys()],
                            "value_list": list(item.values())
                        })
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    stack.append((k, v))

        # initialising the dataframe from the raw data.
        if len(donation_items) > 0:
            df = pd.DataFrame.from_records(donation_items)

        # a data donation package without at least a few play activities is not useful       
        # play activities are referred to as 'videolist' by TikTok 
        n_play_activities = len(df[df['activity_type'] == 'videolist'])
        if n_play_activities <= 10:
            if self.verbose: print(f"Discarding {filename} as it only has {n_play_activities} play activities.")
            return pd.DataFrame()

        return df






    def process_single(self, df: pd.DataFrame):

        df = df.copy()

        # -----------------------------------------------------
        # unpack the variable/value list. The two lists variable & value list contain a label (e.g. 'link')
        # and the value (e.g. 'https://www.tiktok.com/...') at the corresponding indeces. At index 0 is always the date
        # and I'm only unpacking index 1 in addition of date even though there may be additional data in the lists.

        # if 'date' is not the first element in the variable_list, something is wrong with this activity
        # so I keep activities/rows that have at least two elements in the variable_list and the first element is 'date'
        mask_date = df['variable_list'].map(lambda x: isinstance(x, list) and len(x) > 1 and x[0] == 'date')
        df = df[mask_date].copy()

        mask_activity_type = df['activity_type'].map(lambda x:"chat history with" not in x)
        df = df[mask_activity_type].copy()

        # get the date from index zero (I don't need the variable name)
        df['date'] = pd.to_datetime(df['value_list'].str[0], format='%Y-%m-%d %H:%M:%S', errors='coerce')

        # remove rows with invalid dates
        df = df[df['date'].notna()].copy()

        if self.verbose:
            print(f"   [{df['raw_file'].iloc[0]}] Keeping {len(df):,} rows w OK timestamp.")


        # get the variable name and the associated value from index 1 and assign them to primary_label and extra_data
        # primary_label is just a temporary holder in this function
        try:
             df['primary_label'] = df['variable_list'].str[1]
             df['extra_data'] = df['value_list'].str[1]
        except:
             df['primary_label'] = pd.NA
             df['extra_data'] = pd.NA


        # -----------------------------------------------------
        # item_id: 

        # extract item_id and clean it up (from the video_url)
        item_ids_from_url = df["extra_data"].astype("string").str.rsplit("/", n=2).str[-2]
        digits = item_ids_from_url.str.fullmatch(r"\d+")
        item_ids = item_ids_from_url.where(digits)

        # since items are extracted from the video url, the primary label must be 'link' and
        # the activity type must not be null (assuming it is play, fave or something like that)
        mask = (df["primary_label"]=="link") & (df["activity_type"].notna())
        df["item_id"] = item_ids.where(mask)
        
        # nullify extra_data where item_id was extracted to get rid of redundant data - I don't 
        # need the url any longer
        df.loc[df["item_id"].notnull(), "extra_data"] = pd.NA

        # convert item_id to pyarrow string
        df["item_id"] = df["item_id"].astype("string[pyarrow]")


        # -----------------------------------------------------
        # activity_type: 

        # map activity types
        df["activity_type"] = df["activity_type"].map({
            'videolist':'play', 'commentslist':'comment', 'post':'post',
            'searchlist':'search', 'fanslist':'followed_by', 'following':'following',
            'itemfavoritelist':'fave', 'favoritevideolist':'fave'
        })
        reaction_activities = {"comment","fave","share"}
        
        # activity_type is NA for login activities - this fixes that by creating a new activity type
        df.loc[df[df["primary_label"]=="ip"].index,"activity_type"] = "login"
        
        # Convert activity_type to pyarrow string
        df["activity_type"] = df["activity_type"].astype("string[pyarrow]")

        # cleanup - remove play activities that don't have an item_id
        df = df[((df["activity_type"] != "play") | (df["item_id"].notna()))].copy()
        

        # -----------------------------------------------------
        # utc_timestamp and tz_offset:

        # tiktok timestamps are in nanoseconds - convert date to seconds since epoch
        # rename timestamp to utc_timestamp and convert to datetime
        df['timestamp'] = (df['date'].astype("int64") // 1_000_000_000)
        df = df.rename(columns={"timestamp": "utc_timestamp"})
        df["utc_timestamp"] = pd.to_datetime(df["utc_timestamp"], unit='s', utc=True)

        # infer timezone offset note that inferring timezone assumes that this is
        # an 'actual' TikTok user using TikTok like a normal TikTok user does.
        # If this ddp was created in an 'artificial way', the inference will get it wrong 
        df["tz_offset"] = infer_timezone_offset(df["utc_timestamp"])

        # sort by timestamp and reset index
        df.sort_values("utc_timestamp", inplace=True, kind='mergesort')
        df.reset_index(drop=True, inplace=True)


        # -----------------------------------------------------
        # It seems like the data donation packages keep play logs for a certain time back
        # in time, but they keep other engagement stats for longer. It is difficult to handle
        # engagement stats without connection to a play activity, so I remove all activities before 
        # the first play activity. It feels a bit brutal to throw away data, but I'm not sure what else to do.
        #if (df["activity_type"] == "play").any():
        #    first_play_idx = df[df["activity_type"] == "play"].index[0]
        #    df = df.loc[first_play_idx:].copy()

        #print(len(df))


        # ----------------------------------------------------------------------------------------------
        # Associate comments without an item_id to the item_id of the preceding activity within
        # the same short burst. Only comments are backfilled — other activity types retain their
        # original item_id (or null).
        #
        # NOTE: this is a TRANSIENT, per-raw-file grouping used ONLY for comment item_id
        # backfill — hence the deliberately short 180s gap. It is dropped at the end of this
        # block and is NOT the persistent "sitting" session_id assigned to every activity later
        # (see assign_session_ids / add_session_ids: 900s gap, computed on the full
        # per-collection sequence after migration, and persisted for downstream analysis).

        # 1. calculate time between activities (in seconds)
        df['delta'] = df['utc_timestamp'] - df['utc_timestamp'].shift(1)
        df['delta'] = df['delta'].dt.total_seconds()

        # 2. use the time delta to establish bursts of activities very close together, which I
        # assume belong to the same brief engagement (e.g. watching a video and commenting on
        # it). The 180s limit is a reasonable max time to spend on one video and engage with it.
        df['_assoc_break'] = (df['delta'].isna()) | (df['delta'] > 180)
        df['_assoc_session'] = df['_assoc_break'].astype(bool).cumsum()

        # 3. Forward-fill item_id within each burst, then apply only to comment rows that
        # are missing an item_id. All other activity types keep their original value.
        ffilled_item_id = df.groupby('_assoc_session')['item_id'].ffill()
        comment_missing = (df['activity_type'] == 'comment') & df['item_id'].isna()
        df.loc[comment_missing, 'item_id'] = ffilled_item_id[comment_missing]

        df.drop(columns=['_assoc_break', '_assoc_session', 'delta'], inplace=True)


        # -----------------------------------------------------
        # play_duration: forward time-delta to the next recorded activity, attributed
        # to play events (shared, platform-agnostic derivation — see derive_play_duration).

        return derive_play_duration(df)












class TikTokAIOCollection(TikTokDDPCollection):
    """TikTok DDP data fetched from AIO AWS infrastructure (S3/DynamoDB).

    Uses the same DDP JSON format as TikTokDDPCollection but loads data
    from the Australian Internet Observatory's AWS S3 bucket instead of
    user-uploaded files. Also fetches participant metadata from DynamoDB.
    """

    ingestion_mode = "fetch"
    raw_path = "aio_raw"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.data_source = "aio"
        self.raw_path = "aio_raw"


    def load_raw(self, skip_these_raw_files: list[str] = []):
        """Fetch recent donations and participant metadata from AWS, then load files."""
        from fyp.donations import (
            get_donation_metadata_from_aio_aws,
            get_recent_data_donations_from_aio_aws,
        )
        if self.verbose:
            print("Fetching recent AIO donations from AWS...")
        try:
            get_recent_data_donations_from_aio_aws(
                storage_location=self.raw_path
            )
        except Exception as e:
            if self.verbose:
                print(f"AWS data fetch failed: {e}. Processing existing local files.")

        if self.verbose:
            print("Fetching AIO participant metadata from DynamoDB...")
        try:
            get_donation_metadata_from_aio_aws(verbose=self.verbose)
        except Exception as e:
            if self.verbose:
                print(f"AWS metadata fetch failed: {e}.")

        super().load_raw(skip_these_raw_files=skip_these_raw_files)





class TikTokZeeschuimerCollection(ForYouBaseCollection):

    platform_url_template = "https://www.tiktok.com/@/video/{item_id}"
    source_platform = "tiktok"

    raw_path = "zeeschuimer_raw"

    def __init__(self, collection_id: str = None, verbose: bool = False):
        # The extra_data column is used for the timezone name

        super().__init__(collection_id, verbose)
        self.raw_path = "zeeschuimer_raw" #"/Users/<user>/fyp_local/activity_data/zeeschuimer/zeeschuimer_raw"
        self.min_required_rows_per_raw_file = 1
        self.source_platform = "tiktok"
        self.data_source = "zeeschuimer"
        self.accepted_tiktok_urls = [
            'https://www.tiktok.com/foryou',
            'https://www.tiktok.com/',
            'https://www.tiktok.com/en',
        ]





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        #data = []
        #with open(filename, 'r') as file:
        #    for line in file:
        #        data.append(json.loads(line))
            
        data = data_io.read_ndjson_file(storage_location = self.raw_path, filename = filename)

        if data is not None and len(data) > 0:
            df = pd.json_normalize(data)

            # Only keeping data from accepted tiktok urls
            if 'source_platform_url' in df.columns:
                df = df[df['source_platform_url'].isin(self.accepted_tiktok_urls)].copy()
        
        return df






    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        # zeeschuimer data is really basic - well, there is a lot of useful data in the ndjson, but to generate
        # an activity collection, which is the purpose here, I am only using the item_id and the timestamp

        df = df.copy()
        
        # Extract lots of useful data from the source_url to get tz_offset etc
        source_details = []
        for ii in df.index:
            source_details += [clean_url(df['source_url'][ii])]
        source_details = pd.DataFrame(source_details, index=df.index)
        df = pd.merge(left=df, right=source_details, left_index=True, right_index=True)


        # -----------------------------------------------------
        # I call all activities from zeeschuimer 'observe' to distinguish it from 'play'
        df["activity_type"] = "observe"


        # -----------------------------------------------------
        # item_id: 
        # Filter valid item_ids to make sure they're not corrupted
        if 'item_id' in df.columns:
             df = df[df.item_id.map(lambda x:all([u in "0123456789" for u in x]) and len(x) == 19)].copy()
        

        # -----------------------------------------------------
        # tz_offset and utc_timestamp:

        # timestamp_collected is a Unix epoch in milliseconds — parse directly
        # as tz-aware UTC. The prior implementation used datetime.fromtimestamp()
        # which returns a naive datetime in the *server's* local timezone, then
        # relocalised it as if it were in source_url.tz_name. That only produced
        # correct UTC when the ingestion server and the user happened to share a
        # timezone — off by the local offset otherwise.
        df["utc_timestamp"] = pd.to_datetime(
            df["timestamp_collected"].astype(np.int64), unit='ms', utc=True
        )

        unique_tz = df["source_url.tz_name"].dropna().unique()

        # tz_offset is the user's offset from UTC at the time of the event,
        # derived from source_url.tz_name so DST boundaries are respected.
        if len(unique_tz) == 1:
            tz = ZoneInfo(unique_tz[0])
            df["tz_offset"] = (
                df["utc_timestamp"].dt.tz_convert(tz).apply(
                    lambda t: t.utcoffset().total_seconds() / 3600 if pd.notna(t) else np.nan
                )
            )
        elif len(unique_tz) > 1:
            offset_parts = []
            for tz_name, block in df.groupby("source_url.tz_name", sort=False):
                tz = ZoneInfo(tz_name)
                part = block["utc_timestamp"].dt.tz_convert(tz).apply(
                    lambda t: t.utcoffset().total_seconds() / 3600 if pd.notna(t) else np.nan
                )
                offset_parts.append(part)
            df["tz_offset"] = pd.concat(offset_parts).sort_index()
        
        # I'm keeping this information in the extra_data column. It's a string so it works fine
        df.rename(columns={"source_url.tz_name": "extra_data"}, inplace=True)

        return df







@functools.lru_cache(maxsize=1)
def _config_timezone_offset() -> float:
    """Return the project timezone's current UTC offset in hours (fallback only).

    Used when a YouTube Takeout timestamp carries an unrecognised timezone
    abbreviation; the row is still converted to UTC using this offset, and the
    per-donor ``tz_offset`` is re-inferred downstream from the UTC series.
    Cached — the offset cannot meaningfully change within one process run.
    """
    tzname = fyp_cf["misc"].get("TIME_ZONE", "UTC")
    try:
        now = datetime.now(ZoneInfo(tzname))
    except ZoneInfoNotFoundError:
        return 0.0
    off = now.utcoffset()
    return off.total_seconds() / 3600 if off is not None else 0.0





class InstagramDDPCollection(ForYouBaseCollection):
    """Instagram "Download Your Information" export ingester.

    Parses the two activity streams we care about from the uploaded zip: viewed
    reels (``story_interactions/stories_viewed.json`` → ``activity_type='play'``)
    and liked posts (``likes/liked_posts.json`` → ``activity_type='fave'``).
    Both the current ``label_values`` record schema and the classic
    ``string_list_data`` / ``string_map_data`` schema are supported. The donated
    caption and owner are captured as an enrichment seed via the base ``seed_*``
    contract. Structural failures (unreadable zip, missing members, invalid
    JSON) raise so the file stays pending instead of being silently discarded.
    """

    platform_url_template = "https://www.instagram.com/reel/{item_id}/"
    source_platform = "instagram"
    raw_path = "instagram_raw"

    # (inner zip-member suffix, activity_type) for each stream we ingest.
    _STREAMS = [
        ("story_interactions/stories_viewed.json", "play"),
        ("likes/liked_posts.json", "fave"),
    ]
    _SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|p|tv)/([\w-]+)")





    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "instagram"
        self.data_source = "ddp"
        self.min_required_rows_per_raw_file = 10





    @classmethod
    def zip_member_suffixes(cls) -> list[str]:
        """The two activity-stream members read from the export zip."""
        return [suffix for suffix, _ in cls._STREAMS]





    @staticmethod
    def _records(payload: object) -> list[dict]:
        """Normalise a stream file's JSON into a flat list of activity records.

        Handles a bare list, a bare dict holding a single record, and wrapper
        dicts like ``{"likes_media_likes": [...]}`` (classic exports).
        """
        if payload is None:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            if "label_values" in payload or "string_list_data" in payload:
                return [payload]
            for value in payload.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
        return []





    @classmethod
    def _extract(cls, record: dict) -> tuple[str | None, str | None, str | None, str | None, int | None]:
        """Return ``(item_id, desc, author_id, author_name, timestamp)`` for one record.

        Supports both Instagram export record schemas: the current
        ``label_values`` list (URL/Caption labels + doubly-nested ``Owner``
        block) and the classic ``string_list_data`` / ``string_map_data`` shape
        (record-level ``title`` is the media owner's username; the href and
        timestamp live in the first ``string_list_data`` entry).
        """
        url = desc = author_id = author_name = None
        timestamp = record.get("timestamp")

        if "label_values" in record:
            for lv in record.get("label_values", []):
                label = lv.get("label")
                if label == "URL" and not url:
                    url = lv.get("value") or lv.get("href")
                elif label == "Caption" and not desc:
                    desc = lv.get("value")
                elif lv.get("title") == "Owner":
                    for outer in lv.get("dict", []):
                        for inner in outer.get("dict", []):
                            if inner.get("label") == "Name" and not author_name:
                                author_name = inner.get("value")
                            elif inner.get("label") == "Username" and not author_id:
                                author_id = inner.get("value")
        else:
            author_id = record.get("title") or None
            entries = record.get("string_list_data") or []
            first = entries[0] if entries and isinstance(entries[0], dict) else {}
            url = first.get("href")
            if timestamp is None:
                timestamp = first.get("timestamp")
            if timestamp is None:
                smd = record.get("string_map_data") or {}
                for entry in smd.values():
                    if isinstance(entry, dict) and entry.get("timestamp"):
                        timestamp = entry["timestamp"]
                        break

        item_id = None
        if url:
            match = cls._SHORTCODE_RE.search(url)
            if match:
                item_id = match.group(1)
        return item_id, desc, author_id, author_name, timestamp





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Extract the viewed-reels and liked-posts streams from the upload zip.

        Raises:
            ValueError: when the zip is unreadable, holds none of the expected
                members, or a member is not valid JSON — structural failures
                that must stay pending rather than be discarded as too-small.
        """
        local_path = data_io.local_copy(storage_location=self.raw_path, filename=filename)
        if not local_path:
            raise ValueError(f"could not fetch '{filename}' from '{self.raw_path}'")

        try:
            members = read_zip_members(local_path, [s for s, _ in self._STREAMS])
        finally:
            data_io.release_local_copy(local_path)
        if all(raw is None for raw in members.values()):
            raise ValueError(
                f"'{filename}' contains none of the expected Instagram activity "
                f"files ({', '.join(s for s, _ in self._STREAMS)})"
            )

        rows = []
        for suffix, activity_type in self._STREAMS:
            raw = members[suffix]
            if raw is None:
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"'{filename}' member '{suffix}' is not valid JSON: {exc}") from exc
            for record in self._records(payload):
                item_id, desc, author_id, author_name, timestamp = self._extract(record)
                # item_id is nullable in the activity contract (classic story
                # views carry no URL); a row without a timestamp is useless.
                if timestamp is None:
                    continue
                rows.append({
                    "item_id": item_id if item_id else pd.NA,
                    "activity_type": activity_type,
                    "ig_timestamp": timestamp,
                    "seed_desc": repair_mojibake(desc) if desc else pd.NA,
                    "seed_author_id": author_id if author_id else pd.NA,
                    "seed_author_name": repair_mojibake(author_name) if author_name else pd.NA,
                })

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame.from_records(rows)





    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert the unix view/like timestamps to UTC and finalize the frame."""
        df = df.copy()
        df["utc_timestamp"] = pd.to_datetime(
            df["ig_timestamp"], unit="s", utc=True, errors="coerce"
        )
        return derive_play_duration(self._finalize_activity_frame(df))





class YouTubeDDPCollection(ForYouBaseCollection):
    """YouTube / Google Takeout watch-history ingester.

    Parses ``history/watch-history.json`` or ``history/watch-history.html``
    from the uploaded Takeout zip — Takeout emits one or the other depending on
    the account's export settings; JSON is preferred when both are present
    because its timestamps are unambiguous ISO-8601 UTC. Organic video watches
    become ``activity_type='play'``; served ad impressions ("From Google Ads")
    become ``activity_type='ad_play'``; non-video events (Shorts-creation,
    community-post views) are dropped. The donated title and channel are
    captured as an enrichment seed via the base ``seed_*`` contract.
    """

    platform_url_template = "https://www.youtube.com/watch?v={item_id}"
    source_platform = "youtube"
    raw_path = "youtube_raw"

    _MEMBER_SUFFIX_HTML = "history/watch-history.html"
    _MEMBER_SUFFIX_JSON = "history/watch-history.json"
    _BODY_RE = re.compile(r'body-1[^>]*>(.*?)</div>', re.S)
    _CAPTION_RE = re.compile(r'mdl-typography--caption">(.*?)</div>', re.S)
    _VIDEO_RE = re.compile(r'watch\?v=([\w-]{11})')
    _TITLE_RE = re.compile(r'watch\?v=[\w-]{11}[^"]*">(.*?)</a>', re.S)
    _CHANNEL_RE = re.compile(r'/channel/([\w-]+)"[^>]*>(.*?)</a>', re.S)
    _CHANNEL_ID_RE = re.compile(r'/channel/([\w-]+)')
    # Takeout renders timestamps in the account's display locale. Supported:
    # day-first ("29 Jun 2026, 21:43:42 AEST", September as "Sept", June/July in
    # full) and US month-first 12-hour ("Apr 4, 2024, 5:36:02 PM PDT"), with an
    # abbreviated zone or a "GMT+05:30"-style offset. AM/PM may be preceded by
    # a narrow/regular no-break space in newer exports.
    _TS_RE = re.compile(
        r'(\d{1,2} [A-Za-z]{3,9} \d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}), '
        r'(\d{1,2}:\d{2}:\d{2})'
        r'(?:[\s\u202f\u00a0]*([APap][Mm]))?'
        r'[\s\u202f\u00a0]+([A-Z]{2,5}(?:[+-]\d{1,2}:?\d{2})?)'
    )
    _GMT_OFFSET_RE = re.compile(r'^(?:GMT|UTC)([+-])(\d{1,2}):?(\d{2})$')
    _MONTH_NORM_RE = re.compile(r'([A-Za-z]{3})[A-Za-z]*')

    # Timezone abbreviation -> UTC offset (hours). Some abbreviations are
    # ambiguous across regions; those get the interpretation most common in
    # Takeout exports (IST: India, not Ireland/Israel; CST/EST/PST: US) and are
    # reported per file so mislabelled donations stay auditable. Unknown
    # abbreviations fall back to the project timezone's offset.
    _TZ_OFFSETS = {
        "AEST": 10, "AEDT": 11, "ACST": 9.5, "ACDT": 10.5, "AWST": 8,
        "NZST": 12, "NZDT": 13, "GMT": 0, "UTC": 0, "BST": 1, "IST": 5.5,
        "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "WET": 0, "WEST": 1,
        "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
        "PST": -8, "PDT": -7, "HKT": 8, "JST": 9, "KST": 9, "SGT": 8,
    }
    _AMBIGUOUS_TZ = {"IST", "CST", "BST", "EST"}





    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "youtube"
        self.data_source = "ddp"
        self.min_required_rows_per_raw_file = 10





    @classmethod
    def zip_member_suffixes(cls) -> list[str]:
        """Both watch-history members; a Takeout zip carries one of the two."""
        return [cls._MEMBER_SUFFIX_JSON, cls._MEMBER_SUFFIX_HTML]





    @classmethod
    def _parse_history(cls, html_text: str) -> list[dict]:
        """Return one raw row per watch-history cell that references a video.

        Cells without a ``watch?v=`` link (Shorts-creation, community-post views)
        are skipped. Ad impressions are flagged from the details/caption cell
        (not the whole block, whose title text could contain the marker
        phrase); removed/unavailable videos keep their id but carry no donated
        title.
        """
        rows: list[dict] = []
        for block in html_text.split('<div class="outer-cell')[1:]:
            body_match = cls._BODY_RE.search(block)
            body = body_match.group(1) if body_match else ""

            video_match = cls._VIDEO_RE.search(body)
            if not video_match:
                continue
            item_id = video_match.group(1)

            title_match = cls._TITLE_RE.search(body)
            title = html.unescape(title_match.group(1)) if title_match else None
            if title and "watch?v=" in title:
                title = None  # removed/unavailable video: title is just the URL

            channel_match = cls._CHANNEL_RE.search(body)
            channel_id = channel_match.group(1) if channel_match else None
            channel_name = html.unescape(channel_match.group(2)) if channel_match else None

            caption_match = cls._CAPTION_RE.search(block)
            caption = caption_match.group(1) if caption_match else ""

            ts_match = cls._TS_RE.search(body)
            rows.append({
                "item_id": item_id,
                "is_ad": "From Google Ads" in caption,
                "yt_date": ts_match.group(1) if ts_match else None,
                "yt_time": ts_match.group(2) if ts_match else None,
                "yt_ampm": ts_match.group(3) if ts_match else None,
                "yt_tz": ts_match.group(4) if ts_match else None,
                "seed_desc": title if title else pd.NA,
                "seed_author_id": channel_id if channel_id else pd.NA,
                "seed_author_name": channel_name if channel_name else pd.NA,
            })
        return rows





    @classmethod
    def _parse_history_json(cls, payload: list) -> list[dict]:
        """Return one raw row per JSON watch-history record that references a video.

        Records without a ``watch?v=`` video id in ``titleUrl`` (Shorts-creation
        tools, Shorts-ad views with no target video, community posts) are
        skipped. Ad impressions carry ``details: [{"name": "From Google Ads"}]``;
        removed/unavailable videos echo the watch URL as the title and carry no
        donated title. Timestamps are ISO-8601 UTC strings, emitted verbatim in
        ``yt_json_time`` and converted in ``load_single_raw``.
        """
        rows: list[dict] = []
        for record in payload:
            if not isinstance(record, dict):
                continue
            video_match = cls._VIDEO_RE.search(record.get("titleUrl") or "")
            if not video_match:
                continue

            title = (record.get("title") or "").removeprefix("Watched ").strip()
            if not title or "watch?v=" in title:
                title = None  # removed/unavailable video: title is just the URL

            subtitles = record.get("subtitles") or []
            channel = subtitles[0] if subtitles and isinstance(subtitles[0], dict) else {}
            channel_match = cls._CHANNEL_ID_RE.search(channel.get("url") or "")

            details = record.get("details") or []
            is_ad = any(
                isinstance(detail, dict) and detail.get("name") == "From Google Ads"
                for detail in details
            )

            rows.append({
                "item_id": video_match.group(1),
                "is_ad": is_ad,
                "yt_json_time": record.get("time"),
                "seed_desc": title if title else pd.NA,
                "seed_author_id": channel_match.group(1) if channel_match else pd.NA,
                "seed_author_name": channel.get("name") or pd.NA,
            })
        return rows





    @classmethod
    def _convert_timestamps(cls, df: pd.DataFrame, donor_tz=None) -> pd.Series:
        """Vectorised conversion of the parsed timestamp components to UTC.

        Normalises the month token to its 3-letter form (Takeout renders
        "Sept"/"June"/"July"), tries the day-first then the US month-first date
        format, and applies 12-hour AM/PM arithmetic to build each row's naive
        local wall-clock. The local time is then turned into UTC either by
        localising to an authoritative ``donor_tz`` (from the manifest —
        DST-correct and unambiguous) or, when none was supplied, by subtracting
        the offset read from the row's own timezone label (abbreviation map or
        an explicit GMT±HH:MM). Rows whose components did not parse come back NaT.

        Args:
            df: Frame with ``yt_date`` / ``yt_time`` / ``yt_ampm`` / ``yt_tz``.
            donor_tz: A ``tzinfo`` from the ingestion manifest, or ``None``.
        """
        dates_raw = df["yt_date"].astype("string").str.replace(cls._MONTH_NORM_RE, r"\1", regex=True)
        dates = pd.to_datetime(dates_raw, format="%d %b %Y", errors="coerce")
        dates = dates.fillna(pd.to_datetime(dates_raw, format="%b %d, %Y", errors="coerce"))

        times = pd.to_timedelta(df["yt_time"].astype("string").fillna(""), errors="coerce")
        ampm = df["yt_ampm"].astype("string").str.upper().fillna("")
        hours = times.dt.components["hours"]
        pm_shift = ((ampm == "PM") & (hours < 12)).astype("int64") * 12
        am_shift = ((ampm == "AM") & (hours == 12)).astype("int64") * -12
        times = times + pd.to_timedelta(pm_shift + am_shift, unit="h")

        naive = dates + times

        # Authoritative donor timezone from the manifest overrides the (sometimes
        # ambiguous) display label entirely.
        if donor_tz is not None:
            return naive.dt.tz_localize(
                donor_tz, ambiguous="NaT", nonexistent="shift_forward"
            ).dt.tz_convert("UTC")

        tz = df["yt_tz"].astype("string").fillna("")
        offsets = tz.map(cls._TZ_OFFSETS).astype("float64")
        gmt_parts = tz.str.extract(cls._GMT_OFFSET_RE)
        gmt_known = gmt_parts[1].notna()
        gmt_sign = np.where((gmt_parts[0] == "-").fillna(False), -1.0, 1.0)
        gmt_hours = pd.to_numeric(gmt_parts[1], errors="coerce").fillna(0)
        gmt_minutes = pd.to_numeric(gmt_parts[2], errors="coerce").fillna(0)
        gmt_offsets = gmt_sign * (gmt_hours + gmt_minutes / 60)
        offsets = offsets.where(~(offsets.isna() & gmt_known), pd.Series(gmt_offsets, index=df.index))

        unknown = offsets.isna() & (tz != "")
        ambiguous = tz.isin(cls._AMBIGUOUS_TZ)
        if unknown.any():
            print(
                f"WARNING: {int(unknown.sum())} YouTube row(s) carry an unrecognised "
                f"timezone label ({sorted(tz[unknown].unique().tolist())}); "
                f"falling back to the project timezone offset. Set a donor timezone "
                f"in the upload form to resolve this exactly."
            )
        if ambiguous.any():
            print(
                f"NOTE: {int(ambiguous.sum())} YouTube row(s) use an ambiguous timezone "
                f"abbreviation ({sorted(tz[ambiguous].unique().tolist())}); using the "
                f"most common Takeout interpretation. Set a donor timezone in the "
                f"upload form to resolve this exactly."
            )
        offsets = offsets.fillna(_config_timezone_offset())

        return (naive - pd.to_timedelta(offsets, unit="h")).dt.tz_localize("UTC")





    def load_single_raw(self, filename: str) -> pd.DataFrame:
        """Extract the watch-history member from the Takeout zip and parse it.

        Takeout exports the history as JSON or HTML depending on the account's
        export settings; both members are requested in one archive pass and the
        JSON one is preferred (unambiguous ISO-8601 UTC timestamps, no display
        locale involved). Timestamps are converted here (not in
        ``process_single``) so an unsupported locale is detected while the file
        can still be left pending rather than ledger-blacklisted.

        Raises:
            ValueError: when the zip is unreadable, has no watch-history
                member, holds invalid JSON, or none of its rows yields a
                parseable timestamp (unsupported display locale) — structural
                failures that must stay pending rather than be discarded as
                too-small.
        """
        local_path = data_io.local_copy(storage_location=self.raw_path, filename=filename)
        if not local_path:
            raise ValueError(f"could not fetch '{filename}' from '{self.raw_path}'")

        try:
            members = read_zip_members(
                local_path, [self._MEMBER_SUFFIX_JSON, self._MEMBER_SUFFIX_HTML]
            )
        finally:
            data_io.release_local_copy(local_path)
        raw_json = members[self._MEMBER_SUFFIX_JSON]
        raw_html = members[self._MEMBER_SUFFIX_HTML]
        if raw_json is None and raw_html is None:
            raise ValueError(
                f"'{filename}' contains no '{self._MEMBER_SUFFIX_JSON}' or "
                f"'{self._MEMBER_SUFFIX_HTML}' member"
            )

        donor_tz = parse_donor_timezone(self._current_file_tz)
        if raw_json is not None:
            try:
                payload = json.loads(raw_json.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"'{filename}': '{self._MEMBER_SUFFIX_JSON}' is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, list):
                raise ValueError(
                    f"'{filename}': '{self._MEMBER_SUFFIX_JSON}' is not a list of records"
                )
            rows = self._parse_history_json(payload)
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame.from_records(rows)
            df["utc_timestamp"] = pd.to_datetime(
                df["yt_json_time"], utc=True, format="ISO8601", errors="coerce"
            )
            hint = ""
        else:
            rows = self._parse_history(raw_html.decode("utf-8", errors="replace"))
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame.from_records(rows)
            df["utc_timestamp"] = self._convert_timestamps(df, donor_tz=donor_tz)
            hint = (
                "" if donor_tz is not None
                else " — probably an unsupported display locale; set a donor "
                "timezone in the upload form to bypass the label."
            )

        if df["utc_timestamp"].isna().all():
            raise ValueError(
                f"'{filename}': {len(df)} watch event(s) found but no timestamp "
                f"could be parsed{hint}"
            )
        return df





    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Flag ads vs organic watches and finalize the frame."""
        df = df.copy()
        df["activity_type"] = np.where(df["is_ad"].fillna(False), "ad_play", "play")
        return derive_play_duration(self._finalize_activity_frame(df))





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
