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


def run_video_map_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Build the niche map from the embedding store.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional ``n_niches``, ``map_sample``, ``pca_dim``.
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

    reporter.log(f"Starting video map refresh (n_niches={n_niches}, map_sample={map_sample})...")
    result = build_niche_map(
        n_niches=n_niches, map_sample=map_sample, pca_dim=pca_dim, reporter=reporter,
    )
    reporter.emit_data({
        "map_videos": result["videos"],
        "map_niches": result["niches"],
        "map_mapped": result["mapped"],
    })
    reporter.update_progress(100, "Done")
    reporter.log(
        f"Video map refresh complete: {result['videos']:,} videos, "
        f"{result['niches']} niches, {result['mapped']:,} mapped."
    )




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Cluster embeddings into niches + 2D map")
    parser.add_argument("--n-niches", type=int, default=None, help="Number of niches")
    parser.add_argument("--map-sample", type=int, default=None, help="Videos projected to 2D")
    parser.add_argument("--pca-dim", type=int, default=None, help="PCA dimensionality")
    args = parser.parse_args()

    task_args = {}
    if args.n_niches is not None:
        task_args["n_niches"] = args.n_niches
    if args.map_sample is not None:
        task_args["map_sample"] = args.map_sample
    if args.pca_dim is not None:
        task_args["pca_dim"] = args.pca_dim

    reporter = LocalStatusReporter("video_map_refresh")
    try:
        run_video_map_refresh(reporter=reporter, task_args=task_args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
