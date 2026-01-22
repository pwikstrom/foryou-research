
import sys
import os
import pandas as pd
import numpy as np

# Ensure we can import fyp from the current directory
sys.path.append(os.getcwd())

import fyp
import fyp.data_io as data_io
import fyp.pca as pca

def main():
    print("Initializing...")
    try:
        cf = fyp.initialize()
    except Exception as e:
        print(f"Initialization failed: {e}")
        return

    print("Loading data...")
    dataset_name = "small_study_1_recoded.parquet"
    
    # Check if file exists first to give better error
    # (Assuming we can't easily check 'cache' alias path without cf, but load_parquet handles it)
    
    try:
        small_study = data_io.load_parquet(cf=cf, storage_location="cache", filename=dataset_name)
        if small_study is None:
             print(f"Failed to load {dataset_name}. It might not exist in the cache.")
             return
        print(f"Loaded dataset '{dataset_name}' with shape: {small_study.shape}")
    except Exception as e:
        print(f"Error loading data: {e}")
        return
        
    print("\nRunning calculate_scaled_pca_scores...")
    try:
        # Run the function
        scores, interpretations = pca.calculate_scaled_pca_scores(
            cf=cf,
            study_recoded_dataset=small_study,
            study_name="sanity_check_study", # Providing a dummy name just in case, though we pass dataset directly
            save_to_cache=False,
            verbose=True,
            scale_it=True
        )
        
        print("\n" + "="*40)
        print("SANITY CHECK RESULTS")
        print("="*40)
        
        if scores is None:
            print("❌ Result is None! Something went wrong inside the function (check previous logs).")
            return
            
        # 1. Shape check
        print(f"✅ Output Shape: {scores.shape}")
        
        # 2. Columns check
        print(f"Columns found: {len(scores.columns)}")
        # print first few
        print(f"First 10 columns: {list(scores.columns)[:10]}")
        
        # 3. Check for specific expected columns
        expected_meta = ["D_donation_id", "T_local_weekday", "T_local_date"]
        missing = [c for c in expected_meta if c not in scores.columns]
        if missing:
            print(f"⚠️  Missing expected metadata columns: {missing}")
        else:
            print(f"✅ Expected metadata columns present: {expected_meta}")

        # 4. NaN check
        total_nans = scores.isna().sum().sum()
        if total_nans > 0:
            print(f"⚠️  Found {total_nans} NaNs in the output dataframe.")
            # Breakdown
            nan_cols = scores.columns[scores.isna().any()].tolist()
            print(f"   Columns with NaNs: {nan_cols[:10]} {'...' if len(nan_cols)>10 else ''}")
        else:
            print("✅ No NaNs found in output.")

        # 5. Scaling check (Optional: check if numeric columns look standardized)
        # We need to pick a column that is a PCA score (e.g., contains 'C0')
        pca_cols = [c for c in scores.columns if '_C0' in c or '_C1' in c]
        if pca_cols:
            sample_col = pca_cols[0]
            col_data = scores[sample_col]
            # Convert to numeric, forcing errors to NaNs to be safe
            col_data_num = pd.to_numeric(col_data, errors='coerce').dropna()
            
            if len(col_data_num) > 0:
                mean_val = col_data_num.mean()
                std_val = col_data_num.std()
                print(f"ℹ️  Scaling Check on '{sample_col}': Mean={mean_val:.4f}, Std={std_val:.4f}")
                if abs(mean_val) < 0.1 and 0.9 < std_val < 1.1:
                    print("   (Looks like it is standardized)")
                else:
                    print("   (Does NOT look perfectly standardized - might be expected given concatenations or subsets)")
                    
        # 6. Interpretations check
        print(f"✅ Interpretations dictionary has {len(interpretations)} keys.")
        
    except Exception as e:
        print(f"❌ Error during execution: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
