"""Project-root discovery and process/script path constants.

Extracted from ``fyp_config`` in the subpackage restructure so the path layer
is stdlib-only and importable without any config machinery. Importing this
module preserves the historical import-time side effects of ``import
fyp.fyp_config``: the project root is discovered (``FYP_CONFIG_PATH`` env
override, else a ``__proj__.py`` walk from the current working directory) and
appended to ``sys.path``.

All names are re-exported by ``fyp.fyp_config`` — external code
(``web_interface/process_manager.py``, the web routes) keeps importing them
from there.
"""

import os
import sys
from pathlib import Path

# FYP_CONFIG_PATH points directly at a config TOML (normally
# <root>/config/config.toml) and derives the project root from it — this lets
# fyp be imported from outside a project tree (reuse in other projects).
# Absent the env var, behavior is unchanged: look for the folder that contains
# the __proj__.py file, which is the root folder for the project structure.
abs_project_root_path: str
_env_config_path = os.environ.get("FYP_CONFIG_PATH")
if _env_config_path:
    abs_project_root_path = str(Path(_env_config_path).resolve().parent.parent)
else:
    _cwd = Path(os.getcwd())
    _candidates = [_cwd] + list(_cwd.parents)
    for _p in _candidates:
        if (_p / "__proj__.py").exists():
            abs_project_root_path = str(_p)
            break
    else:
        raise FileNotFoundError("Could not find __proj__.py in any parent directory")
sys.path.append(abs_project_root_path)


PROJECT_ROOT = Path(abs_project_root_path)

QUEUE_SCRAPER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_scraper.py"
QUEUE_ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_annotator.py"
QUEUE_ANNOTATOR_BATCH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_queue_annotator_batch.py"
META_REFRESH_GROUPS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_meta_refresh_groups.py"
TIMELINES_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_timelines_refresh.py"
RECODE_REFRESH_STUDIES_SCRIPT = PROJECT_ROOT / "web_interface" / "run_recode_refresh_studies.py"
PCA_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_pca_refresh.py"
SEQUENCE_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_sequence_refresh.py"
SESSIONS_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_sessions_refresh.py"
EMBEDDINGS_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_embeddings_refresh.py"
VIDEO_MAP_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_video_map_refresh.py"
CONSOLIDATE_ENRICHMENT_SCRIPT = PROJECT_ROOT / "web_interface" / "run_consolidate_enrichment.py"
RETOKENISE_HASHTAGS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_retokenise_hashtags.py"
INGEST_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ingest_refresh.py"
AIO_FETCH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_aio_fetch.py"
COLLECTION_METADATA_REFRESH_SCRIPT = PROJECT_ROOT / "web_interface" / "run_collection_metadata_refresh.py"
COLLECTION_DELETE_SCRIPT = PROJECT_ROOT / "web_interface" / "run_collection_delete.py"
AB_EVAL_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ab_eval.py"
OPS_REPORT_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ops_report.py"
ENRICHMENT_SUPERVISOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_enrichment_supervisor.py"
PYTHON_EXEC = sys.executable
