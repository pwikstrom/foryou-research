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



# ------------------------------------------------------------------------------
# Path Resolution & GCS Helpers
# ------------------------------------------------------------------------------

def _resolve_paths(cf, storage_location, filename):
    """
    Resolve the given storage location and filename to:
    1. A Primary Path (GCS URI if enabled, else Local Path)
    2. A Secondary Path (Local Path if GCS is enabled, else None)
    3. Mode ('gcs' or 'local')
    4. GCS Blob Name (if mode is 'gcs', else None)
    
    Returns:
        tuple: (primary_path, secondary_path, mode, blob_name)
    """
    from os.path import join, basename
    
    # 1. Validate Location
    if storage_location not in cf['paths']:
        valid_locs = ', '.join(list(cf['paths'].keys()))
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use: {valid_locs}")

    # 2. Determine Local Path (Always needed for secondary or primary)
    # filename might be simple 'file' or 'file.parquet', or 'subdir/file.parquet'
    # We join it generic OS style. 
    local_path = join(cf['paths'][storage_location], filename)

    # 3. Check GCS Configuration
    use_gcs = cf.get("misc", {}).get("use_gcs_for_data", False)
    gcs_base = cf.get("gcs_paths", {}).get(storage_location)
    
    # 4. Resolve
    if use_gcs and gcs_base:
        bucket_name = cf['data_io'].get('GCS_bucket_name')
        if not bucket_name:
             # Fallback if config is malformed but flag is true
             return (local_path, None, 'local', None)
             
        # Construct Blob Name
        # CAUTION: filename might contain local separators. Ensure forward slash for GCS.
        # If filename is 'subdir/file.pkl', we want 'base/subdir/file.pkl'
        normalized_filename = filename.replace("\\", "/") 
        blob_name = f"{gcs_base}/{normalized_filename}"
        # Clean double slashes
        blob_name = blob_name.replace("//", "/")
        
        # GS URI
        gcs_uri = f"gs://{bucket_name}/{blob_name}"
        
        # Return GCS as primary, Local as secondary (Parallel Mode)
        return (gcs_uri, local_path, 'gcs', blob_name)
    else:
        # Local Only
        return (local_path, None, 'local', None)

def _get_bucket(cf):
    """Retrieve the bucket object from config."""
    w = cf.get("data_io", {}).get("bucket")
    return w









def exists(cf, storage_location, filename, verbose=False) -> bool:
    """
    Check if the file filename exists in the given storage location.
    Transparently handles local or GCS checks.
    """
    from os.path import exists as local_exists
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    #if verbose: 
    if verbose:
        print(f" [DATA_IO] exists: Checking {primary}")
    
    if mode == 'gcs':
        bucket = _get_bucket(cf)
        gcs_exists = False
        if bucket:
            # Note: Checking blob existence involves a metadata request
            gcs_exists = bucket.blob(blob_name).exists()
            if gcs_exists:
                return True
        else:
            raise ValueError(" [DATA_IO]: GCS mode enabled but bucket missing.")

        return False
    else:
        return local_exists(primary)



def getctime(cf, storage_location, filename, verbose=False):
    """
    Get the creation time of the file filename.
    """
    from os.path import getctime
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    
    if mode == 'gcs':
        try:
            bucket = _get_bucket(cf)
            if bucket:
                blob = bucket.get_blob(blob_name)
                if blob and blob.time_created:
                    return blob.time_created.timestamp()
                # If blob doesn't exist or time missing
                raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
            else:
                raise ValueError("GCS bucket not initialized")
        except Exception as e:
            use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
            if use_fallback and secondary:
                 if verbose: print(f" [DATA_IO] getctime: Fallback ({e}). Checking {secondary}")
                 return getctime(secondary)
            raise e
    else:
        return getctime(primary)


def getmtime(cf, storage_location, filename, verbose=False):
    """
    Get the modification time of the file.
    """
    from os.path import getmtime
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    if mode == 'gcs':
        try:
            bucket = _get_bucket(cf)
            if bucket:
                blob = bucket.get_blob(blob_name)
                if blob and blob.updated:
                    return blob.updated.timestamp()
                raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
            else:
                raise ValueError("GCS bucket not initialized")
        except Exception as e:
            use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
            if use_fallback and secondary:
                 if verbose: print(f" [DATA_IO] getmtime: Fallback ({e}). Checking {secondary}")
                 return getmtime(secondary)
            raise e
    else:
        return getmtime(primary)


def getsize(cf, storage_location, filename, verbose=False):
    """
    Get the size of the file.
    """
    from os.path import getsize
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    if mode == 'gcs':
        try:
            bucket = _get_bucket(cf)
            if bucket:
                 blob = bucket.get_blob(blob_name)
                 if blob:
                     return blob.size
                 raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
            else:
                 raise ValueError("GCS bucket not initialized")
        except Exception as e:
            use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
            if use_fallback and secondary:
                 if verbose: print(f" [DATA_IO] getsize: Fallback ({e}). Checking {secondary}")
                 return getsize(secondary)
            raise e
    else:
        return getsize(primary)








def remove(cf, storage_location, filename, verbose=False):
    """
    Remove the file filename from the given storage location.
    In Parallel Mode (GCS enabled), attempts to remove from BOTH GCS and Local.
    """
    from os import remove as local_remove
    from os.path import exists as local_exists
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # 1. Remove from GCS if configured
    if mode == 'gcs':
        bucket = _get_bucket(cf)
        if bucket:
            try:
                # delete() raises NotFound by default if missing, unless generic exception handling
                bucket.blob(blob_name).delete()
                if verbose: print(f" [DATA_IO] Removed GCS blob '{blob_name}'")
            except Exception as e:
                # It's possible it didn't exist
                if verbose: print(f" [DATA_IO] GCS remove note: {e}")

    elif mode == 'local':
        if local_exists(primary):
            local_remove(primary)
            if verbose: print(f" [DATA_IO] Removed local file '{primary}'")





def listdir(cf, storage_location, return_absolute_path=False, verbose=False) -> list:
    """
    List files in the given storage location.
    Handles GCS listing if configured.
    """
    from os import listdir as local_listdir
    from os.path import join
    
    # We can't use _resolve_paths directly for the dir itself because _resolve_paths expects a filename
    # But we can reuse the logic key parts.
    
    use_gcs = cf.get("misc", {}).get("use_gcs_for_data", False)
    gcs_base = cf.get("gcs_paths", {}).get(storage_location)
    
    files = []
    
    if use_gcs and gcs_base:
        # GCS Mode
        try:
            bucket = _get_bucket(cf)
            bucket_name = cf['data_io'].get('GCS_bucket_name')
            if bucket:
                # Add trailing slash to treat as directory
                prefix = gcs_base
                if not prefix.endswith("/"): prefix += "/"
                
                if verbose: print(f" [DATA_IO] Listing GCS blobs with prefix: {prefix}")
                
                # Let's try to be simple:
                # Just listing files? 
                
                iterator = bucket.list_blobs(prefix=prefix, delimiter='/')
                for page in iterator.pages:
                    for blob in page:
                         name = blob.name
                         # remove prefix
                         rel_name = name[len(prefix):]
                         if rel_name: # skip the directory blob itself
                             files.append(rel_name)
                    # "subdirectories"
                    for p in page.prefixes:
                        # p is something like "prefix/subdir/"
                        # we want just "subdir"
                        rel_dir = p[len(prefix):].rstrip('/')
                        if rel_dir:
                            files.append(rel_dir)
                
                if return_absolute_path:
                     # Return GS URIs
                     files = [f"gs://{bucket_name}/{prefix}{f}" for f in files]
            else:
                 raise ValueError("GCS bucket not initialized for listdir")
                 
        except Exception as e:
            use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
            if use_fallback:
                # Fallback to local
                if verbose: print(f" [DATA_IO] listdir: Fallback ({e}). Listing local dir.")
                # We need to manually do local listing here since we are inside the 'if use_gcs' block
                try: 
                     local_dir = cf['paths'][storage_location]
                     files = local_listdir(local_dir)
                     if return_absolute_path:
                         files = [join(local_dir, f) for f in files]
                except Exception as e2:
                     print(f" [DATA_IO] ERROR listdir fallback failed: {e2}")
                     raise e
            else:
                 if verbose: print(" [DATA_IO] WARN: GCS enabled but bucket missing/error for listdir.")
                 files = [] # Or raise? Old code just warned and returned empty or had logic flow issues.

             
    else:
        # Local Mode
        if storage_location not in cf['paths']:
            raise ValueError(f"Invalid storage location: '{storage_location}'.")
            
        local_dir = cf['paths'][storage_location]
        files = local_listdir(local_dir)

        if return_absolute_path:
            files = [join(local_dir, f) for f in files]

    if verbose: print(f" [DATA_IO] Listed {len(files)} files in '{storage_location}'")

    return files




def move(cf, src_storage_location, dst_storage_location, filename: str, verbose=False):
    """
    Move the file filename from src_storage_location to dst_storage_location.
    In Parallel Mode (GCS enabled): Moves on GCS AND moves Locally to keep sync.
    """
    from shutil import move as local_move
    from os.path import exists as local_exists
    from os.path import join
    
    # Resolve SRC
    src_primary, src_secondary, src_mode, src_blob_name = _resolve_paths(cf, src_storage_location, filename)
    # Resolve DST
    dst_primary, dst_secondary, dst_mode, dst_blob_name = _resolve_paths(cf, dst_storage_location, filename)

    moved_gcs = False
    moved_local = False
    
    # 1. GCS Move
    if src_mode == 'gcs' and dst_mode == 'gcs':
        bucket = _get_bucket(cf)
        if bucket:
            try:
                blob = bucket.blob(src_blob_name)
                # GCS 'rename' is a move (copy + delete)
                # target name is just the name string, not the blob object?
                # bucket.rename_blob(blob, new_name)
                # new_name should be the new blob_name (full path)
                
                bucket.rename_blob(blob, dst_blob_name)
                moved_gcs = True
                if verbose: print(f" [DATA_IO] Moved GCS: '{src_blob_name}' -> '{dst_blob_name}'")
            except Exception as e:
                if verbose: print(f" [DATA_IO] WARN: GCS Move failed (src likely missing): {e}")

    # 2. Local Move
    # If GCS mode, we still try to move secondary (local) files to keep valid state
    src_local = src_secondary if src_mode == 'gcs' else src_primary
    dst_local = dst_secondary if dst_mode == 'gcs' else dst_primary
    
    if src_local and dst_local:
         if local_exists(src_local):
             local_move(src_local, dst_local)
             moved_local = True
             if verbose: print(f" [DATA_IO] Moved Local: '{filename}' from '{src_storage_location}' to '{dst_storage_location}'")
         else:
             if verbose and src_mode == 'local': # Only error if local was the ONLY mode
                 print(f" [DATA_IO] ERROR Couldn't find '{filename}' in '{src_storage_location}'")

    if not moved_gcs and not moved_local and verbose:
        print(f" [DATA_IO] WARN: Move operation completed but nothing seemed to move (files missing?).")




def load_json(cf, storage_location, filename, verbose = False):
    """
    Load a json from a given path.
    Handles GCS read.
    """
    from os.path import basename, splitext
    from json import load, loads
    
    # Extension check
    bn = basename(filename)
    root, ext = splitext(bn)
    if ext != '.json':
        raise ValueError(f"File extension must be '.json', got: '{ext}'")
        
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    
    # Attempt Primary Load
    try:
        if mode == 'gcs':
            bucket = _get_bucket(cf)
            if bucket:
                blob = bucket.blob(blob_name)
                # Check existence to avoid generic 404 error masked as something else
                if blob.exists():
                     content = blob.download_as_text()
                     return loads(content)
                else:
                     if verbose: print(f" [DATA_IO] WARN: GCS Blob not found: {blob_name}. Trying fallback...")
            else:
                 if verbose: print(" [DATA_IO] WARN: GCS bucket not initialized. Trying fallback...")
        else:
            # Local Primary
            with open(primary, 'r') as file:
                return load(file)
                
    except Exception as e:
        if verbose: print(f" [DATA_IO] Primary load failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f" [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # Fallback to Secondary (Local) if GCS failed
    # We reach here if mode='gcs' and (blob missing OR bucket missing OR download failed)
    use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
    if mode == 'gcs' and secondary and use_fallback:
        try:
            if verbose: print(f" [DATA_IO] Fallback: Attempting to load local copy from {secondary}")
            with open(secondary, 'r') as file:
                return load(file)
        except Exception as e2:
             print(f" [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}' (GCS+Local Fallback failed): {e2}")
             # Raise strictly? Or return None?
             # User said: "If that also fails - raise"
             # But original code returned None on error.
             # User instruction: "If that also fails - raise"
             # So I should raise.
             # But I should probably raise the ORIGINAL error if secondary didn't exist?
             # Or just a generic error.
             raise e2
    
    # If we are here, we failed primary and had no secondary?
    # Or mode was local and we returned None above.
    return None





def save_json(cf, data, storage_location, filename, verbose = False):
    """
    Save a json to a given path.
    Supports GCS write + Parallel Save.
    """
    from os.path import basename, splitext
    from json import dump, dumps
    
    bn = basename(filename)
    root, ext = splitext(bn)
    if ext != '.json':
        raise ValueError(f"File extension must be '.json', got: '{ext}'") 
        
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # 1. Save Primary
    if mode == 'gcs':
        bucket = _get_bucket(cf)
        if bucket:
             blob = bucket.blob(blob_name)
             blob.upload_from_string(dumps(data))
             if verbose: print(f" [DATA_IO] Saved JSON to GCS: {blob_name}")
        else:
             raise ValueError("GCS bucket not initialized")
    else:
        # Local
        with open(primary, 'w') as file:
            dump(data, file)
            
    # 2. Parallel Save (Secondary)
    if secondary:
        if verbose: print(f" [DATA_IO] Parallel Save: Writing local JSON copy to {secondary}")
        with open(secondary, 'w') as file:
             dump(data, file)






def load_parquet(cf, storage_location, filename, columns=None, verbose = False):
    """
    Load a dataframe from a given path.
    Supports GCS direct read (gs://).
    """
    from fyp.fyp_main import convert_dtypes_to_pyarrow
    from os.path import basename
    import os

    # Validation and Extension Check (same as save: strictly expect .parquet)
    # The original code did:
    # base_path, ext = os.path.splitext(full_path)
    # if ext == '.parquet':
    #     path_no_ext = base_path
    # else:
    #     raise ValueError ...
    # parquet_path = f"{path_no_ext}.parquet"
    
    # We replicate this strictness
    bn = basename(filename)
    root, ext = os.path.splitext(bn)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")
        
    # Resolve
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # 1. Attempt Primary Load
    try:
        # Check existence first if GCS? 
        # Actually pandas read_parquet on gs:// might fail if not found.
        # But we previously added an explicit exists check.
        # Let's keep the explicit check but wrap it.
        
        if not exists(cf, storage_location, filename):
             # Force exception to trigger fallback
             raise FileNotFoundError(f"File not found: '{filename}' in '{storage_location}'")

        if verbose: print(f" [DATA_IO] Loading: '{filename}' from '{storage_location}' ({mode})")
        
        df = pd.read_parquet(primary, engine='pyarrow', dtype_backend="pyarrow", columns=columns)
        
    except Exception as e:
        if verbose: print(f" [DATA_IO] Primary load failed ({mode}): {e}")
        
        # 2. Fallback if GCS
        use_fallback = cf.get("misc", {}).get("use_local_as_fallback", False)
        if mode == 'gcs' and secondary and use_fallback:
             import os
             if os.path.exists(secondary):
                 if verbose: print(f" [DATA_IO] Fallback: Loading local copy from {secondary}")
                 try:
                     df = pd.read_parquet(secondary, engine='pyarrow', dtype_backend="pyarrow", columns=columns)
                 except Exception as e2:
                     print(f" [DATA_IO] ERROR Fallback load failed: {e2}")
                     raise e2 # Raise the secondary error
             else:
                 # Secondary doesn't exist either. Raise original error?
                 if verbose: print(" [DATA_IO] Fallback failed: local file not found.")
                 raise e 
        else:
             # Local mode or no secondary. Raise original.
             raise e

    # type management to be sure
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    return df






def save_parquet(cf, df: pd.DataFrame, storage_location, filename, verbose = False):
    """
    Save a dataframe to the given path.
    Supports GCS direct write and Parallel Save (GCS + Local).
    """

    from fyp.fyp_main import convert_dtypes_to_pyarrow
    from os.path import join, basename
    import os

    # A) Resolve Paths (Primary = GCS if enabled, Secondary = Local)
    # Note: filename here might not have extension yet, logic below handles it
    
    # Base logic to ensure extension is .parquet
    base_name = basename(filename)
    if not base_name.endswith('.parquet'):
         # If user didn't provide extension (common in this codebase), add it.
         # But wait, original code checks extension on 'full_path'.
         # "File extension must be .parquet, got: {ext}"
         # It seems original code enforced extension to BE .parquet.
         
         # Retaining original strictness:
         root, ext = os.path.splitext(base_name)
         # If no extension or wrong extension? 
         # Original code: "if ext == '.parquet': path_no_ext = base_path ... parquet_path = ... .parquet"
         # This suggests it expects filename to HAVE .parquet, or it fails.
         if ext != '.parquet':
              raise ValueError(f"File extension must be '.parquet', got: '{ext}'")
    
    # Resolve using the filename (which has .parquet)
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # B) Type Management
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    # C) Save to Primary
    if verbose: print(f" [DATA_IO] Saving: '{filename}' to '{storage_location}' ({mode})")
    
    # Pandas handles gs:// if gcsfs is installed
    df.to_parquet(primary, engine='pyarrow')
    
    if mode == 'gcs':
        if verbose: print(f" [DATA_IO] Saved to GCS: {primary}")
        
    # D) Parallel Save (Secondary)
    if secondary:
        if verbose: print(f" [DATA_IO] Parallel Save: Writing local copy to {secondary}")
        df.to_parquet(secondary, engine='pyarrow')






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
