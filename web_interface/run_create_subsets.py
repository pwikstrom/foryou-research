import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

import fyp

if __name__ == "__main__":
    if len(sys.argv) > 1:
        study_name = sys.argv[1]
    else:
        print("Error: Study name required.")
        sys.exit(1)

    print(f"Starting Create Item Subsets for study: {study_name}")
    try:
        #cf = fyp.initialize()
        interesting_videos = fyp.generate_unique_videos_to_scrape_and_annotate(
            cf = None,
            study_name = study_name,
            load_from_cache = True,
            save_to_cache = True,
            verbose = True)
        subsets = {}
        for k in interesting_videos:
            subsets[k] = set(interesting_videos[k].index)
        
        # Calculate counts for valid sets
        import json
        data = {k: len(v) for k, v in subsets.items() if isinstance(v, set)}
        print(f"::DATA:: {json.dumps(data)}")

        print("::PROGRESS:: {\"done\": 1, \"total\": 1, \"rate\": 0, \"eta\": \"0s\"}") # Fake completion
        print("Create Item Subsets completed successfully.")
    except KeyboardInterrupt:
        print("\nProcess stopped by user.")
    except Exception as e:
        print(f"Process crashed: {e}")






    """return {
        'downloaded_and_annotated': downloaded_and_annotated,
        'downloaded_not_annotated': downloaded_not_annotated,
        'failed_annotations': failed_annotations,
        'missing_downloads': missing_downloads,
        'failed_scrapes': failed_scrapes,
        'unseen_videos': unseen_videos,
        'completed_downloads': completed_downloads,
        'machine_annotated_videos': machine_annotated_videos,
    }"""