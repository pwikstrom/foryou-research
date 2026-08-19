"""Unit tests for the ``.env`` auto-loader in ``fyp.core.fyp_config``.

The loader fills environment-variable gaps from ``<project_root>/.env`` at
config-initialize time; exported values must always win over the file.
"""

from __future__ import annotations

from fyp.core.fyp_config import _load_dotenv


def test_load_dotenv_missing_file_is_noop(tmp_path):
    """No .env file -> nothing applied, no error."""
    assert _load_dotenv(str(tmp_path)) == []


def test_load_dotenv_parses_and_applies(tmp_path, monkeypatch):
    """Plain, quoted and export-prefixed lines load; comments/blanks skip."""
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "FYP_TEST_DOTENV_A=plain\n"
        'FYP_TEST_DOTENV_B="double quoted"\n'
        "export FYP_TEST_DOTENV_C='single quoted'\n"
        "not a key value line\n"
    )
    for key in ("FYP_TEST_DOTENV_A", "FYP_TEST_DOTENV_B", "FYP_TEST_DOTENV_C"):
        monkeypatch.delenv(key, raising=False)

    applied = _load_dotenv(str(tmp_path))

    assert applied == ["FYP_TEST_DOTENV_A", "FYP_TEST_DOTENV_B", "FYP_TEST_DOTENV_C"]
    import os

    assert os.environ["FYP_TEST_DOTENV_A"] == "plain"
    assert os.environ["FYP_TEST_DOTENV_B"] == "double quoted"
    assert os.environ["FYP_TEST_DOTENV_C"] == "single quoted"
    for key in ("FYP_TEST_DOTENV_A", "FYP_TEST_DOTENV_B", "FYP_TEST_DOTENV_C"):
        monkeypatch.delenv(key, raising=False)


def test_load_dotenv_exported_value_wins(tmp_path, monkeypatch):
    """A variable already in the environment is never overwritten."""
    (tmp_path / ".env").write_text("FYP_TEST_DOTENV_WIN=from-file\n")
    monkeypatch.setenv("FYP_TEST_DOTENV_WIN", "exported")

    applied = _load_dotenv(str(tmp_path))

    assert applied == []
    import os

    assert os.environ["FYP_TEST_DOTENV_WIN"] == "exported"
