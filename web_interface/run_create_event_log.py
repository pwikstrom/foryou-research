import sys
import argparse
import os
import traceback

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
    import fyp.organize_datasets as organize_datasets
    from fyp.fyp_main import connect_to_google, initialize


    parser = argparse.ArgumentParser(description="Run organize_datasets.create_study_main_dataset")
    parser.add_argument("study_name", help="Name of the study")
    args = parser.parse_args()

    # Load CF
    cf = initialize(verbose=False)
    if cf['data_io']['use_gcs_for_data']:
        cf = connect_to_google(cf)

    try:
        print(f"Starting CREATE EVENT LOG for study: {args.study_name}")
        organize_datasets.create_study_main_dataset(
            cf = cf,
            study_name = args.study_name,
            verbose = False)
        print("Process completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
