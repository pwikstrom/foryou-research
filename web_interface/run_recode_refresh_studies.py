import sys
from pathlib import Path
from datetime import datetime

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        import argparse
        from fyp.fyp_config import fyp_cf
        from fyp.studies import init_study_defs, save_study_defs
        from fyp.organize_datasets import create_study_recoded_dataset
        import fyp.data_io as data_io

        parser = argparse.ArgumentParser()
        parser.add_argument('--studies', type=str, default=None,
                            help='Comma-separated study names to refresh (default: all)')
        # Accept positional study_name for backwards compatibility with existing start_process calls
        parser.add_argument('study_name', nargs='?', default=None)
        args = parser.parse_args()

        print("Starting Study Definitions (Recoded Data) Refresh...")

        # Init studies
        init_study_defs()
        studies = fyp_cf.get('study_defs', {})

        # Filter to targeted studies if specified
        if args.studies:
            target_names = [s.strip() for s in args.studies.split(',')]
            studies = {k: v for k, v in studies.items() if k in target_names}
            print(f"Targeted refresh for {len(studies)} study/studies: {', '.join(studies.keys())}")

        total = len(studies)
        if total == 0:
            print("No studies found to refresh.")

        # Pre-load enrichment status once for all studies
        df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
                df_status = df_status.reset_index()

        for i, (study_name, config) in enumerate(studies.items()):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Study {i+1}/{total}\" }}")
            print(f"Processing study: {study_name}")

            try:
                # Force generation of the recoded dataset for the study
                df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)

                if df_study is None:
                    print(f"Skipping {study_name}: No data generated.")
                    studies[study_name]['stats'] = {
                        "unique_videos": 0,
                        "scraped_videos": 0,
                        "annotated_videos": 0,
                        "unique_collections": 0
                    }
                else:
                    print(f"  Successfully refreshed data for {study_name} ({len(df_study)} rows)")

                    # Calculate and persist stats
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
                            print(f"  Warning: Could not match enrichment status for {study_name}: {e}")

                    studies[study_name]['stats'] = {
                        "unique_videos": unique_videos,
                        "scraped_videos": scraped_videos,
                        "annotated_videos": annotated_videos,
                        "unique_collections": unique_collections
                    }
                    studies[study_name]['last_updated'] = datetime.now().isoformat()

            except Exception as e:
                print(f"Error processing {study_name}: {e}")

        # Persist updated stats to studies.json
        fyp_cf['study_defs'] = studies
        save_study_defs()
        print("Stats saved to studies.json.")

        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print("Study Definitions (Recoded Data) refresh completed.")

    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
