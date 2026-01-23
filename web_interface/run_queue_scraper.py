
import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    import argparse
    from fyp.scrape import queue_scraper_loop

    parser = argparse.ArgumentParser(description="Run queue scraper")
    parser.add_argument("--batch-size", type=int, default=5, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=1, help="Max batches")
    
    args = parser.parse_args()

    print(f"Starting Queue Scraper")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    try:
        queue_scraper_loop(
            cf=None,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            verbose=False,
            dry_run=False
        )
        print("Queue scraping process completed.")

    except Exception as e:
        print(f"Queue scraping process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
