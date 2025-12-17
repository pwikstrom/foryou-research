import sys
import argparse
import os
import traceback

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
    import fyp.organize_datasets_OPTIMIZED as organize_datasets
    #from fyp.fyp_main import init_config

    #cf = init_config()

    parser = argparse.ArgumentParser(description="Run organize_datasets_OPTIMIZED.export_logs")
    parser.add_argument("study_name", help="Name of the study")
    args = parser.parse_args()

    try:
        print(f"Starting CREATE EVENT LOG for study: {args.study_name}")
        organize_datasets.export_logs(
            cf = None,
            study_name = args.study_name)
        print("Process completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
