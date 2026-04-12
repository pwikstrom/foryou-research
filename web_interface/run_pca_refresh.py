import sys
import argparse
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        from fyp.studies import init_study_defs
        from fyp.pca import calculate_scaled_pca_scores

        parser = argparse.ArgumentParser()
        parser.add_argument('--studies', type=str, default=None,
                            help='Comma-separated study names to refresh (default: all)')
        parser.add_argument('study_name', nargs='?', default=None)
        args = parser.parse_args()

        print("Starting PCA / Correlations Refresh...")

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

        for i, (study_name, config) in enumerate(studies.items()):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Study {i+1}/{total}\" }}")
            print(f"Processing study: {study_name}")

            try:
                stats = config.get('stats', {})
                annotated = stats.get('annotated_videos', 0)

                if annotated == 0:
                    print(f"  Skipping {study_name}: no annotated videos.")
                    continue

                result = calculate_scaled_pca_scores(
                    study_name=study_name,
                    load_from_cache=False,
                    save_to_cache=True,
                    verbose=False,
                )

                if result is not None:
                    print(f"  Successfully refreshed PCA for {study_name} ({len(result)} rows)")
                else:
                    print(f"  Skipping {study_name}: PCA returned no data.")

            except Exception as e:
                print(f"Error processing {study_name}: {e}")

        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print("PCA / Correlations refresh completed.")

    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
