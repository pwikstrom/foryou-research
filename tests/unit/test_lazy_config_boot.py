"""Regression tests pinning Phase 4's lazy config init.

Importing a migrated fyp module must NOT run the heavy fyp_config init
(``initialize`` -> ``_connect_to_google`` -> ``load_var_schema``); the init —
and its ``[BOOT]`` timing line — fires exactly once, on first access of
``fyp_cf`` (module ``__getattr__``, PEP 562).

Known, by-design exception: ``import fyp.ingest`` still triggers init because
the collection subclasses self-register their raw-upload locations at class
definition (``__init_subclass__`` -> ``data_io.register_location()``) — upload
routes depend on that happening at import time.

Each scenario needs a fresh interpreter with captured stdout, so this test
shells out.

Usage:
    python -m pytest tests/unit/test_lazy_config_boot.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )




def test_import_does_not_boot_and_first_access_boots_once():
    """`import fyp.pca` emits no [BOOT]; first fyp_cf access emits exactly one."""
    code = (
        "import sys;"
        "import fyp.pca;"
        "sys.stdout.write('===IMPORTED===');"
        "from fyp.fyp_config import fyp_cf;"
        "assert isinstance(fyp_cf, dict);"
        "import fyp.fyp_config as fc;"
        "assert fyp_cf is fc.fyp_cf is fc.get_config()"
    )
    out = _run(code)
    assert out.returncode == 0, out.stderr[-2000:]
    before, sep, after = out.stdout.partition("===IMPORTED===")
    assert sep, "marker missing from stdout"
    assert "[BOOT]" not in before, f"import alone ran the heavy init:\n{before}"
    assert after.count("[BOOT]") == 1, f"expected exactly one [BOOT]:\n{after}"




def test_app_import_boots_exactly_once():
    """The Flask app import graph still runs the heavy init exactly once."""
    out = _run("import web_interface.fyp_data_hub")
    assert out.returncode == 0, out.stderr[-2000:]
    count = out.stdout.count("[BOOT]")
    assert count == 1, f"expected exactly one [BOOT] in app boot, got {count}:\n{out.stdout}"
