import sys
import os
import pandas as pd
from pathlib import Path
from google.api_core.exceptions import Forbidden as google_Forbidden
from google.cloud import storage as gcs_storage
import http.client
import toml



# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
here = os.getcwd().split("/")
while not os.path.exists(os.path.join("/".join(here),"__proj__.py")):
    here.pop()
abs_project_root_path = os.path.join("/".join(here))
sys.path.append(abs_project_root_path)


#import fyp



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




def initialize(
    verbose: bool = False,
    abs_project_root_path: str = None
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
        sys.path.append(abs_project_root_path)

    
    # ------------------------------------------------------------------
    # Load essential config - let it blow up if the files aren't found
    # ------------------------------------------------------------------
    config_path = os.path.join(abs_project_root_path,"config","config.toml")
    cf = toml.load(config_path)
    cf["paths"]["project_root"] = abs_project_root_path


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
    # initialize paths
    # ------------------------------------------------------------------
    # Resolve relative paths against the project root for consistent file access.
    cf["paths"]["local_data"] = os.path.abspath(os.path.join(cf["paths"]["project_root"], cf["paths"]["local_data"]))


    cf["paths"]["activity_data"] = os.path.join(cf["paths"]["local_data"],"activity_data")
    cf["paths"]["processed_activities"] = os.path.join(cf["paths"]["activity_data"],"processed")

    # paths to zeeschuimer data
    cf["paths"]["zeeschuimer"] = os.path.join(cf["paths"]["activity_data"], "zeeschuimer")
    cf["paths"]["zeeschuimer_raw"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_raw")
    #cf["paths"]["zeeschuimer_refined"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_refined")
    #cf["paths"]["zeeschuimer_main"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_main")

    # paths to ddp data
    cf["paths"]["ddp"] = os.path.join(cf["paths"]["activity_data"], "ddp")
    cf["paths"]["ddp_raw"] = os.path.join(cf["paths"]["ddp"], "ddp_raw")
    #cf["paths"]["ddp_processed"] = os.path.join(cf["paths"]["ddp"], "ddp_processed")
    #cf["paths"]["ddp_main"] = os.path.join(cf["paths"]["ddp"], "ddp_main")
    cf["paths"]["ddp_participants"] = os.path.join(cf["paths"]["ddp"], "ddp_participants")

    # paths to scrape data
    cf["paths"]["scrape"] = os.path.join(cf["paths"]["local_data"], "scrape")

    # paths to machine annotations
    cf["paths"]["machine_annotations"] = os.path.join(cf["paths"]["local_data"], "machine_annotations")
    cf["paths"]["machine_annotations_raw"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_raw")
    cf["paths"]["machine_annotations_refined"] = os.path.join(cf["paths"]["machine_annotations"], "machine_annotations_refined")

    # other paths
    cf["paths"]["recoded"] = os.path.join(cf["paths"]["local_data"], "recoded")
    #cf["paths"]["prompts"] = os.path.join(cf["paths"]["local_data"], "prompts")
    cf["paths"]["archive"] = os.path.join(cf["paths"]["local_data"], "archive")
    cf["paths"]["users"] = os.path.join(cf["paths"]["local_data"], "users") 
    cf["paths"]["cache"] = os.path.join(cf["paths"]["local_data"], "cache") 
    cf["paths"]["studies"] = os.path.join(cf["paths"]["local_data"], "studies") 
    
    cf["paths"]["temp"] = "/tmp/fyp/"
    os.makedirs(cf["paths"]["temp"], exist_ok=True)
    

    # ------------------------------------------------------------------
    # prepare gen ai parameters for initialisation
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
    # prepare data storage for initialisation - either gcs or local
    # ------------------------------------------------------------------
    # This is not set by the config so I'm setting it to None
    cf["data_io"]["bucket"] = None

    # If running on Cloud Run, force all storage to GCS
    if os.environ.get("K_SERVICE"):
        print("Cloud Run detected. Forcing all storage to GCS.")
        cf['data_io']['use_gcs_for_data'] = True
        cf['data_io']['use_gcs_for_cache'] = True
        cf['data_io']['use_gcs_for_media'] = True
        cf['misc']['local_mode'] = False

    # If local mode is enabled, set the GCS flags to False
    elif cf['misc']['local_mode']:
        print("Local mode is enabled. GCS data will not be used.")
        cf['data_io']['use_gcs_for_data'] = False
        cf['data_io']['use_gcs_for_cache'] = False
        cf['data_io']['use_gcs_for_media'] = False

    if cf['data_io']['use_gcs_for_data']:
        cf["gcs_paths"] = {}
        gcs_prefix = cf["data_io"].get("gcs_data_prefix", "")
        for k, v in cf["paths"].items():
            if isinstance(v, str) and v.startswith(cf["paths"]["local_data"]) and k != "local_data":
                rel = os.path.relpath(v, cf["paths"]["local_data"])
                if rel == ".": 
                    gcs_path = gcs_prefix
                else:
                    gcs_path = f"{gcs_prefix}/{rel}" if gcs_prefix else rel            
                cf["gcs_paths"][k] = gcs_path
        
    # create missing local folders - note that this function first checks relevant flags and
    # only creates folders if needed 
    _create_local_dirs(cf, verbose=verbose)


    return cf






# check internet connectivity
def _online_ok(url="www.qut.edu.au",
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






def _connect_to_google(cf, verbose=False):

    if cf["data_io"]["bucket"] is not None:
        return cf

    cf["data_io"]["bucket"] = None

    if cf['misc']['local_mode'] or not (cf['data_io']['use_gcs_for_data'] or cf['data_io']['use_gcs_for_cache'] or cf['data_io']['use_gcs_for_media']):
        return cf

    if _online_ok():

        # Initialize a GCS storage client
        try:
            bucket_client = gcs_storage.Client()

            # Get the GCS bucket
            bucket = bucket_client.get_bucket(cf["data_io"]["GCS_bucket_name"])

            # Try to access the GCS bucket's metadata
            bucket.reload()
            cf["data_io"]["bucket"] = bucket
            print(f"Access to GCS bucket '{bucket.name}' ({bucket.location}) is authorized.")
            if verbose:
                if cf['data_io']['use_gcs_for_data']:
                    print(f"Data is stored in GCS")
                else:
                    print(f"Data is stored locally")
                if cf['data_io']['use_gcs_for_cache']:
                    print(f"Cache is stored in GCS")
                else:
                    print(f"Cache is stored locally")
                if cf['data_io']['use_gcs_for_media']:
                    print(f"Media is stored in GCS")
                else:
                    print(f"Media is stored locally")

            return cf
        
        except google_Forbidden:
            print(f"I don't have access to the GCS.")
        except Exception as e:
            print(f"A GCS error occurred: {e}")

    else:
        print("No internet connection. Running local mode.")
        cf['misc']['local_mode'] = True
    

    cf['data_io']['use_gcs_for_data'] = False
    cf['data_io']['use_gcs_for_cache'] = False
    cf['data_io']['use_gcs_for_media'] = False
    _create_local_dirs(cf, verbose=verbose)
    return cf







def load_var_schema(cf, verbose=False):
    # Load variable schema
    if cf['data_io']['use_gcs_for_data']:
        if verbose:
            print(f"Loading variable schema from GCS", end="", flush=True)
        var_schema_path = f"gs://{cf['data_io']['GCS_bucket_name']}/data/var_schema.csv"
    else:
        if verbose:
            print(f"Loading variable schema from local disk", end="", flush=True)
        var_schema_path = os.path.join(cf['paths']['local_data'], "var_schema.csv")
    try:
        cf["var_schema"] = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
        if verbose:
            print(f" - OK. Shape: {cf['var_schema'].shape}")
    except Exception as e:
        print(f"\nCRITICAL ERROR: Failed to load variable schema: {e}")
    return cf








PROJECT_ROOT = Path(abs_project_root_path)


# Initialize things
fyp_cf = initialize()
fyp_cf = _connect_to_google(fyp_cf, verbose = True)
fyp_cf = load_var_schema(fyp_cf, verbose=True)


QUEUE_SCRAPER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_scraper.py"
QUEUE_ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_annotator.py"
META_REFRESH_VIEWER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_viewer.py"
META_REFRESH_GROUPS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_groups.py"
TIMELINES_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_timelines_refresh.py"
PROCESS_STATS_FILE = PROJECT_ROOT / "web_interface" / "process_stats.json"
PYTHON_EXEC = sys.executable
