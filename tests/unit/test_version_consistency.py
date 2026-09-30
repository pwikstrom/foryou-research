"""The release version must agree across every file that records it.

A release bump touches five files; Zenodo reads ``.zenodo.json`` in preference
to ``CITATION.cff``, so a missed file mislabels the archived snapshot. This
pins them together, along with the release date and the changelog links.
"""

import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _package_version() -> str:
    m = re.search(r'^__version__ = "([^"]+)"', _read("fyp/__init__.py"), re.M)
    assert m, "fyp/__init__.py must define __version__"
    return m.group(1)


def _latest_changelog_release() -> tuple[str, str]:
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\] — (\d{4}-\d{2}-\d{2})$", _read("CHANGELOG.md"), re.M)
    assert m, "CHANGELOG.md must have a released '## [x.y.z] — YYYY-MM-DD' heading"
    return m.group(1), m.group(2)


def test_version_agrees_across_release_files():
    cff = yaml.safe_load(_read("CITATION.cff"))
    versions = {
        "fyp/__init__.py": _package_version(),
        "pyproject.toml": tomllib.loads(_read("pyproject.toml"))["project"]["version"],
        "CITATION.cff": str(cff["version"]),
        ".zenodo.json": json.loads(_read(".zenodo.json"))["version"],
        "CHANGELOG.md": _latest_changelog_release()[0],
    }
    assert len(set(versions.values())) == 1, f"release versions disagree: {versions}"


def test_release_date_and_changelog_link_match():
    version, date = _latest_changelog_release()
    cff = yaml.safe_load(_read("CITATION.cff"))
    assert str(cff["date-released"]) == date, (
        f"CITATION.cff date-released {cff['date-released']} != CHANGELOG {date}"
    )
    assert re.search(
        rf"^\[{re.escape(version)}\]: \S+/releases/tag/v{re.escape(version)}$",
        _read("CHANGELOG.md"),
        re.M,
    ), f"CHANGELOG.md lacks the [{version}] link"


def test_unreleased_link_compares_from_the_latest_release():
    version, _ = _latest_changelog_release()
    assert re.search(
        rf"^\[Unreleased\]: \S+/compare/v{re.escape(version)}\.\.\.HEAD$",
        _read("CHANGELOG.md"),
        re.M,
    ), f"CHANGELOG.md's [Unreleased] link must compare from v{version}"
