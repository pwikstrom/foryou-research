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

import gcsfs
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from fyp.types import convert_dtypes_to_pyarrow
from fyp.logging_setup import get_logger

logger = get_logger(__name__)

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

    Emitted at DEBUG so it stays out of the process logs shown in the UI, where
    it drowned the worker's own output — every status write emits one. Set
    ``FYP_LOG_LEVEL=DEBUG`` to bring the timings back.
    """
    bn = os.path.basename(filename) if filename else ""
    logger.debug(f"[IO] op={op} loc={loc} file={bn} mode={mode} bytes={bytes_} ms={t_ms:.1f}")



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
        logger.info(f"    [DATA_IO] Registered storage location '{name}' -> {abs_path}")








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
        except Exception:
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






def get_parquet_num_rows(storage_location: str = "cache", filename: str = "") -> int | None:
    """Return a parquet file's row count from its footer, without reading rows.

    The row-count counterpart of :func:`get_parquet_columns`. Returns None
    when the file does not exist.
    """

    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    if not exists(storage_location=storage_location, filename=filename):
        return None

    primary, _secondary, _mode, _blob_name = _resolve_paths(storage_location, filename)
    return int(pq.read_metadata(primary).num_rows)








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
                if verbose: logger.info(f"    [DATA_IO] Removed GCS blob '{blob_name}'")
            except Exception as e:
                # It's possible it didn't exist
                if verbose: logger.warning(f"    [DATA_IO] GCS remove note: {e}")

    else:
        if os.path.exists(primary):
            os.remove(primary)
            if verbose: logger.info(f"    [DATA_IO] Removed local file '{primary}'")
        else:
            if verbose: logger.warning(f"    [DATA_IO] File '{primary}' not found in local storage")







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
             if verbose: logger.error(f"    [DATA_IO] ERROR: Source file not found in temp: '{src_path}'")
             return

        if dst_mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                try:
                    blob = bucket.blob(dst_blob_name)
                    blob.upload_from_filename(src_path)
                    if verbose: logger.info(f"    [DATA_IO] Uploaded from temp to GCS: '{src_path}' -> '{dst_blob_name}'")
                    # Remove local temp file after successful upload
                    os.remove(src_path)
                except Exception as e:
                    if verbose: logger.warning(f"    [DATA_IO] WARN: Failed to upload/move from temp to GCS: {e}")
            else:
                 if verbose: logger.warning("    [DATA_IO] WARN: GCS bucket not initialized for temp move.")

        elif dst_mode == 'local':
             if dst_primary:
                shutil.move(src_path, dst_primary)
                if verbose: logger.info(f"    [DATA_IO] Moved from temp to local: '{src_path}' -> '{dst_primary}'")
             else:
                 if verbose: logger.error("    [DATA_IO] ERROR: Destination path resolution failed for local move.")
        
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
                if verbose: logger.info(f"    [DATA_IO] Moved GCS: '{src_blob_name}' -> '{dst_blob_name}'")
            except Exception as e:
                if verbose: logger.warning(f"    [DATA_IO] WARN: GCS Move failed (src likely missing): {e}")

    # Local Move
    elif src_mode == 'local' and dst_mode == 'local':

        if src_primary and dst_primary:
            shutil.move(src_primary, dst_primary)
            if verbose: logger.info(f"    [DATA_IO] Moved Local: '{filename}' from '{src_storage_location}' to '{dst_storage_location}'")
        else:
            if verbose and src_mode == 'local':
                logger.error(f"    [DATA_IO] ERROR Couldn't find '{filename}' in '{src_storage_location}'")






def rename(storage_location: str = "", src_filename: str = "", dst_filename: str = "", verbose: bool = False) -> bool:
    """Rename a file within a single storage location.

    Local mode is an atomic filesystem move; GCS mode uses ``rename_blob``
    (a server-side copy + delete). An existing file at ``dst_filename`` is
    overwritten, matching ``save_*`` semantics.

    Args:
        storage_location: The named storage location holding the file.
        src_filename: The current filename.
        dst_filename: The new filename.
        verbose: When True, log the rename.

    Returns:
        True when the source existed and was renamed, False when it was absent.
    """

    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    if src_filename == "" or dst_filename == "":
        raise ValueError("Filename cannot be empty")

    if src_filename == dst_filename:
        return exists(storage_location=storage_location, filename=src_filename)

    src_primary, _, src_mode, src_blob_name = _resolve_paths(storage_location, src_filename)
    dst_primary, _, _, dst_blob_name = _resolve_paths(storage_location, dst_filename)

    if src_mode == 'gcs':
        bucket = _get_bucket()
        if not bucket:
            raise ValueError("GCS bucket not initialized for rename")
        blob = bucket.blob(src_blob_name)
        if not blob.exists():
            return False
        bucket.rename_blob(blob, dst_blob_name)
        if verbose: logger.info(f"    [DATA_IO] Renamed GCS: '{src_blob_name}' -> '{dst_blob_name}'")
        return True

    if not src_primary or not os.path.exists(src_primary):
        return False
    shutil.move(src_primary, dst_primary)
    if verbose: logger.info(f"    [DATA_IO] Renamed Local: '{src_filename}' -> '{dst_filename}' in '{storage_location}'")
    return True









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
            logger.warning(f"    [DATA_IO] WARN: File extension is not '.ndjson': '{ext}' (filename: {bn})")
        
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
                     if verbose: logger.warning(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: logger.warning("    [DATA_IO] WARN: GCS bucket not initialized.")
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
        if verbose: logger.warning(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
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
                     if verbose: logger.warning(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                 if verbose: logger.warning("    [DATA_IO] WARN: GCS bucket not initialized.")
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
        if verbose: logger.warning(f"    [DATA_IO] Loading json failed ({mode}): {e}")
        # If we are in local mode, primary failed, no secondary. Raise/Return None.
        if mode == 'local':
             logger.error(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
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
            if verbose: logger.warning("    [DATA_IO] WARN: GCS bucket not initialized.")
            return None
        blob = bucket.blob(blob_name)
        if not blob.exists():
            if verbose: logger.warning(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
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
    if verbose: logger.warning(f"    [DATA_IO] WARN: Local file not found: {primary}.")
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
            logger.info(f"    [DATA_IO] Released temp copy: {abs_path}")
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
        if verbose: logger.warning(f"    [DATA_IO] WARN: File extension is not '.json': '{ext}' (filename: {bn})")
        
    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    payload = json.dumps(data)

    # 1. Save Primary
    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
             blob = bucket.blob(blob_name)
             blob.upload_from_string(payload)
             if verbose: logger.info(f"    [DATA_IO] Saved JSON to GCS: {blob_name}")
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






# Per-path locks for the local branch of update_json. In-process only — local
# mode normally has a single server instance, so the lock guards concurrent
# threads; the cross-instance guarantee comes from the GCS generation check.
_update_json_locks: dict = {}
_update_json_locks_guard = threading.Lock()






def update_json(storage_location: str = "cache", filename: str = "",
                mutate=None, default=None, max_retries: int = 6,
                verbose: bool = False):
    """Atomically read-modify-write a JSON file (lost-update safe).

    The concurrency-safe replacement for the ``load_json`` → mutate →
    ``save_json`` pattern on shared state (scrape queues, process stats).
    In GCS mode the write carries an ``if_generation_match`` precondition:
    if another process wrote the object between our read and write, the
    upload fails with 412 and the whole read-mutate-write cycle is retried
    against the fresh contents — no update is ever silently overwritten.
    In local mode a per-path lock serializes in-process writers and the file
    is written to a temp file then ``os.replace``d, so readers never observe
    a torn file.

    Args:
        storage_location: The storage-location key to resolve.
        filename: The JSON file to update.
        mutate: ``(current) -> new`` callback. Receives the parsed JSON
            contents (or ``default`` when the file is missing/invalid) and
            returns the value to save. It may be called several times under
            contention, so it must be a pure function of its argument.
            Returning ``None`` skips the save (nothing to change).
        default: Value passed to ``mutate`` when the file does not exist.
        max_retries: Attempts before giving up under sustained contention.
        verbose: When True, print diagnostic notices.

    Returns:
        The value returned by ``mutate`` on the attempt that was persisted
        (or ``None`` when ``mutate`` skipped the save).

    Raises:
        ValueError: on empty ``filename``/``storage_location`` or missing
            ``mutate``, or when the GCS bucket is not initialized.
        RuntimeError: when every retry lost the generation race.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")
    if mutate is None:
        raise ValueError("mutate callback is required")

    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    def _fresh_default():
        # JSON round-trip copy so retries never see a mutated shared default.
        return json.loads(json.dumps(default)) if default is not None else None

    _t_io = _time.perf_counter()

    if mode == 'gcs':
        from google.api_core import exceptions as gcs_exceptions

        bucket = _get_bucket()
        if not bucket:
            raise ValueError("GCS bucket not initialized")

        for attempt in range(max_retries):
            blob = bucket.get_blob(blob_name)
            if blob is None:
                current = _fresh_default()
                generation = 0  # precondition: object must not exist yet
                blob = bucket.blob(blob_name)
            else:
                generation = blob.generation
                try:
                    current = json.loads(blob.download_as_text())
                except (gcs_exceptions.NotFound, json.JSONDecodeError):
                    current = _fresh_default()

            new_value = mutate(current)
            if new_value is None:
                return None

            payload = json.dumps(new_value)
            try:
                blob.upload_from_string(payload, if_generation_match=generation)
            except (gcs_exceptions.PreconditionFailed, gcs_exceptions.NotFound):
                # Another writer changed the object between our read and
                # write — back off briefly and retry against fresh contents.
                if verbose:
                    logger.info(f"    [DATA_IO] update_json generation conflict on "
                                f"{blob_name} (attempt {attempt + 1}/{max_retries})")
                _time.sleep(0.2 * (attempt + 1))
                continue

            _io_log(
                op="update_json",
                loc=storage_location,
                filename=filename,
                mode=mode,
                bytes_=len(payload),
                t_ms=(_time.perf_counter() - _t_io) * 1000.0,
            )
            return new_value

        raise RuntimeError(
            f"update_json: lost the write race on '{blob_name}' "
            f"{max_retries} times — giving up."
        )

    # Local mode: per-path lock + write-temp-then-rename.
    with _update_json_locks_guard:
        lock = _update_json_locks.setdefault(os.path.abspath(primary), threading.Lock())

    with lock:
        current = _fresh_default()
        if os.path.exists(primary):
            try:
                with open(primary, encoding='utf-8') as file:
                    current = json.loads(file.read())
            except (OSError, json.JSONDecodeError):
                current = _fresh_default()

        new_value = mutate(current)
        if new_value is None:
            return None

        payload = json.dumps(new_value)
        os.makedirs(os.path.dirname(primary), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(primary), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                file.write(payload)
            os.replace(tmp_path, primary)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    _io_log(
        op="update_json",
        loc=storage_location,
        filename=filename,
        mode=mode,
        bytes_=len(payload),
        t_ms=(_time.perf_counter() - _t_io) * 1000.0,
    )
    return new_value




def load_text(storage_location: str = "cache", filename: str = "", verbose: bool = False) -> str | None:
    """Load a UTF-8 text file (e.g. a TOML contract) from a storage location.

    The text analogue of :func:`load_json` — used for raw text payloads (TOML,
    prompts) that must round-trip verbatim without JSON parsing. Handles the GCS
    read path.

    Args:
        storage_location: The storage-location key to resolve.
        filename: The file to read.
        verbose: When True, print diagnostic notices.

    Returns:
        The file's text, or ``None`` when it cannot be read.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    _t_io = _time.perf_counter()
    try:
        if mode == 'gcs':
            bucket = _get_bucket()
            if bucket:
                blob = bucket.blob(blob_name)
                if blob.exists():
                    content = blob.download_as_text()
                    _io_log(
                        op="load_text",
                        loc=storage_location,
                        filename=filename,
                        mode=mode,
                        bytes_=len(content),
                        t_ms=(_time.perf_counter() - _t_io) * 1000.0,
                    )
                    return content
                if verbose: logger.warning(f"    [DATA_IO] WARN: GCS Blob not found: {blob_name}.")
            else:
                if verbose: logger.warning("    [DATA_IO] WARN: GCS bucket not initialized.")
        else:
            with open(primary, encoding='utf-8') as file:
                content = file.read()
                _io_log(
                    op="load_text",
                    loc=storage_location,
                    filename=filename,
                    mode=mode,
                    bytes_=len(content),
                    t_ms=(_time.perf_counter() - _t_io) * 1000.0,
                )
                return content
    except Exception as e:
        if verbose: logger.warning(f"    [DATA_IO] Loading text failed ({mode}): {e}")
        if mode == 'local':
            logger.error(f"    [DATA_IO] ERROR Couldn't load '{filename}' from '{storage_location}': {e}")
            return None

    return None




def save_text(data: str = "", storage_location: str = "cache", filename: str = "", verbose: bool = False) -> int:
    """Save a UTF-8 text string (e.g. a TOML contract) to a storage location.

    The text analogue of :func:`save_json` — the payload is written verbatim
    (no JSON encoding) with a ``text/plain; charset=utf-8`` content type on GCS.

    Args:
        data: The text to write.
        storage_location: The storage-location key to resolve.
        filename: The destination file.
        verbose: When True, print diagnostic notices.

    Returns:
        ``0`` on success.
    """
    if data is None:
        raise ValueError("Data cannot be empty")
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if storage_location == "":
        raise ValueError("Storage location cannot be empty")

    primary, secondary, mode, blob_name = _resolve_paths(storage_location, filename)

    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        if bucket:
            blob = bucket.blob(blob_name)
            blob.upload_from_string(data, content_type="text/plain; charset=utf-8")
            if verbose: logger.info(f"    [DATA_IO] Saved text to GCS: {blob_name}")
        else:
            raise ValueError("GCS bucket not initialized")
    else:
        os.makedirs(os.path.dirname(primary), exist_ok=True)
        with open(primary, 'w', encoding='utf-8') as file:
            file.write(data)

    _io_log(
        op="save_text",
        loc=storage_location,
        filename=filename,
        mode=mode,
        bytes_=len(data),
        t_ms=(_time.perf_counter() - _t_io) * 1000.0,
    )

    return 0







def _repair_stringified_multiindex(df: pd.DataFrame) -> pd.DataFrame:
    """Restore tuple column names from their string representations.

    When pandas metadata is lost from a parquet file (e.g. after a pyarrow
    read/write round-trip), tuple columns like ('participants', 'email') are
    loaded as flat strings "('participants', 'email')".  This function detects
    that situation and parses them back to real tuples.

    When every column parses to a tuple the result is a real ``MultiIndex``;
    when tuples and plain strings are mixed it stays a flat Index so scalar
    columns like 'collection_id' remain accessible via ``df['collection_id']``.
    Callers that set an index should do so *before* calling this, which is what
    puts the frame in the all-tuples case.

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

    # When EVERY column is a tuple, build the MultiIndex explicitly. Left to
    # pandas, ``pd.Index(list_of_tuples)`` auto-promotes to a MultiIndex only
    # sometimes (2.2.x): the flat outcome made ``df.loc[row, ('personas',
    # 'active_days')]`` read the tuple as a list-of-labels and raise — an
    # intermittent timelines-refresh failure from identical stored bytes.
    # A mix of tuples and plain strings must stay a flat Index so scalar
    # columns like 'collection_id' remain directly accessible.
    if all(isinstance(c, tuple) for c in new_cols):
        df.columns = pd.MultiIndex.from_tuples(new_cols)
    else:
        df.columns = pd.Index(new_cols, tupleize_cols=False)
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
                    logger.info(f"    [DATA_IO] Parquet column '{ec}' not loaded since not requested")

            columns = list(set(confirmed_columns))
            if verbose:
                logger.info(f"    [DATA_IO] Column selection: {columns}")


        if verbose:
            logger.info(f"    [DATA_IO] Loading: all parquet files from folder '{storage_location}' (gcs)... ")
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
            logger.info(f"    [DATA_IO] ...done (shape: {df.shape})")

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
            logger.info(f"    [DATA_IO] Loaded parquet(s) shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

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
            if verbose: logger.warning(f"    [DATA_IO] WARN: Column selection failed: {e}")
            existing_cols = []
        columns = [c for c in columns if c in existing_cols]
        if verbose:
            logger.info(f"    [DATA_IO] Column selection: {columns}")


    if verbose: logger.info(f"    [DATA_IO] Loading: '{filename}' from '{storage_location}' ({mode})...")
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
        logger.warning(f" !! [DATA_IO] WARNING: Loading '{filename}' failed: {e}")
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
        logger.info(f"    [DATA_IO] ...done. Shape: {df.shape}. Time: {(t2-t1).total_seconds():.1f} seconds")

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
            logger.info(f"    [DATA_IO] Selective: {len(missing)} requested column(s) not in schema, skipping: {missing}")
        if not cols_to_read:
            logger.warning(f" !! [DATA_IO] WARNING: load_parquet_selective: no requested columns exist in '{filename}'")
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
        logger.warning(f" !! [DATA_IO] WARNING: load_parquet_selective: read failed for '{filename}': {e}")
        return None
    _t_io_ms = (_time.perf_counter() - _t_io) * 1000.0

    # Strip the `pandas` schema metadata to avoid the
    # `list<element: string>[pyarrow]` dtype-resolution failure during
    # to_pandas() when other (unselected) columns are list-typed on disk.
    meta = tbl.schema.metadata or {}
    new_meta = {k: v for k, v in meta.items() if k != b'pandas'}
    tbl = tbl.replace_schema_metadata(new_meta or None)

    df = tbl.to_pandas(types_mapper=pd.ArrowDtype)

    # Set the index BEFORE repairing tuple column names: with the scalar id
    # column out of the way the remaining columns are all tuples, so the repair
    # can build a real MultiIndex instead of an ambiguous mixed flat Index.
    if set_index is not None and set_index in df.columns:
        df = df.set_index(set_index)

    df = _repair_stringified_multiindex(df)

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
        logger.info(f"    [DATA_IO] Selective load: '{filename}' shape={df.shape} time={(t2-t1).total_seconds():.3f}s")

    return df




# ----------------------------------------------------------------------------
# Streaming primitives for corpus-scale workers.
#
# These keep peak memory at one record batch / one byte range instead of a
# whole file. They deliberately reuse the exact local-vs-GCS plumbing the
# eager functions use (_resolve_paths + gcsfs / _get_bucket) — never
# local_copy(), whose temp dir is memory-backed on Cloud Run.
# ----------------------------------------------------------------------------


def iter_parquet_batches(
        storage_location: str = "cache",
        filename: str = "",
        columns: list = None,
        filters: list = None,
        batch_size: int = 131_072,
        verbose: bool = False,
    ):
    """Stream a parquet file as pyarrow RecordBatches.

    The bounded-memory counterpart of :func:`load_parquet_selective`: the
    same column projection and filter semantics, but yielded one record
    batch at a time so peak memory is O(batch_size), not O(file).

    Args:
        storage_location: Named storage location (e.g. "cache", "recoded").
        filename: Parquet file name. Must end in ".parquet".
        columns: Optional on-disk column names to project. Requested columns
            missing from the schema are skipped (with a notice), matching
            :func:`load_parquet_selective`.
        filters: Optional list-of-tuples filter expressions (same shapes
            :func:`load_parquet_selective` accepts). Rows are filtered during
            the scan; pushdown prunes row groups only when the file is sorted
            on the filter column.
        batch_size: Maximum rows per yielded RecordBatch.
        verbose: Print a scan notice.

    Yields:
        ``pyarrow.RecordBatch`` objects (possibly empty batches are skipped).
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
    if mode == 'gcs':
        dataset = pads.dataset(primary, format='parquet',
                               filesystem=gcsfs.GCSFileSystem())
    else:
        dataset = pads.dataset(primary, format='parquet')

    cols_to_read = None
    if columns is not None:
        existing_cols = dataset.schema.names
        missing = [c for c in columns if c not in existing_cols]
        cols_to_read = [c for c in columns if c in existing_cols]
        if missing and verbose:
            logger.info(f"    [DATA_IO] iter_parquet_batches: {len(missing)} requested column(s) not in schema, skipping: {missing}")
        if not cols_to_read:
            logger.warning(f" !! [DATA_IO] WARNING: iter_parquet_batches: no requested columns exist in '{filename}'")
            return

    expr = pq.filters_to_expression(filters) if filters else None
    scanner = dataset.scanner(columns=cols_to_read, filter=expr,
                              batch_size=batch_size)
    if verbose:
        logger.info(f"    [DATA_IO] Streaming '{filename}' (batch_size={batch_size:,})")
    for batch in scanner.to_batches():
        if batch.num_rows:
            yield batch




def write_parquet_stream(
        storage_location: str = "cache",
        filename: str = "",
        batches=None,
        schema: "pa.Schema" = None,
        compression: str = "zstd",
        compression_level: int = 5,
        verbose: bool = False,
    ) -> int:
    """Write an iterable of RecordBatches/Tables as one parquet file.

    The bounded-memory counterpart of :func:`save_parquet`: batches are
    written through a ``ParquetWriter`` to a local tempfile (one batch resident
    at a time — no frame copies), then moved into place / uploaded once.

    Args:
        storage_location: Named storage location.
        filename: Destination parquet filename.
        batches: Iterable of ``pyarrow.RecordBatch`` or ``pyarrow.Table``
            objects, all matching ``schema``.
        schema: The file schema (required — the iterable may be empty, and an
            empty file must still carry the right columns).
        compression: Parquet codec.
        compression_level: Codec level.
        verbose: Print a completion notice.

    Returns:
        The number of rows written.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if schema is None:
        raise ValueError("schema is required")

    primary, _, mode, blob_name = _resolve_paths(storage_location, filename)

    _t_io = _time.perf_counter()
    n_rows = 0
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            tmp_path = tmp.name
        with pq.ParquetWriter(tmp_path, schema, compression=compression,
                              compression_level=compression_level) as writer:
            for batch in (batches or []):
                if isinstance(batch, pa.Table):
                    writer.write_table(batch)
                else:
                    writer.write_batch(batch)
                n_rows += batch.num_rows
        if mode == 'gcs':
            bucket = _get_bucket()
            if not bucket:
                raise ValueError("GCS bucket not initialized")
            bucket.blob(blob_name).upload_from_filename(tmp_path)
        else:
            os.makedirs(os.path.dirname(primary), exist_ok=True)
            shutil.move(tmp_path, primary)
            tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    _io_log(op="write_parquet_stream", loc=storage_location, filename=filename,
            mode=mode, bytes_=0, t_ms=(_time.perf_counter() - _t_io) * 1000.0)
    if verbose:
        logger.info(f"    [DATA_IO] Streamed {n_rows:,} rows to '{filename}'")
    return n_rows




def concat_parquet_files(
        src_storage_location: str = "cache",
        src_filenames: list = None,
        dst_storage_location: str = "cache",
        dst_filename: str = "",
        batch_size: int = 131_072,
        verbose: bool = False,
    ) -> int:
    """Concatenate parquet files into one, at one-record-batch peak memory.

    Built on :func:`iter_parquet_batches` + :func:`write_parquet_stream`.
    All sources must share a schema (the first file's schema is used; a
    mismatched source raises).

    Args:
        src_storage_location: Location of the source files.
        src_filenames: Ordered source parquet filenames.
        dst_storage_location: Destination location.
        dst_filename: Destination parquet filename.
        batch_size: Rows per streamed batch.
        verbose: Print a completion notice.

    Returns:
        Total rows written.
    """
    if not src_filenames:
        raise ValueError("src_filenames cannot be empty")

    first_primary, _, first_mode, _ = _resolve_paths(src_storage_location, src_filenames[0])
    if first_mode == 'gcs':
        with gcsfs.GCSFileSystem().open(first_primary) as f:
            schema = pq.read_schema(f)
    else:
        schema = pq.read_schema(first_primary)
    # ParquetWriter compares logical schemas; the source's pandas metadata
    # would make every later file "mismatch" spuriously.
    schema = schema.remove_metadata()

    def _all_batches():
        for src in src_filenames:
            for batch in iter_parquet_batches(
                    storage_location=src_storage_location, filename=src,
                    batch_size=batch_size):
                yield pa.record_batch(batch.columns, schema=schema)

    n_rows = write_parquet_stream(
        storage_location=dst_storage_location, filename=dst_filename,
        batches=_all_batches(), schema=schema, verbose=verbose)
    if verbose:
        logger.info(f"    [DATA_IO] Concatenated {len(src_filenames)} file(s) -> '{dst_filename}' ({n_rows:,} rows)")
    return n_rows




def save_bytes(data: bytes = b"", storage_location: str = "cache",
               filename: str = "", verbose: bool = False) -> int:
    """Save a raw binary payload to a storage location.

    The binary analogue of :func:`save_text`.

    Args:
        data: The bytes to write.
        storage_location: The storage-location key to resolve.
        filename: The destination file.
        verbose: When True, print diagnostic notices.

    Returns:
        The number of bytes written.
    """
    if data is None:
        raise ValueError("Data cannot be None")
    if filename == "":
        raise ValueError("Filename cannot be empty")

    primary, _, mode, blob_name = _resolve_paths(storage_location, filename)
    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        if not bucket:
            raise ValueError("GCS bucket not initialized")
        bucket.blob(blob_name).upload_from_string(
            bytes(data), content_type="application/octet-stream")
    else:
        os.makedirs(os.path.dirname(primary), exist_ok=True)
        with open(primary, 'wb') as file:
            file.write(data)
    _io_log(op="save_bytes", loc=storage_location, filename=filename,
            mode=mode, bytes_=len(data), t_ms=(_time.perf_counter() - _t_io) * 1000.0)
    if verbose:
        logger.info(f"    [DATA_IO] Saved {len(data):,} bytes to '{filename}'")
    return len(data)




def load_bytes(storage_location: str = "cache", filename: str = "",
               start: int = None, length: int = None,
               verbose: bool = False) -> bytes | None:
    """Load a raw binary payload (optionally one byte range).

    Args:
        storage_location: The storage-location key to resolve.
        filename: The file to read.
        start: Optional first byte offset (None = start of file).
        length: Optional number of bytes (None = to end of file).
        verbose: When True, print diagnostic notices.

    Returns:
        The bytes, or None when the file does not exist.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if not exists(storage_location, filename):
        return None

    primary, _, mode, blob_name = _resolve_paths(storage_location, filename)
    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        blob = bucket.blob(blob_name)
        if start is None and length is None:
            data = blob.download_as_bytes()
        else:
            s = start or 0
            end = (s + length - 1) if length is not None else None
            data = blob.download_as_bytes(start=s, end=end)
    else:
        with open(primary, 'rb') as file:
            if start:
                file.seek(start)
            data = file.read(length) if length is not None else file.read()
    _io_log(op="load_bytes", loc=storage_location, filename=filename,
            mode=mode, bytes_=len(data), t_ms=(_time.perf_counter() - _t_io) * 1000.0)
    if verbose:
        logger.info(f"    [DATA_IO] Loaded {len(data):,} bytes from '{filename}'")
    return data




def read_byte_ranges(storage_location: str = "cache", filename: str = "",
                     ranges: list = None, max_workers: int = 32,
                     verbose: bool = False) -> list:
    """Read many byte ranges from one stored object.

    The random-access primitive under the dense embedding sidecar: local mode
    is one file descriptor + ``os.pread`` per range; GCS mode fans ranged
    GETs (``Blob.download_as_bytes(start=, end=)``) over a thread pool.
    Callers should sort and coalesce adjacent ranges first — each range is
    one request in GCS mode.

    Args:
        storage_location: The storage-location key to resolve.
        filename: The file to read.
        ranges: List of ``(offset, length)`` tuples.
        max_workers: Thread-pool width for GCS mode.
        verbose: When True, print a summary notice.

    Returns:
        A list of ``bytes`` objects, positionally matching ``ranges``.
    """
    if filename == "":
        raise ValueError("Filename cannot be empty")
    if not ranges:
        return []

    primary, _, mode, blob_name = _resolve_paths(storage_location, filename)
    _t_io = _time.perf_counter()
    if mode == 'gcs':
        bucket = _get_bucket()
        blob = bucket.blob(blob_name)

        def _one(rng):
            off, length = rng
            return blob.download_as_bytes(start=off, end=off + length - 1)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            out = list(ex.map(_one, ranges))
    else:
        # seek+read, not os.pread — the latter does not exist on Windows.
        with open(primary, 'rb') as file:
            out = []
            for off, length in ranges:
                file.seek(off)
                out.append(file.read(length))
    total = sum(len(b) for b in out)
    _io_log(op="read_byte_ranges", loc=storage_location, filename=filename,
            mode=mode, bytes_=total, t_ms=(_time.perf_counter() - _t_io) * 1000.0)
    if verbose:
        logger.info(f"    [DATA_IO] Read {len(ranges)} range(s), {total:,} bytes from '{filename}'")
    return out




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
        logger.info(f"    [DATA_IO] Total DF memory usage: {total_memory_mb:.2f} MB.")
        logger.info(f"    [DATA_IO] Saving '{filename}' to '{storage_location}' ({mode})...")

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
                    logger.error(f"   [DATA_IO ASYNC] Parquet save failed: {future.exception()}")
            else:
                if verbose:
                    logger.info("    [DATA_IO ASYNC] Parquet save succeeded.")

        def safe_save(df):
            # This 'with' block ensures only one thread can execute the save at a time
            with file_lock:
                if verbose:
                    logger.info(f"    [DATA_IO ASYNC] Starting save to {primary}... (locked)")
                _write_parquet(df)
                if verbose:
                    logger.info(f"    [DATA_IO ASYNC] Finished save to {primary}. (unlocked)")

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

    if verbose: logger.info(f"    [DATA_IO] ...moving on. Shape: {this_df.shape}")

    return this_df

