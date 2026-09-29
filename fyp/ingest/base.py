"""Platform-independent ingestion of donated activity data.

Defines the collection base class that turns raw donation files into activity
rows (timezone inference, session ids, play durations, engagement tokens), the
concrete multi-platform collection, and the ingestion ledger and approval flow.
"""

import functools
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

import fyp.core.data_io as data_io
from fyp.core import activity_contract as _activity_contract
from fyp.core import activity_versioning as _activity_versioning
from fyp.core.activity_vocabulary import KNOWN_ACTIVITY_TYPES, share_method_base
from fyp.core.logging_setup import get_logger
from fyp.core.polars_ops import fast_vertical_concat
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import label
from fyp.core.types import convert_dtypes_to_pyarrow
from fyp.scrape import scrape_contract as _scrape_contract
from fyp.scrape import scrape_versioning as _scrape_versioning

from . import ingestion_ledger, transforms
from . import structure_sentinel as _structure_sentinel
from .donations import (
    demographic_metadata_columns,
    generate_collection_metadata,
    strip_demographic_columns,
)

# Moved to .transforms and .ingestion_ledger; re-exported for callers of this
# module's old surface.
from .ingestion_ledger import (  # noqa: F401
    BLOCKED_OUTCOME,
    INGESTION_LEDGER_FILENAME,
    LEDGER_SKIP_OUTCOMES,
    LEGACY_DISCARDED_FILENAME,
    LEGACY_MIGRATION_NOTE,
)
from .transforms import (  # noqa: F401
    MANIFEST_TZ_COLUMN,
    WEEKDAY_MAPPER,
    assign_session_ids,
    derive_play_duration,
    parse_donor_timezone,
    zone_offset_hours,
)

logger = get_logger(__name__)


def _collections_label() -> str:
    """The config-derived collections label."""
    return label("COLLECTIONS_LABEL")


# Maps a collection's standard donated-metadata scratch columns to the canonical
# scrape base fields (config/scrape_contract.toml) written by save_enrichment_seed.
_SEED_TO_CANONICAL = {
    "seed_desc": "desc",
    "seed_author_id": "author_id",
    "seed_author_name": "author_name",
    "seed_create_time": "create_time",
}


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
        "tz_offset": "double[pyarrow]",
        "item_id": "string[pyarrow]",
        "ts_added_to_dataset": "timestamp[ns][pyarrow]",
        "extra_data": "string[pyarrow]",
        "link_method": "string[pyarrow]",
    }
    _ACTIVITY_REQUIRED_CORE = [
        "activity_type",
        "utc_timestamp",
        "collection_id",
        "data_source",
        "tz_offset",
    ]


COLLECTION_TAGS_FILENAME = "collections_tags.json"
STUDIES_FILENAME = "studies.json"


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
        tags = (
            data_io.load_json(storage_location=storage_location, filename=COLLECTION_TAGS_FILENAME)
            or {}
        )

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
            # Account link: the new entry's decision wins; an undecided new
            # entry inherits the old one. Two different accounts is a real
            # conflict — keep the new one but say so.
            if "user_id" in old_entry:
                if "user_id" not in new_entry:
                    new_entry["user_id"] = old_entry["user_id"]
                elif (
                    new_entry.get("user_id")
                    and old_entry.get("user_id")
                    and new_entry["user_id"] != old_entry["user_id"]
                ):
                    logger.warning(
                        f"cid_remap {old_cid} -> {new_cid}: collections linked to different "
                        f"accounts ({old_entry['user_id']!r} vs {new_entry['user_id']!r}); "
                        f"keeping {new_entry['user_id']!r}"
                    )
            tags[new_cid] = new_entry
            summary["tag_keys_merged"].append((old_cid, new_cid))
        else:
            tags[new_cid] = old_entry
            summary["tag_keys_renamed"].append((old_cid, new_cid))
        tags_changed = True

    if tags_changed and save:
        data_io.save_json(
            data=tags, storage_location=storage_location, filename=COLLECTION_TAGS_FILENAME
        )

    # --- studies.json ---
    studies = {}
    if data_io.exists(storage_location=storage_location, filename=STUDIES_FILENAME):
        studies = (
            data_io.load_json(storage_location=storage_location, filename=STUDIES_FILENAME) or {}
        )

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
        data_io.save_json(
            data=studies, storage_location=storage_location, filename=STUDIES_FILENAME
        )

    if verbose:
        logger.info(
            f"cid_remap propagated: tags renamed={len(summary['tag_keys_renamed'])}, "
            f"merged={len(summary['tag_keys_merged'])}; studies updated="
            f"{len(summary['studies_updated'])}; unmapped old keys="
            f"{len(summary['unmapped_old_keys'])}"
        )

    return summary


class ForYouBaseCollection(ABC):
    platform_url_template: str | None = None
    # Class attributes so registries (e.g. the viewer's platform URL map, the
    # raw-upload location list) can read platform facts without instantiating;
    # __init__ mirrors them per instance.
    source_platform: str | None = None
    raw_path: str | None = None
    ingestion_mode: str = "upload"
    # The activity_type values this platform's process_single can produce,
    # all drawn from activity_vocabulary.KNOWN_ACTIVITY_TYPES. A registry test
    # (tests/unit/test_ingest_activity_vocabulary.py) checks the declaration
    # against the class's section maps, and process() notes any file whose
    # rows fall outside it — a drifted export vintage shows up in the ledger
    # rather than as a silent new category downstream.
    emitted_activity_types: frozenset[str] = frozenset()
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
        through :func:`fyp.core.data_io.register_location`, so adding a platform needs
        no static ``fyp_config`` edit. Runs at class definition (import time) so
        the location exists in every process before any request touches it —
        upload routes must not depend on a collection having been instantiated
        first. Locations already present in config (the built-in ddp/aio/
        zeeschuimer ones) are left untouched; failures are printed loudly but
        never break the import.

        Historical wrinkle: because the three TikTok keys pre-exist in
        ``fyp_config`` they resolve to source-keyed folders
        (``activity_data/ddp``, ``/aio``, ``/zeeschuimer``), while platforms
        added later (Instagram, YouTube) get platform-keyed folders from this
        convention (``activity_data/instagram/instagram_raw``). The mixed
        scheme is deliberate-by-inertia — see the note in ``fyp_config.py``.
        """
        raw_path = cls.__dict__.get("raw_path") or getattr(cls, "raw_path", None)
        source_platform = getattr(cls, "source_platform", None)
        if not raw_path or not source_platform:
            return
        try:
            abs_path = os.path.join(_cf()["paths"]["activity_data"], source_platform, raw_path)
            data_io.register_location(raw_path, abs_path)
        except Exception as exc:
            logger.warning(
                f"WARNING: could not register raw location '{raw_path}' for {cls.__name__}: {exc}"
            )

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
        # Per-file notes a parser records while loading (see note_file);
        # reset per load_raw run.
        self.parse_notes_this_run: dict[str, list[str]] = {}
        self.discarded_raw_files = []
        self.discarded_collections_filename = "discarded_collection_files.json"
        self.source_platform = getattr(type(self), "source_platform", None)
        self.data_source = None
        self.collections = []
        # Donor timezone for the file currently being loaded (from the manifest);
        # set per-file in load_raw so load_single_raw can honour it.
        self._current_file_tz = None
        # Structure-drift detector for this run (fyp.ingest.structure_sentinel.
        # StructureSentinel), injected by run_ingest_refresh. When None, no
        # structure checks run and load_raw behaves exactly as before.
        self.sentinel = None
        # Files withheld from this run because their structure deviated from
        # the learned baseline: {filename: verdict dict}.
        self.quarantined_this_run: dict[str, dict] = {}
        # Manifest entries whose stored name is in the skip set (load_raw).
        self.blocked_this_run: dict[str, str] = {}
        # This run's manifest, for copying provenance into the ledger.
        self.manifest_this_run: dict[str, dict] = {}
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
        # What a parser read from a file before handing back its frame, when
        # that differs from the frame's length (see record_load_count).
        self.records_read_this_run: dict[str, int] = {}
        self.load_drops_this_run: dict[str, dict[str, int]] = {}

    def clear(self):
        self.data = pd.DataFrame()
        self.state = "empty"

    def load_processed(self, processed_fn: str, drop_similar_activity_sequences: bool = True):

        if self.verbose:
            logger.info(
                f"Loading processed data from {processed_fn}. Data source: {self.source_platform}_{self.data_source}"
            )

        new_processed_data = data_io.load_parquet(
            storage_location=self.processed_storage_location,
            filename=processed_fn,
            verbose=False,
        )

        if len(self.data) > 0:
            if self.state != "processed":
                if self.verbose:
                    logger.warning(
                        f"Warning: There is data in this collection but the state is '{self.state}'. Existing data must be processed. Cannot load new data."
                    )
                return
            if self.verbose:
                logger.info(
                    f"Adding {len(new_processed_data):,} new processed activities to existing {len(self.data):,} activities."
                )
            # Vertical concat via polars: parallel, avoids pandas' O(n) copy
            # on accumulating appends. Matters at events-scale (tens of millions
            # of rows). See fyp/core/polars_ops.py.
            self.data = fast_vertical_concat([self.data, new_processed_data])
        else:
            if self.verbose:
                logger.info(f"Loading {len(new_processed_data):,} processed activities.")
            self.data = new_processed_data.copy()

        self.state = "processed"

        if drop_similar_activity_sequences:
            if self.verbose:
                logger.info(
                    "Dropping activities from files with overlapping/similar activity sequences"
                )
            cid_remap = self.identify_similar_file_content(drop_them=True)
            if cid_remap:
                apply_cid_remap_to_metadata(cid_remap, verbose=self.verbose)

        if self.verbose:
            logger.info(f"There are now {len(self.data):,} activities in the collection.")

    def save_processed(self):

        if self.state != "processed":
            logger.warning(
                f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first."
            )
            return

        fn = f"{self.source_platform}_{self.data_source}_processed_activities.parquet"

        if len(self.data) > 0:
            local_time_cols = [c for c in self.data.columns if c.startswith("local_")]
            if len(local_time_cols) > 0:
                logger.info(
                    "This dataset seem to have 'local time features' added. I am dropping these columns when saving."
                )
                self.data.drop(local_time_cols, axis=1, inplace=True)

            _ = data_io.save_parquet(
                df=self.data, storage_location=self.processed_storage_location, filename=fn
            )

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        data_io.save_json(
            data=self.discarded_raw_files,
            storage_location=self.processed_storage_location,
            filename=self.discarded_collections_filename,
            verbose=False,
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
            tz = transforms._first_manifest_tz(df)
            if tz is not None:
                df["tz_offset"] = transforms.zone_offset_hours(df["utc_timestamp"], tz)
            else:
                df["tz_offset"] = transforms.infer_timezone_offset(df["utc_timestamp"])
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

    def load_raw(
        self, skip_these_raw_files: list[str] = [], held_for_review: set[str] | None = None
    ):
        """Load every raw file in ``raw_path`` that is not in the skip set.

        Args:
            skip_these_raw_files: Stored names already in the dataset or the
                discard list; never opened.
            held_for_review: Stored names the structure sentinel quarantined
                in an earlier run. They are in the skip set by design (the
                ledger says ``quarantined_structure``) and their manifest
                entries stay pending until an admin approves or rejects the
                file, so the name-collision tripwire must not fire on them.
        """
        if self.verbose:
            logger.info(
                f"Loading raw data for collection '{self.source_platform}_{self.data_source}'."
            )

        if self.state != "empty":
            logger.info(
                f"This collection '{self.source_platform}_{self.data_source}' is not empty. The current data will be replaced."
            )

        if self.raw_path is None:
            raise ValueError("No raw path has been set for this collection.")

        self.load_failed_this_run = {}
        self.file_stats_this_run = {}
        # Notes a parser records about a file while loading it (e.g. an
        # ambiguous time-zone label it had to resolve); copied onto the
        # file's intake stats once the file is accepted, and from there onto
        # the ledger entry, so the resolution is recorded per file rather
        # than only logged.
        self.parse_notes_this_run: dict[str, list[str]] = {}
        self.records_read_this_run = {}
        self.load_drops_this_run = {}
        self.blocked_this_run = {}
        self.manifest_this_run = {}

        MANIFEST_FILENAME = "ingestion_manifest.json"

        all_the_files = [
            fn
            for fn in data_io.listdir(self.raw_path)
            if not fn.startswith(".") and fn != MANIFEST_FILENAME
        ]

        all_the_files = [
            fn for fn in all_the_files if fn not in skip_these_raw_files + self.discarded_raw_files
        ]

        # Load ingestion manifest (written at upload time with collection_id / tags per file)
        manifest: dict = {}
        if data_io.exists(storage_location=self.raw_path, filename=MANIFEST_FILENAME):
            manifest = (
                data_io.load_json(
                    storage_location=self.raw_path, filename=MANIFEST_FILENAME, verbose=False
                )
                or {}
            )
        self.manifest_this_run = {
            fn: (meta if isinstance(meta, dict) else {}) for fn, meta in manifest.items()
        }

        # Tripwire. A manifest entry is an upload waiting for THIS run; when
        # its stored name is in the skip set the loop below never opens it,
        # and pruning it as "processed" would lose the upload without a trace
        # (a donor's upload whose name collided with an old test collection's
        # raw file was once lost that way). Stored names are generated and
        # unique, so a hit can only mean a bug or a hand-placed file: report it
        # loudly and leave the entry pending for a human.
        skip_set = set(skip_these_raw_files) | set(self.discarded_raw_files)
        held = set(held_for_review or ())
        for fn in manifest:
            if fn not in skip_set or fn in held:
                continue
            reason = (
                "its name is in the discard list"
                if fn in self.discarded_raw_files
                else "its name is already a raw file in the dataset"
            )
            self.blocked_this_run[fn] = reason
            logger.error(
                f"ERROR: pending upload '{fn}' in {self.raw_path} was NOT ingested: "
                f"{reason}. The entry stays pending; give the file a fresh stored "
                f"name or remove the entry."
            )

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

            records_read = self.records_read_this_run.pop(fn, None)
            self.file_stats_this_run[fn] = {
                "raw_rows": int(records_read if records_read is not None else len(one_df)),
                "dropped": dict(self.load_drops_this_run.pop(fn, {})),
            }
            parse_notes = self.parse_notes_this_run.pop(fn, None)
            if parse_notes:
                self.file_stats_this_run[fn]["parse_notes"] = list(parse_notes)

            if len(one_df) > 0:
                mtime = data_io.getmtime(storage_location=self.raw_path, filename=fn)
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
                one_df[transforms.MANIFEST_TZ_COLUMN] = (
                    self._current_file_tz if self._current_file_tz else pd.NA
                )

                if self.verbose:
                    logger.info(f"Loaded file: {fn}. Number of rows: {len(one_df):,}")

            # Skip files with fewer than min_required_rows_per_raw_file activities
            # (an arbitrary floor, 10 by default).
            if len(one_df) >= self.min_required_rows_per_raw_file:
                # Structure-drift check (sentinel Phase A): a quarantined verdict
                # withholds the file's rows from this run; the file is reviewed
                # in the Data Management UI. A sentinel failure must never
                # block ingestion, so it degrades to ingest-with-warning.
                verdict = None
                if self.sentinel is not None:
                    try:
                        # Client-reviewed (browser-pruned) uploads are missing
                        # whole sections by design, so they evaluate against
                        # their own "__reviewed" baseline variant.
                        variant = "reviewed" if file_meta.get("client_reviewed") else None
                        verdict = self.sentinel.check_raw(self, fn, one_df, variant=variant)
                    except Exception as exc:
                        logger.warning(
                            f"WARNING: structure check failed for '{fn}': {exc}. Ingesting anyway."
                        )
                withheld = list((verdict or {}).get("withheld_sections") or [])
                if withheld:
                    # The donor's choice, not drift: noted on the ledger entry.
                    self.file_stats_this_run[fn]["withheld_sections"] = withheld
                    if self.verbose:
                        logger.info(
                            f"   [{fn}] Donation leaves out {len(withheld)} section(s): "
                            + ", ".join(withheld[:8])
                            + (" …" if len(withheld) > 8 else "")
                        )
                if verdict is not None and verdict["status"] == "quarantined":
                    self.quarantined_this_run[fn] = verdict
                    if self.verbose:
                        logger.info(f"Quarantining file: {fn} (structure drift).")
                else:
                    many_dfs.append(one_df)
            else:
                if self.verbose:
                    logger.info(f"Discarding file: {fn}. Too few rows: {len(one_df):,}")
                self.discarded_raw_files.append(fn)

        if len(many_dfs) > 1:
            # Vertical concat via polars — fast multi-frame stack of per-file
            # raw DataFrames. See fyp/core/polars_ops.py.
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
        """Read one raw upload into a frame of candidate activity rows.

        A platform subclass implements this. The base load loop calls it per
        file (already filtered by ``accepted_upload_suffixes`` and the review
        step), stamps ``raw_file`` and the manifest tail onto the result, and
        feeds it to :meth:`process_single`. The frame's layout is the
        platform's own — TikTok returns one row per exported record with its
        key/value lists, Instagram and YouTube return the near-final columns —
        so the contract is only:

        * return an EMPTY frame for a file the Hub should discard as too
          small (see ``min_required_rows_per_raw_file``; count viewing rows,
          never engagement, so a like-list cannot carry a donation over the
          floor);
        * RAISE for a structural failure (unreadable zip, missing members,
          invalid JSON) so the file stays pending instead of vanishing;
        * populate the ``seed_*`` scratch columns (see ``_SEED_COLUMNS``) if
          the export carries item metadata worth keeping as an enrichment
          seed;
        * call :meth:`note_file` for anything a reader should know about
          how the file was parsed.

        Every export section the parser keeps must also be listed in the
        class's :meth:`review_manifest` with a participant-facing title — the
        review is the donor's consent surface, and the browser strips any
        section it does not list before upload.
        """

    def note_file(self, filename: str, message: str) -> None:
        """Record a per-file note from a parser for the ingestion ledger.

        Called from ``load_single_raw`` when the parser resolved something a
        reader of the data should know about the file (an ambiguous
        time-zone abbreviation, an unrecognised label that fell back to the
        project zone). The note lands on the ledger entry's ``notes``.

        Args:
            filename: The raw file being loaded.
            message: One plain-language sentence.
        """
        self.parse_notes_this_run.setdefault(filename, []).append(message)

    def record_load_count(
        self, filename: str, records_read: int, dropped: dict[str, int] | None = None
    ) -> None:
        """Record what ``load_single_raw`` read when its frame does not show it.

        A parser that drops records while loading (a browser capture's
        records from pages outside the feed) or returns an empty frame for a
        file below its floor would otherwise leave the ledger with the
        frame's length as the file's row count: zero for a too-small export,
        and a count that silently omits the load-time exclusions. Called
        from ``load_single_raw``; the load loop reads it back.

        Args:
            filename: The raw file being loaded.
            records_read: Records in the file before any load-time drop.
            dropped: Load-time drops by reason (e.g. ``outside_whitelist``).
        """
        self.records_read_this_run[filename] = int(records_read)
        if dropped:
            self.load_drops_this_run[filename] = {
                k: int(v) for k, v in dropped.items() if int(v) > 0
            }

    def _explicit_drops(self, filename: str) -> int:
        """Rows already recorded under any drop reason for ``filename`` this run."""
        dropped = (self.file_stats_this_run.get(str(filename)) or {}).get("dropped") or {}
        return int(sum(int(n) for n in dropped.values()))

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
        :func:`fyp.core.utils.read_zip_members`. The web upload UI uses this list to
        slim large donation zips client-side before upload, keeping only the
        listed members. Empty for platforms whose uploads are consumed whole.

        Returns:
            Member-name suffixes, or an empty list when client-side slimming
            does not apply to this platform.
        """
        return []

    @classmethod
    def review_manifest(cls) -> dict | None:
        """Describe this platform's export sections for the pre-upload review UI.

        The participant donation flow parses the export in the browser, shows
        one card per section listed here, lets the donor delete rows, and
        uploads only the pruned artifact. The manifest must stay in lockstep
        with what ``load_single_raw`` / ``process_single`` actually read —
        platform classes build it from the same constants their parsers use
        (guarded by tests/unit/test_donation_review_manifest.py).

        Returns:
            A manifest dict (see the platform overrides), or None when the
            platform has no pre-upload review (uploads go through unchanged).
        """
        return None

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
            A fingerprint dict (see :mod:`fyp.ingest.structure_sentinel`).
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
                return _structure_sentinel.fingerprint_zip(
                    local_path, type(self).zip_member_suffixes()
                )
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
                logger.info(
                    f"There is no data from platform/data_source '{self.source_platform}_{self.data_source}'. Nothing for me to do."
                )
            return

        if self.state != "raw":
            if self.verbose:
                logger.warning(
                    f"Platform/data_source '{self.source_platform}_{self.data_source}' is not in raw state. Cannot process. Please load raw data first."
                )
            return

        if self.verbose:
            logger.info(
                f"Processing {len(self.data):,} raw rows for platform/data_source '{self.source_platform}_{self.data_source}'..."
            )

        # Per-file row counts before/after the platform's process_single pass:
        # the difference is rows the platform could not turn into activities
        # (unreadable timestamp, missing item reference, ...). Counted here —
        # generically, per raw_file — because each platform drops these rows
        # inside its own process_single.
        _before = {str(k): int(v) for k, v in self.data.groupby("raw_file").size().items()}
        _explicit_before = {fn: self._explicit_drops(fn) for fn in _before}

        self.data = self.data.groupby("raw_file", group_keys=False)[self.data.columns].apply(
            self.process_single
        )

        self._note_undeclared_activity_types()

        # A platform may record its own reasons inside process_single (the
        # TikTok parser counts records outside its section whitelist); those
        # are subtracted so not_parseable is only the rows it failed to read.
        _after: dict[str, int] = {}
        if "raw_file" in self.data.columns and len(self.data) > 0:
            _after = {str(k): int(v) for k, v in self.data.groupby("raw_file").size().items()}
        self._record_file_drops(
            {
                fn: n - _after.get(fn, 0) - (self._explicit_drops(fn) - _explicit_before[fn])
                for fn, n in _before.items()
            },
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

        good_columns = list(
            (set(self.additional_columns.keys()) | set(list(self.REQUIRED_COLUMNS.keys())))
            & set(self.data.columns)
        )

        self.data = self.data[good_columns].copy()
        self._standardize()
        self.state = "processed"

        if self.verbose:
            logger.info(
                f"Raw data from platform/data_source '{self.source_platform}_{self.data_source}' is now processed. Number of rows: {len(self.data):,}"
            )

    @abstractmethod
    def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
        """Turn one raw file's frame into activity rows on the shared schema.

        A platform subclass implements this; :meth:`process` calls it once per
        ``raw_file`` group. The rows it returns must carry:

        * ``utc_timestamp`` — tz-aware UTC; ``tz_offset`` is added by the
          shared tail (a donor zone from the manifest wins, otherwise it is
          inferred from the activity rhythm);
        * ``activity_type`` — a value from
          ``activity_vocabulary.KNOWN_ACTIVITY_TYPES`` and from this class's
          ``emitted_activity_types``: viewing rows (``play``, ``observe``,
          ``ad_play``) are what studies are built on; engagement rows
          (``fave`` = like, ``save`` = bookmark, ``comment``, ``share``) fold
          onto a play; standalone rows (``follow``, ``followed_by``,
          ``search``, ``login``, ``post``) are kept for participant-facing
          stats only;
        * ``item_id`` — the platform's video/post id. Required on viewing
          rows and on any engagement row that should fold onto its play: an
          engagement row WITHOUT an item id is never folded and only ever
          counted from its standalone row, so a platform whose export names
          no media for a section (Instagram comments) should say so in its
          class docstring rather than invent one;
        * ``extra_data`` — the row's own payload: comment text, search term,
          followed username, share method. On play rows it is overwritten by
          the fold with the ``"<type>[:context]"`` tokens of the engagement
          that landed on that play;
        * ``link_method`` — set only when the parser itself inferred an item
          id (TikTok's ``ffill_180s``); the fold adds its own values.

        Finish with ``self._finalize_activity_frame(df)`` (tz_offset,
        chronological order) and return ``derive_play_duration(df)``, which
        derives dwell time and performs the engagement fold. Drop rows the
        platform cannot read (no timestamp, no item on a viewing row) inside
        this method; :meth:`process` counts them per file as
        ``not_parseable``.
        """

    def _note_undeclared_activity_types(self) -> None:
        """Record, per file, any activity_type outside this class's declaration.

        Never drops or raises: the rows are kept exactly as the parser made
        them. The point is visibility — an export vintage that starts
        producing a value the class did not declare (or one outside
        ``KNOWN_ACTIVITY_TYPES`` altogether) lands as a ledger note on the
        file, instead of as a silent new category in every downstream count.
        """
        if (
            not self.emitted_activity_types
            or "activity_type" not in self.data.columns
            or "raw_file" not in self.data.columns
            or len(self.data) == 0
        ):
            return
        allowed = set(self.emitted_activity_types) & KNOWN_ACTIVITY_TYPES
        undeclared = self.data[
            ~self.data["activity_type"].isin(list(allowed)) & self.data["activity_type"].notna()
        ]
        if len(undeclared) == 0:
            return
        for raw_file, grp in undeclared.groupby("raw_file"):
            types = ", ".join(sorted(str(t) for t in grp["activity_type"].unique()))
            self.note_file(
                str(raw_file), f"{len(grp):,} row(s) carry an undeclared activity type ({types})."
            )
            logger.warning(
                f"[{raw_file}] {len(grp):,} row(s) carry an activity type outside "
                f"{type(self).__name__}.emitted_activity_types: {types}"
            )

    def identify_similar_file_content(
        self,
        overlap_threshold: float = 0.2,
        min_shared_seconds: int = 3,
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
             activity_type], keep='last')`` so the newest donation's row wins
             on overlapping events. Share rows also key on their method
             (without the `` ×n`` record count), so two shares of one video in
             the same second by different methods both survive. ``tz_offset`` is deliberately NOT in the
             key: the same event re-donated with a different supplied zone
             is the same event, and the newest donation's offset should win
             rather than both rows surviving (a re-donation with a corrected
             zone would otherwise double every overlapping row).

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
            min_shared_seconds: A pair sharing fewer distinct seconds than
                this is never clustered, whatever its ratio. The ratio alone
                lets a five-event browser capture coinciding with one second
                of a large export reach 20 %; three shared seconds is the
                smallest overlap a genuine re-donation of a ten-event file
                can show.
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
            logger.warning(
                f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot identify similar file content. Please process data first."
            )
            return {}

        if len(self.data) == 0:
            return {}

        # 1. Per-raw_file timestamp sets at second resolution.
        seconds = (self.data["utc_timestamp"].astype("int64[pyarrow]") // 1_000_000_000).astype(
            "int64"
        )
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
            for b in raw_files[i + 1 :]:
                ts_b = ts_sets[b]
                denom = min(len(ts_a), len(ts_b))
                if denom == 0:
                    continue
                shared = len(ts_a & ts_b)
                if shared < min_shared_seconds:
                    continue
                if shared / denom > overlap_threshold:
                    union(a, b)

        clusters: dict[str, list[str]] = {}
        for f in raw_files:
            clusters.setdefault(find(f), []).append(f)

        multi_clusters = [files for files in clusters.values() if len(files) > 1]

        # 3. For each multi-file cluster, pick canonical collection_id from
        # the raw_file with the latest ts_added_to_dataset.
        canonical_map: dict[str, str] = {}  # raw_file -> canonical collection_id
        cid_remap: dict[str, str] = {}  # old_collection_id -> new_collection_id
        if multi_clusters:
            latest_per_file = self.data.groupby("raw_file", observed=True)[
                "ts_added_to_dataset"
            ].max()
            collection_id_per_file = self.data.groupby("raw_file", observed=True)[
                "collection_id"
            ].first()
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
        # A share's method is part of its identity: a chat send and a link
        # copy of one video in the same second are two shares. The " ×n"
        # record count is not, so a re-donation still meets its older copy.
        rows_before = len(self.data)
        if "extra_data" in self.data.columns:
            is_share = (self.data["activity_type"] == "share").fillna(False)
            share_key = pd.Series("", index=self.data.index, dtype="string[pyarrow]")
            if is_share.any():
                share_key[is_share] = (
                    self.data.loc[is_share, "extra_data"]
                    .astype("string")
                    .map(share_method_base, na_action="ignore")
                    .fillna("")
                )
        else:
            share_key = pd.Series("", index=self.data.index, dtype="string[pyarrow]")
        self.data = (
            self.data.assign(_share_key=share_key)
            .sort_values("ts_added_to_dataset", kind="mergesort")
            .drop_duplicates(
                subset=["collection_id", "item_id", "utc_timestamp", "activity_type", "_share_key"],
                keep="last",
            )
            .drop(columns="_share_key")
            .copy()
        )
        if self.verbose and rows_before > len(self.data):
            logger.info(
                f"Deduped {rows_before - len(self.data):,} overlapping rows within clusters."
            )

        return cid_remap

    def add_local_time_features(self) -> None:
        df = self.data

        # A refresh with nothing ingested (fresh install, all files pending)
        # leaves an empty frame with no columns — nothing to derive.
        if len(df) == 0 or "tz_offset" not in df.columns:
            return

        offset_timedelta = pd.to_timedelta(df["tz_offset"], unit="h")
        df["local_timestamp"] = df["utc_timestamp"] + offset_timedelta

        ts = df["local_timestamp"]

        iso = ts.dt.isocalendar()  # DataFrame: year, week, day
        iso["day"] = iso["day"].map(transforms.WEEKDAY_MAPPER)
        iso["year_week"] = iso["year"].astype(str) + "-" + iso["week"].astype(str)

        # Assign as plain lists, then convert explicitly, so these columns end up
        # pyarrow-backed rather than object dtype.
        df["local_weekday"] = iso["day"].to_list()
        df["local_weekday"] = df["local_weekday"].convert_dtypes(dtype_backend="pyarrow")
        df["local_week"] = iso["year_week"].to_list()
        df["local_week"] = df["local_week"].convert_dtypes(dtype_backend="pyarrow")

        local_hour = ts.dt.hour.astype("uint8[pyarrow]")
        # The activity contract declares local_hour, so it is stored as well as
        # used to derive the day segment.
        df["local_hour"] = local_hour

        df["local_day_segment"] = local_hour.map(transforms._day_segment_from_hour).convert_dtypes(
            dtype_backend="pyarrow"
        )

        df["local_date"] = ts.dt.date.astype("date32[pyarrow]")

    def add_session_ids(self, gap_threshold_s: int | None = None) -> None:
        """Assign a persistent sitting-level ``session_id`` to every activity.

        Thin wrapper around :func:`assign_session_ids` (``None`` gap reads
        ``[sessions] session_gap_s`` from the config). Call this *after*
        sub-collections are migrated, so the full per-collection sequence is in
        ``self.data`` (a sitting may span multiple raw files). Persisted by
        ``save_processed`` alongside the local-time features.
        """
        self.data = transforms.assign_session_ids(self.data, gap_threshold_s=gap_threshold_s)

    def _standardize(self):
        """
        Ensures the dataframe has all required columns and correct dtypes.
        """
        df = self.data.copy()

        df["source_platform"] = self.source_platform
        df["data_source"] = self.data_source

        if "collection_id" not in df.columns:
            if self.collection_id is not None:
                df["collection_id"] = self.collection_id
            elif "raw_file" in df.columns:
                logger.warning(
                    "No collection_id on the rows being standardized; falling back "
                    "to the raw filename. Uploads through the Hub always carry a "
                    "manifest collection_id — this is a legacy path."
                )
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
                        logger.warning(
                            f"Error casting {col} to {dtype}: {e}. Trying fyp.types.convert_dtypes_to_pyarrow."
                        )
                    # Fallback to the robust converter
                    # converting specific column to pyarrow backed using the helper
                    # Note: convert_dtypes_to_pyarrow works on DF, but we can try to apply it to the column or the whole DF later

        # Use the robust converter for the whole DF for good measure to ensure everything is pyarrow backed where possible
        # and specifically fixing complex types if any
        try:
            df = convert_dtypes_to_pyarrow(df, verbose=False)
        except Exception as e:
            if self.verbose:
                logger.warning(f"Warning: convert_dtypes_to_pyarrow failed: {e}")

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
        df.sort_values("utc_timestamp", inplace=True, kind="mergesort")
        df.reset_index(drop=True, inplace=True)

        self.data = df.copy()


class ForYouCollection(ingestion_ledger.IngestionLedgerMixin, ForYouBaseCollection):
    def __init__(self, collection_id: str = None, verbose: bool = False):
        super().__init__(collection_id, verbose)
        self.source_platform = "all"
        self.data_source = "foryou"
        self.collections = []
        self.ledger_filename = ingestion_ledger.INGESTION_LEDGER_FILENAME
        self.ledger: dict = {"schema_version": 1, "files": {}}
        self._load_ledger()

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
            storage_location=self.processed_storage_location, filename=fn, verbose=False
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
        scrape-side backfill in ``fyp.scrape.consolidate.consolidate_and_save_scrape_data``).
        """
        default_platform = (
            _scrape_contract.default_platform(_scrape_contract.load_contract()) or "tiktok"
        )
        if "source_platform" not in self.data.columns:
            self.data["source_platform"] = pd.NA
        n_missing = int(self.data["source_platform"].isna().sum())
        if n_missing:
            logger.info(
                f"Backfilling source_platform='{default_platform}' on {n_missing:,} pre-column activity row(s)."
            )
        self.data["source_platform"] = (
            self.data["source_platform"].fillna(default_platform).astype("string[pyarrow]")
        )

    def _backfill_play_duration(self) -> None:
        """Recompute ``play_duration`` for platforms ingested before it went base (self-heal).

        IG/YT rows ingested while ``play_duration`` was TikTok-only carry all-NA
        values, yet the forward-delta derivation needs nothing beyond the
        persisted ``utc_timestamp`` / ``activity_type`` / ``item_id`` per
        ``raw_file``. Per-file sequences may have gaps where the merge dedupe
        kept a newer donation's copy of a row, so a backfilled duration can
        run to the next surviving row. Recomputes only platform groups whose play rows are ALL NA —
        already-derived platforms (TikTok) are untouched and repeat runs are
        no-ops.
        """
        if "raw_file" not in self.data.columns or "activity_type" not in self.data.columns:
            return
        if "play_duration" not in self.data.columns:
            self.data["play_duration"] = pd.Series(
                pd.NA, index=self.data.index, dtype="int64[pyarrow]"
            )

        for platform, grp in self.data.groupby("source_platform", dropna=False):
            grp_plays = grp[grp["activity_type"] == "play"]
            if len(grp_plays) == 0 or grp_plays["play_duration"].notna().any():
                continue
            logger.info(f"Backfilling play_duration for {len(grp):,} '{platform}' activity row(s).")
            for _, file_grp in grp.groupby("raw_file", dropna=False):
                ordered = file_grp.sort_values("utc_timestamp", kind="mergesort")
                recomputed = transforms.derive_play_duration(ordered)
                self.data.loc[ordered.index, "play_duration"] = recomputed[
                    "play_duration"
                ].set_axis(ordered.index)
                self.data.loc[ordered.index, "extra_data"] = recomputed["extra_data"].set_axis(
                    ordered.index
                )
        self.data["play_duration"] = self.data["play_duration"].astype("int64[pyarrow]")

    def process(self):
        if len(self.collections) == 0:
            logger.warning(
                "This ForYouCollection does not have any sub collections. You need to register a collection class first."
            )
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
                logger.warning(
                    "This ForYouCollection does not have any sub collections. You need to register a collection class first."
                )
            return
        if self.verbose:
            logger.info("Loading new raw data for the registered sub collections...")

        for collection in self.collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        if len(self.data) > 0:
            skip_these_raw_files = (
                self.data["raw_file"].unique().tolist() + self.discarded_raw_files
            )
            if self.verbose:
                logger.info(
                    f"Skipping {len(skip_these_raw_files):,} raw files that are already discarded or already in the collection."
                )
        else:
            skip_these_raw_files = self.discarded_raw_files

        # Files the sentinel quarantined earlier stay pending (manifest entry
        # kept, name in the ledger skip set) until reviewed — legitimate, not
        # a collision, so the sub-collections' tripwire must ignore them.
        held_for_review = {
            fn
            for fn, meta in (self.ledger.get("files") or {}).items()
            if (meta or {}).get("outcome") == "quarantined_structure"
        }
        for collection in self.collections:
            collection.load_raw(
                skip_these_raw_files=skip_these_raw_files, held_for_review=held_for_review
            )

        if self.verbose:
            logger.info(
                f"Done loading raw {sum([len(collection.data) for collection in self.collections]):,} rows for the registered sub collections."
            )

    def migrate_sub_collections(self):

        processed_collections = [
            collection for collection in self.collections if collection.state == "processed"
        ]

        if len(processed_collections) == 0:
            if self.verbose:
                logger.info("No processed sub collections to migrate. Nothing for me to do.")
            return

        if self.verbose:
            logger.info(
                f"Migrating {len(processed_collections):,} processed sub collections to the top..."
            )
            logger.info(f"There are {len(self.data):,} rows in the top collection already.")

        # Vertical concat via polars — stacks all processed sub-collections
        # into the top-level collection in a single parallel pass.
        # See fyp/core/polars_ops.py.
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
        # Kept for run_ingest_refresh: an older file whose rows a re-donation
        # replaced entirely leaves no row to say which collection it joined.
        self.last_cid_remap = dict(cid_remap or {})
        if cid_remap:
            apply_cid_remap_to_metadata(cid_remap, verbose=self.verbose)

        for collection in processed_collections:
            self.discarded_raw_files.extend(collection.discarded_raw_files)
        self.discarded_raw_files = list(set(self.discarded_raw_files))

        for collection in processed_collections:
            logger.info(
                f"Migrated {len(collection.data):,} activities from '{collection.source_platform}_{collection.data_source}'."
            )
            collection.data = pd.DataFrame()
            collection.state = "empty"

        if self.verbose:
            logger.info(
                f"Done migrating the sub collections. There are now {len(self.data):,} activities in the top collection. Sub collections are empty."
            )

    def save_processed(self):

        if self.state != "processed":
            logger.warning(
                f"Collection '{self.source_platform}_{self.data_source}' is not processed. Cannot save this data. Please process data first."
            )
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
                filename=f"{_collections_label()}_metadata.parquet",
            ):
                old_metadata = data_io.load_parquet(
                    storage_location=self.processed_storage_location,
                    filename=f"{_collections_label()}_metadata.parquet",
                    verbose=False,
                )

            self.stats = generate_collection_metadata(
                self.data,
                update_col=None,
                sort_by=None,
                verbose=True,
                save_to_disk_ok=False,
                load_from_disk=False,
            )

            if old_metadata is not None and not old_metadata.empty:
                # Carry over columns set outside the generator — but never
                # the demographic ones: those moved to user accounts and a
                # stale copy must not resurrect them.
                demographic = set(demographic_metadata_columns(old_metadata.columns))
                preserved_cols = [
                    c
                    for c in old_metadata.columns
                    if c not in self.stats.columns and c not in demographic
                ]
                if preserved_cols:
                    self.stats = pd.merge(
                        self.stats,
                        old_metadata[preserved_cols],
                        left_index=True,
                        right_index=True,
                        how="left",
                    )

            self.stats[("other", "accepted")] = True
            self.stats[("participants", "date")] = self.stats[("other", "ts_added_to_dataset")]
            self.stats = strip_demographic_columns(self.stats)

            data_io.save_parquet(
                df=self.stats,
                storage_location=self.processed_storage_location,
                filename=f"{_collections_label()}_metadata.parquet",
                asyncronous=False,
            )

            # activity data
            data_io.save_parquet(
                df=self.data,
                storage_location=self.processed_storage_location,
                filename=f"{_collections_label()}_recoded.parquet",
                asyncronous=False,
            )

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

        self.prune_manifests()


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
