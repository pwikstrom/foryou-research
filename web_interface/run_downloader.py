import sys
from pathlib import Path

# Add project root to sys.path to ensure fyp can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))


if __name__ == "__main__":
    import argparse
    import fyp.scrape as dv
    from fyp.fyp_main import connect_to_google, initialize
    
    parser = argparse.ArgumentParser(description="Run downloader")
    parser.add_argument("study_name", help="Name of the study")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches")
    parser.add_argument("--testing", action="store_true", help="Enable test mode")
    
    args = parser.parse_args()

    print(f"Starting downloader for study: {args.study_name}")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")
    
    # Load CF
    cf = initialize(verbose=False)
    if cf['data_io']['use_gcs_for_data']:
        cf = connect_to_google(cf)


    #import fyp
    try:
        dv.download_videos_loop(
            cf = cf,
            study_name=args.study_name,
            batch_size=args.batch_size,
            max_batches=args.max_batches
        )
        print("Scrape and download process completed.")
        print("-"*100)
    except Exception as e:
        print(f"Scrape and download process failed: {e}")
        print("-"*100)
        import traceback
        traceback.print_exc()
        sys.exit(1)
