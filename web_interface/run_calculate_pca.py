import sys
import argparse
import os
import traceback

def main():
    # Add project root to path
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

    import fyp.pca as pca
    #from fyp.fyp_main import connect_to_google, initialize


    parser = argparse.ArgumentParser(description="Run pca.calculate_scaled_pca_scores")
    parser.add_argument("study_name", help="Name of the study")
    args = parser.parse_args()

    # Load CF
    #cf = initialize(verbose=False)
    #if cf['data_io']['use_gcs_for_data']:
    #    cf = connect_to_google(cf)

    try:
        print(f"Starting CALCULATE PCA SCORES for study: {args.study_name}")
                
        result = pca.calculate_scaled_pca_scores(
            cf = None,
            study_name = args.study_name,
            study_recoded_dataset = None,
            minimum_group_size = 10,
            target_explained_variance = 0.8,
            drop_rare_globally_below = 0.01,
            scale_it = True,
            load_from_cache = True,
            save_to_cache = True,
            verbose = True
            )

        if result is None:
            print("Calculate PCA scores failed.")
            sys.exit(1)
        else:
            print("Calculate PCA scores completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
