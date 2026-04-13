# -*- coding: utf-8 -*-
"""
Single-study refresh: recalculate stats, PCA, and metadata for one study.

Dispatched as a Cloud Task when saving a study definition on Cloud Run,
or run synchronously as a subprocess in local dev.
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_study_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh stats, PCA, and metadata for a single study.

    Args:
        reporter: Status reporter (local or GCS).
        task_args: Must contain 'study_name'. Optional: 'refresh_pca' (bool),
                   'refresh_metadata' (bool).
    """
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.pca import calculate_scaled_pca_scores
    from fyp.studies import init_study_defs, save_study_defs
    from web_interface import explorer_backend as explorer
    from web_interface.data_service import (
        get_viz_config, load_schema_metadata, study_cache, make_serializable,
    )
    from web_interface.routes.management_routes import _calculate_stats

    if not task_args or "study_name" not in task_args:
        raise ValueError("task_args must contain 'study_name'")

    study_name: str = task_args["study_name"]
    refresh_pca: bool = task_args.get("refresh_pca", True)
    refresh_metadata: bool = task_args.get("refresh_metadata", True)

    reporter.log(f"Starting study refresh for '{study_name}'...")

    # ---- Step 1: Calculate stats (creates recoded dataset) ----
    reporter.update_progress(0, "Calculating stats...")
    reporter.log(f"Calculating stats for {study_name}...")

    init_study_defs()
    studies = fyp_cf["study_defs"]
    if study_name not in studies:
        raise ValueError(f"Study '{study_name}' not found in study definitions")

    study_config = studies[study_name]
    study_config["STUDY_NAME"] = study_name

    # Reuse the same stats logic as the synchronous save path
    stats = _calculate_stats(study_config, save_to_cache=True)

    # Persist stats to study definition
    studies[study_name]["stats"] = stats
    studies[study_name]["last_updated"] = datetime.now(timezone.utc).isoformat()
    fyp_cf["study_defs"] = studies
    save_study_defs()
    reporter.log(f"Stats: {stats}")

    if reporter.check_cancelled():
        reporter.log("Cancelled by user.")
        return

    # ---- Step 2: PCA ----
    # Always delete stale PCA to avoid version mismatch
    if data_io.exists(storage_location="cache", filename=f"{study_name}_PCA.parquet"):
        data_io.remove(storage_location="cache", filename=f"{study_name}_PCA.parquet")

    if refresh_pca and stats["annotated_videos"] > 0:
        reporter.update_progress(25, "Calculating PCA...")
        reporter.log(f"Running PCA for {study_name}...")
        calculate_scaled_pca_scores(
            study_name=study_name, load_from_cache=True, save_to_cache=True,
        )
        reporter.log("PCA complete.")
    else:
        reporter.log("PCA skipped (no annotated videos or flag off).")

    if reporter.check_cancelled():
        reporter.log("Cancelled by user.")
        return

    # ---- Step 3: Metadata ----
    # Always invalidate stale metadata and RAM cache
    for suffix in ("_viewer_metadata.json", "_explorer_metadata.json"):
        fn = f"{study_name}{suffix}"
        if data_io.exists(storage_location="cache", filename=fn):
            data_io.remove(storage_location="cache", filename=fn)

    # Invalidate RAM cache
    try:
        with study_cache.lock:
            if study_name in study_cache.cache:
                del study_cache.cache[study_name]
                reporter.log(f"Invalidated RAM cache for {study_name}")
    except Exception:
        pass

    if refresh_metadata and stats["unique_videos"] > 0:
        reporter.update_progress(50, "Generating metadata...")
        reporter.log(f"Loading data for metadata generation...")
        df, col_types = explorer.load_data(study_name, verbose=False)

        if df is not None:
            # Viewer metadata
            reporter.update_progress(60, "Generating viewer metadata...")
            df_viewer = df[df.annotated_ok].copy()
            df_viewer = df_viewer[df_viewer["activity_type"].isin(["play", "observe"])]
            df_viewer = df_viewer[df_viewer["item_id"].notna()]
            viewer_meta = explorer.get_metadata(df_viewer, col_types)
            viewer_meta = load_schema_metadata(viewer_meta)
            data_io.save_json(
                data=make_serializable(viewer_meta),
                storage_location="cache",
                filename=f"{study_name}_viewer_metadata.json",
                verbose=False,
            )
            reporter.log("Viewer metadata saved.")

            if reporter.check_cancelled():
                reporter.log("Cancelled by user.")
                return

            # Explorer metadata
            reporter.update_progress(75, "Generating explorer metadata...")
            df_explorer = df[df.annotated_ok].copy()
            df_explorer = df_explorer[df_explorer["activity_type"].isin(["play", "observe"])]
            df_explorer = df_explorer[df_explorer["item_id"].notna()]
            explorer_meta = explorer.get_metadata(df_explorer, col_types)

            viz_config = get_viz_config()
            stats_res = explorer.get_current_stats(df_explorer, col_types, viz_config=viz_config)
            explorer_meta["total_stats"] = stats_res["stats"]

            try:
                the_recoded_file = f"{study_name}_recoded.parquet"
                if data_io.exists(storage_location="cache", filename=the_recoded_file):
                    explorer_meta["source_file"] = the_recoded_file
                    mtime = datetime.fromtimestamp(
                        data_io.getmtime(storage_location="cache", filename=the_recoded_file),
                    )
                    explorer_meta["source_file_modified"] = mtime.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    explorer_meta["source_file"] = "Unknown"
                    explorer_meta["source_file_modified"] = ""
            except Exception:
                explorer_meta["source_file"] = "Error"
                explorer_meta["source_file_modified"] = ""

            explorer_meta = load_schema_metadata(explorer_meta)
            data_io.save_json(
                data=make_serializable(explorer_meta),
                storage_location="cache",
                filename=f"{study_name}_explorer_metadata.json",
                verbose=False,
            )
            reporter.log("Explorer metadata saved.")
    else:
        reporter.log("Metadata refresh skipped.")

    reporter.emit_data({"study_name": study_name, "stats": stats})
    reporter.log(f"Study refresh for '{study_name}' complete.")




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    if len(sys.argv) < 2:
        print("Usage: python run_study_refresh.py <study_name> [--no-pca] [--no-metadata]")
        sys.exit(1)

    study_name = sys.argv[1]
    args = {"study_name": study_name}
    if "--no-pca" in sys.argv:
        args["refresh_pca"] = False
    if "--no-metadata" in sys.argv:
        args["refresh_metadata"] = False

    reporter = LocalStatusReporter("study_refresh")
    try:
        run_study_refresh(reporter=reporter, task_args=args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
