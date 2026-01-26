# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""

import os
import toml
import pandas as pd
import sys


############################################################################################################
###                     Initialize things
############################################################################################################





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
        sys.path.append(abs_project_root_path)


    
    # ------------------------------------------------------------------
    # Load essential files - let it blow up if the files aren't found
    # ------------------------------------------------------------------
    where_to_start = toml.load(os.path.join(abs_project_root_path,"config","core.toml"))

    config_path = os.path.join(abs_project_root_path,"config",where_to_start["core"]["config_fn"])

    var_schema_path = os.path.join(abs_project_root_path, "config", where_to_start["core"]["var_schema_fn"])

    # Load main config
    cf = toml.load(config_path)
    cf["paths"]["project_root"] = abs_project_root_path

    # Load variable schema
    cf["var_schema"] = pd.read_csv(var_schema_path, dtype_backend="pyarrow")







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
    # initialize paths
    # ------------------------------------------------------------------
    # Resolve relative paths against the project root for consistent file access.
    cf["paths"]["local_data"] = os.path.abspath(os.path.join(cf["paths"]["project_root"], cf["paths"]["local_data"]))

    # paths to zeeschuimer data
    cf["paths"]["zeeschuimer"] = os.path.join(cf["paths"]["local_data"],"activity_data", "zeeschuimer")
    cf["paths"]["zeeschuimer_raw"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_raw")
    cf["paths"]["zeeschuimer_refined"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_refined")
    #cf["paths"]["zeeschuimer_main"] = os.path.join(cf["paths"]["zeeschuimer"], "zeeschuimer_main")

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
    #cf["paths"]["exports"] = os.path.join(cf["paths"]["local_data"], "exports")
    cf["paths"]["archive"] = os.path.join(cf["paths"]["local_data"], "archive")
    cf["paths"]["users"] = os.path.join(cf["paths"]["local_data"], "users") 
    cf["paths"]["cache"] = os.path.join(cf["paths"]["local_data"], "cache") 
    cf["paths"]["studies"] = os.path.join(cf["paths"]["local_data"], "studies") 

 
    
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

    
    # Load study definitions using data_io
    # This must be done after GCS setup so data_io works correctly if using GCS




    return cf













if __name__ == "__main__":
    print("Module is being run directly.")
