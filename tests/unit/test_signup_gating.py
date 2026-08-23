"""New-user signup gating: the shipped default, and the route that honours it.

A Hub instance can hold donated feed data, and there is no way to un-see what
an account has already opened. So the guarantee these tests pin is: a fresh
install does not let anyone who finds the URL self-register into an *active*
account. Two halves, and both have to hold —

1. ``DEFAULTS`` ships with approval required, so an install that never opens
   the admin page is closed rather than open.
2. ``signup()`` actually reads the setting and marks the account accordingly.

Neither half was covered before; a deployment was found serving a real corpus
to self-registered accounts because the shipped default was the open one.
"""

import pytest

from web_interface import admin_settings


def test_shipped_default_requires_admin_approval():
    """A fresh install (no admin_settings.json) must gate new signups.

    Read the fallback directly rather than through get_setting(), which a
    local store would mask -- the point is what ships, not what this machine
    happens to have saved.
    """
    assert admin_settings.DEFAULTS["new_user_admin_approval_required"] is True


@pytest.fixture
def client():
    from web_interface.fyp_data_hub import app

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.mark.parametrize(
    "require_approval, expected_approved",
    [(True, False), (False, True)],
    ids=["gating-on-lands-unapproved", "gating-off-lands-active"],
)
def test_signup_approves_only_when_gating_is_off(
    client, monkeypatch, require_approval, expected_approved
):
    """The stored setting decides ``approved``; nothing else may.

    ``add_user`` is stubbed so no user file is written, and the
    pending-signup email is stubbed so the test never touches SMTP.
    """
    from web_interface.routes import auth_routes

    captured = {}

    def _fake_add_user(username, password, role, approved=False, display_username=None, **kwargs):
        captured["approved"] = approved
        captured["role"] = role
        return True, "ok"

    monkeypatch.setattr(auth_routes.user_manager, "add_user", _fake_add_user)
    # A signup for an email that already has a passwordless participant
    # account claims it instead; this test is about fresh signups.
    monkeypatch.setattr(auth_routes.user_manager, "find_user_by_email", lambda email: None)
    monkeypatch.setattr(
        auth_routes, "get_new_user_approval_required", lambda: require_approval
    )
    monkeypatch.setattr(auth_routes, "get_default_new_user_role", lambda: "viewer")
    monkeypatch.setattr(
        auth_routes, "_notify_admin_of_pending_signup", lambda *a, **k: None
    )

    response = client.post(
        "/signup",
        data={
            "username": "someone@example.org",
            "display_username": "someone",
            "password": "correct horse battery staple",
            "confirm_password": "correct horse battery staple",
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 302)
    assert captured, "signup() never reached add_user -- form validation rejected the post"
    assert captured["approved"] is expected_approved
