import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

import fyp.machine_annotation as ma

if __name__ == "__main__":
    if len(sys.argv) > 1:
        study_name = sys.argv[1]
    else:
        study_name = "everything" # Default or raise error
    
    print(f"Starting annotator for study: {study_name}")
    try:
        ma.create_a_new_dataset_just_for_annotating_downloaded_videos(study_name)
    except KeyboardInterrupt:
        print("\nAnnotator stopped by user.")
    except Exception as e:
        print(f"Annotator crashed: {e}")
