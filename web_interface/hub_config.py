import sys
from pathlib import Path
import os

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT)) # Ensure fyp module is importable

import fyp
import fyp.data_io as data_io

# Initialize configuration to access paths
fyp_cf = fyp.initialize(verbose=False)
if fyp_cf['data_io']['use_gcs_for_data']:
    fyp_cf = fyp.connect_to_google(fyp_cf)

DOWNLOADER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_downloader.py"
INGEST_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ingest_ndjson.py"
ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_annotator.py"
MONITOR_SCRIPT = PROJECT_ROOT / "web_interface" / "monitor_scrape_folder_and_annotate.py"
CREATE_SUBSETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_subsets.py"
REGENERATE_DATASETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_regenerate_datasets.py"
CREATE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_event_log.py"
RECODE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_recode_event_log.py"
CALCULATE_PCA_SCRIPT = PROJECT_ROOT / "web_interface" / "run_calculate_pca.py"
CONFIG_FILE_STUDIES = PROJECT_ROOT / "config" / "studies.toml"
CONFIG_FILE_CORE = PROJECT_ROOT / "config" / "config.toml"
PROCESS_STATS_FILE = PROJECT_ROOT / "web_interface" / "process_stats.json"
PYTHON_EXEC = sys.executable
