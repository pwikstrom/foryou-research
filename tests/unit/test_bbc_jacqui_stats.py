import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pandas as pd

from fyp import data_io
from fyp.fyp_config import fyp_cf, initialize
from fyp.studies import init_study_defs
from web_interface.routes.management_routes import _calculate_stats

# initialize config
initialize()
init_study_defs()

print("--- Testing BBC_Jacqui Stats after fix ---")
study_name = "BBC_Jacqui"

if 'study_defs' not in fyp_cf or study_name not in fyp_cf['study_defs']:
    print(f"Study {study_name} not found in config.")
    sys.exit(1)

config = fyp_cf['study_defs'][study_name]
config['STUDY_NAME'] = study_name
stats, _ = _calculate_stats(config, save_to_cache=False)
print(f"Stats from _calculate_stats: {stats}")

if stats['scraped_videos'] == 0 or stats['annotated_videos'] == 0:
    print("TEST FAILED: Stats are still zero!")
    sys.exit(1)
else:
    print("TEST PASSED: Stats are non-zero!")
    sys.exit(0)
