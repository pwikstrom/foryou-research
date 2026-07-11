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
        reporter.update_progress(int((i / total) * 100), f"Processing {study_name} ({i + 1}/{total})...")
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

            if result is not None:
                reporter.log(f"  Successfully refreshed PCA for {study_name} ({len(result)} rows)")
            else:
                reporter.log(f"  Skipping {study_name}: PCA returned no data.")

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        _t_study = time.perf_counter() - _t_study_start
        reporter.log(f"  [TIMING] study={study_name} total={_t_study:.2f}s")
        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    _t_run = time.perf_counter() - _t_run_start
    reporter.log(f"[TIMING] pca_refresh wall={_t_run:.2f}s studies={total}")
    reporter.log("PCA / Correlations refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args = {}
        if args.studies:
            task_args["studies"] = args.studies
        return task_args

    run_worker(
        run_pca_refresh,
        "pca_refresh",
        arg_specs=[
            (('--studies',), {'type': str, 'default': None,
                              'help': 'Comma-separated study names to refresh (default: all)'}),
            (('study_name',), {'nargs': '?', 'default': None}),
        ],
        make_task_args=_make_task_args,
    )
