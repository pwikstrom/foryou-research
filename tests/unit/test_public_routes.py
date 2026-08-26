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


def test_logout_lands_on_the_home_page(app, client, monkeypatch):
    """Logging out ends on '/', which renders the public landing for the
    now-anonymous visitor, not on the login form."""
    user = User("logout-test", "admin", password_hash="x", approved=True)
    monkeypatch.setattr(fl_utils, "_get_user", lambda: user)
    resp = client.get("/logout")
    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/") in ("", "http://localhost")


def test_landing_has_no_inline_login_form(client):
    """The landing hero stands alone; logging in happens via the top menu."""
    body = client.get("/").get_data(as_text=True)
    assert "landing-login-hero__card" not in body
    assert 'action="/login"' not in body


def test_old_guide_url_redirects_to_thehub(client):
    resp = client.get("/guide")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/thehub")


def test_participate_links_the_wizard(client):
    """Both participate CTAs must lead into the on-site participation wizard."""
    body = client.get("/participate").get_data(as_text=True)
    assert "/participate/start" in body
    # The external QUT donation site is no longer the entry point.
    assert "foryouparticipate.net" not in body


def test_participate_start_renders_all_stages_and_platforms(client):
    resp = client.get("/participate/start")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for label in ("Request your data", "Waiting for your data", "I have my data"):
        assert label in body
    # The shared how-to partial in inline mode: one body per platform.
    for platform in ("tiktok", "instagram", "youtube"):
        assert f'id="wiz-howto-{platform}"' in body
    # Anonymous visitors get the signup/login CTAs, threaded through ?next=.
    assert "/signup?next=" in body
    assert "/login?next=" in body


def test_participate_start_in_sitemap(client):
    body = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/participate/start" in body


def test_go_upload_redirects_to_my_collections_hash(client):
    resp = client.get("/participate/go-upload")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/#my_stuff/my-collections/upload")


def test_go_tour_redirects_to_tour_hash(client):
    resp = client.get("/participate/go-tour")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/#tour")


def test_terms_page_renders(client):
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "Terms of use" in resp.get_data(as_text=True)
