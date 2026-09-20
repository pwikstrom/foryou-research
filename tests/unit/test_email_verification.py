"""Signup email verification: token, gates, routes, policy, prune.

The guarantee pinned here: a self-service signup cannot log in — and, when
approval gating is on, does not reach the admin — until a link emailed to its
address has been opened. And the flip side: nobody who already had an account
is locked out, and an install without outgoing mail keeps working.

Persistence runs against the in-memory ``_FakeStore`` from
``test_user_manager_mutations`` so no user file is written; every send is
stubbed so nothing touches SMTP.
"""

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from web_interface import auth, email_verification


# --- helpers ---------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.files = {}

    def exists(self, storage_location, filename):
        return filename in self.files

    def listdir(self, storage_location, return_absolute_path=False):
        return list(self.files.keys())

    def load_json(self, storage_location, filename, **kwargs):
        return copy.deepcopy(self.files.get(filename))

    def save_json(self, data, storage_location, filename, **kwargs):
        self.files[filename] = copy.deepcopy(data)

    def remove(self, storage_location, filename):
        self.files.pop(filename, None)


@pytest.fixture
def store():
    return _FakeStore()


@pytest.fixture
def manager(store):
    patches = [
        patch.object(auth.data_io, "exists", store.exists),
        patch.object(auth.data_io, "listdir", store.listdir),
        patch.object(auth.data_io, "load_json", store.load_json),
        patch.object(auth.data_io, "save_json", store.save_json),
        patch.object(auth.data_io, "remove", store.remove),
        patch.object(auth.role_manager, "role_exists", lambda role: True),
    ]
    for p in patches:
        p.start()
    try:
        yield auth.UserManager(storage_location="users", bootstrap=False)
    finally:
        for p in patches:
            p.stop()


@pytest.fixture
def client():
    from web_interface.fyp_data_hub import app

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


FORM = {
    "username": "someone@example.org",
    "display_username": "someone",
    "password": "correct horse battery staple",
    "confirm_password": "correct horse battery staple",
    "accept_terms": "on",
}


def _signup_user(**over):
    kw = {"username": "someone@example.org", "role": "viewer",
          "password_hash": auth.hash_password("pw"), "approved": True,
          "origin": {"source": "signup", "at": "2026-09-01T00:00:00+00:00"},
          "created_at": "2026-09-01T00:00:00+00:00"}
    kw.update(over)
    return auth.User(**kw)


# --- user record -------------------------------------------------------------

def test_record_without_the_field_loads_as_legacy_verified():
    """An account that predates the feature must not be locked out."""
    u = auth._user_from_record({"username": "old@example.org", "password_hash": "x"})
    assert u.email_verified_via == auth.EMAIL_VERIFIED_LEGACY
    assert u.email_verified()


def test_record_with_null_field_is_unverified():
    u = auth._user_from_record({"username": "new@example.org", "password_hash": "x",
                                "email_verified_via": None})
    assert not u.email_verified()


def test_roundtrip_keeps_verification_fields():
    u = _signup_user(email_verified_via="link", email_verified_at="2026-09-02T00:00:00+00:00",
                     email_verification_sent_at="2026-09-01T00:00:01+00:00")
    back = auth._user_from_record(u.to_dict())
    assert back.email_verified_via == "link"
    assert back.email_verified_at == "2026-09-02T00:00:00+00:00"
    assert back.email_verification_sent_at == "2026-09-01T00:00:01+00:00"


def test_add_user_defaults_to_unverified_and_admin_via_is_verified(manager):
    manager.add_user("s@example.org", "pw", "viewer")
    assert not manager.get_user("s@example.org").email_verified()
    manager.add_user("a@example.org", "pw", "viewer", email_verified_via=auth.EMAIL_VERIFIED_ADMIN)
    a = manager.get_user("a@example.org")
    assert a.email_verified() and a.email_verified_at == a.created_at


def test_claim_resets_verification(manager):
    manager.add_user("p@example.org", None, "viewer", approved=True,
                     account_kind=auth.ACCOUNT_KIND_PARTICIPANT,
                     email_verified_via=auth.EMAIL_VERIFIED_INGEST)
    ok, _ = manager.claim_participant_account("p@example.org", "pw")
    assert ok
    assert not manager.get_user("p@example.org").email_verified()


def test_mark_and_record_sent(manager):
    manager.add_user("s@example.org", "pw", "viewer")
    assert manager.mark_email_verified("s@example.org", via="")[0] is False
    ok, _ = manager.record_verification_sent("s@example.org")
    assert ok and manager.get_user("s@example.org").email_verification_sent_at
    ok, _ = manager.mark_email_verified("s@example.org", via=auth.EMAIL_VERIFIED_LINK)
    assert ok
    u = manager.get_user("s@example.org")
    assert u.email_verified_via == "link" and u.email_verified_at


def test_verify_user_refuses_unverified(manager):
    manager.add_user("s@example.org", "pw", "viewer", approved=True)
    assert manager.verify_user("s@example.org", "pw") is None
    manager.mark_email_verified("s@example.org", via=auth.EMAIL_VERIFIED_LINK)
    assert manager.verify_user("s@example.org", "pw") is not None


# --- token -------------------------------------------------------------------

def test_token_roundtrip_and_next(client):
    u = _signup_user()
    with client.application.test_request_context():
        tok = email_verification.make_token(u, next_target="/participate/go-upload")
        parsed = email_verification.parse_token(tok, lambda name: u if name == u.username else None)
    assert parsed is not None
    assert parsed[0] is u and parsed[1] == "/participate/go-upload"


def test_token_rejected_when_tampered_expired_or_password_changed(client):
    u = _signup_user()
    with client.application.test_request_context():
        tok = email_verification.make_token(u)
        assert email_verification.parse_token(tok + "x", lambda n: u) is None
        assert email_verification.parse_token(tok, lambda n: None) is None
        changed = _signup_user(password_hash=auth.hash_password("other"))
        assert email_verification.parse_token(tok, lambda n: changed) is None
        with patch.object(email_verification, "TOKEN_MAX_AGE_S", -1):
            assert email_verification.parse_token(tok, lambda n: u) is None


# --- policy ------------------------------------------------------------------

@pytest.mark.parametrize("setting, mail, expected", [
    (True, True, None),
    (False, True, auth.EMAIL_VERIFIED_SETTING_OFF),
    (True, False, auth.EMAIL_VERIFIED_MAIL_UNCONFIGURED),
])
def test_skip_reason(monkeypatch, setting, mail, expected):
    monkeypatch.setattr(email_verification, "get_signup_email_verification_required", lambda: setting)
    monkeypatch.setattr(email_verification, "mail_configured", lambda: mail)
    assert email_verification.skip_reason() == expected
    assert email_verification.verification_required() is (expected is None)


def test_shipped_default_requires_verification():
    from web_interface import admin_settings
    assert admin_settings.DEFAULTS["signup_email_verification_required"] is True
    assert admin_settings.SETTING_TYPES["signup_email_verification_required"] is bool


def test_send_link_honours_cooldown(monkeypatch, manager):
    manager.add_user("s@example.org", "pw", "viewer")
    sends = []
    monkeypatch.setattr(email_verification, "send_verification_email_async",
                        lambda **kw: sends.append(kw))
    monkeypatch.setattr(email_verification, "_absolute_verify_url", lambda tok: "http://x/v/" + tok)
    monkeypatch.setattr(email_verification, "make_token", lambda user, next_target=None: "tok")
    u = manager.get_user("s@example.org")
    assert email_verification.send_verification_link(manager, u) is True
    assert sends[0]["to_email"] == "s@example.org" and sends[0]["verify_url"] == "http://x/v/tok"
    sends[0]["on_success"]()
    u = manager.get_user("s@example.org")
    assert u.email_verification_sent_at
    assert email_verification.send_verification_link(manager, u) is False
    assert email_verification.send_verification_link(manager, u, force=True) is True
    assert len(sends) == 2


# --- routes ------------------------------------------------------------------

@pytest.fixture
def signup_env(monkeypatch, manager):
    """Signup against the fake-store manager, every email stubbed."""
    from web_interface.routes import auth_routes

    sent, notified = [], []
    monkeypatch.setattr(auth_routes, "user_manager", manager)
    monkeypatch.setattr(auth_routes.email_verification, "send_verification_email_async",
                        lambda **kw: sent.append(kw))
    monkeypatch.setattr(auth_routes, "_notify_admin_of_pending_signup",
                        lambda *a, **k: notified.append(a))
    monkeypatch.setattr(auth_routes, "get_default_new_user_role", lambda: "viewer")
    monkeypatch.setattr(auth_routes, "get_new_user_approval_required", lambda: True)
    monkeypatch.setattr(email_verification, "get_signup_email_verification_required", lambda: True)
    monkeypatch.setattr(email_verification, "mail_configured", lambda: True)
    return {"sent": sent, "notified": notified, "manager": manager}


def test_signup_sends_link_and_defers_admin_notification(client, signup_env):
    r = client.post("/signup", data=FORM, follow_redirects=False)
    assert r.status_code == 302
    u = signup_env["manager"].get_user("someone@example.org")
    assert u is not None and not u.email_verified() and not u.approved
    assert len(signup_env["sent"]) == 1
    assert "/verify-email/" in signup_env["sent"][0]["verify_url"]
    assert signup_env["notified"] == []


def test_resignup_on_unverified_account_only_resends(client, signup_env, monkeypatch):
    client.post("/signup", data=FORM)
    m = signup_env["manager"]
    before = m.get_user("someone@example.org").to_dict()
    monkeypatch.setattr(email_verification, "RESEND_COOLDOWN_S", 0)
    r = client.post("/signup", data={**FORM, "password": "different", "confirm_password": "different"})
    assert r.status_code == 302
    assert len(signup_env["sent"]) == 2
    after = m.get_user("someone@example.org").to_dict()
    assert after["password_hash"] == before["password_hash"]
    assert auth.verify_password(after["password_hash"], FORM["password"])


def test_signup_without_mail_admits_with_stamp_and_notifies(client, signup_env, monkeypatch, caplog):
    monkeypatch.setattr(email_verification, "mail_configured", lambda: False)
    with caplog.at_level("WARNING"):
        client.post("/signup", data=FORM)
    u = signup_env["manager"].get_user("someone@example.org")
    assert u.email_verified_via == auth.EMAIL_VERIFIED_MAIL_UNCONFIGURED
    assert signup_env["sent"] == []
    assert len(signup_env["notified"]) == 1
    assert "WITHOUT email verification" in caplog.text


def test_signup_with_setting_off_keeps_old_flow(client, signup_env, monkeypatch):
    monkeypatch.setattr(email_verification, "get_signup_email_verification_required", lambda: False)
    client.post("/signup", data=FORM)
    u = signup_env["manager"].get_user("someone@example.org")
    assert u.email_verified_via == auth.EMAIL_VERIFIED_SETTING_OFF
    assert signup_env["sent"] == [] and len(signup_env["notified"]) == 1


def test_login_blocked_until_verified_then_approval_applies(client, signup_env):
    client.post("/signup", data=FORM)
    m = signup_env["manager"]
    r = client.post("/login", data={"username": FORM["username"], "password": FORM["password"]},
                    follow_redirects=True)
    assert b"verify your email" in r.data
    assert b"Resend verification link" in r.data
    with client.session_transaction() as sess:
        assert "_user_id" not in sess

    m.mark_email_verified(FORM["username"], via=auth.EMAIL_VERIFIED_LINK)
    r = client.post("/login", data={"username": FORM["username"], "password": FORM["password"]},
                    follow_redirects=True)
    assert b"pending approval" in r.data


def test_verify_route_stamps_notifies_and_threads_next(client, signup_env):
    client.post("/signup", data={**FORM, "next": "/participate/go-upload"})
    m = signup_env["manager"]
    url = signup_env["sent"][0]["verify_url"]
    token = url.rsplit("/verify-email/", 1)[1]
    r = client.get(f"/verify-email/{token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login?next=/participate/go-upload")
    u = m.get_user(FORM["username"])
    assert u.email_verified_via == auth.EMAIL_VERIFIED_LINK
    assert len(signup_env["notified"]) == 1

    # Opening it again is harmless and does not re-notify.
    client.get(f"/verify-email/{token}")
    assert len(signup_env["notified"]) == 1


def test_verify_route_rejects_bad_token(client, signup_env):
    r = client.get("/verify-email/not-a-token", follow_redirects=True)
    assert b"invalid or has expired" in r.data


def test_resend_route_is_neutral(client, signup_env, monkeypatch):
    client.post("/signup", data=FORM)
    monkeypatch.setattr(email_verification, "RESEND_COOLDOWN_S", 0)
    r1 = client.post("/verify-email/resend", data={"username": FORM["username"]}, follow_redirects=True)
    r2 = client.post("/verify-email/resend", data={"username": "nobody@example.org"}, follow_redirects=True)
    assert b"a new verification link is on its way" in r1.data
    assert b"a new verification link is on its way" in r2.data
    assert len(signup_env["sent"]) == 2  # signup + the one real resend


def test_email_check_reports_unverified(client, signup_env):
    client.post("/signup", data=FORM)
    r = client.get(f"/api/signup/email-check?email={FORM['username']}")
    assert r.get_json()["status"] == "unverified"


# --- prune -------------------------------------------------------------------

def test_prune_unverified_signups(manager, monkeypatch):
    import web_interface.collection_accounts as ca
    monkeypatch.setattr(ca, "collections_for_user", lambda uid, fresh=False: ["c1"] if uid == "owner@example.org" else [])
    now = datetime(2026, 9, 20, tzinfo=UTC)
    old = (now - timedelta(days=8)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()

    def add(name, **kw):
        base = {"role": "viewer", "approved": True,
                "origin": {"source": "signup", "at": old},
                "display_username": None}
        base.update(kw)
        manager.add_user(name, "pw", base.pop("role"), approved=base.pop("approved"),
                         display_username=base.pop("display_username"),
                         origin=base.pop("origin"), **base)

    add("stale@example.org")
    add("recent@example.org")
    add("owner@example.org")
    add("verified@example.org", email_verified_via=auth.EMAIL_VERIFIED_LINK)
    add("claimed@example.org", origin={"source": "aio_ingest", "at": old},
        account_kind=auth.ACCOUNT_KIND_PARTICIPANT)
    manager.add_user("admin@example.org", "pw", auth.ROLE_ADMIN, approved=True,
                     email_verified_via=auth.EMAIL_VERIFIED_ADMIN)
    # created_at is stamped "now" by add_user; backdate the ones that matter.
    for name in ("stale@example.org", "owner@example.org", "verified@example.org", "claimed@example.org"):
        manager.get_user(name).created_at = old
        manager.save_user(name)
    manager.get_user("recent@example.org").created_at = fresh
    manager.save_user("recent@example.org")

    signups, claims = manager.unverified_signups()
    assert {u.username for u in signups} == {"stale@example.org", "recent@example.org", "owner@example.org"}
    assert {u.username for u in claims} == {"claimed@example.org"}

    removed = manager.prune_unverified_signups(max_age_days=7, now=now)
    assert removed == ["stale@example.org"]
    assert manager.get_user("stale@example.org") is None
    for kept in ("recent@example.org", "owner@example.org", "verified@example.org",
                 "claimed@example.org", "admin@example.org"):
        assert manager.get_user(kept) is not None
