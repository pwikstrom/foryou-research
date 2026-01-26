import sys
import argparse
import os
import traceback

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
    import fyp.recode_variables as recode_variables

    parser = argparse.ArgumentParser(description="Run recode_variables.recode_events_df")
    parser.add_argument("study_name", help="Name of the study")
    args = parser.parse_args()


    try:
        print(f"Starting process to recode main datasetfor study: {args.study_name}")
        result = recode_variables.recode_events_df(
            study_name = args.study_name,
            study_dataset = None,
            load_from_cache = True,
            save_to_cache = True,
            verbose = True
            )
        if result is None:
            print("Process failed.")
            sys.exit(1)
        else:
            print("Process completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
