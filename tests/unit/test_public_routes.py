"""Public mini-site routing guard.

Anonymous visitors must reach the landing page and the public content pages
without authentication, protected routes must still bounce to /login, and an
authenticated user on ``/`` must get the app shell (not the landing page).
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


@pytest.mark.parametrize("path", ["/", "/about", "/guide", "/faq", "/login", "/signup"])
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
