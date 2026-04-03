import inspect
import sys
import os
import importlib.util
from unittest.mock import MagicMock

# Mock fyp package and submodules to bypass fyp/__init__.py and fyp_main.py
sys.modules['fyp'] = MagicMock()
sys.modules['fyp.fyp_main'] = MagicMock()
sys.modules['fyp.machine_annotation'] = MagicMock()
sys.modules['google'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['google.cloud'] = MagicMock()
sys.modules['pandas'] = MagicMock()
sys.modules['numpy'] = MagicMock()
sys.modules['zoneinfo'] = MagicMock()

# We need pandas and numpy to be available for the module to load, 
# but since we are just checking signatures, mocks might be enough IF the module doesn't use them at top level in a way that fails.
# Actually, the module imports pandas and numpy. Mocks should be fine as long as we don't call functions.
# But wait, `session_id_counter = np_int64(0)` in default args.
# So `numpy.int64` must exist.
import numpy as np
sys.modules['numpy'] = np # Use real numpy if available, or mock if not. 
# If real numpy is not available, we can mock it.
try:
    import numpy as real_np
    sys.modules['numpy'] = real_np
except ImportError:
    sys.modules['numpy'] = MagicMock()
    sys.modules['numpy'].int64 = int # Mock int64 as int

# Same for pandas if needed, but the module uses `from pandas import ...` inside functions mostly.
# Top level: `import pandas as pd`
try:
    import pandas as real_pd
    sys.modules['pandas'] = real_pd
except ImportError:
    sys.modules['pandas'] = MagicMock()

# zoneinfo
try:
    import zoneinfo as real_zi
    sys.modules['zoneinfo'] = real_zi
except ImportError:
    sys.modules['zoneinfo'] = MagicMock()


file_path = '/Users/<user>/GitHub_main/fyp_main_v02/fyp/organize_datasets.py'
module_name = "organize_datasets"

try:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    od = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = od
    spec.loader.exec_module(od)
    print("Module loaded successfully.")
except Exception as e:
    print(f"Error loading module: {e}")
    sys.exit(1)

functions_to_check = [
    "extract_local_time_features",
    "load_scrape_metadata",
    "load_failed_scrapes",
    "load_zeeschuimer_data",
    "sample_ddp_events",
    "load_ddp_events",
    "load_special_s",
    "load_study_datasets",
    "identify_unique_videos",
    "calculate_all_unique_video_subsets",
    "save_selected_unique_video_subsets",
    "_check_for_null_values_in_df",
    "process_baseline_for_log_export",
    "add_session_info_to_ddp_log",
    "process_ddp_log_for_log_export",
    "process_scrape_metadata_for_log_export",
    "process_machine_annotations_for_log_export",
    "process_and_combine_logs_for_log_export",
    "merge_all_study_datasets",
    "filter_log_against_sampled__groups",
    "save_logs",
    "save_logs_as_csv"
]

all_passed = True
for func_name in functions_to_check:
    if hasattr(od, func_name):
        func = getattr(od, func_name)
        sig = inspect.signature(func)
        if 'verbose' in sig.parameters:
            default_val = sig.parameters['verbose'].default
            if default_val is False:
                print(f"[PASS] {func_name} has verbose=False")
            else:
                print(f"[FAIL] {func_name} has verbose={default_val}, expected False")
                all_passed = False
        else:
            print(f"[FAIL] {func_name} does not have 'verbose' parameter")
            all_passed = False
    else:
        print(f"[FAIL] {func_name} not found in module")
        all_passed = False

if all_passed:
    print("\nAll functions have the correct verbose signature.")
else:
    print("\nSome functions failed verification.")
