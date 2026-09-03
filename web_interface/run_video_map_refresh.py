"""Video map refresh: cluster the embedding store into niches + a 2D map.

Single-shot Cloud Task (or local subprocess) that reads the dense embeddings
written by the embeddings_refresh worker, assigns every video a niche, projects
a sample to 2D, names the niches with Gemini, and writes
``recoded/video_map.parquet`` + ``recoded/video_niches.json``
(see :mod:`fyp.video_map`).
"""

import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_video_map_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Build the niche map from the embedding store.

    Emits how far the partition actually moved (``map_niche_changed``,
    ``map_new_videos``, ``map_cold_start``). The refresh pipeline reads those to
    decide whether the study, timeline and session caches need rebuilding: a
    warm-started append that moves no video between niches leaves every
    downstream cache correct, and the whole cascade is skipped.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional ``n_niches``, ``map_sample``, ``pca_dim``,
            ``reset_labels``. ``auto_refresh`` is accepted and ignored — the
            downstream refresh is planned by the pipeline, not by this worker.

    Returns:
        None. Dispatch of the dependent steps is the pipeline's business.
    """
    from fyp.video_map import (
        DEFAULT_MAP_SAMPLE,
        DEFAULT_N_NICHES,
        DEFAULT_PCA_DIM,
        build_niche_map,
    )

    task_args = task_args or {}
    n_niches = int(task_args.get("n_niches") or DEFAULT_N_NICHES)
    map_sample = int(task_args.get("map_sample") or DEFAULT_MAP_SAMPLE)
    pca_dim = int(task_args.get("pca_dim") or DEFAULT_PCA_DIM)
    reset_labels = bool(task_args.get("reset_labels"))

    reporter.log(
        f"Starting video map refresh (n_niches={n_niches}, map_sample={map_sample}, "
        f"reset_labels={reset_labels})..."
    )
    result = build_niche_map(
        n_niches=n_niches, map_sample=map_sample, pca_dim=pca_dim,
        reset_labels=reset_labels, reporter=reporter,
    )
    reporter.emit_data({
        "map_videos": result["videos"],
        "map_niches": result["niches"],
        "map_mapped": result["mapped"],
        # The pipeline's change signals. A missing key reads as "unknown" and
        # refreshes everything downstream, so always emit them.
        "map_niche_changed": int(result.get("niche_changed") or 0),
        "map_new_videos": int(result.get("new_videos") or 0),
        "map_cold_start": bool(result.get("cold_start")),
    })
    reporter.update_progress(100, "Done")
    reporter.log(
        f"Video map refresh complete: {result['videos']:,} videos, "
        f"{result['niches']} niches, {result['mapped']:,} mapped."
    )

    reporter.log(
        f"Niche assignment: {result.get('niche_changed', 0):,} video(s) changed "
        f"niche, {result.get('new_videos', 0):,} newly mapped."
    )
    return None




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args = {}
        if args.n_niches is not None:
            task_args["n_niches"] = args.n_niches
        if args.map_sample is not None:
            task_args["map_sample"] = args.map_sample
        if args.pca_dim is not None:
            task_args["pca_dim"] = args.pca_dim
        task_args["reset_labels"] = bool(args.reset_labels)
        return task_args

    # The dependent steps are dispatched by the refresh pipeline once this
    # worker finishes (process_routes on Cloud Run, monitor_process_completion
    # locally) — this script only builds the map.
    run_worker(
        run_video_map_refresh,
        "video_map_refresh",
        arg_specs=[
            (("--n-niches",), {"type": int, "default": None, "help": "Number of niches"}),
            (("--map-sample",), {"type": int, "default": None, "help": "Videos projected to 2D"}),
            (("--pca-dim",), {"type": int, "default": None, "help": "PCA dimensionality"}),
            (("--auto-refresh",), {"action": "store_true",
                                   "help": "Accepted for compatibility and ignored — the dependent "
                                           "refreshes are planned by the refresh pipeline."}),
            (("--reset-labels",), {"action": "store_true",
                                   "help": "Regenerate every niche name from scratch (no carry-over from the previous build)."}),
        ],
        make_task_args=_make_task_args,
        description="Cluster embeddings into niches + 2D map",
    )
