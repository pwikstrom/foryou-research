import sys
import os
import time
import pandas as pd
import numpy as np
import traceback

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import fyp.recode_variables as recode_new
import fyp.recode_variables_old as recode_old
from fyp.fyp_main import init_config

def run_verification():
    print("Loading test data from test_data.parquet...")
    try:
        df = pd.read_parquet("test_data.parquet", engine="pyarrow", dtype_backend="pyarrow")
    except Exception as e:
        print(f"Failed to load test_data.parquet: {e}")
        # Fallback to verify logic exists without it if file missing, but user said it exists
        return

    print(f"Loaded DataFrame with shape: {df.shape}")
    
    # Initialize config
    # We might need to handle 'study_name' argument if recode_events_df requires it
    cf = init_config()
    
    # 1. Run OLD
    print("\n" + "="*40)
    print("Running OLD Implementation (recode_variables_old.py)")
    print("="*40)
    df_old_input = df.copy()
    
    start_old = time.time()
    try:
        df_old_output = recode_old.recode_events_df(
            cf=cf,
            cool_events_in=df_old_input,
            verbose=False,
            save_it=False
        )
    except TypeError:
        print("Retrying old implementation with minimal args...")
        df_old_output = recode_old.recode_events_df(
            cf=cf,
            cool_events_in=df_old_input,
            verbose=False,
            save_it=False
        )
    except Exception as e:
        print(f"Old implementation CRASHED: {e}")
        traceback.print_exc()
        df_old_output = None

    end_old = time.time()
    time_old = end_old - start_old
    print(f"Old implementation took: {time_old:.4f} seconds")

    # 2. Run NEW
    print("\n" + "="*40)
    print("Running NEW Implementation (recode_variables.py)")
    print("="*40)
    df_new_input = df.copy()
    start_new = time.time()
    try:
        df_new_output = recode_new.recode_events_df(
            cf=cf,
            cool_events_in=df_new_input,
            verbose=False,
            save_it=False
        )
    except Exception as e:
        print(f"New implementation CRASHED: {e}")
        traceback.print_exc()
        df_new_output = None
        
    end_new = time.time()
    time_new = end_new - start_new
    print(f"New implementation took: {time_new:.4f} seconds")
    
    # 3. Compare
    if df_new_output is not None and df_old_output is not None:
        print(f"\nSpeedup Factor: {time_old / time_new:.2f}x")
        
        print("\n" + "="*40)
        print("Comparing Outputs")
        print("="*40)
        
        cols_old = set(df_old_output.columns)
        cols_new = set(df_new_output.columns)
        
        if cols_old != cols_new:
            print("WARNING: Column mismatch!")
            print(f"Columns in OLD but not NEW: {cols_old - cols_new}")
            print(f"Columns in NEW but not OLD: {cols_new - cols_old}")
            
        common_cols = sorted(list(cols_old.intersection(cols_new)))
        
        mismatches = 0
        for c in common_cols:
            s1 = df_old_output[c]
            s2 = df_new_output[c]
            
            # Simple equality check first
            try:
                # Relaxed check: allows dtype differences (e.g. object vs string[pyarrow]) slightly
                # if values are same.
                # However, assert_series_equal is strict on data.
                # If dtypes differ, we might need to cast for comparison.
                
                # Align types for comparison if needed
                if s1.dtype != s2.dtype:
                     # try comparing as string or object
                     # or specific conversions
                     pass

                pd.testing.assert_series_equal(s1, s2, check_dtype=False, check_index_type=False, rtol=1e-5)
            except AssertionError as e:
                mismatches += 1
                # Detailed error?
                # Sometimes lists [1] vs 1 comparison fails?
                print(f"MISMATCH in column '{c}'")
                print(f"Details: {e}")
                # Show sample
                print(f"Sample OLD: {s1.dropna().head(3).tolist()}")
                print(f"Sample NEW: {s2.dropna().head(3).tolist()}")
                print("-" * 20)
                
                if mismatches > 5:
                    print("Stopping detailed mismatch reporting after 5 errors.")
                    break
        
        if mismatches == 0:
            print("\nSUCCESS: All common columns match perfectly!")
        else:
            print(f"\nFAILURE: {mismatches} columns mismatched.")

    else:
        print("Cannot compare outputs due to failure.")

if __name__ == "__main__":
    run_verification()
