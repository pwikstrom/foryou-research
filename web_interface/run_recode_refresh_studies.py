import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_recode_refresh_studies(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh recoded datasets and stats for studies."""
    from fyp.fyp_config import fyp_cf
    from fyp.studies import init_study_defs, save_study_defs
    from fyp.organize_datasets import create_study_recoded_dataset
    import fyp.data_io as data_io

    task_args = task_args or {}
    reporter.log("Starting Study Definitions (Recoded Data) Refresh...")

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
    df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
    if df_status is not None and not df_status.empty:
        if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
            df_status = df_status.reset_index()

    for i, (study_name, config) in enumerate(studies.items()):
        if reporter.check_cancelled():
            reporter.log("Cancelled by user.")
            break
        reporter.update_progress(int((i / total) * 100), f"Processing {study_name} ({i + 1}/{total})...")
        reporter.log(f"Processing study: {study_name}")

        try:
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, enrichment_status=df_status, verbose=False)

            if df_study is None:
                reporter.log(f"Skipping {study_name}: No data generated.")
                studies[study_name]['stats'] = {
                    "unique_videos": 0,
                    "scraped_videos": 0,
                    "annotated_videos": 0,
                    "unique_collections": 0
                }
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
                studies[study_name]['last_updated'] = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    # Persist updated stats to studies.json
    # Merge updated studies back into the full study_defs to avoid clobbering
    # non-targeted studies when a filtered refresh is run.
    for sn, sc in studies.items():
        fyp_cf['study_defs'][sn] = sc
    save_study_defs()
    reporter.log("Stats saved to studies.json.")
    reporter.log("Study Definitions (Recoded Data) refresh completed.")




if __name__ == "__main__":
    import argparse
    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser()
    parser.add_argument('--studies', type=str, default=None,
                        help='Comma-separated study names to refresh (default: all)')
    parser.add_argument('study_name', nargs='?', default=None)
    args = parser.parse_args()

    task_args = {}
    if args.studies:
        task_args["studies"] = args.studies

    reporter = LocalStatusReporter("recode_refresh_studies")
    try:
        run_recode_refresh_studies(reporter=reporter, task_args=task_args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
