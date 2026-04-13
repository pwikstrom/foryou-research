import sys
from pathlib import Path
from datetime import datetime

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_meta_refresh_groups(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh explorer (group comparisons) metadata for all studies."""
    from fyp.fyp_config import fyp_cf
    from web_interface.explorer_backend import load_data, get_metadata, get_current_stats, make_serializable
    from web_interface.data_service import load_schema_metadata, get_viz_config
    from fyp.studies import init_study_defs
    import fyp.data_io as data_io

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

            # Context = Explorer (Annotated OK + Activity Filter)
            if 'annotated_ok' in df.columns:
                df_explorer = df[df.annotated_ok].copy()
            else:
                df_explorer = df.copy()
            df_explorer = df_explorer[df_explorer['activity_type'].isin(['play', 'observe'])]
            df_explorer = df_explorer[df_explorer['item_id'].notna()]

            reporter.log(f"  Generating metadata for {len(df_explorer)} items...")
            meta = get_metadata(df_explorer, col_types)

            # Calculate Stats
            viz_config = get_viz_config()
            stats_res = get_current_stats(df_explorer, col_types, viz_config=viz_config)
            meta['total_stats'] = stats_res['stats']

            # Source Info Injection
            try:
                the_recoded_file = f"{study_name}_recoded.parquet"
                if data_io.exists(storage_location="cache", filename=the_recoded_file):
                    meta['source_file'] = the_recoded_file
                    mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file))
                    meta['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
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
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("meta_refresh_groups")
    try:
        run_meta_refresh_groups(reporter=reporter)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
