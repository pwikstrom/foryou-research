import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

import fyp.machine_annotation as ma

if __name__ == "__main__":
    import argparse
    import traceback
    
    parser = argparse.ArgumentParser(description="Run annotator")
    parser.add_argument("study_name", help="Name of the study")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches")
    parser.add_argument("--testing", action="store_true", help="Enable test mode")
    
    args = parser.parse_args()

    print(f"Starting annotator for study: {args.study_name}")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    try:
        ma.annotate_videos_loop(
            study_name=args.study_name,
            batch_size=args.batch_size,
            max_batches=args.max_batches
        )
        print("Annotator process completed.")
    except Exception as e:
        print(f"Annotator failed: {e}")
        traceback.print_exc()
        sys.exit(1)
