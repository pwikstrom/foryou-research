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
WRITE_BOTH = False

# If True, we verify that the loaded Parquet data matches what would be loaded from Pickle (if it exists).
# This is slow but useful for verification.
VERIFY_ON_LOAD = False

# If True, forcing fallback to Pickle only (emergency switch).
USE_PICKLE_ONLY = False


def load_dataset(path: str, verbose: bool = False, **kwargs) -> pd.DataFrame:
    """
    Load a dataframe from a given path (base path without extension or with .pkl/.parquet).
    """
    from fyp.fyp_main import convert_dtypes_to_pyarrow

    base_path, ext = os.path.splitext(path)
    if ext in ['.pkl', '.parquet']:
        path_no_ext = base_path
    else:
        path_no_ext = path 

    parquet_path = f"{path_no_ext}.parquet"
    pickle_path = f"{path_no_ext}.pkl"

    df = None
    loaded_from_parquet = False

    # Try Parquet
    if os.path.exists(parquet_path) and not USE_PICKLE_ONLY:
        if verbose: print(f" [DATA_IO] Loading Parquet: {parquet_path}")
        df = pd.read_parquet(parquet_path, engine='pyarrow', dtype_backend="pyarrow")
        loaded_from_parquet = True
            
    # Try Pickle
    if os.path.exists(pickle_path) and df is None:
        if verbose: print(f" [DATA_IO] Loading Pickle: {pickle_path}")
        df = pd.read_pickle(pickle_path, **kwargs)
        loaded_from_parquet = False
    
    if df is None:
        raise FileNotFoundError(f"Neither Parquet nor Pickle found for: {path_no_ext}")

    # this is really only necessary if it has been loaded from pickle
    if not loaded_from_parquet:
        if verbose: print(f" [DATA_IO] Converting data from Pickle file to Parquet types")

        df = convert_dtypes_to_pyarrow(df, verbose=verbose)

        """for col in df.columns:
            try:
                df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
            except:
                print(col)
                df[col] = df[col].map(fix_surrogates)
                try:
                    df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
                except:
                    print(f"Failed to convert {col}")
            
            if df[col].dtype == 'object':
                df[col] = fix_complex_types(df[col].copy(), verbose=verbose)
                df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')"""

    return df










def save_dataset(df: pd.DataFrame, path: str, verbose: bool = False, **kwargs):
    """
    Save a dataframe to the given path.
    
    Logic:
    1. Always save to .parquet (unless USE_PICKLE_ONLY is True).
    2. If WRITE_BOTH is True, also save to .pkl.
    """

    from os import rename
    from fyp.fyp_main import convert_dtypes_to_pyarrow


    base_path, ext = os.path.splitext(path)
    if ext in ['.pkl', '.parquet']:
        path_no_ext = base_path
    else:
        path_no_ext = path

    parquet_path = f"{path_no_ext}.parquet"
    pickle_path = f"{path_no_ext}.pkl"


    # UPDATED - now expecting all dtypes to be pyarrow compatible
    # type management to ensure pyarrow can handle the data
    # but I'm doing it for pickle save as well to ensure consistency 
    #df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    # 1. Save Parquet
    if not USE_PICKLE_ONLY:
        if True:#try:
            # pyarrow handles lists/dicts natively, no need for JSON conversion
            if verbose: print(f" [DATA_IO] Saving Parquet: {parquet_path}")
            

            df.to_parquet(parquet_path, engine='pyarrow', **kwargs) 

        """except Exception as e:
             print(f" [DATA_IO] ERROR: Failed to save Parquet {parquet_path}: {e}")
             # If parquet write fails, we MUST ensure we write pickle if intended
             if not WRITE_BOTH:
                 print(f" [DATA_IO] Force-writing Pickle due to Parquet failure.")
                 df.to_pickle(pickle_path, **kwargs)
                 return"""

    # 2. Save Pickle (Dual Write / Legacy)
    if WRITE_BOTH or USE_PICKLE_ONLY:
        print(f" [DATA_IO] Saving Pickle: {pickle_path}")
        df.to_pickle(pickle_path, **kwargs)





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
