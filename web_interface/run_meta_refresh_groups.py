import sys
from datetime import UTC, datetime
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_meta_refresh_groups(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh explorer (group comparisons) metadata for all studies."""
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.studies import init_study_defs
    from web_interface.data_service import load_schema_metadata
    from web_interface.explorer_backend import (
        get_current_stats,
        get_metadata,
        load_data,
        make_serializable,
    )

    reporter.log("Starting Group Comparisons (Explorer) Metadata Refresh...")

    # Init studies
    init_study_defs()
    studies = fyp_cf.get('study_defs', {})

    total = len(studies)
    for i, (study_name, config) in enumerate(studies.items()):
        if reporter.check_cancelled():
            reporter.log("Cancelled by user.")
            break
        reporter.update_progress(int((i / total) * 100), f"Processing {study_name} ({i + 1}/{total})...")
        reporter.log(f"Processing study: {study_name}")

        try:
            df, col_types = load_data(study_name, verbose=False)

            if df is None:
                reporter.log(f"Skipping {study_name}: No data found.")
                continue

            # Context = Explorer (Annotated OK + Activity Filter).
            # The [viz] require_annotated_items flag mirrors data_service.get_explorer_data
            # so the saved metadata reflects the same row set the Explore tab will show.
            # When the flag is False we still require scraped_ok so items without media
            # are excluded.
            require_annotated = fyp_cf.get("viz", {}).get("require_annotated_items", True)
            if require_annotated:
                if 'annotated_ok' in df.columns:
                    df_explorer = df[df['annotated_ok'].fillna(False)].copy()
                else:
                    df_explorer = df.iloc[0:0].copy()
            else:
                if 'scraped_ok' in df.columns:
                    df_explorer = df[df['scraped_ok'].fillna(False)].copy()
                else:
                    df_explorer = df.iloc[0:0].copy()
            df_explorer = df_explorer[df_explorer['activity_type'].isin(['play', 'observe'])]
            df_explorer = df_explorer[df_explorer['item_id'].notna()]

            reporter.log(f"  Generating metadata for {len(df_explorer)} items...")
            meta = get_metadata(df_explorer, col_types)

            # Calculate Stats — log/bins decided from the full-study metadata.
            stats_res = get_current_stats(df_explorer, col_types, number_meta=meta)
            meta['total_stats'] = stats_res['stats']

            # Source Info Injection
            try:
                the_recoded_file = f"{study_name}_recoded.parquet"
                if data_io.exists(storage_location="cache", filename=the_recoded_file):
                    meta['source_file'] = the_recoded_file
                    mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file), tz=UTC)
                    meta['source_file_modified'] = mtime.isoformat(timespec='seconds')
                else:
                    meta['source_file'] = "Unknown"
                    meta['source_file_modified'] = ""
            except Exception:
                meta['source_file'] = "Error"
                meta['source_file_modified'] = ""

            meta = load_schema_metadata(meta)

            filename = f"{study_name}_explorer_metadata.json"
            data_io.save_json(data=make_serializable(meta), storage_location="cache", filename=filename, verbose=False)
            reporter.log(f"  Updated metadata for {study_name}")

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")
            import traceback
            traceback.print_exc()

        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    reporter.log("Group Comparisons Metadata refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    # run_meta_refresh_groups never reads task_args; the default empty dict
    # passed by run_worker is equivalent to the old no-kwarg call.
    run_worker(run_meta_refresh_groups, "meta_refresh_groups")
