import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


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
    _t_load = time.perf_counter() - _t_start
    reporter.log(f"Loaded {rows_before:,} existing processed activities ({_t_load:.1f}s)")

    reporter.update_progress(20, "Loading raw uploads from registered subclasses...")
    _t_phase = time.perf_counter()
    main_collection.load_raw()
    raw_rows = sum(len(c.data) for c in main_collection.collections)
    _t_raw = time.perf_counter() - _t_phase
    reporter.log(f"Loaded {raw_rows:,} new raw activities ({_t_raw:.1f}s)")

    reporter.update_progress(40, "Processing raw activities...")
    _t_phase = time.perf_counter()
    main_collection.process()
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
