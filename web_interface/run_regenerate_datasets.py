import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))


if __name__ == "__main__":
    import fyp.organize_datasets_OPTIMIZED as organize_datasets
    if len(sys.argv) > 1:
        study_name = sys.argv[1]
    else:
        print("Usage: python run_regenerate_datasets.py <study_name>")
        # Defaulting is dangerous for this operation, better to fail if not provided, 
        # but matching other scripts behavior if needed. 
        # For now, let's exit.
        sys.exit(1)

    print(f"Starting dataset regeneration for study: {study_name}")
    try:
        #cf = fyp.init_project()
        organize_datasets.load_datasets(
            cf = None,
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
