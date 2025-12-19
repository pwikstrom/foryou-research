# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

from typing import Iterable, List


############################################################################################################
###                     Initialize project
############################################################################################################

def create_dirs(this_cf: dict, clear_temp_dir: bool = False) -> None:
    from os import makedirs
    from os.path import join
    from os import listdir, remove

    for k in ["main", "zeeschuimer_raw", "zeeschuimer_refined", "ddp", "temp", "backup", "scrape", "exports"]:
        makedirs(this_cf["paths"][k], exist_ok=True)

    if clear_temp_dir:
        for fn in listdir(temp_path(this_cf)):
            remove(join(temp_path(this_cf),fn))





def init_config(
    verbose=False,
    abs_project_root_path=None
    ) -> dict:

    from os import environ
    from os.path import join, abspath
    import toml

    if abs_project_root_path is None:

        from os import getcwd
        from os.path import exists
        from sys import path as sys_path

        here = getcwd().split("/")
        while not exists(join("/".join(here),"__proj__.py")):
            here.pop()

        # this is the root folder for the project structure
        abs_project_root_path = join("/".join(here))
        if verbose:
            print("Project root:",abs_project_root_path)

        # add project root path to PATH since the modules are located in the project structure
        sys_path.append(abs_project_root_path)


    where_to_start = toml.load(join(abs_project_root_path,"config","core.toml"))
    config_path = join(abs_project_root_path,"config",where_to_start["core"]["config_fn"])
    study_defs_path = join(abs_project_root_path,"config",where_to_start["core"]["study_defs_fn"])
    

    cf = toml.load(config_path)
    # Prefer env var for secrets; fall back to file if present (avoid committing real keys)
    gcp_bucket_name = environ.get("FYP_GCP_BUCKET_NAME")
    if gcp_bucket_name:
        cf["media_storage"]["GCP_bucket"] = gcp_bucket_name

    # Prefer env var for secrets; fall back to file if present (avoid committing real keys)
    gemini_env_key = environ.get("GEMINI_API_KEY")
    if gemini_env_key:
        cf["machine"]["key"] = gemini_env_key

    # Load variable scheme
    try:
        import pandas as pd
        from os.path import exists
        var_scheme_path = join(abs_project_root_path, "config", "var_scheme.csv")
        if exists(var_scheme_path):
             # Need to ensure exists is imported or just try/except
             cf["var_scheme"] = pd.read_csv(var_scheme_path)
        else:
             print(f"Warning: var_scheme.csv not found at {var_scheme_path}")
             cf["var_scheme"] = pd.DataFrame()
    except Exception as e:
        if verbose:
            print(f"Failed to load var_scheme.csv: {e}")
        cf["var_scheme"] = pd.DataFrame()

    study_defs = toml.load(study_defs_path)
    for study_name in study_defs.keys():
        study_defs[study_name]["STUDY_NAME"] = study_name
    cf["study_defs"] = study_defs

    cf["paths"]["project_root"] = abs_project_root_path

    cf["machine"]["client"] = None
    cf["machine"]["global_generation_config"] = None
    cf["media_storage"]["bucket"] = None


    cf["paths"]["main"] = abspath(join(abs_project_root_path, cf["paths"]["main"]))
    cf["paths"]["main_no_sync"] = abspath(join(abs_project_root_path, cf["paths"]["main_not_gdrive_synced"]))

    for p in cf["machine"].keys():
        if "prompt" in p:
            cf["machine"][p] = join(cf["paths"]["project_root"],"prompts",cf["machine"][p])


    if verbose:
        print(f"Initialising with main data directory: {cf['paths']['main']}")

    # paths to folders
    cf["paths"]["temp"] = join(cf["paths"]["main_no_sync"], "temp")
    cf["paths"]["backup"] = join(cf["paths"]["main"], "backup")
    cf["paths"]["ddp"] = join(cf["paths"]["main"],"activity_data", "participant_logs")
    cf["paths"]["zeeschuimer_raw"] = join(cf["paths"]["main"],"activity_data", "zeeschuimer_raw")
    cf["paths"]["zeeschuimer_refined"] = join(cf["paths"]["main"],"activity_data", "zeeschuimer_refined")
    cf["paths"]["scrape"] = join(cf["paths"]["main"], "scrape")
    cf["paths"]["ddp_raw"] = join(cf["paths"]["ddp"], "raw")
    cf["paths"]["ddp_processed"] = join(cf["paths"]["ddp"], "processed")
    cf["paths"]["ddp_main"] = join(cf["paths"]["ddp"], "main")
    cf["paths"]["ddp_participants"] = join(cf["paths"]["ddp"], "main", "participants_raw")
    cf["paths"]["machine_annotations"] = join(cf["paths"]["main"], "machine_annotations", "new_gen")
    cf["paths"]["exports"] = join(cf["paths"]["main"], "exports")

    return cf




def connect_to_google(cf_in):

    from copy import copy

    from google import genai
    from google.genai import types
    from google.api_core.exceptions import Forbidden
    from google.cloud import storage
    import http.client as httplib

    cf = copy(cf_in)


    # function to check internet connectivity
    def _checkInternetHttplib(url="www.qut.edu.au",
                            timeout=3):
        connection = httplib.HTTPConnection(url,
                                            timeout=timeout)
        try:
            # only header requested for fast operation
            connection.request("HEAD", "/")
            connection.close()  # connection closed
            print("Internet On")
            return True
        except Exception as exep:
            print(exep)
            return False


    if not _checkInternetHttplib():
        print("No internet connection. Running local mode without connecting to Google services.")
        return cf

    try:
        with open(cf['machine']['new_prompt'], 'r') as file:
            machine_new_prompt = file.read()

        cf["machine"]["client"] = genai.Client(
            vertexai=cf["machine"]["vertexai"],
            project=cf["machine"]["project"],
            location=cf["machine"]["location"],
            http_options=types.HttpOptions(
                api_version=cf["machine"]["http_options_api_version"],
                timeout=cf["machine"]["http_options_timeout"]
            )
        )

        cf["machine"]["global_generation_config"] = types.GenerateContentConfig(
            system_instruction=machine_new_prompt,
            temperature=cf["machine"]["temperature"],
            max_output_tokens=cf["machine"]["max_output_tokens"],
            response_mime_type=cf["machine"]["response_mime_type"],
            presence_penalty=cf["machine"]["presence_penalty"],
            frequency_penalty=cf["machine"]["frequency_penalty"],
            thinking_config=types.ThinkingConfig(thinking_budget=cf["machine"]["thinking_budget"]),
        )

        print("Google Gemini initialized successfully")

    except:
        print("Error Gemini API key. Gemini won't be available.")


    # Initialize a GCP storage client
    try:
        bucket_client = storage.Client()

        # Get the GCP bucket
        bucket = bucket_client.get_bucket(cf["media_storage"]["GCP_bucket"])

        # Try to access the GCP bucket's metadata
        bucket.reload()
        cf["media_storage"]["bucket"] = bucket
        print(f"Access to the project Google Cloud Storage bucket is authorized.")
    except Forbidden:
        print(f"You don't have access to the project Google Cloud Storage bucket.")
    except Exception as e:
        print(f"A Google Cloud Storage error occurred: {e}")
    
    return cf







def init_project(clear_temp_dir=False, verbose=False) -> dict:

    if verbose:
        print("\n\nInitializing...\n\n")

    cf = init_config(verbose=verbose, abs_project_root_path=None)
    create_dirs(cf, clear_temp_dir)

    return cf
        









############################################################################################################
###                     File, directory mgmt
############################################################################################################




def temp_path(cf: dict, filename: str = "") -> str:
    #import toml
    from os.path import join

    #cf = toml.load(CONFIG_PATH)
    temp_dir = join(cf["paths"]["main_not_gdrive_synced"],"temp")
    return join(temp_dir, filename)




def get_recent_files(directory, suffix=None, how_recent=10):
    from os import listdir
    from os.path import isfile, join, getmtime, getctime
    from datetime import datetime, timedelta

    current_time = datetime.now()
    recent_files = []

    for filename in listdir(directory):
        file_path = join(directory, filename)
        if isfile(file_path) and (suffix is None or file_path.endswith(suffix)):
            modified_time = datetime.fromtimestamp(getmtime(file_path))
            created_time = datetime.fromtimestamp(getctime(file_path))
            time_difference = current_time - max(modified_time, created_time)
            if time_difference < timedelta(minutes=how_recent):
                recent_files.append({"filename":file_path, "mtime":modified_time, "ctime":created_time})

    return sorted(recent_files,key=lambda x: x["mtime"], reverse=True)



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
            
            # Read pickle to get shape. 
            # Note: Reading large pickles might be slow. Optimization: read only metadata if possible?
            # Standard pandas read_pickle loads whole object.
            df = pd.read_pickle(file_path)
            
            rows, cols = df.shape if hasattr(df, "shape") else (len(df), "N/A")
            if "item_id" in df.columns:
                nunique_items = df["item_id"].nunique()
            else:
                nunique_items = "N/A"
            
            details.append({
                "filename": fn,
                "rows": rows,
                "cols": cols,
                "nunique_items": nunique_items,
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





############################################################################################################
###                     Utilities
############################################################################################################

def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def sort_by_similarity(reference: str, candidates: Iterable[str]) -> List[str]:
    """
    Return the candidates sorted from most to least similar to the reference string.
    Similarity is measured via difflib.SequenceMatcher ratio (0.0–1.0).
    """
    from difflib import SequenceMatcher

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
    from numpy import nan as np_nan
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
        return np_nan




def clean_url(the_url: str) -> dict:
    from urllib.parse import unquote
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
###                     manage media storage / GCP bucket
############################################################################################################

def DONT_USE_get_gcp_bucket(bucket_name, verbose = False):
    from google.api_core.exceptions import Forbidden
    from google.cloud import storage

    try:
        # Initialize a client
        client = storage.Client()

        # Get the bucket
        bucket = client.get_bucket(bucket_name)

        # Try to access the bucket's metadata
        bucket.reload()
        if verbose:
            print(f"Access to the bucket '{bucket_name}' is authorized.")
        return bucket
    except Forbidden:
        if verbose:
            print(f"You don't have access to the bucket '{bucket_name}'.")
        return None
    except Exception as e:
        if verbose:
            print(f"An error occurred: {e}")
        return None



def DONT_USE_init_media_storage(verbose=False):
    from os.path import join

    #cf = init_config()

    if cf["media_storage"]["storage_type"]=="GCP":
        if verbose:
            print("Connecting to GCP bucket...")
        main_media_storage = get_gcp_bucket(cf["media_storage"]["GCP_bucket"])
        if main_media_storage is None:
            print("Could not connect to GCP bucket. Exiting.")
            return None
    else:
        if verbose:
            print("Using local storage.")
        main_media_storage = cf["media_storage"]["local_storage_dir"]
    return main_media_storage





def DONT_USE_list_files_in_storage(storage_location, prefix="", include_sub_prefixes=True, suffix=""):
    from os.path import join
    from os import listdir

    if isinstance(storage_location,str): # if it's a string, it's a local directory
        files_in_storage = [fn for fn in listdir(join(storage_location,prefix)) if fn.endswith(suffix)]
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
    from os.path import join, exists
    from shutil import copyfile
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(join(source_dir,filename)):
            copyfile(join(source_dir,filename), join(storage_location,prefix,filename))
        else:
            print(f"File '{filename}' not found in '{source_dir}'")
    else:
        blob = storage_location.blob(join(prefix,filename))
        blob.upload_from_filename(join(source_dir,filename))


def DONT_USE_load_blob_from_storage(storage_location, filename, prefix="", dest_dir=""):
    from os.path import join, exists
    from shutil import copyfile
    if isinstance(storage_location,str): # if it's a string, it's a local directory
        if exists(join(storage_location,prefix,filename)):
            copyfile(join(storage_location,prefix,filename), join(dest_dir,filename))
        else:
            print(f"File '{filename}' not found in '{join(storage_location,prefix)}'")
    else:
        blob = storage_location.blob(join(prefix,filename))
        blob.download_to_filename(join(dest_dir,filename))






if __name__ == "__main__":
    print("Module is being run directly.")
