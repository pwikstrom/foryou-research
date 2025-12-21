# -*- coding: utf-8 -*-
"""
Script Name: data_io.py
Description: Centralized I/O operations for dataframes, handling the migration from Pickle to Parquet.
Author: Antigravity (Assistant)
"""

import pandas as pd
import os
import logging
import json

# =============================================================================
# Configuration
# =============================================================================
# If True, writing a dataset will write BOTH .parquet and .pkl files.
# This is for the transition period to ensure we can always rollback.
WRITE_BOTH = True

# If True, we verify that the loaded Parquet data matches what would be loaded from Pickle (if it exists).
# This is slow but useful for verification.
VERIFY_ON_LOAD = False

# If True, forcing fallback to Pickle only (emergency switch).
USE_PICKLE_ONLY = True


def load_dataset(path: str, verbose: bool = False, **kwargs) -> pd.DataFrame:
    """
    Load a dataframe from a given path (base path without extension or with .pkl/.parquet).
    
    Logic:
    1. If USE_PICKLE_ONLY is True, try loading .pkl.
    2. Try loading .parquet.
    3. If .parquet fails or doesn't exist, fall back to .pkl.
    4. If VERIFY_ON_LOAD is True, load both and compare.
    """
    base_path, ext = os.path.splitext(path)
    if ext in ['.pkl', '.parquet']:
        path_no_ext = base_path
    else:
        path_no_ext = path 

    parquet_path = f"{path_no_ext}.parquet"
    pickle_path = f"{path_no_ext}.pkl"

    # Emergency Switch
    if USE_PICKLE_ONLY:
        if verbose: print(f" [DATA_IO] Loading Pickle (Enforced): {pickle_path}")
        return pd.read_pickle(pickle_path, **kwargs)

    # Try Parquet
    if os.path.exists(parquet_path):
        try:
            if verbose: print(f" [DATA_IO] Loading Parquet: {parquet_path}")
            df = pd.read_parquet(parquet_path, **kwargs)
            
            if VERIFY_ON_LOAD and os.path.exists(pickle_path):
                _verify_data(df, pickle_path, **kwargs)
                
            return df
        except Exception as e:
            print(f" [DATA_IO] WARNING: Failed to load Parquet {parquet_path}: {e}")
            print(f" [DATA_IO] Falling back to Pickle...")
    
    # Fallback to Pickle
    if os.path.exists(pickle_path):
        if verbose: print(f" [DATA_IO] Loading Pickle (Fallback): {pickle_path}")
        return pd.read_pickle(pickle_path, **kwargs)
    
    raise FileNotFoundError(f"Neither Parquet nor Pickle found for: {path_no_ext}")


def save_dataset(df: pd.DataFrame, path: str, verbose: bool = False, **kwargs):
    """
    Save a dataframe to the given path.
    
    Logic:
    1. Always save to .parquet (unless USE_PICKLE_ONLY is True).
    2. If WRITE_BOTH is True, also save to .pkl.
    """

    from os import rename


    base_path, ext = os.path.splitext(path)
    if ext in ['.pkl', '.parquet']:
        path_no_ext = base_path
    else:
        path_no_ext = path

    parquet_path = f"{path_no_ext}.parquet"
    pickle_path = f"{path_no_ext}.pkl"

    # 1. Save Parquet
    if not USE_PICKLE_ONLY:
        try:
            # Handle potential complex types before saving to Parquet
            # We work on a shallow copy to avoid modifying the original df in memory if it's used elsewhere
            # But deep copy is too expensive. We'll modify a copy of the columns that need it.
            df_parquet = df.copy(deep=False) 
            
            for col in df_parquet.columns:
                # Check if object dtype (candidates for mixed types or lists/dicts)
                if df_parquet[col].dtype == 'object':
                    # Check a sample- non-null value
                    sample = df_parquet[col].dropna().iloc[0] if not df_parquet[col].dropna().empty else None
                    if isinstance(sample, (list, dict)):
                        # Serialize to JSON string
                        df_parquet[col] = df_parquet[col].apply(lambda x: json.dumps(x) if isinstance(x, (list, dict)) else x)
                    
                    # Ensure all object columns are cast to string for Parquet compatibility
                    # This handles mixed types (e.g. ints and strings) which crash Parquet
                    df_parquet[col] = df_parquet[col].astype(str).replace('nan', None).replace('None', None)

            if verbose: print(f" [DATA_IO] Saving Parquet: {parquet_path}")
            temp_path = parquet_path + ".tmp"
            df_parquet.to_parquet(temp_path, engine='pyarrow', index=False, **kwargs) 
            rename(temp_path, parquet_path)
        except Exception as e:
             print(f" [DATA_IO] ERROR: Failed to save Parquet {parquet_path}: {e}")
             # If parquet write fails, we MUST ensure we write pickle if intended
             if not WRITE_BOTH:
                 print(f" [DATA_IO] Force-writing Pickle due to Parquet failure.")
                 temp_path = pickle_path + ".tmp"
                 df.to_pickle(temp_path, **kwargs)
                 rename(temp_path, pickle_path)
                 return

    # 2. Save Pickle (Dual Write / Legacy)
    if WRITE_BOTH or USE_PICKLE_ONLY:
        if verbose: print(f" [DATA_IO] Saving Pickle: {pickle_path}")
        temp_path = pickle_path + ".tmp"
        df.to_pickle(temp_path, **kwargs)
        rename(temp_path, pickle_path)


def _verify_data(parquet_df, pickle_path, **kwargs):
    """
    Compare loaded Parquet DF with Pickle DF.
    """
    try:
        pickle_df = pd.read_pickle(pickle_path, **kwargs)
        # Basic check: shape
        if parquet_df.shape != pickle_df.shape:
            print(f" [DATA_IO] VERIFICATION FAILED: Shapes differ! Parquet: {parquet_df.shape}, Pickle: {pickle_df.shape}")
        else:
            print(f" [DATA_IO] Verification passed: Shapes match.")
    except Exception as e:
        print(f" [DATA_IO] Verification error: {e}")

# ------------------------------------------------------------------------------
# Data Management Utilities (Moved from fyp_main.py)
# ------------------------------------------------------------------------------

def get_study_export_files(cf = None, study_name = None):
    from os import listdir
    from os.path import join, getmtime
    from fyp.fyp_main import init_config
    from numpy import mean as np_mean
    from datetime import datetime

    if cf is None:
        cf = init_config()
    
    if study_name is None:
        raise ValueError("study_name is required")

    export_file_categories = ["HALF_BAKED", "PCA", "LOG", "RECODED"]
    study_files = {category: [] for category in export_file_categories}
    
    for fn in listdir(cf["paths"]["exports"]):
        if fn.startswith(study_name) and fn.endswith(".pkl"):
            for category in export_file_categories:
                if category in fn:
                    study_files[category].append(getmtime(join(cf["paths"]["exports"], fn)))
    
    for category in study_files:
        if len(study_files[category]) == 0:
            study_files[category] = "No file found"
        elif len(study_files[category]) == 1:
            study_files[category] = f"1 file saved on {datetime.fromtimestamp(int(study_files[category][0]))}"
        else:
            oldest_file = int(min(study_files[category]))
            newest_file = int(max(study_files[category]))
            study_files[category] = f"{len  (study_files[category])} files from {datetime.fromtimestamp(oldest_file)} to {datetime.fromtimestamp(newest_file)}"

    return study_files


def get_dataset_details(cf=None, study_name=None):
    from os import listdir
    from os.path import join, getsize
    from fyp.fyp_main import init_config
    import pandas as pd
    
    if cf is None:
        cf = init_config()
        
    if study_name is None:
        raise ValueError("study_name is required")

    group_factors = cf['var_scheme'][cf['var_scheme']['role']=='group_factor']['variable_name'].tolist()

    details = []
    export_path = cf["paths"]["exports"]
    
    try:
        files = [f for f in listdir(export_path) if f.startswith(study_name) and f.endswith(".pkl")]
    except FileNotFoundError:
        return []

    for fn in files:
        file_path = join(export_path, fn)
        try:
            # Get size in KB
            size_kb = getsize(file_path) / 1024
            
            # Read dataset safely
            # Note: calling load_dataset directly within same module
            df = load_dataset(file_path)
            
            rows, cols = df.shape if hasattr(df, "shape") else (len(df), "N/A")
            if "item_id" in df.columns:
                nunique_items = df["item_id"].nunique()
            else:
                nunique_items = "N/A"

            all_group_factors_in_df = all([gf in df.columns for gf in group_factors])
            if all_group_factors_in_df:
                group_factor_counts = len(df.groupby(group_factors).size())
            else:
                all_group_factors_in_df = all([gf[2:] in df.columns for gf in group_factors])
                if all_group_factors_in_df:
                    group_factor_counts = len(df.groupby([gf[2:] for gf in group_factors]).size())
                else:
                    group_factor_counts = "N/A"

            
            details.append({
                "filename": fn,
                "rows": rows,
                "cols": cols,
                "nunique_items": nunique_items,
                "group_factor_counts": group_factor_counts,
                "size_kb": round(size_kb, 0)
            })
            
            # Clean up memory
            del df
            
        except Exception as e:
            details.append({
                "filename": fn,
                "error": str(e)
            })
            
    # Sort by filename
    details.sort(key=lambda x: x["filename"])
    return details
