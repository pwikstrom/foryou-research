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
    """Vertex mode emits [machine.gemini] project and nothing else machine-related."""
    answers = setup.Answers(data_dir="/tmp/d", gemini_mode="vertex", vertex_project="my-proj")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["machine"] == {"gemini": {"project": "my-proj"}}






def test_build_config_toml_api_key_mode():
    """API-key mode emits vertexai = false (the key itself stays in the env)."""
    answers = setup.Answers(data_dir="/tmp/d", gemini_mode="api_key")
    parsed = tomllib.loads(setup.build_config_toml(answers))
    assert parsed["machine"] == {"gemini": {"vertexai": False}}
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






def test_build_env_file_header_says_auto_loaded():
    """A freshly created .env explains it is auto-loaded (not the old warning)."""
    text = setup.build_env_file(setup.Answers(flask_secret="fsk"), "")
    assert "Loaded automatically" in text
    assert "NOT auto-loaded" not in text






def test_check_environment_levels_without_local_models():
    """Default checks carry sane levels and skip the local-model rows."""
    results = setup.check_environment(include_local_models=False)
    by_name = {r.name: r for r in results}
    assert by_name["python"].level == "required"
    assert by_name["virtualenv"].level == "recommended"
    assert by_name["ffmpeg"].level == "optional"
    assert by_name["yt-dlp"].level == "info"
    assert not any(r.name.startswith("local qwen") for r in results)
    assert not any(r.name.startswith("local minicpm") for r in results)






def test_print_checks_required_failure_flips_exit(capsys):
    """print_checks returns False only when a *required* check fails."""
    ok_required = setup.CheckResult("python", True, "3.12", "required", level="required")
    bad_optional = setup.CheckResult("ffmpeg", False, "not found", "youtube", level="optional")
    assert setup.print_checks([ok_required, bad_optional]) is True
    bad_required = setup.CheckResult("python", False, "3.8", "required", level="required")
    assert setup.print_checks([bad_required]) is False
    capsys.readouterr()






def test_free_space_gb_on_missing_path(tmp_path):
    """A not-yet-existing directory is measured via its nearest ancestor."""
    free = setup.free_space_gb(str(tmp_path / "does" / "not" / "exist"))
    assert free is not None and free > 0
