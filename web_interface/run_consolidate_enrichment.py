import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_consolidate_enrichment(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Consolidate enrichment data (scrapes + machine annotations)."""
    import fyp.data_io as data_io
    from fyp.organize_datasets import (
        consolidate_enrichment_data, SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL
    )

    _t_run_start = time.perf_counter()

    # Stage 1: Count new files before consolidation
    reporter.update_progress(0, "Counting new files...")
    _t_phase = time.perf_counter()

    known_scrape: set[str] = set()
    known_annotation: set[str] = set()
    if data_io.exists(storage_location="recoded", filename="consolidated_enrichment_files.json"):
        meta_before = data_io.load_json(
            storage_location="recoded", filename="consolidated_enrichment_files.json"
        )
        known_scrape = set(meta_before.get(SCRAPES_LABEL, {}).get("filenames", []))
        known_annotation = set(
            meta_before.get(MACHINE_ANNOTATIONS_LABEL, {}).get("filenames", [])
        )

    current_scrape = {
        fn for fn in data_io.listdir(storage_location="scrape")
        if fn.startswith(SCRAPES_LABEL) and fn.endswith(".parquet")
    }
    current_annotation = {
        fn for fn in data_io.listdir(storage_location="machine_annotations_refined")
        if fn.startswith(MACHINE_ANNOTATIONS_LABEL) and fn.endswith(".parquet")
    }

    new_scrape_count = len(current_scrape - known_scrape)
    new_annotation_count = len(current_annotation - known_annotation)

    _t_discover = time.perf_counter() - _t_phase

    # Stage 2: Run consolidation
    reporter.update_progress(10, "Consolidating annotations...")
    _t_phase = time.perf_counter()

    force = bool(task_args.get("force_consolidation")) if task_args else False
    result = consolidate_enrichment_data(force_consolidation=force, verbose=False)
    had_new_data = result.get("had_new_data", False) if result else False
    impact = result.get("impact") if result else None

    _t_consolidate = time.perf_counter() - _t_phase

    # Stage 3: Emit results
    now_iso = datetime.now(timezone.utc).isoformat()

    data_payload: dict = {
        "had_new_data": had_new_data,
        "new_scrape_files": new_scrape_count,
        "new_annotation_files": new_annotation_count,
        "last_status_refresh": now_iso,
        # Always record when consolidation was last run — the UI warning uses
        # this timestamp to decide whether the scraper/annotator has completed
        # more recently than the last consolidation. had_new_data in the same
        # payload separately captures whether anything actually changed.
        "last_consolidation": now_iso,
        # Always emit consolidation_impact (None when nothing changed) so the
        # UI panel clears after a no-op run. emit_data merges into stats, so
        # omitting the key would leave the previous run's impact in place.
        "consolidation_impact": impact if impact else None,
    }

    reporter.emit_data(data_payload)
    _t_total = time.perf_counter() - _t_run_start
    reporter.log(
        f"[TIMING] consolidate_enrichment discover={_t_discover:.2f}s "
        f"consolidate={_t_consolidate:.2f}s total={_t_total:.2f}s "
        f"new_scrape={new_scrape_count} new_anno={new_annotation_count} "
        f"had_new_data={had_new_data}"
    )
    reporter.log("Consolidation finished.")




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("consolidate_enrichment")
    try:
        run_consolidate_enrichment(reporter=reporter)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
