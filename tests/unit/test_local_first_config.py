"""Local-first committed config defaults + first-run guardrails.

Guards the public-release changes in ``fyp/core/fyp_config.py`` and
``config/config.toml``:

- committed defaults are home-relative (``~/fyp_local``) with GCS off and an
  empty Vertex project, so a fresh clone runs on any machine;
- ``initialize()`` expands ``~`` in the path defaults;
- a leftover ``~CHANGE-ME~`` placeholder from config.local.toml.example
  fails loud instead of silently creating a wrong directory.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

import fyp.fyp_config as fyp_config

ROOT = Path(__file__).resolve().parents[2]






def _temp_project(tmp_path: Path) -> Path:
    """Create a throwaway project root holding a copy of the committed config."""
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "config.toml", root / "config" / "config.toml")
    return root






def test_committed_defaults_are_local_first():
    """The tracked config.toml must stay generic: home path, GCS off, no project."""
    with open(ROOT / "config" / "config.toml", "rb") as fh:
        cf = tomllib.load(fh)
    assert cf["paths"]["local_data"].startswith("~")
    assert cf["paths"]["local_media"].startswith("~")
    assert cf["data_io"]["use_gcs_for_data"] is False
    assert cf["data_io"]["use_gcs_for_media"] is False
    assert cf["data_io"]["use_gcs_for_cache"] is False
    assert cf["machine"]["gemini"]["project"] == ""






def test_initialize_expands_home_relative_paths(tmp_path, monkeypatch):
    """``~/fyp_local`` resolves under the current user's home on any OS."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("FYP_FORCE_GCS", raising=False)
    root = _temp_project(tmp_path)

    cf = fyp_config.initialize(abs_project_root_path=str(root))

    assert cf["paths"]["local_data"] == str(tmp_path / "fyp_local")
    assert cf["paths"]["media"] == str(tmp_path / "fyp_local" / "media")
    assert cf["data_io"]["use_gcs_for_data"] is False






def test_initialize_keeps_absolute_override(tmp_path, monkeypatch):
    """An absolute path from config.local.toml passes through untouched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("FYP_FORCE_GCS", raising=False)
    root = _temp_project(tmp_path)
    override = tmp_path / "elsewhere"
    (root / "config" / "config.local.toml").write_text(
        f'[paths]\nlocal_data = "{override}"\nlocal_media = "{override / "media"}"\n'
    )

    cf = fyp_config.initialize(abs_project_root_path=str(root))

    assert cf["paths"]["local_data"] == str(override)






def test_change_me_placeholder_fails_loud(tmp_path, monkeypatch):
    """A copied-but-unedited config.local.toml.example raises, never mkdirs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _temp_project(tmp_path)
    (root / "config" / "config.local.toml").write_text(
        '[paths]\nlocal_data = "~CHANGE-ME~/fyp_local"\n'
        'local_media = "~CHANGE-ME~/fyp_local/media"\n'
    )

    with pytest.raises(ValueError, match="CHANGE-ME"):
        fyp_config.initialize(abs_project_root_path=str(root))
