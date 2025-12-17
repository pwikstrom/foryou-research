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
        print(f"Starting RECODE EVENT LOG for study: {args.study_name}")
        #cf = fyp.init_project()
        recode_variables.recode_events_df(cf = None, study_name = args.study_name)
        print("Process completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
