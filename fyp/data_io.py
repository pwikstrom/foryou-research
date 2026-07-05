"""
Script Name: data_io.py
Description: Centralized I/O operations for dataframes.
Author: Patrik
"""



import datetime as _dt
import io
import json
import os
import shutil
import tempfile
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import gcsfs
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from fyp.types import convert_dtypes_to_pyarrow

# NOTE: fyp.fyp_config is accessed LAZILY — a module-level `from fyp.fyp_config
# import fyp_cf` makes this module part of an import cycle: any entry module
# that imports data_io first leaves it partially initialized while fyp_config's
# module-level load_var_schema runs, so the contract overlays' registry reads
# hit half-defined functions and silently lost legacy metadata (per-instance
# schema-hash drift, pinned 2026-07-02). Keep config access function-level.


def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf


def _io_log(op: str, loc: str, filename: str, mode: str, bytes_: int, t_ms: float) -> None:
    """Emit a single-line IO timing log line for grepping in Cloud Run logs.

    Format: [IO] op=OP loc=LOC file=FILE mode=MODE bytes=N ms=MS
    Bytes is an approximation (in-memory DataFrame size for parquet, JSON payload
    length for json). Used to compare GCS vs local I/O cost.
    """
    bn = os.path.basename(filename) if filename else ""
    print(f"[IO] op={op} loc={loc} file={bn} mode={mode} bytes={bytes_} ms={t_ms:.1f}")



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
    #print(60*"==")
    #print(storage_location)
    #print(60*"==")
    if storage_location not in _cf()['paths']:
        valid_locs = ', '.join(list(_cf()['paths'].keys()))
        raise ValueError(f"Invalid storage location: '{storage_location}'. Use: {valid_locs}")



    # 2. Check GCS Configuration
    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = _cf()['data_io']['use_gcs_for_cache']
    else:
        use_gcs = _cf()['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = _cf()['gcs_paths'][storage_location]

    bucket_name = _cf()['data_io']['GCS_bucket_name']
    
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
        local_path = os.path.join(_cf()['paths'][storage_location], filename)
        return (local_path, None, 'local', None)



def _get_bucket():
    """Retrieve the bucket object from config."""
    w = _cf()['data_io']['bucket']
    return w





def register_location(name: str, abs_path: str, verbose: bool = False) -> None:
    """Register a storage location at runtime so ``_resolve_paths`` accepts it.

    Lets a component (e.g. a new ingestion collection class) declare its own
    storage location without a static edit to ``fyp_config``. Mirrors the
    GCS-path derivation done at config load: when data is served from GCS the
    matching ``gcs_paths`` entry is derived from ``abs_path``'s position under
    ``local_data``; in local mode the directory is created. A location that is
    already registered is left untouched.

    Args:
        name: The storage-location key (e.g. ``"instagram_raw"``).
        abs_path: The absolute local path the key resolves to. Must live under
            ``paths.local_data`` so the GCS path stays derivable.
        verbose: When True, print a one-line registration notice.

    Raises:
        ValueError: if ``abs_path`` does not live under ``paths.local_data`` —
            registering it anyway would leave the location resolvable locally
            but broken in GCS mode.
    """
    cf = _cf()
    if name in cf['paths']:
        return

    local_data = cf['paths'].get('local_data', "")
    rel = os.path.relpath(abs_path, local_data) if local_data else ".."
    if rel.startswith(".."):
        raise ValueError(
            f"Storage location '{name}' path '{abs_path}' is not under "
            f"paths.local_data ('{local_data}') — no GCS path is derivable."
        )

    cf['paths'][name] = abs_path

    if cf['data_io'].get('use_gcs_for_data'):
        gcs_prefix = cf['data_io'].get('gcs_data_prefix', "")
        if rel == ".":
            gcs_path = gcs_prefix
        else:
            gcs_path = f"{gcs_prefix}/{rel}" if gcs_prefix else rel
        cf.setdefault('gcs_paths', {})[name] = gcs_path
    else:
        os.makedirs(abs_path, exist_ok=True)

    if verbose:
        print(f"    [DATA_IO] Registered storage location '{name}' -> {abs_path}")








def get_recent_files(storage_location: str = "cache", suffix: str = None, how_recent: int = 10):

    current_time = datetime.now()
    recent_files = []

    for filename in listdir(storage_location = storage_location):
        #file_path = join(storage_location, filename)
        if suffix is None or filename.endswith(suffix):
            modified_time = datetime.fromtimestamp(getmtime(storage_location = storage_location, filename = filename))
            created_time = datetime.fromtimestamp(getctime(storage_location = storage_location, filename = filename))
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
        bucket = _cf()['data_io']['bucket']
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





def stat(storage_location: str = "cache", filename: str = "", verbose: bool = False) -> dict | None:
    """Return a fingerprint dict ({'size': int, 'mtime': float}) for a file, or None if missing.

    Single round-trip on GCS (one get_blob call) — cheaper than calling getsize + getmtime separately.
    Returns None when the file does not exist so callers can use it directly as a sentinel.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")

    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    if mode == 'gcs':
        bucket = _get_bucket()
        if not bucket:
            raise ValueError("GCS bucket not initialized")
        blob = bucket.get_blob(blob_name)
        if blob is None:
            return None
        mtime = blob.updated.timestamp() if blob.updated else 0.0
        return {"size": int(blob.size or 0), "mtime": float(mtime)}
    else:
        if not os.path.exists(primary):
            return None
        return {"size": int(os.path.getsize(primary)), "mtime": float(os.path.getmtime(primary))}




def get_parquet_columns(storage_location: str = "cache", filename: str = "") -> list[str] | None:
    """Return the column names of a parquet file without reading row data.

    Uses `pq.read_metadata` on the resolved primary path (local path or gs:// URI),
    which only reads the file footer. Returns None when the file does not exist.
    Used by incremental-refresh planners that need to know which columns belong
    to source parquets (scrapes, annotations) without actually loading them.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    if not exists(storage_location=storage_location, filename=filename):
        return None

    primary, _secondary, _mode, _blob_name = _resolve_paths(storage_location, filename)
    meta = pq.read_metadata(primary)
    return list(meta.schema.to_arrow_schema().names)








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
        else:
            if verbose: print(f"    [DATA_IO] File '{primary}' not found in local storage")







def listdir(storage_location: str = "cache", return_absolute_path: bool = False, verbose: bool = False) -> list:
    """
    List files in the given storage location.
    Handles GCS listing if configured.
    """

    _t_io = _time.perf_counter()

    gcs_base = False
    use_gcs = False
    if storage_location == 'cache':
        use_gcs = _cf()['data_io']['use_gcs_for_cache']
    else:
        use_gcs = _cf()['data_io']['use_gcs_for_data']

    if use_gcs:
        gcs_base = _cf()['gcs_paths'][storage_location]


    files = []

    if use_gcs and gcs_base:
        # GCS Mode
        try:
            bucket = _cf()['data_io']['bucket']
            bucket_name = _cf()['data_io']['GCS_bucket_name']
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
                 
        except Exception:
            #if verbose: print("    [DATA_IO] WARN: GCS enabled but bucket missing/error for listdir.")
            files = [] # Or raise? Old code just warned and returned empty or had logic flow issues.

             
    else:
        # Local Mode
        if storage_location not in _cf()['paths']:
            raise ValueError(f"Invalid storage location: '{storage_location}'.")
        local_dir = _cf()['paths'][storage_location]

        #if verbose: print(f"    [DATA_IO] Listing files in local storage: {local_dir}")

        if not os.path.isdir(local_dir):
            os.makedirs(local_dir, exist_ok=True)
            return []

        files = os.listdir(local_dir)

        if return_absolute_path:
            files = [os.path.join(local_dir, f) for f in files]

        #if verbose: print(f"    [DATA_IO] Listed {len(files)} files in local storage '{storage_location}'")

    _io_log(
        op="listdir",
        loc=storage_location,
        filename="",
        mode=("gcs" if use_gcs and gcs_base else "local"),
        bytes_=len(files),
        t_ms=(_time.perf_counter() - _t_io) * 1000.0,
    )

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
        
        src_path = os.path.join(_cf()['paths']['temp'], filename)
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
                 if verbose: print("    [DATA_IO] ERROR: Destination path resolution failed for local move.")
        
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
                            #line = '{"label":"' + _cf()["misc"]["label"] + '",' + line[1:]
                            #line = '{"log_script":"' + root + '",' + line[1:]
                            data.append(json.loads(line))
                    return data
                else:
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local Primary
            with open(primary, encoding='utf-8') as file:
                for line in file:
                    #line = '{"label":"' + _cf()["misc"]["label"] + '",' + line[1:]
                    #line = '{"log_script":"' + root + '",' + line[1:]
                    data.append(json.loads(line))
            return data

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
    _t_io = _time.perf_counter()
    try:
        if mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                blob = bucket.blob(blob_name)
                # Check existence to avoid generic 404 error masked as something else
                if blob.exists():
                     content = blob.download_as_text()
                     _io_log(
                         op="load_json",
                         loc=storage_location,
                         filename=filename,
                         mode=mode,
                         bytes_=len(content),
                         t_ms=(_time.perf_counter() - _t_io) * 1000.0,
                     )
                     return json.loads(content)
                else:
                     if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            # Local from local
            with open(primary, encoding='utf-8') as file:
                content = file.read()
                _io_log(
                    op="load_json",
                    loc=storage_location,
                    filename=filename,
                    mode=mode,
                    bytes_=len(content),
                    t_ms=(_time.perf_counter() - _t_io) * 1000.0,
                )
                return json.loads(content)

    except Exception as e:
        if verbose: print(f"    [DATA_IO] Loading json failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             print(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
             return None

    # If we are here, things haven't gone very well have they
    return None





def local_copy(storage_location: str = "cache", filename: str = "", verbose: bool = False) -> str | None:
    """Return a local filesystem path to a raw file, downloading it if needed.

    A generic binary accessor for readers that need a real file on disk (e.g.
    ``zipfile``) rather than decoded text. In GCS mode the blob is downloaded to
    the temp directory and that path returned; in local mode the resolved local
    path is returned as-is. Only members the caller opens are read, so the large
    archive never has to be held in memory.

    Args:
        storage_location: The storage-location key to resolve.
        filename: The file to make locally available.
        verbose: When True, print diagnostic notices.

    Returns:
        An absolute local path to the file, or ``None`` when it cannot be found.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")

    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        if not bucket:
            if verbose: print("    [DATA_IO] WARN: GCS bucket not initialized.")
            return None
        blob = bucket.blob(blob_name)
        if not blob.exists():
            if verbose: print(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            return None
        temp_dir = _cf()['paths']['temp']
        os.makedirs(temp_dir, exist_ok=True)
        # Prefix with the storage location so identically-named files from
        # different locations cannot clobber each other's temp copies.
        local_path = os.path.join(temp_dir, f"{storage_location}__{os.path.basename(filename)}")
        blob.download_to_filename(local_path)
        _io_log(
            op="local_copy",
            loc=storage_location,
            filename=filename,
            mode=mode,
            bytes_=os.path.getsize(local_path) if os.path.exists(local_path) else 0,
            t_ms=(_time.perf_counter() - _t_io) * 1000.0,
        )
        return local_path

    if os.path.exists(primary):
        return primary
    if verbose: print(f"    [DATA_IO] WARN: Local file not found: {primary}.")
    return None





def release_local_copy(path: str | None, verbose: bool = False) -> None:
    """Delete a temp copy produced by :func:`local_copy`; no-op otherwise.

    Only paths inside the configured temp directory are removed, so callers can
    call this unconditionally: in local mode ``local_copy`` returns the real
    raw-file path, which must never be deleted. On Cloud Run the temp directory
    is memory-backed, so releasing large downloads promptly matters.

    Args:
        path: The path returned by ``local_copy`` (may be ``None``).
        verbose: When True, print a one-line removal notice.
    """
    if not path:
        return
    temp_dir = os.path.abspath(_cf()['paths']['temp'])
    abs_path = os.path.abspath(path)
    if not abs_path.startswith(temp_dir + os.sep):
        return
    try:
        os.remove(abs_path)
        if verbose:
            print(f"    [DATA_IO] Released temp copy: {abs_path}")
    except FileNotFoundError:
        pass





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

    payload = json.dumps(data)

    # 1. Save Primary
    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
             blob = bucket.blob(blob_name)
             blob.upload_from_string(payload)
             if verbose: print(f"    [DATA_IO] Saved JSON to GCS: {blob_name}")
        else:
             raise ValueError("GCS bucket not initialized")
    else:
        # Local
        os.makedirs(os.path.dirname(primary), exist_ok=True)
        with open(primary, 'w', encoding='utf-8') as file:
            file.write(payload)

    _io_log(
        op="save_json",
        loc=storage_location,
        filename=filename,
        mode=mode,
        bytes_=len(payload),
        t_ms=(_time.perf_counter() - _t_io) * 1000.0,
    )

    return 0
            






def _repair_stringified_multiindex(df: pd.DataFrame) -> pd.DataFrame:
    """Restore tuple column names from their string representations.

    When pandas metadata is lost from a parquet file (e.g. after a pyarrow
    read/write round-trip), tuple columns like ('participants', 'email') are
    loaded as flat strings "('participants', 'email')".  This function detects
    that situation and parses them back to real tuples.

    Uses a plain Index (not MultiIndex) so that plain string columns like
    'collection_id' remain directly accessible via df['collection_id'].

    If no columns look like stringified tuples the DataFrame is returned
    unchanged.
    """
    import ast

    cols = df.columns.tolist()

    # Quick check: are there any string columns that look like tuples?
    has_tuple_strings = any(
        isinstance(c, str) and c.startswith("(") and c.endswith(")")
        for c in cols
    )
    if not has_tuple_strings:
        return df

    new_cols: list = []
    any_parsed = False
    for c in cols:
        if isinstance(c, str) and c.startswith("(") and c.endswith(")"):
            try:
                t = ast.literal_eval(c)
                if isinstance(t, tuple):
                    new_cols.append(t)
                    any_parsed = True
                    continue
            except (ValueError, SyntaxError):
                pass
        # Keep plain strings as-is
        new_cols.append(c)

    if not any_parsed:
        return df

    df.columns = pd.Index(new_cols)
    return df





def load_parquet(
        storage_location: str = "cache",
        filename: str = "", # if filename == '*' -> load all parquet files in storage_location
        #columns=None,
        filters=None,
        verbose = False,
    ):
    """
    Load a dataframe from a given path.
    Supports GCS direct read (gs://).
    """

    columns=None
    #filters=None


    if filename == "":
        raise ValueError("Filename cannot be empty")
    
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")



    def _renamed(s):
        fixer_upper = [
            #("B_local_","local_"),
            #("D_local_","local_"),
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


    if _cf()['data_io']['bucket'] is not None:
        # Initialize GCS filesystem
        fs = gcsfs.GCSFileSystem()


    # if we are to load all parquet files in this location (and it is gcs)
    if filename == "*" and _cf()['data_io']['use_gcs_for_data']:
        gcs_base = _cf().get("gcs_paths", {}).get(storage_location)
        bucket_name = _cf()['data_io'].get('GCS_bucket_name')
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
        _t_io = _time.perf_counter()
        df = pd.read_parquet(
            files,
            filesystem=fs,
            engine='pyarrow',
            use_threads=True,
            dtype_backend="pyarrow",
            columns=columns,
            filters=filters)
        _t_io_ms = (_time.perf_counter() - _t_io) * 1000.0
        if verbose:
            print(f"    [DATA_IO] ...done (shape: {df.shape})")

        # type management to be sure
        df = convert_dtypes_to_pyarrow(df, verbose=verbose)
        df = _repair_stringified_multiindex(df)

        _io_log(
            op="load_parquet_glob",
            loc=storage_location,
            filename=f"*.parquet ({len(files)} files)",
            mode="gcs",
            bytes_=int(df.memory_usage(deep=True).sum()),
            t_ms=_t_io_ms,
        )

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
    _t_io = _time.perf_counter()
    try:
        if mode == 'gcs':
            # Bypass gcsfs: download the blob to memory, then decode with
            # pyarrow directly. Benchmarks on task-runner showed this is
            # ~1.2-2.3x faster than pd.read_parquet("gs://...") and has
            # lower tail-latency variance. See run_benchmark_parquet_read.py.
            _, _, _, blob_name = _resolve_paths(storage_location, filename)
            bucket = _get_bucket()
            if not bucket:
                raise ValueError("GCS bucket not initialized")
            raw = bucket.blob(blob_name).download_as_bytes()
            table = pq.read_table(
                io.BytesIO(raw),
                columns=columns,
                filters=filters,
                use_threads=True,
            )
            df = table.to_pandas(types_mapper=pd.ArrowDtype)
        else:
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
    _t_io_ms = (_time.perf_counter() - _t_io) * 1000.0

    # type management to be sure
    df = convert_dtypes_to_pyarrow(df, verbose=verbose)
    df = _repair_stringified_multiindex(df)

    _io_log(
        op="load_parquet",
        loc=storage_location,
        filename=filename,
        mode=mode,
        bytes_=int(df.memory_usage(deep=True).sum()),
        t_ms=_t_io_ms,
    )

    if verbose:
        t2 = _dt.datetime.now()
        print(f"    [DATA_IO] ...done. Shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

    return df






def load_parquet_selective(
        storage_location: str = "cache",
        filename: str = "",
        columns: list = None,
        filters: list = None,
        set_index: str = None,
        verbose: bool = False,
    ):
    """Load a parquet file with column projection and optional row filters.

    Uses a pyarrow.parquet.read_table pipeline that strips the embedded
    `pandas_metadata` from the table schema before converting to a DataFrame.
    This is required because pandas' default conversion path attempts to
    resolve ArrowDtype for every column listed in `pandas_metadata` (including
    list-typed columns we did not request), which fails on parquet files
    written with the older `list<element: string>` notation.

    Performance: 10x-30x faster than `load_parquet()` on the large
    `*_recoded.parquet` files when only a few columns are needed. See
    `tmp/parquet_selective_loading_findings.md` for measured numbers.

    Args:
        storage_location: Named storage location (e.g. "cache", "recoded").
        filename: Parquet file name. Must end in ".parquet".
        columns: Optional list of on-disk column names to load. None loads all.
            For files with MultiIndex columns (e.g. `collections_metadata.parquet`),
            pass the on-disk stringified-tuple form, e.g.
            `"('personas', 'first_event_ts')"`.
        filters: Optional PyArrow filter expressions, e.g.
            `[("collection_id", "==", "abc")]` or
            `[("item_id", "in", ["a", "b"])]`. Note: filter pushdown only
            prunes row groups when the file is sorted on the filter column;
            otherwise it just discards rows after decode.
        set_index: Optional on-disk column name to set as the index after read.
            Required when the parquet was written with an indexed DataFrame and
            the caller relies on `df.index` (stripping `pandas_metadata` drops
            implicit-index information).
        verbose: Print timing and shape info.

    Returns:
        The loaded DataFrame, or None on failure.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    root, ext = os.path.splitext(filename)
    if ext != '.parquet':
        raise ValueError(f"File extension must be '.parquet', got: '{ext}'")

    if not exists(storage_location, filename):
        raise FileNotFoundError(f"File not found: '{filename}' in '{storage_location}'")

    primary, _, mode, _ = _resolve_paths(storage_location, filename)

    t1 = _dt.datetime.now()

    if mode == 'gcs':
        fs = gcsfs.GCSFileSystem()
        with fs.open(primary) as f:
            existing_cols = pq.read_schema(f).names
    else:
        existing_cols = pq.read_schema(primary).names

    if columns is not None:
        missing = [c for c in columns if c not in existing_cols]
        cols_to_read = [c for c in columns if c in existing_cols]
        if missing and verbose:
            print(f"    [DATA_IO] Selective: {len(missing)} requested column(s) not in schema, skipping: {missing}")
        if not cols_to_read:
            print(f" !! [DATA_IO] WARNING: load_parquet_selective: no requested columns exist in '{filename}'")
            return None
    else:
        cols_to_read = None

    if set_index is not None and cols_to_read is not None and set_index not in cols_to_read:
        cols_to_read = cols_to_read + [set_index]

    _t_io = _time.perf_counter()
    try:
        if mode == 'gcs':
            tbl = pq.read_table(primary, columns=cols_to_read, filters=filters, filesystem=fs)
        else:
            tbl = pq.read_table(primary, columns=cols_to_read, filters=filters)
    except Exception as e:
        print(f" !! [DATA_IO] WARNING: load_parquet_selective: read failed for '{filename}': {e}")
        return None
    _t_io_ms = (_time.perf_counter() - _t_io) * 1000.0

    # Strip the `pandas` schema metadata to avoid the
    # `list<element: string>[pyarrow]` dtype-resolution failure during
    # to_pandas() when other (unselected) columns are list-typed on disk.
    meta = tbl.schema.metadata or {}
    new_meta = {k: v for k, v in meta.items() if k != b'pandas'}
    tbl = tbl.replace_schema_metadata(new_meta or None)

    df = tbl.to_pandas(types_mapper=pd.ArrowDtype)

    df = _repair_stringified_multiindex(df)

    if set_index is not None and set_index in df.columns:
        df = df.set_index(set_index)

    _io_log(
        op="load_parquet_selective",
        loc=storage_location,
        filename=filename,
        mode=mode,
        bytes_=int(df.memory_usage(deep=True).sum()),
        t_ms=_t_io_ms,
    )

    if verbose:
        t2 = _dt.datetime.now()
        print(f"    [DATA_IO] Selective load: '{filename}' shape={df.shape} time={(t2-t1).total_seconds():.3f}s")

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
    
    _t_io = _time.perf_counter()
    _io_sync = True  # only flip to False on the async branch below

    def _write_parquet(df_to_write):
        """Write df as parquet to its resolved destination.

        For GCS, write to a local tempfile then upload via blob.upload_from_filename
        — significantly faster than pd.to_parquet("gs://...") which goes through
        gcsfs and incurs large per-call overhead on small files.
        """
        if mode == 'gcs':
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                    tmp_path = tmp.name
                df_to_write.to_parquet(
                    tmp_path,
                    engine='pyarrow',
                    compression="zstd",
                    compression_level=my_compression_level,
                )
                bucket = _get_bucket()
                if not bucket:
                    raise ValueError("GCS bucket not initialized")
                bucket.blob(blob_name).upload_from_filename(tmp_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        else:
            os.makedirs(os.path.dirname(primary), exist_ok=True)
            df_to_write.to_parquet(
                primary,
                engine='pyarrow',
                compression="zstd",
                compression_level=my_compression_level,
            )

    if storage_location == "cache":
        def alert_finished(future):
            if future.exception():
                if verbose:
                    print(f"   [DATA_IO ASYNC] Parquet save failed: {future.exception()}")
            else:
                if verbose:
                    print("    [DATA_IO ASYNC] Parquet save succeeded.")

        def safe_save(df):
            # This 'with' block ensures only one thread can execute the save at a time
            with file_lock:
                if verbose:
                    print(f"    [DATA_IO ASYNC] Starting save to {primary}... (locked)")
                _write_parquet(df)
                if verbose:
                    print(f"    [DATA_IO ASYNC] Finished save to {primary}. (unlocked)")

        if asyncronous:
            _io_sync = False
            executor = ThreadPoolExecutor(max_workers=2)

            future = executor.submit(safe_save, this_df.copy())
            future.add_done_callback(alert_finished)
        else:
            safe_save(this_df.copy())

    else:
        _write_parquet(this_df)

    if _io_sync:
        _t_io_ms = (_time.perf_counter() - _t_io) * 1000.0
        _io_log(
            op="save_parquet",
            loc=storage_location,
            filename=filename,
            mode=mode,
            bytes_=int(total_memory_bytes),
            t_ms=_t_io_ms,
        )

    if verbose: print(f"    [DATA_IO] ...moving on. Shape: {this_df.shape}")

    return this_df










# ============================================================================
# CSV export
# ============================================================================


def save_logs_as_csv(
    study_name: str = None,
    outdata_filtered: pd.DataFrame = None,
    file_label: str = "",
    verbose: bool = False
    ) -> None:
    """Export a study dataset to CSV with Excel-safe formatting."""

    if study_name is None:
        raise ValueError("study_name must be specified")
    if outdata_filtered is None:
        raise ValueError("outdata_filtered must be specified")

    def _clean_surrogates(text):
        """Remove surrogate characters that can't be encoded in UTF-8."""
        if not isinstance(text, str):
            return text
        try:
            return text.encode('utf-8', 'ignore').decode('utf-8')
        except:
            return ''.join(char for char in text if ord(char) < 0xD800 or ord(char) > 0xDFFF)


    if len(outdata_filtered) == 0:
        if verbose:
            print("A log file has not been generated so a CSV cannot be saved")
    else:
        log_as_csv_filename = study_name + "_" + "_LOG.csv"
        outdata_for_csv_export = outdata_filtered.copy()

        if verbose:
            print("Cleaning string data...")
        string_cols = outdata_for_csv_export.select_dtypes(exclude=['number']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = (
                outdata_for_csv_export[col]
                .astype(str)
                .str.replace("\n", " ", regex=False)
                .str.replace(";", " ", regex=False)
                .str.replace(", ", " ", regex=False)
                .str.replace(" ,", " ", regex=False)
                .str.replace("\t", " ", regex=False)
                .str.replace("|  ", " ", regex=False)
                .str.replace("،", " ", regex=False)  # arabic comma
            )

        # Clean surrogate characters from all string columns to prevent Unicode encoding errors
        if verbose:
            print("Cleaning surrogate characters from string data...")
        string_cols = outdata_for_csv_export.select_dtypes(exclude=['number']).columns
        for col in string_cols:
            outdata_for_csv_export[col] = outdata_for_csv_export[col].apply(_clean_surrogates)

        # all numbers except for those related to session stats can be integers, so let's retype those
        some_float_cols = [c for c in outdata_for_csv_export.select_dtypes(include=[float, np.float64]).columns if "session" not in c]
        outdata_for_csv_export[some_float_cols] = outdata_for_csv_export[some_float_cols].fillna(value=-1).astype(int)

        # Build item URLs from each row's platform (before the Excel quoting
        # below mangles item_id). tiktok_url is kept as a back-compat alias
        # for existing downstream consumers; prefer item_url.
        def _platform_url_templates() -> dict:
            from fyp.ingest import ForYouBaseCollection
            return {
                cls.source_platform: cls.platform_url_template
                for cls in ForYouBaseCollection._registry
                if getattr(cls, "source_platform", None) and getattr(cls, "platform_url_template", None)
            }

        templates = _platform_url_templates()
        tiktok_template = templates.get("tiktok", "https://www.tiktok.com/@/video/{item_id}")
        if "source_platform" in outdata_for_csv_export.columns:
            outdata_for_csv_export["item_url"] = [
                templates.get(p, tiktok_template).format(item_id=i)
                for p, i in zip(
                    outdata_for_csv_export["source_platform"].fillna("tiktok"),
                    outdata_for_csv_export["item_id"].astype(str),
                )
            ]
        else:
            outdata_for_csv_export["item_url"] = [
                tiktok_template.format(item_id=i)
                for i in outdata_for_csv_export["item_id"].astype(str)
            ]
        outdata_for_csv_export["tiktok_url"] = "https://www.tiktok.com/@/video/" + outdata_for_csv_export["item_id"].astype(str) + "/"

        # Convert long numbers to strings for Excel
        for c in ["data_author_id","item_id","music_id","author_id","ts_jiggled"]:
            if c in outdata_for_csv_export.columns:
                outdata_for_csv_export[c] = "'" + outdata_for_csv_export[c].astype(str) + "'"

        # Export with error handling for any remaining encoding issues
        outdata_for_csv_export.to_csv(os.path.join(_cf()['paths']['exports'],log_as_csv_filename), encoding='utf-8-sig', errors='replace')
        if verbose:
            print(f"Exported {len(outdata_for_csv_export):,} observations in {log_as_csv_filename}.")
            print(f"The date of the observations in the log range from {outdata_filtered['local_timestamp'].min()} -- {outdata_filtered['local_timestamp'].max()}")
            print(f"Now: {_dt.datetime.now()}")
