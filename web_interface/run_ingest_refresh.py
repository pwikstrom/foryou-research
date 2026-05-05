import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter




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
) -> list[dict]:
    """For each new raw_file, produce a row describing what happened.

    Outcomes:
      - ``discarded_at_load``: file failed the min-row check in load_raw.
      - ``fully_deduped``: every row collided with an existing collection.
      - ``merged_with_existing``: clustered with one or more existing
        raw_files; the cluster's canonical collection_id may now point at
        this file.
      - ``added_as_new``: standalone collection, no overlap with anything.
    """
    final_df = main_collection.data
    candidate_files = set(raw_counts) | discarded_at_load
    summary: list[dict] = []

    for rf in sorted(candidate_files):
        info = raw_counts.get(rf) or processed_counts.get(rf) or {}
        platform = info.get("platform")
        source = info.get("source")
        raw_rows = raw_counts.get(rf, {}).get("rows", 0)
        processed_rows = processed_counts.get(rf, {}).get("rows", 0)

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

    reporter.update_progress(40, "Processing raw activities...")
    _t_phase = time.perf_counter()
    main_collection.process()
    processed_counts = _per_file_counts(main_collection.collections)
    _t_process = time.perf_counter() - _t_phase
    reporter.log(f"Processed sub collections ({_t_process:.1f}s)")

    reporter.update_progress(60, "Merging into main collection...")
    _t_phase = time.perf_counter()
    main_collection.migrate_sub_collections()
    rows_after = len(main_collection.data)
    _t_migrate = time.perf_counter() - _t_phase
    reporter.log(
        f"Merged into main collection: {rows_before:,} → {rows_after:,} "
        f"(+{rows_after - rows_before:,}) ({_t_migrate:.1f}s)"
    )

    per_file_summary = _build_per_file_summary(
        main_collection,
        raw_counts=raw_counts,
        processed_counts=processed_counts,
        discarded_at_load=discarded_at_load,
        existing_raw_files=existing_raw_files,
    )

    reporter.update_progress(75, "Adding local time features...")
    _t_phase = time.perf_counter()
    main_collection.add_local_time_features()
    _t_local = time.perf_counter() - _t_phase
    reporter.log(f"Added local time features ({_t_local:.1f}s)")

    reporter.update_progress(85, "Regenerating metadata and saving...")
    _t_phase = time.perf_counter()
    main_collection.save_processed()
    _t_save = time.perf_counter() - _t_phase
    reporter.log(f"Saved processed activities + metadata ({_t_save:.1f}s)")

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_added": rows_after - rows_before,
        "files_processed": len(raw_counts),
        "files_discarded_at_load": len(discarded_at_load),
        "per_file_summary": per_file_summary,
    })
    reporter.update_progress(100, f"Ingestion refresh complete ({_t_total:.0f}s).")
    reporter.log(
        f"[TIMING] ingest_refresh load={_t_load:.1f}s raw={_t_raw:.1f}s "
        f"process={_t_process:.1f}s migrate={_t_migrate:.1f}s local={_t_local:.1f}s "
        f"save={_t_save:.1f}s total={_t_total:.1f}s"
    )

    return None




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("ingest_refresh")
    try:
        run_ingest_refresh(reporter=reporter, task_args={})
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
