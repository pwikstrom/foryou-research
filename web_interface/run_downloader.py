import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

import fyp.download_videos as dv

if __name__ == "__main__":
    if len(sys.argv) > 1:
        study_name = sys.argv[1]
    else:
        study_name = "everything" # Default or raise error
        # print("Usage: python run_downloader.py <study_name>")
        # sys.exit(1)

    print(f"Starting downloader for study: {study_name}")
    try:
        dv.download_videos_loop(study_name)
    except KeyboardInterrupt:
        print("\nDownloader stopped by user.")
    except Exception as e:
        print(f"Downloader crashed: {e}")
