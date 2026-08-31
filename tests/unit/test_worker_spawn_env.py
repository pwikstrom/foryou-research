"""Regression test: a spawned worker inherits its parent's store, never its own.

``process_manager.start_process`` launches the ``run_*.py`` workers with
``subprocess.Popen``. The child used to receive only ``WEB_INTERFACE=true``,
which left it to rediscover everything else for itself:

* ``fyp.core.paths`` walks up from the working directory looking for
  ``__proj__.py``, and ``fyp_config.initialize()`` walks again, independently.
  Whichever sentinel those walks land on decides which ``config.toml`` — and
  which gitignored ``config.local.toml`` overlay — the child loads, and the
  overlay is what points this machine at production GCS.
* ``import fyp`` is answered by whichever finder replies first. The editable
  venv install resolves it to the checkout pip was pointed at, which need not
  be the checkout the server is running from; the worker scripts only
  ``sys.path.append`` their own root, so they cannot pre-empt it.

On 2026-08-28 workers spawned during a local end-to-end test from a worktree
read and pruned the production scrape queue while the server itself was on the
local store. ``worker_env`` closes both routes by pinning ``FYP_CONFIG_PATH``
and the project root on ``PYTHONPATH``.

Usage:
    python -m pytest tests/unit/test_worker_spawn_env.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from fyp.fyp_config import PROJECT_ROOT, active_config_path
from web_interface import process_manager

ROOT = Path(PROJECT_ROOT).resolve()

# Printed by the probe below; keeps the assertions off any incidental stdout
# the interpreter or an import-time banner may emit.
_PROBE = (
    "import fyp.core.paths as p, fyp;"
    "print('ROOT=' + p.abs_project_root_path);"
    "print('FYP=' + fyp.__file__)"
)


def _probe(cwd: Path, env: dict) -> dict[str, str]:
    """Run the probe in a child interpreter and return its reported paths."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, cwd=cwd, env=env,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )




def test_worker_env_pins_the_parents_config_file(monkeypatch):
    """The child is told which config TOML to load instead of discovering one.

    The pin has to be computed, not merely inherited: a server started without
    ``FYP_CONFIG_PATH`` — the ordinary case — is exactly the one whose children
    were left to walk for a sentinel of their own.
    """
    monkeypatch.delenv("FYP_CONFIG_PATH", raising=False)

    env = process_manager.worker_env()

    assert env["FYP_CONFIG_PATH"] == active_config_path()
    assert Path(env["FYP_CONFIG_PATH"]).is_file()
    assert env["WEB_INTERFACE"] == "true"




def test_worker_env_puts_the_project_root_first_on_pythonpath():
    """``import fyp`` in the child resolves to this process's checkout."""
    entries = process_manager.worker_env()["PYTHONPATH"].split(os.pathsep)

    assert entries[0] == str(ROOT)




def test_worker_env_keeps_an_inherited_pythonpath_after_the_pins(monkeypatch):
    """Pinning prepends; it never drops what the operator put on the path."""
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(["/somewhere/else", str(ROOT)]))

    entries = process_manager.worker_env()["PYTHONPATH"].split(os.pathsep)

    assert entries[0] == str(ROOT)
    assert entries.count(str(ROOT)) == 1
    assert "/somewhere/else" in entries




@pytest.fixture
def restore_process_slot():
    """Snapshot and restore one entry of the module-level ``processes`` dict."""
    saved: dict = {}

    def _claim(name: str) -> str:
        saved[name] = dict(process_manager.processes[name])
        return name

    yield _claim
    for name, state in saved.items():
        process_manager.processes[name] = state




def test_start_process_spawns_the_worker_with_the_pinned_env(
        monkeypatch, restore_process_slot):
    """The env actually handed to Popen carries the config and import pins."""
    name = restore_process_slot("queue_scraper_tiktok")
    captured: dict = {}

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("")

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.delenv("FYP_CONFIG_PATH", raising=False)
    monkeypatch.setattr(process_manager, "is_cloud_run", lambda: False)
    monkeypatch.setattr(process_manager, "_drain_lease_conflict", lambda n: None)
    monkeypatch.setattr(process_manager.run_logs, "open_run",
                        lambda *a, **k: None)
    # The two worker threads are looked up as module globals at spawn time, so
    # replacing them here keeps the fake process from reaching the completion
    # path (which closes run logs and can chain further work).
    monkeypatch.setattr(process_manager, "enqueue_output", lambda *a, **k: None)
    monkeypatch.setattr(process_manager, "monitor_process_completion",
                        lambda *a, **k: None)
    monkeypatch.setattr(process_manager.subprocess, "Popen", _fake_popen)

    ok, msg = process_manager.start_process(
        name, ROOT / "web_interface" / "run_queue_scraper.py",
        args=["--platform", "tiktok"])

    assert ok, msg
    env = captured["kwargs"]["env"]
    assert env["FYP_CONFIG_PATH"] == active_config_path()
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ROOT)
    assert captured["kwargs"]["cwd"] == str(PROJECT_ROOT)




def test_a_sentinel_in_the_working_directory_hijacks_an_unpinned_child():
    """Control: without the pins the child adopts whatever root it walks into.

    This is the hazard itself — the child runs the parent's code but resolves a
    different project root, hence a different config and a different store.
    """
    with tempfile.TemporaryDirectory() as tmp:
        decoy = Path(tmp).resolve()
        (decoy / "__proj__.py").touch()

        reported = _probe(decoy, {"PATH": "/usr/bin:/bin",
                                  "PYTHONPATH": str(ROOT)})

    assert reported["ROOT"] == str(decoy)




def test_worker_env_beats_a_sentinel_in_the_working_directory(monkeypatch):
    """With the pins, the same child resolves this process's root and code."""
    monkeypatch.delenv("FYP_CONFIG_PATH", raising=False)
    env = process_manager.worker_env()

    with tempfile.TemporaryDirectory() as tmp:
        decoy = Path(tmp).resolve()
        (decoy / "__proj__.py").touch()

        reported = _probe(decoy, env)

    assert reported["ROOT"] == str(ROOT)
    assert Path(reported["FYP"]).resolve().parent.parent == ROOT
