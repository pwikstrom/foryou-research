"""Project-root discovery and the ``PROJECT_ROOT`` / ``PYTHON_EXEC`` constants.

Extracted from ``fyp_config`` in the subpackage restructure so the path layer
is stdlib-only and importable without any config machinery. Importing this
module preserves the historical import-time side effects of ``import
fyp.core.fyp_config``: the project root is discovered (``FYP_CONFIG_PATH`` env
override, else a ``__proj__.py`` walk from the current working directory) and
appended to ``sys.path``.

All names are re-exported by ``fyp.core.fyp_config``. The web app's worker script
paths are not here: the library does not know the app's file layout; they live
in ``web_interface/worker_registry.py``.
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

PYTHON_EXEC = sys.executable
