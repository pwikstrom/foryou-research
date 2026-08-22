"""Citation metadata derived from CITATION.cff.

The footer, /thehub and the FAQ all render `citation` from the context
processor, so a silent parse failure would replace a real reference with a
fallback link on every page. These tests pin the parse and the two rendered
forms against the repository's own CITATION.cff.
"""

import pathlib

import pytest
import yaml

CITATION_FILE = pathlib.Path(__file__).resolve().parents[2] / "CITATION.cff"


@pytest.fixture
def citation():
    from web_interface.citation import get_citation

    get_citation.cache_clear()
    try:
        yield get_citation()
    finally:
        get_citation.cache_clear()


@pytest.fixture
def cff():
    return yaml.safe_load(CITATION_FILE.read_text(encoding="utf-8"))


def test_citation_is_available_and_matches_the_cff(citation, cff):
    assert citation["available"] is True
    assert citation["title"] == cff["title"]
    assert citation["version"] == str(cff["version"])
    assert citation["doi"] == cff["doi"]
    assert citation["doi_url"].endswith(cff["doi"])
    assert citation["year"] == str(cff["date-released"])[:4]


def test_apa_reference_carries_author_year_version_and_doi(citation, cff):
    apa = citation["apa"]
    first = cff["authors"][0]
    assert apa.startswith(f"{first['family-names']}, {first['given-names'][0]}.")
    assert f"({citation['year']})" in apa
    assert f"(Version {citation['version']})" in apa
    assert "[Computer software]" in apa
    assert citation["doi_url"] in apa


def test_bibtex_is_a_parseable_software_entry(citation):
    bibtex = citation["bibtex"]
    assert bibtex.startswith("@software{")
    assert bibtex.rstrip().endswith("}")
    for field in ("author", "title", "year", "version", "doi", "url"):
        assert f"  {field.ljust(7)} = {{" in bibtex, field
    # Braces have to balance or BibTeX chokes on the whole .bib file.
    assert bibtex.count("{") == bibtex.count("}")


def test_missing_cff_degrades_instead_of_raising(monkeypatch, tmp_path):
    """A fork that drops CITATION.cff must still be able to serve every page."""
    from web_interface import citation as citation_mod

    monkeypatch.setattr(citation_mod, "_CITATION_FILE", tmp_path / "nope.cff")
    citation_mod.get_citation.cache_clear()
    try:
        assert citation_mod.get_citation() == {"available": False}
    finally:
        citation_mod.get_citation.cache_clear()


def test_unparseable_cff_degrades_instead_of_raising(monkeypatch, tmp_path):
    from web_interface import citation as citation_mod

    broken = tmp_path / "CITATION.cff"
    broken.write_text("title: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(citation_mod, "_CITATION_FILE", broken)
    citation_mod.get_citation.cache_clear()
    try:
        assert citation_mod.get_citation()["available"] is False
    finally:
        citation_mod.get_citation.cache_clear()
