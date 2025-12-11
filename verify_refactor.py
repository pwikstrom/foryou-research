import sys
from pathlib import Path
from unittest.mock import MagicMock
import pandas as pd

# Add project root to sys.path
project_root = Path(__file__).resolve().parent
sys.path.append(str(project_root))

# Initialize config
import fyp.fyp_main as fyp
from fyp.organize_datasets_OPTIMIZED import filter_log_against_sampled_donation_groups
# We can't easily import organize_datasets_OPTIMIZED because it might run code or require heavy deps
# But we can inspect it or just try to import and inspect fyp.cf["var_scheme"]

def verify_refactor():
    print("Initializing project config...")
    # Mock some keys that might be missing in local/test env
    try:
        fyp.init_project(verbose=True, local_mode=True)
    except Exception as e:
        print(f"Init failed (expected in test env?): {e}")

    # Check var_scheme loaded
    if "var_scheme" in fyp.cf:
        print("SUCCESS: `var_scheme` found in config.")
        vs = fyp.cf["var_scheme"]
        if isinstance(vs, pd.DataFrame) and not vs.empty:
            print(f"SUCCESS: `var_scheme` is a non-empty DataFrame with {len(vs)} rows.")
            
            # Check B_ columns logic
            b_vars = vs[vs['variable_name'].str.startswith('B_', na=False)]['variable_name'].tolist()
            print(f"Found {len(b_vars)} B_ variables in scheme.")
            
            # Check D_ columns logic
            d_vars = vs[vs['variable_name'].str.startswith('D_', na=False)]['variable_name'].tolist()
            print(f"Found {len(d_vars)} D_ variables in scheme.")
            
        else:
            print("FAILURE: `var_scheme` is empty or not a DataFrame.")
    else:
        print("FAILURE: `var_scheme` NOT found in config.")

    # Check PCA function signatures (static check or import)
    try:
        from fyp.pca import transform_category_column_to_counts_df, calculate_scaled_pca_scores
        import inspect
        
        sig = inspect.signature(transform_category_column_to_counts_df)
        print(f"PCA Transform Signature: {sig}")
        # defaults should be None now
        
        sig2 = inspect.signature(calculate_scaled_pca_scores)
        print(f"PCA Calculate Signature: {sig2}")
        
        print("SUCCESS: PCA module imported.")
        
    except ImportError as e:
        print(f"FAILURE: Could not import PCA module: {e}")
    except Exception as e:
        print(f"FAILURE inspecting PCA: {e}")

if __name__ == "__main__":
    verify_refactor()
