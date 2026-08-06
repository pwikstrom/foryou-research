import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter
from fyp.ingest import LEDGER_SKIP_OUTCOMES
from fyp.structure_sentinel import StructureSentinel, findings_digest


# Outcomes whose files we want to *show* as "previously skipped" in the UI.
# Mirrors ingest.LEDGER_SKIP_OUTCOMES — kept as a local alias because the UI
# might want to surface additional outcomes in future without changing the
# core ingest skip behaviour.
LEDGER_SKIP_OUTCOMES_FOR_UI = LEDGER_SKIP_OUTCOMES




def _per_file_counts(sub_collections) -> dict[str, dict]:
    """Snapshot per-file row counts across every sub-collection. Also records
    the (platform, source) of each file so the UI can show provenance."""
    out: dict[str, dict] = {}
    for sub in sub_collections:
        if sub.data is None or len(sub.data) == 0:
            continue
        for raw_file, count in sub.data.groupby("raw_file", observed=True).size().items():
            out[str(raw_file)] = {
                "rows": int(count),
                "platform": sub.source_platform,
                "source": sub.data_source,
            }
    return out




def _build_per_file_summary(
    main_collection,
    raw_counts: dict[str, dict],
    processed_counts: dict[str, dict],
    discarded_at_load: set[str],
    existing_raw_files: set[str],
    quarantined: dict[str, dict] | None = None,
    load_failed: dict[str, dict] | None = None,
    file_stats: dict[str, dict] | None = None,
) -> list[dict]:
    """For each new raw_file, produce a row describing what happened.

    Outcomes:
      - ``load_failed``: load_single_raw raised (unreadable/unsupported file);
        the file stays pending and is retried on the next refresh. The error
        message is surfaced in ``notes``.
      - ``discarded_at_load``: file failed the min-row check in load_raw.
      - ``quarantined_structure``: the file's structure or parse-output stats
        deviated from the learned baseline; withheld pending admin review.
      - ``fully_deduped``: every row collided with an existing collection.
      - ``merged_with_existing``: this file's rows joined an existing
        collection that also contains one or more prior raw_files; the
        canonical collection_id may now point at this file.
      - ``added_as_new``: standalone collection, no overlap with anything.
    """
    quarantined = quarantined or {}
    load_failed = load_failed or {}
    file_stats = file_stats or {}
    final_df = main_collection.data
    candidate_files = set(raw_counts) | discarded_at_load | set(quarantined) | set(load_failed)
    summary: list[dict] = []

    for rf in sorted(candidate_files):
        info = raw_counts.get(rf) or processed_counts.get(rf) or {}
        platform = info.get("platform")
        source = info.get("source")
        stats = file_stats.get(rf) or {}
        # The load-loop count is authoritative (raw_counts is derived from the
        # surviving frame, so a too-small file's rows are otherwise lost).
        raw_rows = raw_counts.get(rf, {}).get("rows", 0) or int(stats.get("raw_rows") or 0)
        processed_rows = processed_counts.get(rf, {}).get("rows", 0)
        dropped = stats.get("dropped") or {}

        if rf in load_failed:
            fail = load_failed[rf]
            summary.append({
                "filename": rf,
                "platform": platform or fail.get("platform"),
                "source": source or fail.get("source"),
                "raw_rows": 0,
                "processed_rows": 0,
                "final_rows": 0,
                "outcome": "load_failed",
                "canonical_collection_id": None,
                "merged_with_siblings": [],
                "deduped_rows": 0,
                "dropped": {},
                "notes": fail.get("error"),
            })
            continue

        if rf in quarantined:
            verdict = quarantined[rf]
            summary.append({
                "filename": rf,
                "platform": platform or verdict.get("platform"),
                "source": source or verdict.get("source"),
                "raw_rows": raw_rows or int(verdict.get("raw_stats", {}).get("raw_rows") or 0),
                "processed_rows": processed_rows,
                "final_rows": 0,
                "outcome": "quarantined_structure",
                "canonical_collection_id": None,
                "merged_with_siblings": [],
                "deduped_rows": 0,
                "dropped": dropped,
                "notes": findings_digest(verdict.get("findings") or []),
            })
            continue

        if rf in discarded_at_load:
            summary.append({
                "filename": rf,
                "platform": platform,
                "source": source,
                "raw_rows": raw_rows,
                "processed_rows": 0,
                "final_rows": 0,
                "outcome": "discarded_at_load",
                "canonical_collection_id": None,
                "merged_with_siblings": [],
                "deduped_rows": 0,
                "dropped": dropped,
            })
            continue

        sub_df = final_df[final_df["raw_file"] == rf]
        final_rows = int(len(sub_df))

        if final_rows == 0:
            outcome = "fully_deduped"
            canonical_cid = None
            siblings = []
        else:
            canonical_cid = str(sub_df["collection_id"].iloc[0])
            cluster_df = final_df[final_df["collection_id"] == canonical_cid]
            sibling_files = [
                str(s) for s in cluster_df["raw_file"].dropna().unique().tolist()
                if s != rf
            ]
            existing_siblings = [s for s in sibling_files if s in existing_raw_files]
            siblings = sibling_files
            if existing_siblings:
                outcome = "merged_with_existing"
            else:
                outcome = "added_as_new"

        summary.append({
            "filename": rf,
            "platform": platform,
            "source": source,
            "raw_rows": raw_rows,
            "processed_rows": processed_rows,
            "final_rows": final_rows,
            "outcome": outcome,
            "canonical_collection_id": canonical_cid,
            "merged_with_siblings": siblings,
            "deduped_rows": max(processed_rows - final_rows, 0),
            "dropped": dropped,
        })

    return summary




def run_ingest_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Run the full ingestion refresh pipeline as a Cloud Task.

    Loads the existing processed parquet, ingests any new raw uploads from
    every registered collection subclass, deduplicates, regenerates metadata
    and writes everything back. This is memory-heavy (the activity parquet
    can be 1+ GB) so it must run on the task-runner service rather than the
    web server.
    """
    from fyp.ingest import get_main_collection

    _t_start = time.perf_counter()

    reporter.update_progress(0, "Loading existing processed activity data...")
    main_collection = get_main_collection(verbose=True)
    # Structure-drift sentinel: fingerprints every new file against the learned
    # per-(platform, source) baseline and quarantines deviants (Phase A inside
    # load_raw, Phase B on the processed frames below).
    sentinel = StructureSentinel()
    for sub in main_collection.collections:
        sub.sentinel = sentinel
    main_collection.load_processed()
    rows_before = len(main_collection.data)
    existing_raw_files = (
        set(str(rf) for rf in main_collection.data["raw_file"].dropna().unique().tolist())
        if rows_before > 0 else set()
    )
    discarded_before = set(main_collection.discarded_raw_files)
    _t_load = time.perf_counter() - _t_start
    reporter.log(f"Loaded {rows_before:,} existing processed activities ({_t_load:.1f}s)")

    reporter.update_progress(20, "Loading raw uploads from registered subclasses...")
    _t_phase = time.perf_counter()
    main_collection.load_raw()
    raw_counts = _per_file_counts(main_collection.collections)
    raw_rows = sum(c["rows"] for c in raw_counts.values())
    discarded_after_load: set[str] = set()
    for sub in main_collection.collections:
        discarded_after_load.update(str(f) for f in sub.discarded_raw_files)
    discarded_at_load = discarded_after_load - discarded_before
    _t_raw = time.perf_counter() - _t_phase
    reporter.log(
        f"Loaded {raw_rows:,} new raw activities across {len(raw_counts)} file(s); "
        f"{len(discarded_at_load)} skipped as too-few-rows ({_t_raw:.1f}s)"
    )

    # Persist donated item metadata (caption/title/author) as a per-platform
    # scrape enrichment seed while the raw seed_* columns are still present (they
    # are dropped by process()). Generic: a no-op for collections that supply no
    # seed columns (e.g. TikTok).
    for sub in main_collection.collections:
        try:
            sub.save_enrichment_seed()
        except Exception as exc:
            reporter.log(f"Enrichment-seed capture failed for {sub.source_platform}_{sub.data_source}: {exc}")

    reporter.update_progress(40, "Processing raw activities...")
    _t_phase = time.perf_counter()
    main_collection.process()
    processed_counts = _per_file_counts(main_collection.collections)
    _t_process = time.perf_counter() - _t_phase
    reporter.log(f"Processed sub collections ({_t_process:.1f}s)")

    # Structure-drift Phase B: parse-output sanity + cross-upload drift checks
    # on each file's processed rows. A stat-outlier verdict drops the file's
    # rows before migration so they never reach the main parquet. Sentinel
    # failures never block ingestion.
    for sub in main_collection.collections:
        if sub.data is None or len(sub.data) == 0 or "raw_file" not in sub.data.columns:
            continue
        drop_files: list[str] = []
        try:
            for rf, df_file in sub.data.groupby("raw_file", observed=True):
                verdict = sentinel.check_processed(sub, str(rf), df_file)
                if verdict["status"] == "quarantined":
                    drop_files.append(str(rf))
                    sub.quarantined_this_run[str(rf)] = verdict
        except Exception as exc:
            reporter.log(f"Structure Phase-B check failed for {sub.source_platform}_{sub.data_source}: {exc}")
        if drop_files:
            sub.data = sub.data[~sub.data["raw_file"].isin(drop_files)].copy()
            reporter.log(
                f"Quarantined {len(drop_files)} file(s) from "
                f"{sub.source_platform}_{sub.data_source} for structure drift."
            )

    quarantined_files: dict[str, dict] = {}
    load_failed_files: dict[str, dict] = {}
    for sub in main_collection.collections:
        quarantined_files.update(sub.quarantined_this_run)
        for fn, err in sub.load_failed_this_run.items():
            load_failed_files[fn] = {
                "error": err,
                "platform": sub.source_platform,
                "source": sub.data_source,
            }
    if load_failed_files:
        reporter.log(
            f"{len(load_failed_files)} file(s) could not be read and stay pending: "
            + "; ".join(f"{fn} ({v['error']})" for fn, v in sorted(load_failed_files.items()))
        )

    reporter.update_progress(60, "Merging into main collection...")
    _t_phase = time.perf_counter()
    main_collection.migrate_sub_collections()
    rows_after = len(main_collection.data)
    _t_migrate = time.perf_counter() - _t_phase
    reporter.log(
        f"Merged into main collection: {rows_before:,} → {rows_after:,} "
        f"(+{rows_after - rows_before:,}) ({_t_migrate:.1f}s)"
    )

    # Per-file intake stats captured by the base-class load/process loop:
    # true raw row counts (incl. too-small discards) + drop-reason breakdowns.
    file_stats: dict[str, dict] = {}
    for sub in main_collection.collections:
        file_stats.update(getattr(sub, "file_stats_this_run", {}) or {})

    per_file_summary = _build_per_file_summary(
        main_collection,
        raw_counts=raw_counts,
        processed_counts=processed_counts,
        discarded_at_load=discarded_at_load,
        existing_raw_files=existing_raw_files,
        quarantined=quarantined_files,
        load_failed=load_failed_files,
        file_stats=file_stats,
    )

    # Record the active activity-contract version once per ingest run (idempotent,
    # non-raising) so the registry captures the schema that stamped these rows.
    from fyp import activity_versioning
    activity_versioning.ensure_active_version_registered()

    reporter.update_progress(75, "Adding local time features...")
    _t_phase = time.perf_counter()
    main_collection.add_local_time_features()
    _t_local = time.perf_counter() - _t_phase
    reporter.log(f"Added local time features ({_t_local:.1f}s)")

    reporter.update_progress(78, "Assigning session ids...")
    _t_phase = time.perf_counter()
    main_collection.add_session_ids()
    _t_sess = time.perf_counter() - _t_phase
    reporter.log(f"Assigned session ids ({_t_sess:.1f}s)")

    # Update the persistent ledger with the outcome of every file scanned this
    # run before saving. Files with skip-eligible outcomes (fully_deduped,
    # discarded_at_load, manually_excluded) will be skipped on future ingest
    # runs without being reloaded.
    main_collection.update_ledger(per_file_summary)

    # Learn the ingested files' fingerprints/stats into the baselines and
    # persist every verdict for the review UI. Never blocks the refresh.
    try:
        sentinel.commit(ingested_filenames={
            e["filename"] for e in per_file_summary
            if e.get("outcome") in ("added_as_new", "merged_with_existing")
        })
    except Exception as exc:
        reporter.log(f"Structure-sentinel commit failed: {exc}")

    reporter.update_progress(85, "Regenerating metadata and saving...")
    _t_phase = time.perf_counter()
    main_collection.save_processed()
    _t_save = time.perf_counter() - _t_phase
    reporter.log(f"Saved processed activities + metadata ({_t_save:.1f}s)")

    # Build the list of ledger entries that were SKIPPED this run (i.e. files
    # the ledger remembers from prior runs but didn't reload). Useful for the
    # UI's "previously skipped" section.
    summary_filenames = {entry["filename"] for entry in per_file_summary}
    skipped_previously = []
    for fn, meta in (main_collection.ledger.get("files") or {}).items():
        if fn in summary_filenames:
            continue
        outcome = (meta or {}).get("outcome")
        if outcome not in LEDGER_SKIP_OUTCOMES_FOR_UI:
            continue
        skipped_previously.append({
            "filename": fn,
            "outcome": outcome,
            "platform": meta.get("platform"),
            "source": meta.get("source"),
            "raw_rows": meta.get("raw_rows") or 0,
            "kept_rows": meta.get("kept_rows") or 0,
            "collection_id": meta.get("collection_id"),
            "merged_with_siblings": meta.get("merged_with_siblings") or [],
            "ts_first_seen": meta.get("ts_first_seen"),
            "ts_last_seen": meta.get("ts_last_seen"),
            "notes": meta.get("notes"),
        })
    skipped_previously.sort(key=lambda r: r["filename"])

    # Reconciliation: when newer rows from this run supersede older rows in
    # the same collection (via identify_similar_file_content's dedupe), the
    # net row delta is smaller than the sum of "Added"/"Merged" final_rows.
    # Compute the difference so the UI can explain why.
    contributed_rows = sum(
        int(e.get("final_rows") or 0) for e in per_file_summary
        if e.get("outcome") in ("added_as_new", "merged_with_existing")
    )
    rows_added_net = rows_after - rows_before
    rows_superseded = max(contributed_rows - rows_added_net, 0)

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_added": rows_added_net,
        "rows_contributed_by_new_files": contributed_rows,
        "rows_superseded_in_existing_collections": rows_superseded,
        "files_scanned_this_run": len(per_file_summary),
        "files_added": sum(1 for e in per_file_summary if e.get("outcome") == "added_as_new"),
        "files_merged_with_existing": sum(1 for e in per_file_summary if e.get("outcome") == "merged_with_existing"),
        "files_fully_deduped": sum(1 for e in per_file_summary if e.get("outcome") == "fully_deduped"),
        "files_discarded_at_load": sum(1 for e in per_file_summary if e.get("outcome") == "discarded_at_load"),
        "files_quarantined": sum(1 for e in per_file_summary if e.get("outcome") == "quarantined_structure"),
        "files_load_failed": sum(1 for e in per_file_summary if e.get("outcome") == "load_failed"),
        "files_skipped_previously": len(skipped_previously),
        "per_file_summary": per_file_summary,
        "skipped_previously": skipped_previously,
    })
    reporter.update_progress(100, f"Ingestion refresh complete ({_t_total:.0f}s).")
    reporter.log(
        f"[TIMING] ingest_refresh load={_t_load:.1f}s raw={_t_raw:.1f}s "
        f"process={_t_process:.1f}s migrate={_t_migrate:.1f}s local={_t_local:.1f}s "
        f"save={_t_save:.1f}s total={_t_total:.1f}s"
    )

    return None




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    run_worker(run_ingest_refresh, "ingest_refresh")
