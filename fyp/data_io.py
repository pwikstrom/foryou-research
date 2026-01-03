# -*- coding: utf-8 -*-
"""
Script Name: data_io.py
Description: Centralized I/O operations for dataframes.
Author: Patrik
"""

import pandas as pd
import os
import logging
import json






def exists(cf, storage_location, filename, verbose=False) -> bool:
    """
    Check if the file filename exists in the given storage location.
    """
    from os.path import exists, join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")
    
    full_path = join(cf['paths'][storage_location], filename)

    file_exists = exists(full_path)
    return file_exists



def getctime(cf, storage_location, filename, verbose=False):
    """
    Get the creation time of the file filename in the given storage location.
    """
    from os.path import getctime
    from os.path import join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")
    
    full_path = join(cf['paths'][storage_location], filename)

    file_ctime = getctime(full_path)
    return file_ctime




def getmtime(cf, storage_location, filename, verbose=False):
    """
    Get the modification time of the file filename in the given storage location.
    """
    from os.path import getmtime
    from os.path import join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")
    
    full_path = join(cf['paths'][storage_location], filename)

    file_ctime = getmtime(full_path)
    return file_ctime



def getsize(cf, storage_location, filename, verbose=False):
    """
    Get the size of the file filename in the given storage location.
    """
    from os.path import getsize
    from os.path import join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")
    
    full_path = join(cf['paths'][storage_location], filename)

    file_ctime = getsize(full_path)
    return file_ctime








def remove(cf, storage_location, filename, verbose=False):
    """
    Remove the file filename from the given storage location.
    """
    from os import remove
    from os.path import join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_path = join(cf['paths'][storage_location], filename)

    if exists(cf, storage_location, filename):
        remove(full_path)
        if verbose: print(f" [DATA_IO] Removed '{filename}' from '{storage_location}'")
    else:
        if verbose: print(f" [DATA_IO] ERROR Couldn't find '{filename}' in '{storage_location}'")





def listdir(cf, storage_location, return_absolute_path=False, verbose=False) -> list:
    """
    List files in the given storage location.
    """
    from os import listdir
    from os.path import join

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    files = listdir(cf['paths'][storage_location])

    if return_absolute_path:
        files = [join(cf['paths'][storage_location], f) for f in files]

    if verbose: print(f" [DATA_IO] Listed {len(files)} files in '{storage_location}'")

    return files




def move(cf, src_storage_location, dst_storage_location, filename: str, verbose=False):
    """
    Move the file filename from src_storage_location to dst_storage_location.
    """
    from shutil import move
    from os.path import join

    if src_storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{src_storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    if dst_storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{dst_storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_src = join(cf['paths'][src_storage_location], filename)
    full_dst = join(cf['paths'][dst_storage_location], filename)


    if exists(cf, src_storage_location, filename):
        move(full_src, full_dst)
        if verbose: print(f" [DATA_IO] Moved '{filename}' from '{src_storage_location}' to '{dst_storage_location}'")
    else:
        if verbose: print(f" [DATA_IO] ERROR Couldn't find '{filename}' in '{src_storage_location}'")




def load_json(cf, storage_location, filename, verbose = False):
    """
    Load a json from a given path.
    """
    from os.path import join
    from json import load

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_path = join(cf['paths'][storage_location], filename)

    base_path, ext = os.path.splitext(full_path)
    if ext == '.json':
        path_no_ext = base_path
    else:
        raise ValueError(f"File extension must be '.json', got: '{ext}'") 

    json_path = f"{path_no_ext}.json"

    try:
        with open(full_path, 'r') as file:
            return load(file)
    except Exception as e:
        print(f" [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
        return None





def save_json(cf, data, storage_location, filename, verbose = False):
    """
    Save a json to a given path.
    """
    from os.path import join
    from json import dump

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_path = join(cf['paths'][storage_location], filename)

    base_path, ext = os.path.splitext(full_path)
    if ext == '.json':
        path_no_ext = base_path
    else:
        raise ValueError(f"File extension must be '.json', got: '{ext}'") 

    json_path = f"{path_no_ext}.json"

    with open(full_path, 'w') as file:
        dump(data, file)






def load_parquet(cf, storage_location, filename, columns=None, verbose = False):
    """
    Load a dataframe from a given path (base path without extension or with .pkl/.parquet).
    """
    from fyp.fyp_main import convert_dtypes_to_pyarrow
    from os.path import join, basename


    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_path = join(cf['paths'][storage_location], basename(filename))

    base_path, ext = os.path.splitext(full_path)
    if ext == '.parquet':
        path_no_ext = base_path
    else:
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'") 

    parquet_path = f"{path_no_ext}.parquet"

    df = None

    if os.path.exists(parquet_path):
        if verbose: print(f" [DATA_IO] Loading: '{filename}' from '{storage_location}'")
        df = pd.read_parquet(parquet_path, engine='pyarrow', dtype_backend="pyarrow", columns=columns)
    
    if df is None:
        raise FileNotFoundError(f"File not found: '{filename}' in '{storage_location}'")

    # type management to be sure
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    return df






def save_parquet(cf, df: pd.DataFrame, storage_location, filename, verbose = False):
    """
    Save a dataframe to the given path.
    
    Logic:
    1. Always save to .parquet (unless USE_PICKLE_ONLY is True).
    """

    from os import rename
    from fyp.fyp_main import convert_dtypes_to_pyarrow
    from os.path import join, basename

    if storage_location not in cf['paths']:
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use one of these locations: {', '.join(list(cf['paths'].keys()))}")

    full_path = join(cf['paths'][storage_location], basename(filename))

    base_path, ext = os.path.splitext(full_path)
    if ext == '.parquet':
        path_no_ext = base_path
    else:
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'") 

    parquet_path = f"{path_no_ext}.parquet"

    # type management to ensure pyarrow can handle the data
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    # pyarrow handles lists/dicts natively, no need for JSON conversion
    if verbose: print(f" [DATA_IO] Saving: '{filename}' to '{storage_location}'")
    df.to_parquet(parquet_path, engine='pyarrow') 






# ------------------------------------------------------------------------------
# Data Management Utilities
# ------------------------------------------------------------------------------

def get_study_export_files(cf = None, study_name = None):
    #from os import listdir
    #from os.path import join, getmtime
    from fyp.fyp_main import init_config
    from numpy import mean as np_mean
    from datetime import datetime

    if cf is None:
        cf = init_config()
    
    if study_name is None:
        raise ValueError("study_name is required")

    export_file_categories = ["HALF_BAKED", "PCA", "LOG", "RECODED"]
    study_files = {category: [] for category in export_file_categories}
    
    for fn in listdir(cf, "exports"):
        if fn.startswith(study_name) and fn.endswith(cf['misc']['file_format']):
            for category in export_file_categories:
                if category in fn:
                    study_files[category].append(getmtime(cf, "exports", fn))
    
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
    #from os import listdir
    #from os.path import join, getsize
    from fyp.fyp_main import init_config
    import pandas as pd
    
    if cf is None:
        cf = init_config()
        
    if study_name is None:
        raise ValueError("study_name is required")

    group_factors = cf['var_scheme'][cf['var_scheme']['role']=='group_factor']['variable_name'].tolist()

    details = []

    try:
        files = [f for f in listdir(cf, "exports") if f.startswith(study_name) and f.endswith(cf['misc']['file_format'])]
    except FileNotFoundError:
        return []

    for fn in files:
        try:
            # Get size in KB
            size_kb = getsize(cf, "exports", fn) / 1024
            
            # Read dataset safely
            # Note: calling load_dataset directly within same module
            df = load_parquet(cf, "exports", fn)
            
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
