"""Unit tests for the pure logic in ``scripts/setup.py`` (the setup wizard).

Covers the answers→TOML rendering, data-dir validation, and the append-only
``.env`` merge. The interactive shell is not exercised here.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("fyp_setup_wizard", ROOT / "scripts" / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)






def test_build_config_toml_minimal_local():
    """A GCS-free, Gemini-free run emits only paths + the three GCS toggles."""
    answers = setup.Answers(data_dir="/tmp/fyp_data")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert set(parsed) == {"paths", "data_io"}
    assert parsed["paths"]["local_data"] == "/tmp/fyp_data"
    assert parsed["paths"]["local_media"] == "/tmp/fyp_data/media"
    assert parsed["data_io"] == {
        "use_gcs_for_data": False,
        "use_gcs_for_media": False,
        "use_gcs_for_cache": False,
    }






def test_build_config_toml_vertex():
    """Vertex mode emits [machine] project and nothing else machine-related."""
    answers = setup.Answers(data_dir="/tmp/d", gemini_mode="vertex", vertex_project="my-proj")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["machine"] == {"project": "my-proj"}






def test_build_config_toml_api_key_mode():
    """API-key mode emits vertexai = false (the key itself stays in the env)."""
    answers = setup.Answers(data_dir="/tmp/d", gemini_mode="api_key")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["machine"] == {"vertexai": False}
    assert "GEMINI" not in setup.build_config_toml(answers)






def test_build_config_toml_gcs():
    """GCS mode flips the toggles on and records the bucket."""
    answers = setup.Answers(data_dir="/tmp/d", gcs=True, gcs_bucket="my-bucket")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["data_io"]["use_gcs_for_data"] is True
    assert parsed["data_io"]["GCS_bucket_name"] == "my-bucket"






def test_build_config_toml_contact_email():
    """A provided contact email lands under [site]; skipped -> no [site] table."""
    answers = setup.Answers(data_dir="/tmp/d", contact_email="me@example.org")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["site"] == {"contact_email": "me@example.org"}
    without = tomllib.loads(setup.build_config_toml(setup.Answers(data_dir="/tmp/d")))
    assert "site" not in without






def test_validate_data_dir_rejects_file(tmp_path):
    """An existing regular file is rejected."""
    f = tmp_path / "afile"
    f.write_text("x")
    _, problem = setup.validate_data_dir(str(f))
    assert "file" in problem






def test_validate_data_dir_rejects_repo_checkout():
    """A path inside the repository checkout is flagged."""
    _, problem = setup.validate_data_dir(str(ROOT / "somewhere"))
    assert "repository" in problem






def test_validate_data_dir_accepts_missing_under_writable(tmp_path):
    """A not-yet-existing dir under a writable parent is fine and resolved."""
    resolved, problem = setup.validate_data_dir(str(tmp_path / "new" / "deep"))
    assert problem == ""
    assert resolved == str(tmp_path / "new" / "deep")






def test_build_env_file_append_only():
    """Existing .env lines are preserved; present keys are never rewritten."""
    answers = setup.Answers(gemini_api_key="sekret", flask_secret="fsk")
    existing = "# my notes\nGEMINI_API_KEY=old-value\n"
    merged = setup.build_env_file(answers, existing)
    assert merged.startswith(existing)
    assert "old-value" in merged
    assert "sekret" not in merged
    assert "FLASK_SECRET_KEY=fsk" in merged






def test_build_env_file_no_additions_is_identity():
    """With nothing collected, the existing text is returned unchanged."""
    existing = "A=1\n"
    assert setup.build_env_file(setup.Answers(), existing) == existing
