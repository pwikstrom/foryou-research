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
    Resolve the given storage location and filename to local uri or local path:
    1. A Primary Path (GCS URI if enabled, else Local Path)
    2. A Secondary Path (Local Path if GCS is enabled, else None)
    3. Mode ('gcs' or 'local')
    4. GCS Blob Name (if mode is 'gcs', else None)
    
    Returns:
        tuple: (primary_path, secondary_path, mode, blob_name)
    """
    from os.path import join, basename
    from fyp.fyp_main import connect_to_google
    
    if (cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None) or (storage_location == 'cache' and cf['data_io']['use_gcs_for_cache'] and cf['data_io']['bucket'] is None):
        cf = connect_to_google(cf)

    
    # 1. Validate Location
    if storage_location not in cf['paths']:
        valid_locs = ', '.join(list(cf['paths'].keys()))
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use: {valid_locs}")



    # 2. Check GCS Configuration
    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = cf['data_io']['use_gcs_for_cache']
    else:
        use_gcs = cf['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = cf['gcs_paths'][storage_location]

    bucket_name = cf['data_io']['GCS_bucket_name']
    
    # 3. Resolve
    if use_gcs and gcs_base:
        if not bucket_name:
            raise ValueError("GCS bucket name not found in config")
             
        # Construct Blob Name
        blob_name = f"{gcs_base}/{filename}"
        blob_name = blob_name.replace("//", "/")
        gcs_uri = f"gs://{bucket_name}/{blob_name}"
        
        return (gcs_uri, None, 'gcs', blob_name)
    else:
        # Local
        local_path = join(cf['paths'][storage_location], filename)
        return (local_path, None, 'local', None)



def _get_bucket(cf):
    """Retrieve the bucket object from config."""
    w = cf['data_io']['bucket']
    return w







def find_key_value_in_pq_metadata(
    cf,
    storage_location,
    filename,
    the_key
    ):
    from pyarrow.parquet import read_metadata as pq_read_metadata
    from json import loads as json_loads

    meta = pq_read_metadata(_resolve_paths(cf, storage_location, filename)[0])
    file_metadata_dict = meta.metadata  # This is a dictionary of {bytes: bytes}

    for k in file_metadata_dict:
        try:
            some_dict = json_loads(file_metadata_dict[k].decode('utf-8'))
            if some_dict.get(the_key,None) is not None:
                return some_dict.get(the_key)
        except:
            pass
    return None






def exists(cf, storage_location, filename, verbose=False) -> bool:
    """
    Check if the file filename exists in the given storage location.
    Transparently handles local or GCS checks.
    """
    from os.path import exists as local_exists
    from fyp.fyp_main import connect_to_google


    if (cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None) or (storage_location == 'cache' and cf['data_io']['use_gcs_for_cache'] and cf['data_io']['bucket'] is None):
        cf = connect_to_google(cf)
    
    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    #if verbose: 
    if verbose:
        print(f"    [DATA_IO] exists: Checking {primary}")
    
    if mode == 'gcs':
        bucket = _get_bucket(cf)
        gcs_exists = False
        if bucket:
            # Note: Checking blob existence involves a metadata request
            gcs_exists = bucket.blob(blob_name).exists()
            if gcs_exists:
                return True
        else:
            raise ValueError("    [DATA_IO]: GCS mode enabled but bucket missing.")

        return False
    else:
        return local_exists(primary)






def getctime(cf, storage_location, filename, verbose=False):
    """
    Get the creation time of the file filename.
    """
    from os.path import getctime
    from fyp.fyp_main import connect_to_google
    
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    
    if mode == 'gcs':
        bucket = cf['data_io']['bucket']
        if bucket:
            blob = bucket.get_blob(blob_name)
            if blob and blob.time_created:
                return blob.time_created.timestamp()
            # If blob doesn't exist or time missing
            raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
            raise ValueError("GCS bucket not initialized")
    else:
        return getctime(primary)





def getmtime(cf, storage_location, filename, verbose=False):
    """
    Get the modification time of the file.
    """
    from os.path import getmtime
    from fyp.fyp_main import connect_to_google
    
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    if mode == 'gcs':
        bucket = cf['data_io']['bucket']
        if bucket:
            blob = bucket.get_blob(blob_name)
            if blob and blob.updated:
                return blob.updated.timestamp()
            raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
            raise ValueError("GCS bucket not initialized")
    else:
        return getmtime(primary)






def getsize(cf, storage_location, filename, verbose=False):
    """
    Get the size of the file.
    """
    from os.path import getsize
    from fyp.fyp_main import connect_to_google
    
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    if mode == 'gcs':
        bucket = cf['data_io']['bucket']
        if bucket:
                blob = bucket.get_blob(blob_name)
                if blob:
                    return blob.size
                raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
                raise ValueError("GCS bucket not initialized")
    else:
        return getsize(primary)








def remove(cf, storage_location, filename, verbose=False):
    """
    Remove the file filename from the given storage location.
    """
    from os import remove as local_remove
    from os.path import exists as local_exists
    from fyp.fyp_main import connect_to_google
    
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # 1. Remove from GCS if configured
    if mode == 'gcs':
        bucket = cf['data_io']['bucket']
        if bucket:
            try:
                # delete() raises NotFound by default if missing, unless generic exception handling
                bucket.blob(blob_name).delete()
                if verbose: print(f"    [DATA_IO] Removed GCS blob '{blob_name}'")
            except Exception as e:
                # It's possible it didn't exist
                if verbose: print(f"    [DATA_IO] GCS remove note: {e}")

    else:
        if local_exists(primary):
            local_remove(primary)
            if verbose: print(f"    [DATA_IO] Removed local file '{primary}'")







def listdir(cf, storage_location, return_absolute_path=False, verbose=False) -> list:
    """
    List files in the given storage location.
    Handles GCS listing if configured.
    """
    from os import listdir as local_listdir
    from os.path import join
    from fyp.fyp_main import connect_to_google
    
    # I can't use _resolve_paths directly for the dir itself because _resolve_paths expects a filename

    if (cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None) or (storage_location == 'cache' and cf['data_io']['use_gcs_for_cache'] and cf['data_io']['bucket'] is None):
        cf = connect_to_google(cf)
    
    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = cf['data_io']['use_gcs_for_cache']
    else:
        use_gcs = cf['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = cf['gcs_paths'][storage_location]


    files = []

    if use_gcs and gcs_base:
        # GCS Mode
        try:
            bucket = cf['data_io']['bucket']
            bucket_name = cf['data_io']['GCS_bucket_name']
            if bucket:
                # Add trailing slash to treat as directory
                prefix = gcs_base
                if not prefix.endswith("/"): prefix += "/"
                
                if verbose: print(f"    [DATA_IO] Listing GCS blobs with prefix: {prefix}")
                
                
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
            if verbose: print("    [DATA_IO] WARN: GCS enabled but bucket missing/error for listdir.")
            files = [] # Or raise? Old code just warned and returned empty or had logic flow issues.

             
    else:
        # Local Mode
        if storage_location not in cf['paths']:
            raise ValueError(f"Invalid storage location: '{storage_location}'.")
        local_dir = cf['paths'][storage_location]

        if verbose: print(f"    [DATA_IO] Listing files in local storage: {local_dir}")

        files = local_listdir(local_dir)

        if return_absolute_path:
            files = [join(local_dir, f) for f in files]

    if verbose: print(f"    [DATA_IO] Listed {len(files)} files in local storage '{storage_location}'")

    return files







def move(cf, src_storage_location, dst_storage_location, filename: str, verbose=False):
    """
    Move the file filename from src_storage_location to dst_storage_location.
    """
    from shutil import move as local_move
    from os.path import exists as local_exists
    from os.path import join as local_join
    from fyp.fyp_main import connect_to_google
    from os import remove as local_remove



    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    
    # Resolve DST
    dst_primary, _, dst_mode, dst_blob_name = _resolve_paths(cf, dst_storage_location, filename)


    # temp to storage_location
    if src_storage_location == "temp":
        
        src_path = local_join(cf['paths']['temp'], filename)
        if not local_exists(src_path):
             if verbose: print(f"    [DATA_IO] ERROR: Source file not found in temp: '{src_path}'")
             return

        if dst_mode == 'gcs':
            bucket = _get_bucket(cf)
            if bucket:
                try:
                    blob = bucket.blob(dst_blob_name)
                    blob.upload_from_filename(src_path)
                    if verbose: print(f"    [DATA_IO] Uploaded from temp to GCS: '{src_path}' -> '{dst_blob_name}'")
                    # Remove local temp file after successful upload
                    local_remove(src_path)
                except Exception as e:
                    if verbose: print(f"    [DATA_IO] WARN: Failed to upload/move from temp to GCS: {e}")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized for temp move.")

        elif dst_mode == 'local':
             if dst_primary:
                local_move(src_path, dst_primary)
                if verbose: print(f"    [DATA_IO] Moved from temp to local: '{src_path}' -> '{dst_primary}'")
             else:
                 if verbose: print(f"    [DATA_IO] ERROR: Destination path resolution failed for local move.")
        
        return

    # Resolve SRC
    src_primary, _, src_mode, src_blob_name = _resolve_paths(cf, src_storage_location, filename)

    # GCS Move
    if src_mode == 'gcs' and dst_mode == 'gcs':
        bucket = _get_bucket(cf)
        if bucket:
            try:
                blob = bucket.blob(src_blob_name)
                # GCS 'rename' is a move (copy + delete)
                
                bucket.rename_blob(blob, dst_blob_name)
                if verbose: print(f"    [DATA_IO] Moved GCS: '{src_blob_name}' -> '{dst_blob_name}'")
            except Exception as e:
                if verbose: print(f"    [DATA_IO] WARN: GCS Move failed (src likely missing): {e}")

    # Local Move
    elif src_mode == 'local' and dst_mode == 'local':
    
        if src_primary and dst_primary:
            local_move(src_primary, dst_primary)
            if verbose: print(f"    [DATA_IO] Moved Local: '{filename}' from '{src_storage_location}' to '{dst_storage_location}'")
        else:
            if verbose and src_mode == 'local':
                print(f"    [DATA_IO] ERROR Couldn't find '{filename}' in '{src_storage_location}'")
    
        









# read a file with one json object per line and return a list of dictionaries
def read_ndjson_file(cf, storage_location, filename, verbose=False):
    from os.path import basename, splitext
    from json import load, loads
    from fyp.fyp_main import connect_to_google


    # Extension check
    bn = basename(filename)
    root, ext = splitext(bn)
    if ext != '.ndjson':
        if verbose: 
            print(f"    [DATA_IO] WARN: File extension is not '.ndjson': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)
    
    # Attempt Primary Load
    data = []
    if True:#try:
        if mode == 'gcs':
            bucket = _get_bucket(cf)
            if bucket:
                blob = bucket.blob(blob_name)
                # Check existence to avoid generic 404 error masked as something else
                if blob.exists():
                    with blob.open("r") as file:
                        for line in file:
                            line = '{"label":"' + cf["misc"]["label"] + '",' + line[1:]
                            line = '{"log_script":"' + root + '",' + line[1:]
                            data.append(loads(line))
                    return data
                else:
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local Primary
            with open(primary, 'r') as file:
                for line in file:
                    line = '{"label":"' + cf["misc"]["label"] + '",' + line[1:]
                    line = '{"log_script":"' + root + '",' + line[1:]
                    data.append(loads(line))
            return data
                
    if False:#except Exception as e:
        if verbose: print(f"    [DATA_IO] Primary load failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # If we are here, things haven't gone very well have they
    return None









def load_json(cf, storage_location, filename, verbose = False):
    """
    Load a json from a given path.
    Handles GCS read.
    """
    from os.path import basename, splitext
    from json import load, loads
    from fyp.fyp_main import connect_to_google
    
    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)


    # Extension check
    bn = basename(filename)
    root, ext = splitext(bn)
    if ext != '.json':
        if verbose: print(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
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
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local Primary
            with open(primary, 'r') as file:
                return load(file)
                
    except Exception as e:
        if verbose: print(f"    [DATA_IO] Loading json failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # If we are here, things haven't gone very well have they
    return None





def save_json(cf, data, storage_location, filename, verbose = False):
    """
    Save a json to a given path.
    Supports GCS write + Parallel Save.
    """
    from os.path import basename, splitext
    from json import dump, dumps
    from fyp.fyp_main import connect_to_google

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

    bn = basename(filename)
    root, ext = splitext(bn)
    if ext != '.json':
        if verbose: print(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # 1. Save Primary
    if mode == 'gcs':
        bucket = _get_bucket(cf)
        if bucket:
             blob = bucket.blob(blob_name)
             blob.upload_from_string(dumps(data))
             if verbose: print(f"    [DATA_IO] Saved JSON to GCS: {blob_name}")
        else:
             raise ValueError("GCS bucket not initialized")
    else:
        # Local
        with open(primary, 'w') as file:
            dump(data, file)
    
    return 0
            






def load_parquet(
        cf,
        storage_location,
        filename, # if filename == '*' -> load all parquet files in storage_location
        columns=None,
        filters=None,
        verbose = False,
    ):
    """
    Load a dataframe from a given path.
    Supports GCS direct read (gs://).
    """
    from fyp.fyp_main import convert_dtypes_to_pyarrow, connect_to_google
    from os.path import basename
    import os

    import pyarrow.parquet as pq
    import gcsfs
    from datetime import datetime

    def _renamed(s):
        fixer_upper = [
            ("B_local_","T_local_"),
            ("D_local_","T_local_"),
            (".","_"),
            ("data_",""),
            ("source_url_","source_"),
            ("_collected",""),
            ("framing_analysis_","FA_"),
            ("cultural_representation_analysis_","CRA_"),
            ("ideological_analysis_","IA_"),

        ]
        for fu in fixer_upper:
            s = s.replace(fu[0],fu[1])
        return s

    t1 = datetime.now()

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)

        # Initialize GCS filesystem
        fs = gcsfs.GCSFileSystem()


    # if we are to load all parquet files in this location (and it is gcs)
    if filename == "*" and cf['data_io']['use_gcs_for_data']:
        gcs_base = cf.get("gcs_paths", {}).get(storage_location)
        bucket_name = cf['data_io'].get('GCS_bucket_name')
        files = fs.glob(f'gs://{bucket_name}/{gcs_base}/*.parquet')
        files = ["gs://" + f for f in files]

        # if specific columns are to be loaded, we need to make sure the cols actually exist in the parquet files
        if columns is not None:
            # Read parquet schema
            with fs.open(files[0]) as f: # assume all files have the same schema so it's enough to check the first one
                parquet_schema = pq.read_schema(f)
            existing_cols = parquet_schema.names

            # iterate over the parquet columns and check if they included in the requested columns list
            # OR if a renamed version of the parquet columns are included in the requested columns list
            # I have to do it this way since at some stage in the processing, I'm changing renaming the columns
            # Yes - it's a bit confusing.  
            confirmed_columns = []
            for ec in existing_cols:
                if ec in columns or _renamed(ec) in columns:
                    confirmed_columns += [ec]
                else:
                    print(f"    [DATA_IO] Parquet column '{ec}' not loaded since not requested")

            columns = list(set(confirmed_columns))
            if verbose:
                print(f"    [DATA_IO] Column selection: {columns}")


        if verbose: 
            print(f"    [DATA_IO] Loading: all parquet files from folder '{storage_location}' (gcs)... ")
        df = pd.read_parquet(
            files,
            filesystem=fs,
            engine='pyarrow',
            use_threads=True,
            dtype_backend="pyarrow",
            columns=columns,
            filters=filters)
        if verbose: 
            print(f"    [DATA_IO] ...done (shape: {df.shape})")

        # type management to be sure
        df = convert_dtypes_to_pyarrow(df, verbose=verbose)

        if verbose:
            t2 = datetime.now()
            print(f"    [DATA_IO] Loaded parquet(s) shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

        return df


    # if we have arrived here, we are loading a single parquet file
    root, ext = os.path.splitext(filename)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")

    if not exists(cf, storage_location, filename):
        raise FileNotFoundError(f"File not found: '{filename}' in '{storage_location}'")

    # Resolve path
    primary, _, mode, _ = _resolve_paths(cf, storage_location, filename)

    # if specific columns are to be loaded, we need to make sure the cols actually exist in the parquet files
    if columns is not None:
        if mode == 'gcs':
            # Read parquet schema
            with fs.open(primary) as f:
                parquet_schema = pq.read_schema(f)
        else:
            # Local
            parquet_schema = pq.read_schema(primary)
        existing_cols = parquet_schema.names
        columns = [c for c in columns if c in existing_cols]
        if verbose:
            print(f"    [DATA_IO] Column selection: {columns}")


    if verbose: print(f"    [DATA_IO] Loading: '{filename}' from '{storage_location}' ({mode})...")
    df = pd.read_parquet(
        primary,
        engine='pyarrow',
        dtype_backend="pyarrow",
        use_threads=True,
        columns=columns,
        filters=filters)

    # type management to be sure
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    if verbose:
        t2 = datetime.now()
        print(f"    [DATA_IO] ...done. Shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

    return df



import threading
from concurrent.futures import ThreadPoolExecutor

# Create a global lock object
file_lock = threading.Lock()

def save_parquet(cf, df: pd.DataFrame, storage_location, filename, verbose = False):
    """
    Save a dataframe to the given path.
    Supports GCS direct write and Parallel Save (GCS + Local).
    """

    this_df = df.copy()

    from fyp.fyp_main import convert_dtypes_to_pyarrow, connect_to_google
    from os.path import join, basename
    import os

    if cf['data_io']['use_gcs_for_data'] and cf['data_io']['bucket'] is None:
        cf = connect_to_google(cf)


    # A) Resolve Paths (Primary = GCS if enabled, Secondary = Local)
    # Note: filename here might not have extension yet, logic below handles it
    
    # Base logic to ensure extension is .parquet
    base_name = basename(filename)
    root, ext = os.path.splitext(base_name)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")
    
    # Resolve using the filename (which has .parquet)
    primary, secondary, mode, blob_name = _resolve_paths(cf, storage_location, filename)

    # B) Type Management
    this_df = convert_dtypes_to_pyarrow(this_df, verbose=verbose)

    # C) Save to Primary
    if verbose: print(f"    [DATA_IO] Saving: '{filename}' to '{storage_location}' ({mode})")

    # To get the total memory usage of the DataFrame in bytes:
    memory_per_column = this_df.memory_usage(deep=True) 
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)

    if verbose:
        print(f"    [DATA_IO] Total DF memory usage: {total_memory_mb:.2f} MB.")
        print(f"    [DATA_IO] Saving '{filename}' to '{storage_location}' ({mode})...")

    if total_memory_mb > 100:
        my_compression_level = 7
    elif total_memory_mb > 10:
        my_compression_level = 5
    elif total_memory_mb > 1:
        my_compression_level = 3
    else:
        my_compression_level = 0
    
    if storage_location == "cache":
        def alert_finished(future):
            if future.exception():
                if verbose:
                    print(f"   [DATA_IO ASYNC] Parquet save failed: {future.exception()}")
            else:
                if verbose:
                    print("    [DATA_IO ASYNC] Parquet save succeeded.")
                    
        def safe_save(df, path):
            # This 'with' block ensures only one thread can execute the save at a time
            with file_lock:
                if verbose:
                    print(f"    [DATA_IO ASYNC] Starting save to {path}... (locked)")
                df.to_parquet(path, engine='pyarrow', compression="zstd", compression_level=my_compression_level)
                if verbose:
                    print(f"    [DATA_IO ASYNC] Finished save to {path}. (unlocked)")

        executor = ThreadPoolExecutor(max_workers=2)

        future = executor.submit(safe_save, this_df.copy(), primary)
        future.add_done_callback(alert_finished)

    else:
        this_df.to_parquet(primary, engine='pyarrow', compression="zstd", compression_level=my_compression_level)
    
    
    if verbose: print(f"    [DATA_IO] ...moving on. Shape: {this_df.shape}")
    
    return this_df






# ------------------------------------------------------------------------------
# Data Management Utilities
# ------------------------------------------------------------------------------

def get_study_export_files(cf = None, study_name = None):
    #from os import listdir
    #from os.path import join, getmtime
    from fyp.fyp_main import initialize
    from numpy import mean as np_mean
    from datetime import datetime

    if cf is None:
        cf = initialize()
    
    if study_name is None:
        raise ValueError("study_name is required")

    export_file_categories = ["HALF_BAKED", "PCA", "LOG", "RECODED"]
    study_files = {category: [] for category in export_file_categories}
    for fn in listdir(cf, "exports"):
        if fn.startswith(study_name) and fn.endswith('.parquet'):
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
    from fyp.fyp_main import initialize
    from fyp.recode_variables import get_group_factors_from_var_schema
    import pandas as pd
    
    if cf is None:
        cf = initialize()
        
    if study_name is None:
        raise ValueError("study_name is required")

    group_factors = get_group_factors_from_var_schema(cf = cf)

    details = []

    try:
        files = [f for f in listdir(cf, "exports") if f.startswith(study_name) and f.endswith('.parquet')]
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
