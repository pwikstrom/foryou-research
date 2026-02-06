import sys
import os
import pandas as pd
from pathlib import Path
from google.api_core.exceptions import Forbidden as google_Forbidden
from google.cloud import storage as gcs_storage
import http.client



# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
here = os.getcwd().split("/")
while not os.path.exists(os.path.join("/".join(here),"__proj__.py")):
    here.pop()
abs_project_root_path = os.path.join("/".join(here))
sys.path.append(abs_project_root_path)


import fyp
#import fyp.data_io as data_io




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
        print("No internet connection. Running local mode without connecting to Google services.")
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
            print(f"Loading variable schema from GCS")
        var_schema_path = f"gs://{cf['data_io']['GCS_bucket_name']}/data/var_schema.csv"
    else:
        if verbose:
            print(f"Loading variable schema from local disk")
        var_schema_path = os.path.join(cf['paths']['local_data'], "var_schema.csv")
    try:
        cf["var_schema"] = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
        if verbose:
            print(f"Variable schema loaded. Shape: {cf['var_schema'].shape}")
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to load variable schema: {e}")
    return cf








PROJECT_ROOT = Path(abs_project_root_path)


# Initialize things
fyp_cf = fyp.initialize()
fyp_cf = _connect_to_google(fyp_cf, verbose = True)
fyp_cf = load_var_schema(fyp_cf, verbose=True)


DOWNLOADER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_downloader.py"
INGEST_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ingest_ndjson.py"
ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_annotator.py"
MONITOR_SCRIPT = PROJECT_ROOT / "web_interface" / "monitor_scrape_folder_and_annotate.py"
CREATE_SUBSETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_subsets.py"
REGENERATE_DATASETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_regenerate_datasets.py"
CREATE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_event_log.py"
RECODE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_recode_event_log.py"
CALCULATE_PCA_SCRIPT = PROJECT_ROOT / "web_interface" / "run_calculate_pca.py"
QUEUE_SCRAPER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_scraper.py"
META_REFRESH_VIEWER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_viewer.py"
META_REFRESH_GROUPS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_groups.py"
TIMELINES_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_timelines_refresh.py"
PROCESS_STATS_FILE = PROJECT_ROOT / "web_interface" / "process_stats.json"
PYTHON_EXEC = sys.executable
