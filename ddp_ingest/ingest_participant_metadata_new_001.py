
import random
import pandas as pd
from os import listdir
from os import getcwd
from os.path import join, exists, getmtime
from sys import path as sys_path
import json
import numpy as np

from datetime import datetime
from scipy.stats import chisquare


# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
here = getcwd().split("/")
while not exists(join("/".join(here),"__proj__.py")):
    here.pop()
abs_project_root_path = join("/".join(here))

# add project root path to PATH since the modules are located in the project structure
sys_path.append(abs_project_root_path)

import fyp



from fyp.fyp_main import init_config
cf = init_config()

# # Function definitions


def _deser(value):
    # Convert DynamoDB JSON value → native Python.
    if "S" in value:          # string
        return value["S"]
    if "N" in value:          # number
        num = value["N"]
        return int(num) if num.isdigit() else float(num)
    if "BOOL" in value:       # boolean
        return bool(value["BOOL"])
    if "NULL" in value:       # explicit null
        return None
    if "L" in value:          # list
        return [_deser(v) for v in value["L"]]
    if "M" in value:          # map
        return {k: _deser(v) for k, v in value["M"].items()}
    # Anything else is kept verbatim
    return value

# # Ingest participant metadata


print("Downloading recent participant metadata")
fyp.download_recent_metadata(hours_back=240000,output_dir=cf["paths"]["ddp_participants"])



print("Checking all participant metadata files ")
participant_metadata = {}
for participant_data_file in listdir(cf["paths"]["ddp_participants"]):
    if participant_data_file.endswith(".json"):
        participant_data_path = join(cf["paths"]["ddp_participants"], participant_data_file)
        with open(participant_data_path, "r") as f:
            participant_metadata_raw = json.load(f)
            print(f"P {len(participant_metadata_raw['Items'])} items in the file {participant_data_file}")
            for item in participant_metadata_raw.get("Items", []):
                py_item = {k: _deser(v) for k, v in item.items()}
                participant_metadata[py_item['id']] = py_item

participant_metadata_df = pd.DataFrame(participant_metadata).T

print("---------------------------------------------------------------------------------------------")


print("Loading all DDP events")
all_participant_events_df = pd.read_parquet(join(cf["paths"]["ddp_main"], "all_participant_events.parquet"), engine="pyarrow", dtype_backend="pyarrow")

print("---------------------------------------------------------------------------------------------")


print("Transforming participant metadata")
donation_stats = all_participant_events_df.groupby('donation_id').agg(n_donated_events=pd.NamedAgg(column='timestamp', aggfunc='count'))
participant_metadata_df_2 = pd.merge(donation_stats, participant_metadata_df, left_on='donation_id', right_index=True, how='left')

participant_metadata_df_3 = participant_metadata_df_2.drop(["donationType","url","iat","pk","id","exp","profile","schemaChanged","appliedSchema"],axis=1).copy()
participant_metadata_df_3.reset_index(inplace=True)
participant_metadata_df_3["date"] = participant_metadata_df_3["date"].map(lambda x: x.strftime("%Y-%m-%d %H:%M:%S") if isinstance(x, pd.Timestamp) else x)


dddd = participant_metadata_df_3.convert_dtypes(dtype_backend="pyarrow")
for col in dddd.select_dtypes(include=['object']).columns:
    dddd[col] = fyp.fix_complex_types(dddd[col].copy()).convert_dtypes(dtype_backend='pyarrow')

dddd.to_parquet(join(cf["paths"]["ddp_main"], "all_participant_metadata.parquet"), engine="pyarrow")
print("Saved participant metadata")
print("---------------------------------------------------------------------------------------------")



def fix_one_age(x):
    from re import findall
    from numpy import mean as np_mean

    if x is None:
        return None
    list_of_things = [int(n) for n in findall(r'\d+', str(x))]
    if len(list_of_things) == 0:
            return None
    return float(np_mean(list_of_things))




def fix_age(list_of_things):
    from math import fraction
    outout = []
    for thing in list_of_things:
        if thing == "":
            outout.append(None)
        else:
            outout.append(fix_one_age(thing))
    
    return outout










