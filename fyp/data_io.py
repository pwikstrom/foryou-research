# -*- coding: utf-8 -*-
"""
Script Name: data_io.py
Description: Centralized I/O operations for dataframes.
Author: Patrik
"""



import datetime as _dt
from fyp.types import convert_dtypes_to_pyarrow
import shutil
import gcsfs
import json
import os
import pandas as pd
import pyarrow.parquet as pq
import threading
from concurrent.futures import ThreadPoolExecutor
from fyp.fyp_config import fyp_cf



# ------------------------------------------------------------------------------
# Path Resolution & GCS Helpers
# ------------------------------------------------------------------------------

def _resolve_paths(storage_location: str = "cache", filename: str = ""):
    """
    Resolve the given storage location and filename to local uri or local path:
    1. A Primary Path (GCS URI if enabled, else Local Path)
    2. A Secondary Path (Local Path if GCS is enabled, else None)
    3. Mode ('gcs' or 'local')
    4. GCS Blob Name (if mode is 'gcs', else None)
    
    Returns:
        tuple: (primary_path, secondary_path, mode, blob_name)
    """
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    # 1. Validate Location
    if storage_location not in fyp_cf['paths']:
        valid_locs = ', '.join(list(fyp_cf['paths'].keys()))
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use: {valid_locs}")



    # 2. Check GCS Configuration
    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = fyp_cf['data_io']['use_gcs_for_cache']
    else:
        use_gcs = fyp_cf['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = fyp_cf['gcs_paths'][storage_location]

    bucket_name = fyp_cf['data_io']['GCS_bucket_name']
    
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
        local_path = os.path.join(fyp_cf['paths'][storage_location], filename)
        return (local_path, None, 'local', None)



def _get_bucket():
    """Retrieve the bucket object from config."""
    w = fyp_cf['data_io']['bucket']
    return w








def get_recent_files(storage_location: str = "cache", suffix: str = None, how_recent: int = 10):

    current_time = datetime.now()
    recent_files = []

    for filename in data_io.listdir(storage_location = storage_location):
        #file_path = join(storage_location, filename)
        if suffix is None or filename.endswith(suffix):
            modified_time = datetime.fromtimestamp(data_io.getmtime(storage_location, filename))
            created_time = datetime.fromtimestamp(data_io.getctime(storage_location, filename))
            time_difference = current_time - max(modified_time, created_time)
            if time_difference < timedelta(minutes=how_recent):
                recent_files.append({"filename":filename, "mtime":modified_time, "ctime":created_time})

    return sorted(recent_files,key=lambda x: x["mtime"], reverse=True)








def find_key_value_in_pq_metadata(
    storage_location: str = "cache",
    filename: str = "",
    the_key: str = ""
    ):

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")
    
    if the_key == "":
        raise ValueError("Key cannot be empty")

    meta = pq.read_metadata(_resolve_paths(storage_location, filename)[0])
    file_metadata_dict = meta.metadata  # This is a dictionary of {bytes: bytes}

    for k in file_metadata_dict:
        try:
            some_dict = json.loads(file_metadata_dict[k].decode('utf-8'))
            if some_dict.get(the_key,None) is not None:
                return some_dict.get(the_key)
        except:
            pass
    return None






def exists(storage_location: str = "cache", filename: str = "", verbose: bool = False) -> bool:
    """
    Check if the file filename exists in the given storage location.
    Transparently handles local or GCS checks.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)
    #if verbose:
    #    print(f"    [DATA_IO] exists: Checking {primary}")
    
    if mode == 'gcs':
        bucket = _get_bucket()
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
        return os.path.exists(primary)






def getctime(storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Get the creation time of the file filename.
    """
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)
    
    if mode == 'gcs':
        bucket = fyp_cf['data_io']['bucket']
        if bucket:
            blob = bucket.get_blob(blob_name)
            if blob and blob.time_created:
                return blob.time_created.timestamp()
            # If blob doesn't exist or time missing
            raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
            raise ValueError("GCS bucket not initialized")
    else:
        return os.path.getctime(primary)





def getmtime(storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Get the modification time of the file.
    """
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
            blob = bucket.get_blob(blob_name)
            if blob and blob.updated:
                return blob.updated.timestamp()
            raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
            raise ValueError("GCS bucket not initialized")
    else:
        return os.path.getmtime(primary)






def getsize(storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Get the size of the file.
    """
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
                blob = bucket.get_blob(blob_name)
                if blob:
                    return blob.size
                raise FileNotFoundError(f"GCS Blob not found: {blob_name}")
        else:
                raise ValueError("GCS bucket not initialized")
    else:
        return os.path.getsize(primary)








def remove(storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Remove the file filename from the given storage location.
    """
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    # 1. Remove from GCS if configured
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
            try:
                # delete() raises NotFound by default if missing, unless generic exception handling
                bucket.blob(blob_name).delete()
                if verbose: print(f"    [DATA_IO] Removed GCS blob '{blob_name}'")
            except Exception as e:
                # It's possible it didn't exist
                if verbose: print(f"    [DATA_IO] GCS remove note: {e}")

    else:
        if os.path.exists(primary):
            os.remove(primary)
            if verbose: print(f"    [DATA_IO] Removed local file '{primary}'")







def listdir(storage_location: str = "cache", return_absolute_path: bool = False, verbose: bool = False) -> list:
    """
    List files in the given storage location.
    Handles GCS listing if configured.
    """
    
    
    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = fyp_cf['data_io']['use_gcs_for_cache']
    else:
        use_gcs = fyp_cf['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = fyp_cf['gcs_paths'][storage_location]


    files = []

    if use_gcs and gcs_base:
        # GCS Mode
        try:
            bucket = fyp_cf['data_io']['bucket']
            bucket_name = fyp_cf['data_io']['GCS_bucket_name']
            if bucket:
                # Add trailing slash to treat as directory
                prefix = gcs_base
                if not prefix.endswith("/"): prefix += "/"
                
                #if verbose: print(f"    [DATA_IO] Listing GCS blobs with prefix: {prefix}")
                
                
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

            #if verbose: print(f"    [DATA_IO] Listed {len(files)} files in GCS storage '{storage_location}'")
                 
        except Exception as e:
            #if verbose: print("    [DATA_IO] WARN: GCS enabled but bucket missing/error for listdir.")
            files = [] # Or raise? Old code just warned and returned empty or had logic flow issues.

             
    else:
        # Local Mode
        if storage_location not in fyp_cf['paths']:
            raise ValueError(f"Invalid storage location: '{storage_location}'.")
        local_dir = fyp_cf['paths'][storage_location]

        #if verbose: print(f"    [DATA_IO] Listing files in local storage: {local_dir}")

        files = os.listdir(local_dir)

        if return_absolute_path:
            files = [os.path.join(local_dir, f) for f in files]

        #if verbose: print(f"    [DATA_IO] Listed {len(files)} files in local storage '{storage_location}'")

    return files







def move(src_storage_location: str = "", dst_storage_location: str = "", filename: str = "", verbose: bool = False):
    """
    Move the file filename from src_storage_location to dst_storage_location.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if src_storage_location == "":
        raise ValueError("Source storage location cannot be empty")
    
    if dst_storage_location == "":
        raise ValueError("Destination storage location cannot be empty")
    
    # Resolve DST
    dst_primary, _, dst_mode, dst_blob_name = _resolve_paths(dst_storage_location, filename)


    # temp to storage_location
    if src_storage_location == "temp":
        
        src_path = os.path.join(fyp_cf['paths']['temp'], filename)
        if not os.path.exists(src_path):
             if verbose: print(f"    [DATA_IO] ERROR: Source file not found in temp: '{src_path}'")
             return

        if dst_mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                try:
                    blob = bucket.blob(dst_blob_name)
                    blob.upload_from_filename(src_path)
                    if verbose: print(f"    [DATA_IO] Uploaded from temp to GCS: '{src_path}' -> '{dst_blob_name}'")
                    # Remove local temp file after successful upload
                    os.remove(src_path)
                except Exception as e:
                    if verbose: print(f"    [DATA_IO] WARN: Failed to upload/move from temp to GCS: {e}")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized for temp move.")

        elif dst_mode == 'local':
             if dst_primary:
                shutil.move(src_path, dst_primary)
                if verbose: print(f"    [DATA_IO] Moved from temp to local: '{src_path}' -> '{dst_primary}'")
             else:
                 if verbose: print(f"    [DATA_IO] ERROR: Destination path resolution failed for local move.")
        
        return

    # Resolve SRC
    src_primary, _, src_mode, src_blob_name = _resolve_paths(src_storage_location, filename)

    # GCS Move
    if src_mode == 'gcs' and dst_mode == 'gcs':
        bucket = _get_bucket()
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
            shutil.move(src_primary, dst_primary)
            if verbose: print(f"    [DATA_IO] Moved Local: '{filename}' from '{src_storage_location}' to '{dst_storage_location}'")
        else:
            if verbose and src_mode == 'local':
                print(f"    [DATA_IO] ERROR Couldn't find '{filename}' in '{src_storage_location}'")
    
        









# read a file with one json object per line and return a list of dictionaries
def read_ndjson_file(storage_location: str = "cache", filename: str = "", verbose: bool = False):

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    # Extension check
    bn = os.path.basename(filename)
    root, ext = os.path.splitext(bn)
    if ext != '.ndjson':
        if verbose: 
            print(f"    [DATA_IO] WARN: File extension is not '.ndjson': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)
    
    # Attempt Primary Load
    data = []
    if True:#try:
        if mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                blob = bucket.blob(blob_name)
                # Check existence to avoid generic 404 error masked as something else
                if blob.exists():
                    with blob.open("r") as file:
                        for line in file:
                            line = '{"label":"' + fyp_cf["misc"]["label"] + '",' + line[1:]
                            line = '{"log_script":"' + root + '",' + line[1:]
                            data.append(json.loads(line))
                    return data
                else:
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local Primary
            with open(primary, 'r') as file:
                for line in file:
                    line = '{"label":"' + fyp_cf["misc"]["label"] + '",' + line[1:]
                    line = '{"log_script":"' + root + '",' + line[1:]
                    data.append(json.loads(line))
            return data
                
    if False:#except Exception as e:
        if verbose: print(f"    [DATA_IO] Primary load failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # If we are here, things haven't gone very well have they
    return None









def load_json(storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Load a json from a given path.
    Handles GCS read.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")


    # Extension check
    bn = os.path.basename(filename)
    root, ext = os.path.splitext(bn)
    if ext != '.json':
        if verbose: print(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)
    
    # Attempt Primary Load
    try:
        if mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                blob = bucket.blob(blob_name)
                # Check existence to avoid generic 404 error masked as something else
                if blob.exists():
                     content = blob.download_as_text()
                     return json.loads(content)
                else:
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local from local
            with open(primary, 'r') as file:
                return json.load(file)
                
    except Exception as e:
        if verbose: print(f"    [DATA_IO] Loading json failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # If we are here, things haven't gone very well have they
    return None





def save_json(data = None, storage_location: str = "cache", filename: str = "", verbose: bool = False):
    """
    Save a json to a given path.
    Supports GCS write + Parallel Save.
    """

    if data is None:
        raise ValueError("Data cannot be empty")
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    bn = os.path.basename(filename)
    root, ext = os.path.splitext(bn)
    if ext != '.json':
        if verbose: print(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    # 1. Save Primary
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
             blob = bucket.blob(blob_name)
             blob.upload_from_string(json.dumps(data))
             if verbose: print(f"    [DATA_IO] Saved JSON to GCS: {blob_name}")
        else:
             raise ValueError("GCS bucket not initialized")
    else:
        # Local
        with open(primary, 'w') as file:
            json.dump(data, file)
    
    return 0
            






def load_parquet(
        storage_location: str = "cache",
        filename: str = "", # if filename == '*' -> load all parquet files in storage_location
        columns=None,
        filters=None,
        verbose = False,
    ):
    """
    Load a dataframe from a given path.
    Supports GCS direct read (gs://).
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")



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

    t1 = _dt.datetime.now()


    if fyp_cf['data_io']['bucket'] is not None:
        # Initialize GCS filesystem
        fs = gcsfs.GCSFileSystem()


    # if we are to load all parquet files in this location (and it is gcs)
    if filename == "*" and fyp_cf['data_io']['use_gcs_for_data']:
        gcs_base = fyp_cf.get("gcs_paths", {}).get(storage_location)
        bucket_name = fyp_cf['data_io'].get('GCS_bucket_name')
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
            # TODO: this code is outdated and should be updated
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
            t2 = _dt.datetime.now()
            print(f"    [DATA_IO] Loaded parquet(s) shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

        return df


    # if we have arrived here, we are loading a single parquet file
    root, ext = os.path.splitext(filename)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")

    if not exists(storage_location, filename):
        raise FileNotFoundError(f"File not found: '{filename}' in '{storage_location}'")

    # Resolve path
    primary, _, mode, _ = _resolve_paths(storage_location, filename)

    # if specific columns are to be loaded, we need to make sure the cols actually exist in the parquet files
    if columns is not None:
        try:
            if mode == 'gcs':
                # Read parquet schema
                with fs.open(primary) as f:
                    parquet_schema = pq.read_schema(f)
            else:
                # Local
                parquet_schema = pq.read_schema(primary)
            existing_cols = parquet_schema.names
        except Exception as e:
            if verbose: print(f"    [DATA_IO] WARN: Column selection failed: {e}")
            existing_cols = []
        columns = [c for c in columns if c in existing_cols]
        if verbose:
            print(f"    [DATA_IO] Column selection: {columns}")


    if verbose: print(f"    [DATA_IO] Loading: '{filename}' from '{storage_location}' ({mode})...")
    try:
        df = pd.read_parquet(
            primary,
            engine='pyarrow',
            dtype_backend="pyarrow",
            use_threads=True,
            columns=columns,
            filters=filters)
    except Exception as e:
        print(f" !! [DATA_IO] WARNING: Loading '{filename}' failed: {e}")
        return None

    # type management to be sure
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)

    if verbose:
        t2 = _dt.datetime.now()
        print(f"    [DATA_IO] ...done. Shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

    return df




# Create a global lock object
file_lock = threading.Lock()

def save_parquet(
    df: pd.DataFrame = None, 
    storage_location: str = "cache", 
    filename: str = "", 
    asyncronous: bool = False,
    verbose: bool = False,
    ):
    """
    Save a dataframe to the given path.
    Supports GCS direct write and Parallel Save (GCS + Local).
    """

    if df is None:
        raise ValueError("Dataframe cannot be empty")
    
    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    this_df = df.copy()


    # A) Resolve Paths (Primary = GCS if enabled, Secondary = Local)
    # Note: filename here might not have extension yet, logic below handles it
    
    # Base logic to ensure extension is .parquet
    base_name = os.path.basename(filename)
    root, ext = os.path.splitext(base_name)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")
    
    # Resolve using the filename (which has .parquet)
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    # B) Type Management
    this_df = convert_dtypes_to_pyarrow(this_df, verbose=verbose)

    # C) Save to Primary

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

        if asyncronous:
            executor = ThreadPoolExecutor(max_workers=2)

            future = executor.submit(safe_save, this_df.copy(), primary)
            future.add_done_callback(alert_finished)
        else:
            safe_save(this_df.copy(), primary)

    else:
        this_df.to_parquet(primary, engine='pyarrow', compression="zstd", compression_level=my_compression_level)
    
    
    if verbose: print(f"    [DATA_IO] ...moving on. Shape: {this_df.shape}")
    
    return this_df



