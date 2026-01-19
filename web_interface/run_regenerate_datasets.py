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
    parser = argparse.ArgumentParser(description=f"Regenerate the core dataset.")
    
    args = parser.parse_args()
    
    # Load CF
    cf = initialize(verbose=False)
    if cf['data_io']['use_gcs_for_data']:
        cf = connect_to_google(cf)

    print(f"Starting regeneration of core dataset.")
    try:
        organize_datasets.load_study_datasets(
            cf = cf,
            study_name = 'everything',
            consolidate=True,
            save_to_cache=True,
            verbose=False
        )
        print("core dataset generated successfully.")
    except KeyboardInterrupt:
        print("\nProcess stopped by user.")
        sys.exit(1) # Return non-zero for stop
    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1) # Return non-zero for failure
