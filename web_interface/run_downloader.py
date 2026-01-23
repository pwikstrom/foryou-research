import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))


if __name__ == "__main__":
    import argparse
    from fyp.scrape import scraper_loop
    #from fyp.fyp_main import connect_to_google, initialize
    
    parser = argparse.ArgumentParser(description="Run downloader")
    parser.add_argument("study_name", help="Name of the study")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches")
    
    args = parser.parse_args()

    print(f"Starting downloader for study: {args.study_name}")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")
    
    # Load CF
    #cf = initialize(verbose=False)
    #if cf['data_io']['use_gcs_for_data']:
    #    cf = connect_to_google(cf)

    try:
        scraper_loop(
            cf = None,
            study_name=args.study_name,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            study_dataset = None,
            load_from_cache = True,
            verbose = False,
            dry_run = False
        )
        print("Scrape and download process completed.")

    except Exception as e:
        print(f"Scrape and download process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
