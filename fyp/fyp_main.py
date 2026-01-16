# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

from typing import Iterable, List
from copy import copy
from google import genai
from google.genai import types as gemini_types
from google.api_core.exceptions import Forbidden as google_Forbidden
from google.cloud import storage as gcs_storage
import http.client
import os
import toml
import pandas as pd
from sys import path as sys_path
from datetime import datetime, timedelta
import fyp.data_io as data_io
from json import dumps as json_dumps
import numpy as np
import pyarrow as pa
from difflib import SequenceMatcher
from urllib.parse import unquote


############################################################################################################
###                     Initialize things
############################################################################################################



# check internet connectivity
def _checkInternetHttplib(url="www.qut.edu.au",
                        timeout=3):
    connection = http.client.HTTPConnection(url,
                                        timeout=timeout)
    try:
        # only header requested for fast operation
        connection.request("HEAD", "/")
        connection.close()  # connection closed
        return True
    except Exception as exep:
        print(exep)
        return False



def _create_local_dirs(cf: dict, verbose: bool = False):
    # create missing local folders if not using GCS for data
    if not cf['data_io']['use_gcs_for_data'] or cf['misc']['local_mode']:
        if verbose:
            print(f"Data is stored in locally")
            print(f"Cache is stored in locally")
        for k in cf["paths"].keys():
            os.makedirs(cf["paths"][k], exist_ok=True)
    # create missing local folders if not using GCS for data
    elif not cf['data_io']['use_gcs_for_cache']:
        if verbose:
            print(f"Cache is stored in locally")
        if not os.path.exists(cf["paths"]["cache"]):
            if verbose:
                print("Creating missing local folder for cache")
            os.makedirs(cf["paths"]["cache"], exist_ok=True)




def connect_to_google(cf_in, verbose=False):
    cf = copy(cf_in)

    cf["data_io"]["bucket"] = None

    if verbose:
        print("Checking internet connection...")
    online_ok = _checkInternetHttplib()

    if online_ok:
        print("...I'm online")
        try:
            with open(cf['machine']['prompt'], 'r') as file:
                machine_prompt = file.read()

            cf["machine"]["client"] = genai.Client(
                vertexai=cf["machine"]["vertexai"],
                project=cf["machine"]["project"],
                location=cf["machine"]["location"],
                http_options=gemini_types.HttpOptions(
                    api_version=cf["machine"]["http_options_api_version"],
                    timeout=cf["machine"]["http_options_timeout"]
                )
            )

            cf["machine"]["global_generation_config"] = gemini_types.GenerateContentConfig(
                system_instruction=machine_prompt,
                temperature=cf["machine"]["temperature"],
                max_output_tokens=cf["machine"]["max_output_tokens"],
                response_mime_type=cf["machine"]["response_mime_type"],
                presence_penalty=cf["machine"]["presence_penalty"],
                frequency_penalty=cf["machine"]["frequency_penalty"],
                thinking_config=gemini_types.ThinkingConfig(thinking_budget=cf["machine"]["thinking_budget"]),
            )

            print("Google Gemini initialized successfully")

        except Exception as e:
            print(f"Error Gemini API key. Gemini won't be available. {e}")


        # Initialize a GCS storage client
        try:
            bucket_client = gcs_storage.Client()

            # Get the GCS bucket
            bucket = bucket_client.get_bucket(cf["data_io"]["GCS_bucket_name"])

            # Try to access the GCS bucket's metadata
            bucket.reload()
            cf["data_io"]["bucket"] = bucket
            print(f"Access to the project Google Cloud Storage bucket {bucket.name} located at {bucket.location} is authorized.")
            if verbose:
                if cf['data_io']['use_gcs_for_data']:
                    print(f"Data is stored in GCS")
                if cf['data_io']['use_gcs_for_cache']:
                    print(f"Cache is stored in GCS")
                if cf['data_io']['use_gcs_for_media']:
                    print(f"Media is stored in GCS")


            return cf
        
        except google_Forbidden:
            print(f"I don't have access to the Google Cloud Storage.")
        except Exception as e:
            print(f"A Google Cloud Storage error occurred: {e}")

    else:
        print("...No internet connection. Running local mode without connecting to Google services.")
        cf['misc']['local_mode'] = True
    

    cf['data_io']['use_gcs_for_data'] = False
    cf['data_io']['use_gcs_for_cache'] = False
    cf['data_io']['use_gcs_for_media'] = False
    _create_local_dirs(cf, verbose=verbose)
    return cf






def initialize(
    verbose=False,
    abs_project_root_path=None
    ) -> dict:

    

    # ------------------------------------------------------------------
    # Locate the project root - I don't know what other people do - this works for me
    # ------------------------------------------------------------------
    if abs_project_root_path is None:

        # I put an empty __proj__.py file in the root folder of the project structure
        here = os.getcwd().split("/")
        while not os.path.exists(os.path.join("/".join(here),"__proj__.py")):
            here.pop()

        # this is the root folder for the project structure
        abs_project_root_path = os.path.join("/".join(here))
        if verbose:
            print("Project root:",abs_project_root_path)

        # add project root path to PATH since the modules are located in the project structure
        sys_path.append(abs_project_root_path)


    
    # ------------------------------------------------------------------
    # Load essential files - let it blow up if the files aren't found
    # ------------------------------------------------------------------
    where_to_start = toml.load(os.path.join(abs_project_root_path,"config","core.toml"))

    config_path = os.path.join(abs_project_root_path,"config",where_to_start["core"]["config_fn"])
    study_defs_path = os.path.join(abs_project_root_path,"config",where_to_start["core"]["study_defs_fn"])
    var_schema_path = os.path.join(abs_project_root_path, "config", where_to_start["core"]["var_schema_fn"])

    # Load main config
    cf = toml.load(config_path)
    cf["paths"]["project_root"] = abs_project_root_path

    # Load variable schema
    cf["var_schema"] = pd.read_csv(var_schema_path, dtype_backend="pyarrow")

    # Load study definitions
    study_defs = toml.load(study_defs_path)
    for study_name in study_defs.keys():
        study_defs[study_name]["STUDY_NAME"] = study_name
    cf["study_defs"] = study_defs





    # ------------------------------------------------------------------
    # Use env var for secrets; fall back to config if present (avoid committing real keys)
    # ------------------------------------------------------------------
    gcp_bucket_name = os.environ.get("FYP_GCS_BUCKET_NAME")
    if gcp_bucket_name:
        cf["data_io"]["GCS_bucket_name"] = gcp_bucket_name

    gemini_env_key = os.environ.get("GEMINI_API_KEY")
    if gemini_env_key:
        cf["machine"]["key"] = gemini_env_key



    # ------------------------------------------------------------------
    # prepare gen ai parameters for initialisation, which happens in 'connect_to_google'
    # ------------------------------------------------------------------
    cf["machine"]["client"] = None
    cf["machine"]["global_generation_config"] = None

    # I've used different prompts in the config. This allows for some flexibility.
    # It is expected that the parameter in the config file is a filename to a text file
    # that is located in a folder named 'prompts' in the project root. 
    for p in cf["machine"].keys():
        if "prompt" in p:
            cf["machine"][p] = os.path.join(cf["paths"]["project_root"],"prompts",cf["machine"][p])


    # ------------------------------------------------------------------
    # initialize paths
    # ------------------------------------------------------------------
    # Resolve relative paths against the project root for consistent file access.
    cf["paths"]["local_data"] = os.path.abspath(os.path.join(cf["paths"]["project_root"], cf["paths"]["local_data"]))

    # paths to zeeschuimer data
    cf["paths"]["zeeschuimer"] = os.path.join(cf["paths"]["local_data"],"activity_data", "zeeschuimer")
    cf["paths"]["zeeschuimer_raw"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_raw")
    cf["paths"]["zeeschuimer_refined"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_refined")
    cf["paths"]["zeeschuimer_main"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_main")

    # paths to ddp data
    cf["paths"]["ddp"] = os.path.join(cf["paths"]["local_data"],"activity_data", "ddp")
    cf["paths"]["ddp_raw"] = os.path.join(cf["paths"]["ddp"], "ddp_raw")
    cf["paths"]["ddp_processed"] = os.path.join(cf["paths"]["ddp"], "ddp_processed")
    cf["paths"]["ddp_main"] = os.path.join(cf["paths"]["ddp"], "ddp_main")
    cf["paths"]["ddp_participants"] = os.path.join(cf["paths"]["ddp"], "ddp_participants")

    # paths to scrape data
    cf["paths"]["scrape"] = os.path.join(cf["paths"]["local_data"], "scrape")

    # paths to machine annotations
    cf["paths"]["machine_annotations"] = os.path.join(cf["paths"]["local_data"], "machine_annotations")
    cf["paths"]["machine_annotations_raw"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_raw")
    cf["paths"]["machine_annotations_refined"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_refined")

    # other paths
    cf["paths"]["recoded"] = os.path.join(cf["paths"]["local_data"], "recoded")
    cf["paths"]["exports"] = os.path.join(cf["paths"]["local_data"], "exports")
    cf["paths"]["archive"] = os.path.join(cf["paths"]["local_data"], "archive")
    cf["paths"]["users"] = os.path.join(cf["paths"]["local_data"], "users") 
    cf["paths"]["cache"] = os.path.join(cf["paths"]["local_data"], "cache") 
    
    cf["paths"]["temp"] = "/tmp/fyp/"
    os.makedirs(cf["paths"]["temp"], exist_ok=True)
    

    # This is not set by the config so I'm setting it to None
    cf["data_io"]["bucket"] = None

    # If local mode is enabled, set the GCS flags to False
    if cf['misc']['local_mode']:
        print("Local mode is enabled. GCS data will not be used.")
        cf['data_io']['use_gcs_for_data'] = False
        cf['data_io']['use_gcs_for_cache'] = False
        cf['data_io']['use_gcs_for_media'] = False


    if cf['data_io']['use_gcs_for_data']:

        cf["gcs_paths"] = {}
        gcs_prefix = cf["data_io"].get("gcs_data_prefix", "")

        for k, v in cf["paths"].items():
            if isinstance(v, str) and v.startswith(cf["paths"]["local_data"]) and k != "local_data":
                # calculate relative path from local_data root
                # e.g. /.../data/activity/zeeschuimer -> activity/zeeschuimer
                rel = os.path.relpath(v, cf["paths"]["local_data"])
                
                # Combine with GCS prefix
                # Use forward slashes for GCS always, though on Mac os.path.join uses /
                if rel == ".": 
                    gcs_path = gcs_prefix
                else:
                    gcs_path = f"{gcs_prefix}/{rel}" if gcs_prefix else rel
                    
                cf["gcs_paths"][k] = gcs_path
        


    # create missing local folders - note that this function first checks relevant flags and
    # only creates folders if needed 
    _create_local_dirs(cf, verbose=verbose)

    return cf











############################################################################################################
###                     File, directory mgmt
############################################################################################################








def get_recent_files(cf, storage_location, suffix=None, how_recent=10):

    current_time = datetime.now()
    recent_files = []

    for filename in data_io.listdir(cf, storage_location):
        #file_path = join(storage_location, filename)
        if suffix is None or filename.endswith(suffix):
            modified_time = datetime.fromtimestamp(data_io.getmtime(cf, storage_location, filename))
            created_time = datetime.fromtimestamp(data_io.getctime(cf, storage_location, filename))
            time_difference = current_time - max(modified_time, created_time)
            if time_difference < timedelta(minutes=how_recent):
                recent_files.append({"filename":filename, "mtime":modified_time, "ctime":created_time})

    return sorted(recent_files,key=lambda x: x["mtime"], reverse=True)



def fix_surrogates(text):
    if not isinstance(text, str):
        return text
    # This trick encodes surrogates to UTF-16 and decodes them correctly
    return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')




def fix_complex_types(some_iterable, verbose=False):

    if not len(some_iterable.shape) == 1:
        raise ValueError("Input must be a 1D iterable")

    if verbose:
        print("    [PYARROW dtypes - complex] Starting special treatment of complex types...")
        print("    [PYARROW dtypes - complex] Input iterable length:", some_iterable.shape[0])
    
    # replace nans with pd.NA
    some_iterable[some_iterable.isna()] = pd.NA

    # I think I have to convert the types to strings to count them
    row_types = some_iterable.dropna().map(lambda x:str(type(x)))
    type_counts = row_types.value_counts()

    if verbose:
        tc_for_display = type_counts.to_dict()
        tc_for_display = " | ".join([f"{a.split(chr(39))[1].upper()}: {tc_for_display[a]}" for a in tc_for_display])
        print("    [PYARROW dtypes - complex] Type counts:", tc_for_display)

    # check if there are dicts in the iterable - if yes, convert them to json strings
    if "<class 'dict'>" in type_counts.index:
        dict_indeces = row_types[row_types == "<class 'dict'>"].index
        some_iterable.loc[dict_indeces] = some_iterable.loc[dict_indeces].map(lambda x: json_dumps(x))

        if verbose:
            print("    [PYARROW dtypes - complex] Dicts converted to json strings")

        # If the dicts have been turned into strings, I need to check the
        # types again
        row_types = some_iterable.dropna().map(lambda x:str(type(x)))
        type_counts = row_types.value_counts()

        if verbose:
            tc_for_display = type_counts.to_dict()
            tc_for_display = " | ".join([f"{a.split(chr(39))[1].upper()}: {tc_for_display[a]}" for a in tc_for_display])
            print("    [PYARROW dtypes - complex] Type counts after dict conversion:", tc_for_display)

    # check if elements in lists in the iterable have a single type
    # If they don't - raise an error 
    if "<class 'list'>" in type_counts.index:
    
        list_indeces = row_types[row_types == "<class 'list'>"].index
        element_types = []
        for i in list_indeces:
            for j in some_iterable.loc[i]:
                element_types += [type(j)]
        element_types = list(set(element_types))

        if verbose:
            print("    [PYARROW dtypes - complex] Element types in lists:", " | ".join([str(a) for a in element_types]))

        if len(element_types) > 1:
            raise ValueError("Lists in the iterable contains elements of different types")
        if len(element_types) == 1 and element_types[0] in [list, dict]:
            if verbose:
                print(f"    [PYARROW dtypes - complex] Lists in the iterable contains elements of type {element_types[0]} - converting to json strings")
            for i in list_indeces:
                some_iterable.loc[i] = [json_dumps(j) for j in some_iterable.loc[i]]
    
                    
    # if all rows in the iterable is of the same type, then all is good
    if len(type_counts) == 1:
        if verbose:
            print("    [PYARROW dtypes - complex] All rows in the iterable is of the same type")
        return some_iterable

    # if there are more than a single type and there are lists - convert all to lists
    if "<class 'list'>" in type_counts.index:
        nonlist_indeces = row_types[~row_types.isin(["<class 'list'>"])].index
        try:
            some_iterable.loc[nonlist_indeces] = some_iterable.loc[nonlist_indeces].map(lambda x: [element_types[0](x)])
        except Exception as e:
            if verbose:
                print(f"    [PYARROW dtypes - complex] Failed to convert non-list elements to lists and type {element_types[0]}. Trying one row at a time")
            for i in nonlist_indeces:
                try:
                    some_iterable.loc[i] = [element_types[0](some_iterable.loc[i])]
                except Exception as e:
                    if verbose:
                        print(f"    [PYARROW dtypes - complex] Failed to convert row {i} to list and type {element_types[0]}. Setting to pd.NA")
                    some_iterable.loc[i] = pd.NA
        
        if verbose:
            print("    [PYARROW dtypes - complex] Non-list elements converted to lists")

        return some_iterable


    if "<class 'str'>" in type_counts.index:
        if verbose:
            print("    [PYARROW dtypes - complex] Multiple types, one is 'str' - converting all to pyarrow strings")
        some_iterable = some_iterable.astype('string[pyarrow]')

    
    return some_iterable









def convert_index_dtype_pyarrow(an_index):

    # Handle MultiIndex recursively
    if isinstance(an_index, pd.MultiIndex):
        new_levels = [convert_index_dtype_pyarrow(lvl) for lvl in an_index.levels]
        return an_index.set_levels(new_levels)

    # Use Series.convert_dtypes to handle int, float, string, datetime, etc.
    # robustly mapping to pyarrow backends.
    name = an_index.name

    # Convert to Series to access convert_dtypes
    s = pd.Series(an_index)

    # Attempt optimistic pyarrow conversion
    s_pa = s.convert_dtypes(dtype_backend="pyarrow")
    
    # Reconstruct Index preserving name
    new_index = pd.Index(s_pa)
    new_index.name = name
    return new_index







def convert_dtypes_to_pyarrow(df_in, verbose=False):

    df = df_in.copy()
    
    # ---------------------------------------------------------
    # 1. OPTIMISTIC BATCH CONVERSION
    # ---------------------------------------------------------
    if verbose:
        print("    [PYARROW dtypes] Attempting batch conversion of DF dtype to pyarrow...")
    
    try:
        # This handles the vast majority of "easy" columns (int, float, clean strings)
        # much faster than iterating column by column.
        df = df.convert_dtypes(dtype_backend='pyarrow')
    except Exception as e:
        if verbose:
            print(f"    [PYARROW dtypes] Batch conversion failed ({e}). Falling back to column-wise checks.")

    # ---------------------------------------------------------
    # 2. IDENTIFY AND FIX PROBLEMATIC COLUMNS
    # ---------------------------------------------------------
    # We only need to spend time on columns that are STILL 'object' 
    # (meaning pyarrow couldn't natively handle them).
    # Note: convert_dtypes automatically converts objects to strings if possible.
    # If it fails/ambiguous, it leaves them as object.
    
    cols_to_check = [c for c in df.columns if df[c].dtype == "object"]

    if len(cols_to_check) > 0 and verbose:
        print(f"    [PYARROW dtypes] Refining {len(cols_to_check)} columns that failed simple batch conversion...")

    for col in cols_to_check:
        # A) Try explicit conversion (sometimes works individually if batch had a holistic issue, though rare)
        try:
            df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
        except:
            pass
        
        # If still object, it likely has issues (surrogates, mixed types, etc.)
        if df[col].dtype == "object":
            if verbose:
                print(f"    [PYARROW dtypes] {col} - Fixing surrogates")
            
            # B) Fix surrogates
            try:
                # We apply map only if necessary to save time, but safe to just apply
                df[col] = df[col].map(fix_surrogates)
                df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')
            except Exception as e:
                if verbose:
                    print(f"    [PYARROW dtypes] {col} - ERROR:Surrogate fix didn't fully resolve ({e}).")

        # If STILL object, it's likely complex types (lists, dicts, etc.)
        if df[col].dtype == "object":
            if verbose:
                print(f"    [PYARROW dtypes] {col} is still object - sending it to special treatment of complex types...")
            try:
                # First, ensure contents are normalized (e.g. dicts -> json strings)
                df[col] = fix_complex_types(df[col].copy(), verbose=verbose)
                
                # Now, standard convert_dtypes often fails on lists of strings, leaving them as object.
                # We try to explicitly convert to a pyarrow array and back again.
                try:
                    
                    # Create pyarrow array from the series
                    # type_inference=True is default, but explicit casting can help if we know it's string
                    arrow_array = pa.array(df[col])
                    
                    # Check if the resulting array is actually a list type (or other complex type we want)
                    # If it's just 'string' or 'int', convert_dtypes would have likely caught it, 
                    # but if it's List<String>, convert_dtypes might miss it.
                    if pa.types.is_list(arrow_array.type) or pa.types.is_struct(arrow_array.type):
                         if verbose:
                             print(f"    [PYARROW dtypes] {col} - Explicitly converting to {arrow_array.type} via pyarrow.array...")
                         df[col] = pd.Series(
                             arrow_array, 
                             dtype=pd.ArrowDtype(arrow_array.type),
                             index=df[col].index
                         )
                    else:
                         # Fallback to standard convert_dtypes if it wasn't a complex arrow type
                         df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')

                except Exception as e:
                    if verbose:
                        print(f"    [PYARROW dtypes] {col} - Explicit pyarrow Array conversion failed: {e}")
                    # Fallback to standard 
                    df[col] = df[col].convert_dtypes(dtype_backend='pyarrow')

            except Exception as e:
                # Last resort: if complex fix fails, force string conversion for anything not null
                if verbose: 
                    print(f"    [PYARROW dtypes] {col} - Failed to fix complex types: {e}. Forcing string conversion.")
                df[col] = df[col].astype("string[pyarrow]")
        
        if verbose and df[col].dtype != "object":
             print(f"    [PYARROW dtypes] {col} - Successfully converted to {df[col].dtype}")

    # ---------------------------------------------------------
    # 3. FINAL SAFETY CHECKS (NUMERICS)
    # ---------------------------------------------------------
    numeric_cols_to_check = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]

    if len(numeric_cols_to_check) > 0:
        # trying to calculate describe() to catch overflow issues (integers > 2^53)
        # that would be rejected by explicit float-casting in describe's percentile calc.
        try:
            if verbose:
                print(f"    [PYARROW dtypes] Found {len(numeric_cols_to_check)} numeric columns - checking all for overflows...")
            df[numeric_cols_to_check].describe()
        except Exception as e:
            if verbose:
                print(f"    [PYARROW dtypes] Failed to describe numeric columns in one go - checking each column:")

            # Iterate through all columns that claim to be numeric now
            for c in numeric_cols_to_check:
                try:
                    df[c].describe()
                except Exception as e:
                    if verbose:
                        print(f"    [PYARROW dtypes] WARNING: {e} | {c} doesn't work well as a number - converting to string")
                    df[c] = df[c].astype("string[pyarrow]")
        
    if verbose:
        print("    [PYARROW dtypes] ...conversion complete.")

    return df





############################################################################################################
###                     Utilities
############################################################################################################







def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]




def is_list_like_col(s):
    # Check for the Arrow List type (your original code)
    is_arrow_list = (
        isinstance(s.dtype, pd.ArrowDtype) and 
        pa.types.is_list(s.dtype.pyarrow_dtype)
    )
    # Check for the "good old" object type
    is_object = s.dtype == "object"
    
    return is_arrow_list or is_object



def sort_by_similarity(reference: str, candidates: Iterable[str]) -> List[str]:
    """
    Return the candidates sorted from most to least similar to the reference string.
    Similarity is measured via difflib.SequenceMatcher ratio (0.0–1.0).
    """

    return sorted(
        candidates,
        key=lambda candidate: SequenceMatcher(None, reference, candidate).ratio(),
        reverse=True,
    )






def pretty_str_seconds(proc_time_seconds: float) -> str:
    minutes, seconds = divmod(proc_time_seconds, 60)
    out = ""
    if minutes > 0:
        out += f"{minutes:.0f}m"
    if seconds > 0:
        if minutes > 0:
            out += " and "
        out += f"{seconds:.0f}s"
    return out




def extract_and_join_subkeys(data, sub_keys: list):
    """
    Process a list of dictionaries or a single value, extracting and joining specified sub-keys.

    Args:
    data (list or any): The input data to process. If it's a list, each item is expected to be a dictionary.
    sub_keys (list): A list of keys to extract from each dictionary in the list.

    Returns:
    str or numpy.nan: A string of concatenated values from the specified sub-keys, 
                      or numpy.nan if the input is not a list or is empty.

    Description:
    This function extracts and concatenates values from specific keys in a list of dictionaries.
    If the input is not a list or is empty, it returns numpy.nan.
    For each dictionary in the list, it extracts the values of the specified sub-keys,
    joins them with "__", and then joins all these combined values with " | ".

    Example:
    >>> data = [
    ...     {"id": 1, "name": "John", "age": 30},
    ...     {"id": 2, "name": "Jane", "age": 25},
    ...     {"id": 3, "name": "Bob", "age": 35}
    ... ]
    >>> sub_keys = ["name", "age"]
    >>> result = extract_and_join_subkeys(data, sub_keys)
    >>> print(result)
    'John__30 | Jane__25 | Bob__35'
    """
    joined_values = []
    if isinstance(data, list) and len(sub_keys) > 0:
        for item in data:
            if isinstance(item, dict):
                subkey_values = []
                for sk in sub_keys:
                    if sk in item:
                        subkey_values.append(str(item[sk]))
                joined_values.append("__".join(subkey_values))
        return " | ".join(joined_values)
    else:
        return pd.NA




def clean_url(the_url: str) -> dict:
    outout = {}
    if "?" not in the_url or "&" not in the_url:
        return outout
    for u in the_url.split("?")[1].split("&"):
        v = u.split("=")
        v[1] = unquote(v[1]).replace(",","|")
        try:
            v1 = int(v1)
        except:
            pass
        outout.update({"source_url."+v[0]:v[1]})
    return outout



def flatten_list(nested_list):
    """
    Flattens a nested list into a single list.
    """
    return [item for sublist in nested_list for item in sublist]









############################################################################################################
###                     manage data storage
############################################################################################################

"""def DONT_USE_get_gcp_bucket(bucket_name, verbose = False):

    try:
        # Initialize a client
        client = gcs_storage.Client()

        # Get the bucket
        bucket = client.get_bucket(bucket_name)

        # Try to access the bucket's metadata
        bucket.reload()
        if verbose:
            print(f"Access to the bucket '{bucket_name}' is authorized.")
        return bucket
    except google_Forbidden:
        if verbose:
            print(f"You don't have access to the bucket '{bucket_name}'.")
        return None
    except Exception as e:
        if verbose:
            print(f"An error occurred: {e}")
        return None



def DONT_USE_init_data_io(verbose=False):

    #cf = initialize()

    if cf["data_io"]["storage_type"]=="GC":
        if verbose:
            print("Connecting to GCS bucket...")
        main_data_io = get_gcp_bucket(cf["data_io"]["GCS_bucket_name"])
        if main_data_io is None:
            print("Could not connect to GCS bucket. Exiting.")
            return None
    else:
        if verbose:
            print("Using local storage.")
        main_data_io = cf["data_io"]["local_storage_dir"]
    return main_data_io





def DONT_USE_list_files_in_storage(storage_location, prefix="", include_sub_prefixes=True, suffix=""):

    if isinstance(storage_location,str): # if it's a string, it's a local directory
        files_in_storage = [fn for fn in listdir(os.path.join(storage_location,prefix)) if fn.endswith(suffix)]
    else:
        if suffix != "" and not suffix.startswith("."):
            suffix = "."+suffix
        blobs = storage_location.list_blobs(prefix=prefix)
        files_in_storage = [blob.name for blob in blobs]
        files_in_storage = [fn.replace(prefix,"") for fn in files_in_storage if fn.endswith(suffix)]
        files_in_storage = [fn[1:] if fn[0]=="/" else fn for fn in files_in_storage]
        if not include_sub_prefixes:
            files_in_storage = [fn for fn in files_in_storage if "/" not in fn]
    
    return files_in_storage



def DONT_USE_save_blob_to_storage(storage_location, filename, source_dir="", prefix=""):
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(os.path.join(source_dir,filename)):
            copyfile(os.path.join(source_dir,filename), os.path.join(storage_location,prefix,filename))
        else:
            print(f"File '{filename}' not found in '{source_dir}'")
    else:
        blob = storage_location.blob(os.path.join(prefix,filename))
        blob.upload_from_filename(os.path.join(source_dir,filename))


def DONT_USE_load_blob_from_storage(storage_location, filename, prefix="", dest_dir=""):
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(os.path.join(storage_location,prefix,filename)):
            copyfile(os.path.join(storage_location,prefix,filename), os.path.join(dest_dir,filename))
        else:
            print(f"File '{filename}' not found in '{join(storage_location,prefix)}'")
    else:
        blob = storage_location.blob(os.path.join(prefix,filename))
        blob.download_to_filename(os.path.join(dest_dir,filename))"""






if __name__ == "__main__":
    print("Module is being run directly.")
