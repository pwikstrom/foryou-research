"""
Single-study refresh: recalculate stats, PCA, and metadata for one study.

Dispatched as a Cloud Task when saving a study definition on Cloud Run,
or run synchronously as a subprocess in local dev.
"""

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

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
    import pandas as pd

    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.pca import calculate_scaled_pca_scores
    from fyp.studies import init_study_defs, save_study_defs
    from web_interface import explorer_backend as explorer
    from web_interface.data_service import (
        load_schema_metadata,
        make_serializable,
        study_cache,
    )
    from web_interface.routes.management_routes import (
        _calculate_stats,
        _compute_universe_enrichment,
        _load_study_raw_window,
    )

    if not task_args or "study_name" not in task_args:
        raise ValueError("task_args must contain 'study_name'")

    study_name: str = task_args["study_name"]
    refresh_pca: bool = task_args.get("refresh_pca", True)
    refresh_metadata: bool = task_args.get("refresh_metadata", True)
    force_full_rebuild: bool = bool(task_args.get("force_full_rebuild", False))

    reporter.log(f"Starting study refresh for '{study_name}'...")
    _t_run_start = time.perf_counter()
    _t_stats = 0.0
    _t_pca_phase = 0.0
    _t_meta = 0.0

    # ---- Step 1: Calculate stats (creates recoded dataset) ----
    reporter.update_progress(0, "Calculating stats...")
    reporter.log(f"Calculating stats for {study_name}...")
    _t_phase = time.perf_counter()

    init_study_defs()
    studies = fyp_cf["study_defs"]
    if study_name not in studies:
        raise ValueError(f"Study '{study_name}' not found in study definitions")

    study_config = studies[study_name]
    study_config["STUDY_NAME"] = study_name

    # _calculate_stats creates the recoded dataset and returns it alongside stats.
    # If force_full_rebuild is requested, remove the sidecar first so the
    # fingerprint short-circuit inside create_study_recoded_dataset cannot fire.
    if force_full_rebuild:
        try:
            sidecar_fn = f"{study_name}_recoded.meta.json"
            if data_io.exists(storage_location="cache", filename=sidecar_fn):
                data_io.remove(storage_location="cache", filename=sidecar_fn)
                reporter.log(f"force_full_rebuild: removed sidecar {sidecar_fn}")
        except Exception as exc:
            reporter.log(f"force_full_rebuild: could not remove sidecar: {exc}")

    stats, df_recoded, df_status = _calculate_stats(study_config, save_to_cache=True)

    # Embed the date-range activity universe (by enrichment) into stats so the
    # Define Study modal's mosaic can be seeded when the study is reopened.
    try:
        df_raw_window = _load_study_raw_window(study_config.get("SELECTED_COLLECTIONS") or [])
        if df_raw_window is not None and isinstance(stats, dict):
            stats["universe"] = _compute_universe_enrichment(
                df_raw_window, df_status,
                study_config.get("START_DATE"), study_config.get("END_DATE"),
            )
    except Exception as exc:
        reporter.log(f"universe computation skipped: {exc}")

    # A short-circuited rebuild means the recoded parquet is unchanged on disk.
    # Tagged by create_study_recoded_dataset on the returned DataFrame's attrs.
    refresh_action = (df_recoded.attrs.get("refresh_action") if df_recoded is not None else None)
    is_short_circuit = refresh_action == "short_circuit"
    if is_short_circuit:
        reporter.log("Short-circuit: inputs unchanged since last refresh.")
    elif refresh_action == "enrichment_patch":
        reporter.log("Enrichment patch: re-merged enrichment onto cached activity rows (skipped collections load + sampling).")

    # Persist stats to study definition
    studies[study_name]["stats"] = stats
    studies[study_name]["last_updated"] = datetime.now(UTC).isoformat()
    fyp_cf["study_defs"] = studies
    save_study_defs()
    reporter.log(f"Stats: {stats}")
    _t_stats = time.perf_counter() - _t_phase

    if reporter.check_cancelled():
        reporter.log("Cancelled by user.")
        return

    # ---- Step 2: PCA (reuses in-memory DataFrame) ----
    # On short-circuit, the cached PCA is still valid — keep it. Otherwise the
    # recoded dataset has been rewritten and stale PCA must be removed to avoid
    # a version mismatch.
    pca_filename = f"{study_name}_PCA.parquet"
    pca_exists = data_io.exists(storage_location="cache", filename=pca_filename)

    if is_short_circuit and pca_exists:
        reporter.log("PCA kept (inputs unchanged; cached artifact still valid).")
    else:
        if pca_exists:
            data_io.remove(storage_location="cache", filename=pca_filename)

        if refresh_pca and stats["annotated_videos"] > 0 and df_recoded is not None:
            reporter.update_progress(25, "Calculating PCA...")
            reporter.log(f"Running PCA for {study_name}...")
            _t_phase = time.perf_counter()
            calculate_scaled_pca_scores(
                study_name=study_name,
                study_recoded_dataset=df_recoded,
                load_from_cache=False,
                save_to_cache=True,
            )
            _t_pca_phase = time.perf_counter() - _t_phase
            reporter.log("PCA complete.")
        else:
            reporter.log("PCA skipped (no annotated videos or flag off).")

    if reporter.check_cancelled():
        reporter.log("Cancelled by user.")
        return

    # ---- Step 3: Metadata (reuses in-memory DataFrame) ----
    # Short-circuit keeps the cached metadata + RAM cache entry, since the
    # underlying recoded dataset was not rewritten. A full rebuild still
    # invalidates both (matches the pre-fingerprint behaviour).
    fn = f"{study_name}_explorer_metadata.json"
    metadata_exists = data_io.exists(storage_location="cache", filename=fn)

    if is_short_circuit and metadata_exists:
        reporter.log("Metadata kept (inputs unchanged; cached artifact still valid).")
    else:
        if metadata_exists:
            data_io.remove(storage_location="cache", filename=fn)

        try:
            with study_cache.lock:
                if study_name in study_cache.cache:
                    del study_cache.cache[study_name]
                    reporter.log(f"Invalidated RAM cache for {study_name}")
        except Exception:
            pass

    if (not (is_short_circuit and metadata_exists)) and refresh_metadata and stats["unique_videos"] > 0 and df_recoded is not None:
        reporter.update_progress(50, "Generating metadata...")
        reporter.log("Classifying columns for metadata generation...")
        _t_phase = time.perf_counter()
        col_types = explorer.classify_columns(df_recoded)

        # Filter to annotated (or scraped) play/observe events.
        # The [viz] require_annotated_items flag mirrors data_service.get_explorer_data
        # so the saved metadata (filter dropdowns, value counts) reflects the same
        # row set the Explore tab actually shows at request time. When the flag is
        # False we fall back to scraped_ok so items without media are still excluded.
        require_annotated = fyp_cf.get("viz", {}).get("require_annotated_items", True)
        if require_annotated:
            if "annotated_ok" in df_recoded.columns:
                enrichment_mask = df_recoded["annotated_ok"].fillna(False)
            else:
                enrichment_mask = pd.Series(False, index=df_recoded.index)
        else:
            if "scraped_ok" in df_recoded.columns:
                enrichment_mask = df_recoded["scraped_ok"].fillna(False)
            else:
                enrichment_mask = pd.Series(False, index=df_recoded.index)

        df_filtered = df_recoded[
            enrichment_mask
            & df_recoded['activity_type'].isin(['play', 'observe'])
            & df_recoded['item_id'].notna()
        ].copy()

        reporter.update_progress(60, "Generating explorer metadata...")
        explorer_meta = explorer.get_metadata(df_filtered, col_types)

        stats_res = explorer.get_current_stats(df_filtered, col_types, number_meta=explorer_meta)
        explorer_meta["total_stats"] = stats_res["stats"]

        # Bake the list of collection_ids actually present in the filtered study data.
        # Lets the base metadata endpoint inject the Collection Tags filter without
        # having to load the recoded parquet on every study selection.
        try:
            if "collection_id" in df_filtered.columns:
                explorer_meta["collection_ids"] = sorted(
                    df_filtered["collection_id"].dropna().astype(str).unique().tolist()
                )
            else:
                explorer_meta["collection_ids"] = []
        except Exception as e:
            reporter.log(f"Warning: could not extract collection_ids: {e}")
            explorer_meta["collection_ids"] = []

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
        _t_meta = time.perf_counter() - _t_phase
    elif not (is_short_circuit and metadata_exists):
        reporter.log("Metadata refresh skipped.")

    # Free the large DataFrame now that all steps are done
    del df_recoded

    reporter.emit_data({"study_name": study_name, "stats": stats})
    _t_total = time.perf_counter() - _t_run_start
    reporter.log(
        f"[TIMING] study_refresh study={study_name} "
        f"stats={_t_stats:.2f}s pca={_t_pca_phase:.2f}s meta={_t_meta:.2f}s "
        f"total={_t_total:.2f}s"
    )
    reporter.log(f"Study refresh for '{study_name}' complete.")




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    if len(sys.argv) < 2:
        print("Usage: python run_study_refresh.py <study_name> [--no-pca] [--no-metadata] [--force]")
        sys.exit(1)

    study_name = sys.argv[1]
    args = {"study_name": study_name}
    if "--no-pca" in sys.argv:
        args["refresh_pca"] = False
    if "--no-metadata" in sys.argv:
        args["refresh_metadata"] = False
    if "--force" in sys.argv:
        args["force_full_rebuild"] = True

    reporter = LocalStatusReporter("study_refresh")
    try:
        run_study_refresh(reporter=reporter, task_args=args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
