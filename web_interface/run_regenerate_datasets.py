import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))


if __name__ == "__main__":
    import fyp.organize_datasets as organize_datasets
    import argparse
    import json
    import base64
    from fyp.fyp_main import connect_to_google, initialize

    
    # Argument Parser
    parser = argparse.ArgumentParser(description=f"Regenerate datasets for a study.")
    parser.add_argument('study_name', help="Name of the study")
    
    args = parser.parse_args()
    study_name = args.study_name
    
    # Load CF
    cf = initialize(verbose=False)
    if cf['data_io']['use_gcs_for_data']:
        cf = connect_to_google(cf)

    print(f"Starting dataset regeneration for study: {study_name}")
    try:
        organize_datasets.load_datasets(
            cf = cf,
            study_name = study_name,
            use_half_baked=True,
            delete_all_half_baked_files=True,
            consolidate=True,
            verbose=False
        )
        print("Dataset regeneration completed successfully.")
    except KeyboardInterrupt:
        print("\nProcess stopped by user.")
        sys.exit(1) # Return non-zero for stop
    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1) # Return non-zero for failure
