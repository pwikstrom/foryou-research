"""Indexing and social metadata for the public mini-site.

Every assertion here stands in for a failure that is invisible in the browser.
The site rendered perfectly for a year while shipping one hardcoded <title> on
all seven public pages, no canonical link, no robots.txt and no sitemap; the
only symptom was Search Console reporting 0 indexed pages and 7 excluded once
foryouresearch.net moved off Wix onto this app. Nothing short of a crawl would
have caught it, so these tests are the crawl.
"""

import json

import pytest

from web_interface import seo

# public_bp endpoints that are deliberately absent from PUBLIC_PAGES: the two
# metadata documents, and the three 301s kept alive for old Wix links. Anything
# else that appears under public_bp must be listed in PUBLIC_PAGES, which is
# what test_every_public_page_is_registered enforces.
NON_PAGE_ENDPOINTS = {
    "public_bp.robots",
    "public_bp.sitemap",
    "public_bp.guide",
    "public_bp.our_team",
    "public_bp.be_a_citizen_scientist",
    # Post-login hash-redirect helpers for the participation wizard, not pages.
    "public_bp.participate_go_upload",
    "public_bp.participate_go_tour",
}

CANONICAL = "https://example.org"
REPO = "https://github.com/example/hub"


@pytest.fixture
def app():
    from web_interface.fyp_data_hub import app as flask_app

    flask_app.testing = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def canonical(monkeypatch):
    """Pin ``[site] app_url`` so the tests do not depend on local config."""
    from fyp.core import fyp_config

    monkeypatch.setattr(
        fyp_config, "get_config",
        lambda *a, **k: {"site": {"app_url": CANONICAL,
                                 "contact_email": "info@example.org",
                                 "repo_url": REPO}},
    )


@pytest.fixture
def client(app, canonical):
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# robots.txt and sitemap.xml
# ---------------------------------------------------------------------------


def test_robots_txt_allows_the_site_and_points_at_the_sitemap(client):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    body = response.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert f"Sitemap: {CANONICAL}/sitemap.xml" in body
    for path in seo.DISALLOWED_PATHS:
        assert f"Disallow: {path}" in body


def test_sitemap_lists_every_public_page_as_an_absolute_canonical_url(client):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.mimetype == "application/xml"
    body = response.get_data(as_text=True)

    with client.application.test_request_context():
        from flask import url_for

        expected = {f"{CANONICAL}{url_for(endpoint)}" for endpoint in seo.PUBLIC_PAGES}

    found = set(body.split("<loc>")[1:])
    found = {entry.split("</loc>")[0] for entry in found}
    assert found == expected


def test_sitemap_never_advertises_a_path_robots_forbids(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)

    for path in seo.DISALLOWED_PATHS:
        assert path not in body


def test_every_sitemap_url_actually_serves(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)
    paths = [loc.split("</loc>")[0].replace(CANONICAL, "") for loc in body.split("<loc>")[1:]]

    for path in paths:
        assert client.get(path).status_code == 200, f"{path} is in the sitemap but does not serve"


# ---------------------------------------------------------------------------
# Per-page metadata
# ---------------------------------------------------------------------------


def test_every_public_page_is_registered(app):
    """A new public page must not be able to ship without indexing metadata."""
    registered = {
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if rule.endpoint.startswith("public_bp.")
    } - NON_PAGE_ENDPOINTS

    assert registered | {"index"} == set(seo.PUBLIC_PAGES)


@pytest.mark.parametrize("endpoint", sorted(seo.PUBLIC_PAGES))
def test_public_page_carries_a_self_referencing_canonical(client, endpoint):
    from flask import url_for

    with client.application.test_request_context():
        path = url_for(endpoint)

    html = client.get(path).get_data(as_text=True)
    assert f'<link rel="canonical" href="{CANONICAL}{path}">' in html


def test_titles_and_descriptions_are_unique(client):
    """Identical titles across pages are a duplicate signal in themselves."""
    titles = [page["title"] for page in seo.PUBLIC_PAGES.values()]
    descriptions = [page["description"] for page in seo.PUBLIC_PAGES.values()]

    assert len(set(titles)) == len(titles)
    assert len(set(descriptions)) == len(descriptions)


@pytest.mark.parametrize("endpoint,page", sorted(seo.PUBLIC_PAGES.items()))
def test_titles_and_descriptions_fit_a_result_snippet(endpoint, page):
    """Past roughly these lengths a search engine truncates or rewrites."""
    assert len(page["title"]) <= 60, f"{endpoint} title is {len(page['title'])} chars"
    assert 50 <= len(page["description"]) <= 165, (
        f"{endpoint} description is {len(page['description'])} chars"
    )


def test_public_page_carries_a_social_card(client):
    html = client.get("/participate").get_data(as_text=True)

    assert '<meta property="og:type" content="website">' in html
    assert f'<meta property="og:url" content="{CANONICAL}/participate">' in html
    assert f'<meta property="og:image" content="{CANONICAL}/static/landing/og_card.jpg">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html


def test_the_social_card_image_exists_at_the_advertised_size():
    """og:image declares 1200x630; a mismatch crops badly on every platform."""
    from pathlib import Path

    from PIL import Image

    card = Path(seo.__file__).resolve().parent / "static" / "landing" / "og_card.jpg"
    assert card.exists(), "og_card.jpg is missing — run scripts/make_og_card.py"
    with Image.open(card) as image:
        assert image.size == (1200, 630)


def test_pages_outside_the_public_site_are_noindex_and_uncanonical(client):
    html = client.get("/login").get_data(as_text=True)

    assert '<meta name="robots" content="noindex">' in html
    assert 'rel="canonical"' not in html
    assert "og:image" not in html


# ---------------------------------------------------------------------------
# Host canonicalisation
# ---------------------------------------------------------------------------


def test_www_redirects_to_the_canonical_host(client):
    response = client.get("/participate", base_url="https://www.example.org")

    assert response.status_code == 301
    assert response.headers["Location"] == f"{CANONICAL}/participate"


def test_the_www_redirect_preserves_the_query_string(client):
    response = client.get("/?utm_source=linkedin", base_url="https://www.example.org")

    assert response.status_code == 301
    assert response.headers["Location"] == f"{CANONICAL}/?utm_source=linkedin"


def test_the_canonical_host_is_served_not_redirected(client):
    assert client.get("/participate", base_url=CANONICAL).status_code == 200


def test_an_unrelated_host_is_served_rather_than_stranded(client):
    """Only the www twin redirects; anything else relies on the canonical tag."""
    response = client.get("/participate", base_url="https://fyp-data-hub.a.run.app")

    assert response.status_code == 200
    assert f'<link rel="canonical" href="{CANONICAL}/participate">' in response.get_data(
        as_text=True
    )


def test_no_redirect_and_no_canonical_host_without_app_url(app, monkeypatch):
    """A local or intranet install must not be told what its own name is."""
    from fyp.core import fyp_config

    monkeypatch.setattr(fyp_config, "get_config", lambda *a, **k: {"site": {"app_url": ""}})

    with app.test_client() as client:
        response = client.get("/participate", base_url="https://www.anything.test")

        assert response.status_code == 200
        assert 'href="https://www.anything.test/participate"' in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------


def test_structured_data_describes_the_project_and_its_funding(client):
    payload = _json_ld(client, "/about")
    project = _node(payload, "ResearchProject")

    assert project["name"] == seo.SITE_NAME
    assert project["funding"]["identifier"] == seo.GRANT_ID
    assert project["funder"]["name"] == seo.FUNDER_NAME
    assert set(seo.SAME_AS) <= set(project["sameAs"])
    assert {member["name"] for member in project["member"]} == {m[0] for m in seo.TEAM}


def test_the_hub_page_describes_the_software_from_the_citation(client):
    from web_interface.citation import get_citation

    citation = get_citation()
    software = _node(_json_ld(client, "/thehub"), "SoftwareApplication")

    assert software["codeRepository"] == REPO
    assert software["softwareVersion"] == citation["version"]
    assert software["identifier"] == citation["doi_url"]
    # Fabricating either of these to win a rich result is a manual-action risk.
    assert "aggregateRating" not in software
    assert "offers" not in software


def test_structured_data_honours_an_operator_who_wants_no_repo_links(app, monkeypatch):
    """`repo_url = ""` means no source-code link anywhere, JSON-LD included.

    The rest of the site gates its GitHub links on repo_url; structured data
    reading the repository straight out of CITATION.cff would have quietly
    reinstated an upstream link the operator had switched off.
    """
    from fyp.core import fyp_config

    monkeypatch.setattr(
        fyp_config, "get_config",
        lambda *a, **k: {"site": {"app_url": CANONICAL, "repo_url": ""}},
    )

    with app.test_client() as client:
        html = client.get("/thehub").get_data(as_text=True)

    assert "github.com" not in html
    assert "codeRepository" not in html


def test_structured_data_cannot_break_out_of_its_script_element(client):
    html = client.get("/about").get_data(as_text=True)
    block = html.split('<script type="application/ld+json">')[1].split("</script>")[0]

    assert "<" not in block
    json.loads(block)


def test_the_team_in_structured_data_matches_the_about_page():
    """The two lists restate each other, so pin them together.

    JSON-LD needs the investigators as data; /about renders them as team cards.
    Neither can generate the other without rewriting the page, so this is the
    thing that stops one being updated and the other quietly going stale.
    """
    from pathlib import Path

    about = (Path(seo.__file__).resolve().parent / "templates" / "public" / "about.html").read_text(
        encoding="utf-8"
    )
    rendered = {
        line.split(">")[1].split("<")[0]
        for line in about.splitlines()
        if 'class="team-card-name"' in line
    }

    assert rendered == {member[0] for member in seo.TEAM}


def _json_ld(client, path):
    html = client.get(path).get_data(as_text=True)
    block = html.split('<script type="application/ld+json">')[1].split("</script>")[0]
    return json.loads(block)


def _node(payload, node_type):
    for node in payload["@graph"]:
        if node["@type"] == node_type:
            return node
    raise AssertionError(f"no {node_type} node in the JSON-LD graph")
