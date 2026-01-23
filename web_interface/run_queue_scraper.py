
import sys
from pathlib import Path
import argparse
import os

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    from fyp.scrape import scraper_loop_from_list
    import fyp.data_io as data_io
    from fyp.fyp_main import initialize, connect_to_google

    parser = argparse.ArgumentParser(description="Run queue scraper")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches")
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to pickled cookie file")
    
    args = parser.parse_args()

    # Initialize fyp environment
    cf = initialize(verbose=True)
    if cf['data_io']['use_gcs_for_data']:
        cf = connect_to_google(cf, verbose=True)

    # Attempt to load cookies from GCS (Cloud Run compatible)
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is not None:
        try:
            print("Checking for cookies in GCS (config/cookies.pkl)...")
            blob = cf['data_io']['bucket'].blob("config/cookies.pkl")
            if blob.exists():
                print("Found cookies in GCS. Downloading...")
                local_cookie_path = os.path.join(project_root, "temp_cookies_gcs.pkl")
                blob.download_to_filename(local_cookie_path)
                
                import pickle
                from fyp import scrape
                with open(local_cookie_path, 'rb') as f:
                    cookies = pickle.load(f)
                scrape.pyk.cookies = cookies
                print(f"Successfully injected {len(cookies)} cookies from GCS.")
                
                # Cleanup
                if os.path.exists(local_cookie_path):
                    os.remove(local_cookie_path)
            else:
                print("No cookies found in GCS config/cookies.pkl")
        except Exception as e:
            print(f"Failed to load cookies from GCS: {e}")

    # Also allow local override if provided (legacy/local dev)
    if args.cookie_file and not hasattr(scrape.pyk, 'cookies'):
        try:
            import pickle
            from fyp import scrape
            with open(args.cookie_file, 'rb') as f:
                cookies = pickle.load(f)
            scrape.pyk.cookies = cookies
            print(f"Successfully injected {len(cookies)} cookies from local file {args.cookie_file}")
        except Exception as e:
            print(f"Failed to inject local cookies: {e}")

    print(f"Starting Queue Scraper")
    print(f"Batch settings: Size={args.batch_size}, Max={args.max_batches}")

    try:
        from fyp.scrape import queue_scraper_loop
        
        queue_scraper_loop(
            cf=cf,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            verbose=True,
            dry_run=False
        )
        print("Queue scraping process completed.")

    except Exception as e:
        print(f"Queue scraping process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
