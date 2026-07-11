import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_recode_refresh_studies(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh recoded datasets and stats for studies."""
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.organize_datasets import create_study_recoded_dataset
    from fyp.studies import init_study_defs, save_study_defs

    task_args = task_args or {}
    force_full_rebuild: bool = bool(task_args.get("force_full_rebuild", False))
    reporter.log("Starting Study Definitions (Recoded Data) Refresh...")
    if force_full_rebuild:
        reporter.log("force_full_rebuild=True — sidecars will be removed before each study.")
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

    # Pre-load enrichment status once for all studies
    _t_phase = time.perf_counter()
    df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
    if df_status is not None and not df_status.empty:
        if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
            df_status = df_status.reset_index()
    _t_status_load = time.perf_counter() - _t_phase
    reporter.log(f"[TIMING] enrichment_status load={_t_status_load:.2f}s")

    for i, (study_name, config) in enumerate(studies.items()):
        if reporter.check_cancelled():
            reporter.log("Cancelled by user.")
            break
        reporter.update_progress(int((i / total) * 100), f"Processing {study_name} ({i + 1}/{total})...")
        reporter.log(f"Processing study: {study_name}")
        _t_study_start = time.perf_counter()

        try:
            if force_full_rebuild:
                sidecar_fn = f"{study_name}_recoded.meta.json"
                if data_io.exists(storage_location="cache", filename=sidecar_fn):
                    data_io.remove(storage_location="cache", filename=sidecar_fn)

            df_study = create_study_recoded_dataset(
                study_name=study_name,
                save_to_cache=True,
                enrichment_status=df_status,
                force_full_rebuild=force_full_rebuild,
                verbose=False,
            )

            if df_study is None:
                reporter.log(f"Skipping {study_name}: No data generated.")
                studies[study_name]['stats'] = {
                    "unique_videos": 0,
                    "scraped_videos": 0,
                    "annotated_videos": 0,
                    "unique_collections": 0
                }
            else:
                refresh_action = df_study.attrs.get("refresh_action", "full_rebuild")
                if refresh_action == "short_circuit":
                    reporter.log(f"  Short-circuit for {study_name}: cached parquet reused ({len(df_study)} rows)")
                elif refresh_action == "enrichment_patch":
                    reporter.log(f"  Enrichment patch for {study_name}: re-merged enrichment onto cached activity ({len(df_study)} rows)")
                else:
                    reporter.log(f"  Successfully refreshed data for {study_name} ({len(df_study)} rows)")

                unique_collections = int(df_study['collection_id'].nunique())
                unique_videos = int(df_study['item_id'].nunique())

                scraped_videos = 0
                annotated_videos = 0

                if df_status is not None and not df_status.empty and 'item_id' in df_status.columns:
                    try:
                        study_ids = df_study['item_id'].astype("string[pyarrow]")
                        status_ids = df_status['item_id'].astype("string[pyarrow]")
                        matched = df_status.loc[status_ids.isin(study_ids)]

                        if 'scraped_ok' in matched.columns:
                            scraped_videos = int(matched['scraped_ok'].fillna(False).sum())
                        if 'annotated_ok' in matched.columns:
                            annotated_videos = int(matched['annotated_ok'].fillna(False).sum())
                    except Exception as e:
                        reporter.log(f"  Warning: Could not match enrichment status for {study_name}: {e}")

                studies[study_name]['stats'] = {
                    "unique_videos": unique_videos,
                    "scraped_videos": scraped_videos,
                    "annotated_videos": annotated_videos,
                    "unique_collections": unique_collections
                }
                studies[study_name]['last_updated'] = datetime.now(UTC).isoformat()

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        _t_study = time.perf_counter() - _t_study_start
        reporter.log(f"  [TIMING] study={study_name} total={_t_study:.2f}s")
        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    # Persist updated stats to studies.json
    # Merge updated studies back into the full study_defs to avoid clobbering
    # non-targeted studies when a filtered refresh is run.
    for sn, sc in studies.items():
        fyp_cf['study_defs'][sn] = sc
    save_study_defs()
    reporter.log("Stats saved to studies.json.")
    _t_run = time.perf_counter() - _t_run_start
    reporter.log(f"[TIMING] recode_refresh_studies wall={_t_run:.2f}s studies={total}")
    reporter.log("Study Definitions (Recoded Data) refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args = {}
        if args.studies:
            task_args["studies"] = args.studies
        if args.force:
            task_args["force_full_rebuild"] = True
        return task_args

    run_worker(
        run_recode_refresh_studies,
        "recode_refresh_studies",
        arg_specs=[
            (('--studies',), {'type': str, 'default': None,
                              'help': 'Comma-separated study names to refresh (default: all)'}),
            (('--force',), {'action': 'store_true',
                            'help': 'Force full rebuild of every study, ignoring sidecar fingerprints'}),
            (('study_name',), {'nargs': '?', 'default': None}),
        ],
        make_task_args=_make_task_args,
    )
