"""Small runtime accessors shared across the fyp package.

Import-light by design: nothing here imports ``fyp_config`` at module load, so
any module — including the ones the config boot itself calls into (the
import-cycle rule, CONTRIBUTING.md invariant 1) — may import this at the top.
Modules alias these under their historical private names (``_cf``,
``_collections_label``, ...), which is also what tests patch.
"""

import os
from pathlib import Path


def cf() -> dict:
    """The live config dict (``fyp_config.fyp_cf``), resolved at call time.

    Deliberately not cached: the config boots lazily on first access, and
    tests swap ``fyp_config.fyp_cf`` out from under running code.
    """
    from fyp.core.fyp_config import fyp_cf

    return fyp_cf


def label(name: str) -> str:
    """A storage label from ``[labels]`` (e.g. ``"COLLECTIONS_LABEL"``)."""
    return cf()["labels"][name]


def is_cloud_run() -> bool:
    """True when running as a Cloud Run service (``K_SERVICE`` is set)."""
    return bool(os.environ.get("K_SERVICE"))


def graceful_stop_requested(process_name: str) -> bool:
    """True if the web app has asked worker ``process_name`` to stop gracefully.

    The sentinel is written by ``web_interface.process_manager`` (local mode).
    """
    root = Path(cf()["paths"]["project_root"])
    return (root / "tmp" / "graceful_stop" / f"{process_name}.stop").exists()
