import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_meta_refresh_viewer(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh viewer metadata for all studies."""
    from fyp.fyp_config import fyp_cf
    from web_interface.explorer_backend import load_data, get_metadata, make_serializable
    from web_interface.data_service import load_schema_metadata
    from fyp.studies import init_study_defs
    import fyp.data_io as data_io

    reporter.log("Starting Video Analysis (Viewer) Metadata Refresh...")

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

            # Context = Viewer (Annotated OK + Activity Filter)
            if 'annotated_ok' in df.columns:
                df_viewer = df[df.annotated_ok].copy()
            else:
                df_viewer = df.copy()
            df_viewer = df_viewer[df_viewer['activity_type'].isin(['play', 'observe'])]
            df_viewer = df_viewer[df_viewer['item_id'].notna()]

            reporter.log(f"  Generating metadata for {len(df_viewer)} items...")
            meta = get_metadata(df_viewer, col_types)
            meta = load_schema_metadata(meta)

            filename = f"{study_name}_viewer_metadata.json"
            data_io.save_json(data=make_serializable(meta), storage_location="cache", filename=filename, verbose=False)
            reporter.log(f"  Updated metadata for {study_name}")

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    reporter.log("Video Analysis Metadata refresh completed.")




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    reporter = LocalStatusReporter("meta_refresh_viewer")
    try:
        run_meta_refresh_viewer(reporter=reporter)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
