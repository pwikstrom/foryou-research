"""Signup terms-of-use acceptance and the funnel ``next`` thread.

Pins the recruitment-funnel additions to ``signup()``:

1. A post without the terms checkbox never reaches account creation.
2. ``terms_accepted_at`` is stamped on both creation paths — fresh accounts
   (``add_user``) and claimed passwordless participant accounts
   (``claim_participant_account``).
3. A funnel-origin signup (``next=/participate/...``) queues the guided tour
   (``hub_tour_pending``) and threads ``next`` on to the login redirect.
4. ``_safe_next`` refuses absolute / scheme-relative redirect targets.
"""

import pytest

from web_interface.auth import User
from web_interface.routes import auth_routes
from web_interface.routes.auth_routes import _safe_next


@pytest.fixture
def client():
    from web_interface.fyp_data_hub import app

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def stubbed_signup(monkeypatch):
    """Stub every persistence call signup() makes; capture what it passes."""
    captured = {"settings": {}}

    def _fake_add_user(username, password, role, approved=False, display_username=None,
                       origin=None, terms_accepted_at=None, **kwargs):
        captured["add_user"] = {"username": username, "approved": approved,
                                "terms_accepted_at": terms_accepted_at}
        return True, "ok"

    def _fake_claim(username, password, display_username=None, approved=True,
                    terms_accepted_at=None):
        captured["claim"] = {"username": username, "approved": approved,
                             "terms_accepted_at": terms_accepted_at}
        return True, "ok"

    monkeypatch.setattr(auth_routes.user_manager, "add_user", _fake_add_user)
    monkeypatch.setattr(auth_routes.user_manager, "claim_participant_account", _fake_claim)
    monkeypatch.setattr(auth_routes.user_manager, "find_user_by_email", lambda email: None)
    monkeypatch.setattr(
        auth_routes.user_manager, "update_user_settings",
        lambda username, settings: captured["settings"].update(settings) or (True, "ok"))
    monkeypatch.setattr(auth_routes, "get_new_user_approval_required", lambda: False)
    monkeypatch.setattr(auth_routes, "get_default_new_user_role", lambda: "viewer")
    monkeypatch.setattr(auth_routes, "_notify_admin_of_pending_signup", lambda *a, **k: None)
    return captured


FORM = {
    "username": "someone@example.org",
    "display_username": "someone",
    "password": "correct horse battery staple",
    "confirm_password": "correct horse battery staple",
    "accept_terms": "on",
}


def test_signup_without_terms_is_rejected(client, stubbed_signup):
    form = {k: v for k, v in FORM.items() if k != "accept_terms"}
    resp = client.post("/signup", data=form, follow_redirects=False)
    assert resp.status_code == 200  # re-rendered with a flash, no redirect
    assert "add_user" not in stubbed_signup
    assert "claim" not in stubbed_signup


def test_signup_stamps_terms_accepted_at(client, stubbed_signup):
    resp = client.post("/signup", data=FORM, follow_redirects=False)
    assert resp.status_code == 302
    stamp = stubbed_signup["add_user"]["terms_accepted_at"]
    assert stamp, "terms_accepted_at was not passed to add_user"
    assert "T" in stamp  # ISO timestamp


def test_claim_path_also_stamps_terms(client, stubbed_signup, monkeypatch):
    participant = User("someone@example.org", "viewer", password_hash=None, approved=True)
    monkeypatch.setattr(auth_routes.user_manager, "find_user_by_email",
                        lambda email: participant)
    resp = client.post("/signup", data=FORM, follow_redirects=False)
    assert resp.status_code == 302
    assert stubbed_signup["claim"]["terms_accepted_at"]
    assert "add_user" not in stubbed_signup


def test_funnel_signup_queues_tour_and_threads_next(client, stubbed_signup):
    resp = client.post("/signup?next=/participate/go-tour", data=FORM,
                       follow_redirects=False)
    assert resp.status_code == 302
    assert stubbed_signup["settings"].get("hub_tour_pending") is True
    assert "next=/participate/go-tour" in resp.headers["Location"]


def test_non_funnel_signup_does_not_queue_tour(client, stubbed_signup):
    resp = client.post("/signup", data=FORM, follow_redirects=False)
    assert resp.status_code == 302
    assert "hub_tour_pending" not in stubbed_signup["settings"]


@pytest.mark.parametrize("target, expected", [
    ("/participate/go-upload", "/participate/go-upload"),
    ("/#my_stuff/my-collections", "/#my_stuff/my-collections"),
    ("https://evil.example/phish", None),
    ("//evil.example/phish", None),
    ("", None),
    (None, None),
])
def test_safe_next_allows_only_relative_paths(target, expected):
    assert _safe_next(target) == expected


def test_user_record_roundtrips_terms_accepted_at():
    from web_interface.auth import _user_from_record

    user = User("t@example.org", "viewer", password_hash="x",
                terms_accepted_at="2026-08-26T00:00:00+00:00")
    record = user.to_dict()
    assert record["terms_accepted_at"] == "2026-08-26T00:00:00+00:00"
    assert _user_from_record(record).terms_accepted_at == "2026-08-26T00:00:00+00:00"


def test_email_check_endpoint(client, monkeypatch):
    from web_interface.security import user_manager

    taken = User("t@example.org", "viewer", password_hash="x", approved=True)
    claimable = User("p@example.org", "viewer", password_hash=None, approved=True)
    placeholder = User("p-1@x.org", "viewer", password_hash=None, placeholder=True)
    roster = {"t@example.org": taken, "p@example.org": claimable, "p-1@x.org": placeholder}
    monkeypatch.setattr(user_manager, "find_user_by_email", lambda e: roster.get(e))

    def status(email):
        return client.get(f"/api/signup/email-check?email={email}").get_json()["status"]

    assert status("new@example.org") == "available"
    assert status("t@example.org") == "taken"
    assert status("p@example.org") == "claimable"
    # A placeholder address is not claimable (nobody owns that mailbox); the
    # form lets them proceed and the POST path answers definitively.
    assert status("p-1@x.org") == "taken"
    assert client.get("/api/signup/email-check").get_json()["status"] == "available"
