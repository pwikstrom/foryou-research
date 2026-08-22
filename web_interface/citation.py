"""Citation metadata for this Hub, read from the repository's CITATION.cff.

Three surfaces render the same reference — the footer's "How to cite" box on
every page, the citation section on ``/thehub`` and the FAQ entry — and all of
them read this module. ``CITATION.cff`` is the single source of truth (it is
also what GitHub's "Cite this repository" button and Zenodo read), so a release
that bumps the version and DOI updates the site with no template edits, and a
fork that rewrites the file gets its own citation for free.

Parsed once per process and cached: the file cannot change under a running
server. When it is missing or unparseable, ``get_citation()`` returns a mapping
with ``available == False`` and the templates fall back to pointing at the
repository instead of printing a half-built reference.
"""

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# web_interface/ sits directly under the project root, next to CITATION.cff.
_CITATION_FILE = Path(__file__).resolve().parents[1] / "CITATION.cff"

_UNAVAILABLE: dict = {"available": False}


def _initials(given_names: str) -> str:
    """Return APA-style initials ("Anna Maria" -> "A. M.")."""
    return " ".join(f"{part[0]}." for part in given_names.split() if part)


def _author_apa(author: dict) -> str:
    """One author as "Family, I. M." — the APA reference-list form."""
    family = str(author.get("family-names", "") or "").strip()
    given = str(author.get("given-names", "") or "").strip()
    if family and given:
        return f"{family}, {_initials(given)}"
    return family or given


def _author_bibtex(author: dict) -> str:
    """One author as "Family, Given" — BibTeX's unambiguous name form."""
    family = str(author.get("family-names", "") or "").strip()
    given = str(author.get("given-names", "") or "").strip()
    if family and given:
        return f"{family}, {given}"
    return family or given


def _join_apa(names: list[str]) -> str:
    """Join an APA author list: "A", "A, & B", "A, B, & C"."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + ", & " + names[-1]


@lru_cache(maxsize=1)
def get_citation() -> dict:
    """Return the citation for this software, derived from CITATION.cff.

    Returns:
        A mapping the templates render directly. ``available`` is False (and no
        other key is guaranteed) when CITATION.cff is missing or unparseable.
        Otherwise: ``title``, ``authors`` (APA-joined string), ``year``,
        ``version``, ``doi``, ``doi_url``, ``repository``, ``apa`` and
        ``bibtex``.
    """
    try:
        # Imported here, not at module scope: a broken CITATION.cff must not
        # be able to take the whole app down at import time.
        import yaml

        raw = yaml.safe_load(_CITATION_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read %s; citation blocks fall back to the repo link.",
                       _CITATION_FILE, exc_info=True)
        return dict(_UNAVAILABLE)

    if not isinstance(raw, dict):
        logger.warning("%s did not parse to a mapping; skipping citation blocks.", _CITATION_FILE)
        return dict(_UNAVAILABLE)

    authors = [a for a in (raw.get("authors") or []) if isinstance(a, dict)]
    title = str(raw.get("title", "") or "").strip()
    if not (authors and title):
        logger.warning("%s has no title or authors; skipping citation blocks.", _CITATION_FILE)
        return dict(_UNAVAILABLE)

    version = str(raw.get("version", "") or "").strip()
    doi = str(raw.get("doi", "") or "").strip()
    repository = str(raw.get("repository-code", "") or "").strip()
    # date-released is a YAML date or an ISO string depending on the quoting.
    year = str(raw.get("date-released", "") or "")[:4]

    apa_authors = _join_apa([_author_apa(a) for a in authors])
    doi_url = f"https://doi.org/{doi}" if doi else ""

    # APA 7 software reference: Author. (Year). Title (Version N) [Computer
    # software]. Publisher. DOI — publisher omitted when there is no DOI to
    # attribute it to.
    apa = f"{apa_authors} ({year}). {title}"
    if version:
        apa += f" (Version {version})"
    apa += " [Computer software]."
    if doi_url:
        apa += f" Zenodo. {doi_url}"
    elif repository:
        apa += f" {repository}"

    first_family = str(authors[0].get("family-names", "software") or "software").strip()
    bibtex_key = f"{first_family.split()[0].lower()}{year}foryoudatahub"
    bibtex_lines = [
        f"@software{{{bibtex_key},",
        f"  author  = {{{' and '.join(_author_bibtex(a) for a in authors)}}},",
        f"  title   = {{{title}}},",
        f"  year    = {{{year}}},",
    ]
    if version:
        bibtex_lines.append(f"  version = {{{version}}},")
    if doi:
        bibtex_lines.append(f"  doi     = {{{doi}}},")
    bibtex_lines.append(f"  url     = {{{doi_url or repository}}}")
    bibtex_lines.append("}")

    return {
        "available": True,
        "title": title,
        "authors": apa_authors,
        "year": year,
        "version": version,
        "doi": doi,
        "doi_url": doi_url,
        "repository": repository,
        "apa": apa,
        "bibtex": "\n".join(bibtex_lines),
    }
