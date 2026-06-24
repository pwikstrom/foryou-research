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


# Downstream refreshes dispatched after a map rebuild when auto_refresh is set.
# A rebuild remaps every video's niche, so every study/collection is affected —
# we refresh all of them (no filter). embeddings_refresh is excluded: the map is
# built FROM the embeddings, so re-embedding here would invert the dependency.
_DOWNSTREAM_PIPELINE = [
    {"task": "recode_refresh_studies", "task_args": {}},
    {"task": "meta_refresh_groups", "task_args": {}},
    {"task": "pca_refresh", "task_args": {}},
    {"task": "timelines_refresh", "task_args": {}},
]


def run_video_map_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Build the niche map from the embedding store.

    When ``task_args.auto_refresh`` is True and the map was rebuilt over a
    non-empty corpus, returns a chain dict that dispatches a full recode →
    meta → pca → timelines refresh so the niche columns reach every study cache.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional ``n_niches``, ``map_sample``, ``pca_dim``,
            ``auto_refresh``, ``reset_labels``.

    Returns:
        A chain dict (Cloud Tasks pipeline) when ``auto_refresh`` triggers the
        downstream refresh, else ``None``.
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
    auto_refresh = bool(task_args.get("auto_refresh"))
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
    })
    reporter.update_progress(100, "Done")
    reporter.log(
        f"Video map refresh complete: {result['videos']:,} videos, "
        f"{result['niches']} niches, {result['mapped']:,} mapped."
    )

    # Chain the downstream refresh so the new niche assignments propagate into
    # every study cache. recode runs first, then fans out to the concurrent
    # leaves (meta ‖ pca ‖ timelines) — see build_pipeline_chain. Cloud Tasks
    # consumes this via _run_task_with_stats; local subprocess mode dispatches
    # the same pipeline sequentially in monitor_process_completion.
    if auto_refresh and result.get("videos", 0) > 0:
        from web_interface.run_consolidate_enrichment import build_pipeline_chain
        chain = build_pipeline_chain(list(_DOWNSTREAM_PIPELINE))
        reporter.log(
            f"Auto-refresh: dispatching {chain['next_task']} "
            f"(stage 2/{chain['next_task_args']['pipeline_stage_total']}); "
            f"pipeline={[p['task'] for p in _DOWNSTREAM_PIPELINE]}"
        )
        return chain
    return None




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Cluster embeddings into niches + 2D map")
    parser.add_argument("--n-niches", type=int, default=None, help="Number of niches")
    parser.add_argument("--map-sample", type=int, default=None, help="Videos projected to 2D")
    parser.add_argument("--pca-dim", type=int, default=None, help="PCA dimensionality")
    parser.add_argument("--auto-refresh", action="store_true",
                        help="After rebuilding, refresh all study caches so the new niches propagate.")
    parser.add_argument("--reset-labels", action="store_true",
                        help="Regenerate every niche name from scratch (no carry-over from the previous build).")
    args = parser.parse_args()

    task_args = {}
    if args.n_niches is not None:
        task_args["n_niches"] = args.n_niches
    if args.map_sample is not None:
        task_args["map_sample"] = args.map_sample
    if args.pca_dim is not None:
        task_args["pca_dim"] = args.pca_dim
    task_args["auto_refresh"] = bool(args.auto_refresh)
    task_args["reset_labels"] = bool(args.reset_labels)

    reporter = LocalStatusReporter("video_map_refresh")
    try:
        # In subprocess mode the chain-dispatch return value is ignored — the
        # web service's monitor_process_completion handles downstream
        # orchestration in local dev. Cloud Tasks uses it in _run_task_with_stats.
        run_video_map_refresh(reporter=reporter, task_args=task_args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
