"""Small runtime accessors shared across the fyp package.

Import-light by design: nothing here imports ``fyp_config`` at module load, so
any module — including the ones the config boot itself calls into (the
import-cycle rule, CONTRIBUTING.md invariant 1) — may import this at the top.
Modules alias these under their historical private names (``_cf``,
``_collections_label``, ...), which is also what tests patch.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


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

    The sentinel is written by ``web_interface.tasks.process_manager`` (local mode).
    """
    root = Path(cf()["paths"]["project_root"])
    return (root / "tmp" / "graceful_stop" / f"{process_name}.stop").exists()


def utc_now_iso(timespec: str = "auto") -> str:
    """The current UTC time as an ISO-8601 string with a ``+00:00`` offset.

    Args:
        timespec: As for ``datetime.isoformat`` (``"seconds"`` drops the
            microseconds).
    """
    return datetime.now(UTC).isoformat(timespec=timespec)


def local_now_iso() -> str:
    """The current time in the configured ``[misc] TIME_ZONE``, to the second.

    Offset-aware (e.g. ``+10:00``); UTC when the setting is missing, invalid
    or the config cannot be read.
    """
    try:
        tz = ZoneInfo(cf().get("misc", {}).get("TIME_ZONE", "UTC"))
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz=tz).isoformat(timespec="seconds")


def code_version() -> str:
    """The running code's version: ``<package version>+<git sha>``.

    The sha comes from ``FYP_GIT_SHA`` (set in the container image at build
    time; the image carries no ``.git``), else from ``git rev-parse`` in a
    checkout, else it is omitted. Artifacts stamp this so an analysis can
    check which release built them.
    """
    import subprocess

    from fyp import __version__

    sha = os.environ.get("FYP_GIT_SHA", "").strip()
    if not sha or sha == "unknown":
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return f"{__version__}+{sha}" if sha else __version__
