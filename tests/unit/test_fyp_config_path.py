"""Regression test: FYP_CONFIG_PATH makes fyp importable outside a project root.

Phase 4 (lazy config init) added additive ``FYP_CONFIG_PATH`` support: when
set, ``fyp.fyp_config`` derives the project root from the named config TOML
instead of walking up the cwd looking for ``__proj__.py``. This unblocks
reusing ``fyp/`` from another project (the Phase 3 reuse-blocker): before the
change, ``import fyp.fyp_config`` from an unrelated cwd died in the module-top
root-discovery block with FileNotFoundError.

Each scenario needs a fresh interpreter and a controlled cwd/env, so this
test shells out.

Usage:
    python -m pytest tests/unit/test_fyp_config_path.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, cwd: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin", **extra_env}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=cwd, env=env,
    )




def test_import_outside_project_root_with_env():
    """With FYP_CONFIG_PATH set, import succeeds from an unrelated cwd."""
    code = (
        "import fyp.fyp_config as fc;"
        "from pathlib import Path;"
        f"assert fc.PROJECT_ROOT == Path({str(ROOT)!r}).resolve(), fc.PROJECT_ROOT;"
        "print('IMPORT_OK')"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = _run(
            code, cwd=Path(tmp),
            extra_env={"FYP_CONFIG_PATH": str(ROOT / "config" / "config.toml")},
        )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "IMPORT_OK" in out.stdout




def test_import_outside_project_root_without_env_still_fails():
    """Absent the env var, the __proj__.py discovery (and its error) is unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _run("import fyp.fyp_config", cwd=Path(tmp), extra_env={})
    assert out.returncode != 0
    assert "__proj__.py" in out.stderr
