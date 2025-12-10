import sys
import argparse
import os
import pandas as pd # Needed to load pickle if PCA requires it
import traceback

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import fyp.pca as pca
# Assumption: fyp.cf is initialized when pca is imported or we need to init it.
# Usually initiation happens in the module or we need to call init_project.
import fyp

def main():
    parser = argparse.ArgumentParser(description="Run pca.calculate_scaled_pca_scores")
    parser.add_argument("study_name", help="Name of the study")
    args = parser.parse_args()

    # Ensure project config is loaded for paths
    if not hasattr(fyp, 'cf'):
       fyp.init_project(verbose=False)

    try:
        print(f"Starting CALCULATE PCA SCORES for study: {args.study_name}")
        
        # NOTE: pca.calculate_scaled_pca_scores expects a DataFrame if not None, 
        # or it tries to load from pickle. 
        # Previous inspection showed it might NOT have auto-load logic fully working in all versions,
        # but the user requested 'launch pca.calculate_scaled_pca_scores(study_name)'.
        # We will attempt to call it directly. If it fails due to missing arg, we handle it.
        # However, to be robust based on my view of the code:
        # Step 267 showed the user adding auto-load logic:
        # if some_events_df is None: ... load ...
        # So calling with study_name alone is sufficient.
        
        pca.calculate_scaled_pca_scores(args.study_name)
        print("Process completed successfully.")
    except Exception as e:
        print(f"Process failed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
