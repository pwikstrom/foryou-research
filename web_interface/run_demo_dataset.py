import sys
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_demo_dataset(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Generate + install the synthetic demo dataset (S4 demo study).

    One-click equivalent of ``scripts/generate_demo_dataset.py --write``:
    builds the deterministic synthetic corpus, uploads the donor DDP JSONs
    with manifest entries, writes the scrape batch through the real
    canonicalize/recode path, drops the raw annotation file, and creates the
    demo study definition when absent. Idempotent — fixed output filenames
    mean a re-run overwrites the same artifacts.

    Afterwards the admin still runs the normal Ingest refresh and
    Consolidate & Refresh (which builds the study via its downstream
    pipeline).
    """
    from fyp.ingest.demo_dataset import (
        DEFAULT_AS_OF,
        DEFAULT_DAYS,
        DEFAULT_DONORS,
        DEFAULT_SEED,
        DEMO_STUDY_NAME,
        ensure_demo_study,
        generate,
        write_to_store,
    )

    task_args = task_args or {}
    seed = int(task_args.get("seed", DEFAULT_SEED))
    donors = int(task_args.get("donors", DEFAULT_DONORS))
    days = int(task_args.get("days", DEFAULT_DAYS))
    as_of = str(task_args.get("as_of", DEFAULT_AS_OF))

    reporter.update_progress(5, f"Generating synthetic corpus (seed {seed}, {donors} donors x {days} days)...")
    result = generate(seed=seed, donors=donors, days=days, as_of=as_of)
    n_items = len(result["items"])
    reporter.log(f"Generated {len(result['donor_files'])} donor files over {n_items} items.")

    reporter.update_progress(30, "Writing donor files, scrape batch and annotations to the data store...")
    write_to_store(result)

    reporter.update_progress(85, "Ensuring the demo study definition exists...")
    created = ensure_demo_study(result["collection_ids"], days=days, as_of=as_of)
    reporter.log(f"Demo study definition {'created' if created else 'already present'}: {DEMO_STUDY_NAME}")

    summary = {
        "donor_files": len(result["donor_files"]),
        "items": n_items,
        "collections": result["collection_ids"],
        "study_created": created,
        "next_steps": "Run Ingest refresh, then Consolidate & Refresh.",
    }
    reporter.update_progress(100, "Demo dataset installed. Next: Ingest refresh, then Consolidate & Refresh.")
    reporter.complete(summary)
    return summary


if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    run_worker(run_demo_dataset, "demo_dataset")
