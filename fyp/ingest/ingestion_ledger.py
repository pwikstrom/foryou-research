"""The per-file ingestion ledger: its storage names and outcomes, and the ledger methods of the main collection.

Every raw file ever scanned has a ledger entry recording its outcome, so the
next ingest skips files whose outcome is in :data:`LEDGER_SKIP_OUTCOMES`
without reloading them. :class:`IngestionLedgerMixin` holds the load / save /
update / prune methods that ``ForYouCollection`` inherits.
"""

import copy
from datetime import UTC, datetime

import fyp.core.data_io as data_io
from fyp.core.logging_setup import get_logger
from fyp.ingest.raw_names import MANIFEST_PROVENANCE_KEYS, provenance_from_manifest

from . import structure_sentinel as _structure_sentinel

logger = get_logger(__name__)


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

# A pending manifest entry whose stored name is already in the skip set
# (load_raw's tripwire). Reported in the run result; never written to the
# ledger under that name (it belongs to the older file) and never pruned
# from the manifest.
BLOCKED_OUTCOME = "blocked_name_collision"


class IngestionLedgerMixin:
    """The ingestion-ledger methods of :class:`fyp.ingest.base.ForYouCollection`.

    Loading, upgrading, updating, merging and saving the per-file ledger, and
    pruning the upload manifests of files an ingest consumed. Relies on the
    collection state ``ForYouCollection`` sets up: ``ledger``,
    ``ledger_filename``, ``processed_storage_location``,
    ``discarded_raw_files``, ``collections`` and ``verbose``.
    """

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
        # What storage held when we loaded: save_ledger writes back only what
        # THIS process changed since, so two processes editing the ledger at
        # once (an ingest run and the hub's review buttons) don't erase each
        # other's writes.
        self._ledger_snapshot = copy.deepcopy(ledger.get("files") or {})
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
            fn for fn, meta in files.items() if (meta or {}).get("outcome") in LEDGER_SKIP_OUTCOMES
        ]
        merged = list(dict.fromkeys(ledger_skips + list(self.discarded_raw_files)))
        self.discarded_raw_files[:] = merged

    def update_ledger(self, per_file_summary: list[dict]) -> None:
        """Update the in-memory ledger with the outcomes from a freshly
        completed ingestion. Preserves ``ts_first_seen`` for previously known
        files and stamps ``ts_last_seen`` on every entry touched.

        Args:
            per_file_summary: list of dicts from
                ``run_ingest_refresh.build_per_file_summary``.
        """
        now = datetime.now(UTC).isoformat()
        files = self.ledger.setdefault("files", {})
        manifest_meta: dict[str, dict] = {}
        for collection in getattr(self, "collections", None) or []:
            manifest_meta.update(getattr(collection, "manifest_this_run", {}) or {})
        for entry in per_file_summary:
            fn = entry.get("filename")
            if not fn:
                continue
            # A blocked entry names a file the ledger already describes (the
            # older file that owns that name); writing it here would overwrite
            # that record. The block is reported in the run result instead.
            if entry.get("outcome") == BLOCKED_OUTCOME:
                continue
            existing = files.get(fn) or {}
            files[fn] = {
                "outcome": entry.get("outcome"),
                "raw_rows": int(entry.get("raw_rows") or 0),
                "processed_rows": int(entry.get("processed_rows") or 0),
                "kept_rows": int(entry.get("final_rows") or 0),
                # None (not 0) when the caller never computed it — entries
                # written before this field existed, and the secondary writers
                # that record an outcome without processing a frame. The UI
                # renders that as "—" rather than claiming zero viewing.
                "play_rows": entry.get("play_rows"),
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
            # Provenance from the upload-time manifest entry (original
            # filename, uploader, timezone, review flag): copied here because
            # the manifest entry is pruned once the file is resolved.
            for k, v in provenance_from_manifest(manifest_meta.get(fn)).items():
                files[fn][k] = v
            for k in MANIFEST_PROVENANCE_KEYS:
                if k not in files[fn] and existing.get(k) is not None:
                    files[fn][k] = existing[k]
        self._refresh_discarded_from_ledger()

    def prune_manifests(self) -> None:
        """Drop ingestion-manifest entries this run resolved.

        An entry leaves the manifest only when THIS run consumed its file
        (opened it and ingested, deduped or discarded it — quarantined and
        unreadable files stay pending) or when the raw object is gone. Never
        by matching names against the whole dataset: that is how a pending
        upload whose name collided with an old raw file would be pruned as
        "processed" without being read. Entries the tripwire
        blocked are always kept.
        """
        MANIFEST_FILENAME = "ingestion_manifest.json"
        for collection in self.collections:
            if collection.raw_path is None:
                continue
            if not data_io.exists(storage_location=collection.raw_path, filename=MANIFEST_FILENAME):
                continue
            manifest = (
                data_io.load_json(
                    storage_location=collection.raw_path, filename=MANIFEST_FILENAME, verbose=False
                )
                or {}
            )
            consumed = self._files_consumed_this_run(collection)
            blocked = set(getattr(collection, "blocked_this_run", {}) or {})
            trimmed = {}
            for fn, meta in manifest.items():
                if fn in blocked:
                    trimmed[fn] = meta
                    continue
                if fn in consumed:
                    continue
                if not data_io.exists(storage_location=collection.raw_path, filename=fn):
                    logger.warning(
                        f"Dropping manifest entry '{fn}' from {collection.raw_path}: "
                        f"the raw file no longer exists."
                    )
                    continue
                trimmed[fn] = meta
            if len(trimmed) < len(manifest):
                data_io.save_json(
                    data=trimmed,
                    storage_location=collection.raw_path,
                    filename=MANIFEST_FILENAME,
                    verbose=False,
                )
                if self.verbose:
                    logger.info(
                        f"Cleaned {len(manifest) - len(trimmed)} processed entries from {collection.raw_path}/{MANIFEST_FILENAME}"
                    )

    @staticmethod
    def _files_consumed_this_run(collection) -> set[str]:
        """Stored names a sub-collection opened and resolved in this run:
        every file with load stats, minus the ones held back for review
        (quarantined) or left pending for retry (unreadable)."""
        stats = getattr(collection, "file_stats_this_run", {}) or {}
        held = set(getattr(collection, "quarantined_this_run", {}) or {})
        held |= set(getattr(collection, "load_failed_this_run", {}) or {})
        return {fn for fn in stats if fn not in held}

    def save_ledger(self) -> None:
        """Persist the ledger, merging this process's changes into storage.

        The ledger is edited from two places at once: an ingest run holds it
        in memory for a minute and writes it at the end, and the hub's
        review buttons (approve / reject / unskip) edit it on click. A plain
        overwrite lets whichever writes last win: approvals that land while a
        run is saving are undone by the run's stale copy, which puts the
        ``quarantined_structure`` entries straight back and leaves the files
        invisible (verdict approved, ledger quarantined, upload pending).

        So the stored ledger is re-read and only what changed since this
        process loaded it is applied: entries added or rewritten here win,
        entries removed here are removed, everything else keeps whatever
        storage holds now. One more rule for the run side: a quarantine this
        run recorded is NOT written when the file's stored verdict shows an
        approval or rejection made after this run evaluated it — the admin's
        decision is the newer fact, and the ledger entry the review wanted
        (none, or ``manually_excluded``) is already in storage.
        """
        files = self.ledger.setdefault("files", {})
        snapshot = getattr(self, "_ledger_snapshot", None)
        merged = self._merge_ledger_into_storage(files, snapshot)
        self.ledger["files"] = merged
        self._ledger_snapshot = copy.deepcopy(merged)
        self._refresh_discarded_from_ledger()
        data_io.save_json(
            data=self.ledger,
            storage_location=self.processed_storage_location,
            filename=self.ledger_filename,
            verbose=False,
        )

    def _merge_ledger_into_storage(self, files: dict, snapshot: dict | None) -> dict:
        """Three-way merge of this process's ledger edits over the stored file.

        Args:
            files: This process's in-memory ledger entries.
            snapshot: The entries as loaded by this process, or None when the
                ledger was never loaded from storage (tests, fresh installs):
                then ``files`` is written as-is.

        Returns:
            The merged ``files`` mapping to persist.
        """
        if snapshot is None:
            return files
        stored = self._read_stored_ledger_files()
        if stored is None:
            return files
        merged = dict(stored)
        for fn in snapshot:
            if fn not in files:
                merged.pop(fn, None)
        reviewed_after = self._quarantines_reviewed_after_evaluation(files)
        for fn, entry in files.items():
            if snapshot.get(fn) == entry:
                continue
            if fn in reviewed_after:
                continue
            merged[fn] = entry
        return merged

    def _read_stored_ledger_files(self) -> dict | None:
        """The ``files`` mapping currently in storage, or None when unreadable."""
        try:
            if not data_io.exists(
                storage_location=self.processed_storage_location, filename=self.ledger_filename
            ):
                return {}
            stored = data_io.load_json(
                storage_location=self.processed_storage_location,
                filename=self.ledger_filename,
                verbose=False,
            )
        except Exception as exc:
            logger.warning(f"WARNING: could not re-read the ingestion ledger before saving: {exc}")
            return None
        if not isinstance(stored, dict) or not isinstance(stored.get("files"), dict):
            return {}
        return stored["files"]

    def _quarantines_reviewed_after_evaluation(self, files: dict) -> set[str]:
        """Stored names this run quarantined that an admin has since reviewed."""
        evaluated_at: dict[str, str] = {}
        for collection in getattr(self, "collections", None) or []:
            for fn, verdict in (getattr(collection, "quarantined_this_run", {}) or {}).items():
                evaluated_at[fn] = (verdict or {}).get("ts_evaluated") or ""
        candidates = {
            fn
            for fn, entry in files.items()
            if (entry or {}).get("outcome") == "quarantined_structure" and fn in evaluated_at
        }
        if not candidates:
            return set()
        try:
            stored_verdicts = _structure_sentinel.load_verdicts().get("files") or {}
        except Exception as exc:
            logger.warning(
                f"WARNING: could not read structure verdicts before saving the ledger: {exc}"
            )
            return set()
        return {
            fn
            for fn in candidates
            if _structure_sentinel.review_is_newer(stored_verdicts.get(fn), evaluated_at[fn])
        }

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
        entry["ts_last_seen"] = datetime.now(UTC).isoformat()
        if note:
            entry["notes"] = note
        self._refresh_discarded_from_ledger()
        return True
