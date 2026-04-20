import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_collection_metadata_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Regenerate collections_metadata.parquet from collections_recoded.parquet.

    Loads the full activity parquet (1+ GB) and runs generate_collection_metadata
    over it, then merges any preserved columns from the previous metadata file.
    Memory-heavy enough to OOM the data-hub, so this runs on the task-runner.
    """
    import fyp.data_io as data_io
    import pandas as pd
    from fyp.donations import generate_collection_metadata
    from fyp.organize_datasets import COLLECTIONS_LABEL

    _t_start = time.perf_counter()

    reporter.update_progress(0, "Loading existing metadata (for preserved columns)...")
    old_metadata = None
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
        old_metadata = data_io.load_parquet(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
            verbose=False,
        )
    _t_old = time.perf_counter() - _t_start

    reporter.update_progress(20, "Loading activity events parquet...")
    _t_phase = time.perf_counter()
    events_df = data_io.load_parquet(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        verbose=False,
    )
    if events_df is None or events_df.empty:
        reporter.fail("No events data found in collections_recoded.parquet")
        return None
    _t_events = time.perf_counter() - _t_phase
    reporter.log(f"Loaded {len(events_df):,} events ({_t_events:.1f}s)")

    reporter.update_progress(50, "Regenerating per-collection metadata...")
    _t_phase = time.perf_counter()
    result = generate_collection_metadata(
        collections_df=events_df,
        load_from_disk=False,
        verbose=False,
    )
    _t_generate = time.perf_counter() - _t_phase
    reporter.log(f"Regenerated metadata for {len(result):,} collections ({_t_generate:.1f}s)")

    if old_metadata is not None and not old_metadata.empty:
        preserved_cols = [c for c in old_metadata.columns if c not in result.columns]
        if preserved_cols:
            reporter.update_progress(
                80,
                f"Restoring {len(preserved_cols)} preserved column(s) from previous metadata...",
            )
            result = pd.merge(
                result,
                old_metadata[preserved_cols],
                left_index=True,
                right_index=True,
                how='left',
            )

    reporter.update_progress(90, "Saving metadata...")
    _t_phase = time.perf_counter()
    data_io.save_parquet(
        df=result,
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
        verbose=False,
    )
    _t_save = time.perf_counter() - _t_phase

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "events": int(len(events_df)),
        "collections": int(len(result)),
    })
    reporter.update_progress(
        100,
        f"Metadata regenerated for {len(result):,} collections ({_t_total:.0f}s).",
    )
    reporter.log(
        f"[TIMING] collection_metadata_refresh load_old={_t_old:.1f}s "
        f"load_events={_t_events:.1f}s generate={_t_generate:.1f}s "
        f"save={_t_save:.1f}s total={_t_total:.1f}s"
    )

    return None




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("collection_metadata_refresh")
    try:
        run_collection_metadata_refresh(reporter=reporter, task_args={})
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
