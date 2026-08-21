"""Public mini-site routing guard.

Anonymous visitors must reach the landing page and the public content pages
without authentication, protected routes must still bounce to /login, and an
authenticated user on ``/`` must get the app shell (not the landing page).

Also guards the source-repository links: the public pages route bug reports at
``[site] repo_url``, and an operator who clears that key must get a site with no
source-code links at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import flask_login.utils as fl_utils
import pytest

from web_interface.auth import User


@pytest.fixture(scope="module")
def app():
    os.environ.pop("K_SERVICE", None)
    from web_interface.fyp_data_hub import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.mark.parametrize("path", ["/", "/about", "/participate", "/data-donation", "/thehub", "/faq", "/login", "/signup"])
def test_public_pages_render_anonymously(client, path):
    resp = client.get(path)
    assert resp.status_code == 200


def test_anonymous_landing_shows_public_page(client):
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert "public-header" in body
    assert "The For You Data Hub" in body


def test_protected_route_still_redirects_to_login(client):
    resp = client.get("/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_authenticated_root_renders_app_shell(app, client, monkeypatch):
    user = User("public-routes-test", "admin", password_hash="x", approved=True)
    monkeypatch.setattr(fl_utils, "_get_user", lambda: user)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="home"' in body
    assert "public-header" not in body


@pytest.fixture()
def repo_url(app):
    """The configured [site] repo_url, skipping if an overlay cleared it."""
    from fyp.fyp_config import get_config

    url = str((get_config().get("site", {}) or {}).get("repo_url", "") or "").strip()
    if not url:
        pytest.skip("[site] repo_url is empty in this install's config")
    return url.rstrip("/")


@pytest.mark.parametrize("path", ["/about", "/thehub", "/faq"])
def test_public_pages_link_the_issue_tracker(client, repo_url, path):
    body = client.get(path).get_data(as_text=True)
    assert f"{repo_url}/issues" in body


def test_public_pages_drop_repo_links_when_repo_url_is_empty(client):
    """An operator who sets repo_url = "" gets no source-code links anywhere."""
    from fyp.fyp_config import get_config

    site = get_config().setdefault("site", {})
    original = site.get("repo_url", "")
    site["repo_url"] = ""
    try:
        for path in ("/", "/about", "/participate", "/thehub", "/faq"):
            body = client.get(path).get_data(as_text=True)
            assert "github.com" not in body, f"{path} still links to GitHub"
    finally:
        site["repo_url"] = original


def test_old_guide_url_redirects_to_thehub(client):
    resp = client.get("/guide")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/thehub")


def test_participate_links_the_donation_flow(client):
    """The participate page must hand TikTok users on to the donation site."""
    body = client.get("/participate").get_data(as_text=True)
    assert "https://www.foryouparticipate.net/tiktok/index.html" in body
