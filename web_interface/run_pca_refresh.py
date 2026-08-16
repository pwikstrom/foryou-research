import sys
import time
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_pca_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh PCA / Correlations data for studies."""
    import fyp.data_io as data_io
    from fyp.analysis.stats import compute_group_stats_artifact
    from fyp.fyp_config import fyp_cf
    from fyp.pca import calculate_scaled_pca_scores
    from fyp.studies import init_study_defs

    task_args = task_args or {}
    reporter.log("Starting PCA / Correlations Refresh...")
    _t_run_start = time.perf_counter()

    # Init studies
    init_study_defs()
    studies = fyp_cf.get('study_defs', {})

    # Filter to targeted studies if specified
    target_studies_str = task_args.get("studies")
    if target_studies_str:
        target_names = [s.strip() for s in target_studies_str.split(',')]
        studies = {k: v for k, v in studies.items() if k in target_names}
        reporter.log(f"Targeted refresh for {len(studies)} study/studies: {', '.join(studies.keys())}")

    total = len(studies)
    if total == 0:
        reporter.log("No studies found to refresh.")
        return

    for i, (study_name, config) in enumerate(studies.items()):
        if reporter.check_cancelled():
            reporter.log("Cancelled by user.")
            break
        reporter.update_progress(int((i / total) * 100), f"Study {i + 1}/{total}: {study_name}")
        reporter.log(f"Processing study: {study_name}")
        _t_study_start = time.perf_counter()

        try:
            stats = config.get('stats', {})
            annotated = stats.get('annotated_videos', 0)

            if annotated == 0:
                reporter.log(f"  Skipping {study_name}: no annotated videos.")
                continue

            result = calculate_scaled_pca_scores(
                study_name=study_name,
                load_from_cache=False,
                save_to_cache=True,
                verbose=False,
            )

            # calculate_scaled_pca_scores returns (scores_df, interpretations)
            scores_df = result[0] if isinstance(result, tuple) else result
            if scores_df is not None:
                reporter.log(f"  Successfully refreshed PCA for {study_name} ({len(scores_df)} group rows)")

                # Group-differences artifact (ANOVA/KW sweep + PERMANOVA).
                # Failure never blocks the PCA refresh itself.
                try:
                    stats_payload = compute_group_stats_artifact(scores_df, study_name)
                    data_io.save_json(data=stats_payload, storage_location="cache",
                                      filename=f"{study_name}_corr_stats.json")
                    n_perma = (len(stats_payload['permanova'])
                               + len(stats_payload['permanova_personalization']))
                    reporter.log(f"  Saved group stats for {study_name} "
                                 f"({len(stats_payload['personalization'])} personalization, "
                                 f"{len(stats_payload['anova'])} ANOVA, "
                                 f"{n_perma} PERMANOVA tests)")
                except Exception as e:
                    reporter.log(f"  Group-stats computation failed for {study_name} (continuing): {e}")
            else:
                reporter.log(f"  Skipping {study_name}: PCA returned no data.")

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        _t_study = time.perf_counter() - _t_study_start
        reporter.log(f"  [TIMING] study={study_name} total={_t_study:.2f}s")
        # Same message as the emit above: advances the bar without adding a
        # second, content-free line to the run log (the reporter dedupes
        # consecutive identical progress messages).
        reporter.update_progress(int(((i + 1) / total) * 100),
                                 f"Study {i + 1}/{total}: {study_name}")

    _t_run = time.perf_counter() - _t_run_start
    reporter.log(f"[TIMING] pca_refresh wall={_t_run:.2f}s studies={total}")
    reporter.log("PCA / Correlations refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args = {}
        if args.studies:
            task_args["studies"] = args.studies
        elif args.study_name:
            task_args["studies"] = args.study_name
        return task_args

    run_worker(
        run_pca_refresh,
        "pca_refresh",
        arg_specs=[
            (('--studies',), {'type': str, 'default': None,
                              'help': 'Comma-separated study names to refresh (default: all)'}),
            (('study_name',), {'nargs': '?', 'default': None,
                               'help': 'Single study to refresh; ignored when --studies is given'}),
        ],
        make_task_args=_make_task_args,
        description="Refresh PCA / Correlations data for studies",
    )
