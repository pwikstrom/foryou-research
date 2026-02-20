
import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    import argparse
    from fyp.machine_annotation import queue_annotation_loop

    parser = argparse.ArgumentParser(description="Run queue annotator")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=1, help="Max batches")
    parser.add_argument("study_name", help="Target study name to annotate")
    
    args = parser.parse_args()

    print(f"Starting Queue Annotator")
    print(f"Target Study: {args.study_name} | Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    try:
        queue_annotation_loop(
            study_name=args.study_name,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            verbose=False,
            dry_run=False
        )
        print("Queue annotation process completed.")

    except Exception as e:
        print(f"Queue annotation process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
