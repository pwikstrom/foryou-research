"""GET /api/manage/ingestion/ledger — gate + payload shape (S3 item 1, UI)."""

from types import SimpleNamespace

import pytest

_TEST_VIEWER = "__ledger_test_viewer__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client






def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True






def _grant_permissions(monkeypatch, perms):
    from web_interface import auth

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))






_FAKE_LEDGER = {
    "schema_version": 1,
    "files": {
        "new.zip": {
            "outcome": "added_as_new", "raw_rows": 100, "processed_rows": 90,
            "kept_rows": 85, "deduped_rows": 5,
            "dropped": {"not_parseable": 8, "missing_required": 2},
            "platform": "tiktok", "source": "ddp",
            "ts_last_seen": "2026-07-30T02:00:00+00:00",
        },
        "legacy.zip": {  # pre-extension entry: no processed/deduped/dropped
            "outcome": "fully_deduped", "raw_rows": 50, "kept_rows": 0,
            "platform": "instagram", "source": "ddp",
            "ts_last_seen": "2026-01-01T00:00:00+00:00",
        },
    },
}


def _stub_main_collection(monkeypatch):
    from web_interface.routes.management import ingestion

    monkeypatch.setattr(
        ingestion, "get_main_collection",
        lambda verbose=False: SimpleNamespace(ledger=dict(_FAKE_LEDGER)),
    )






def test_ledger_requires_auth(client):
    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code in (302, 401)






def test_ledger_requires_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code == 403






def test_ledger_payload_shape_and_order(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.data_management.ingestion"])
    _stub_main_collection(monkeypatch)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/manage/ingestion/ledger")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["count"] == 2

    files = payload["files"]
    # Newest first by ts_last_seen
    assert [f["filename"] for f in files] == ["new.zip", "legacy.zip"]
    assert files[0]["dropped"] == {"not_parseable": 8, "missing_required": 2}
    # Legacy entries pass through without the new keys (UI renders em-dashes)
    assert "dropped" not in files[1]






def test_ledger_platform_filter(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.data_management.ingestion"])
    _stub_main_collection(monkeypatch)
    _login(client, _TEST_VIEWER)

    res = client.get("/api/manage/ingestion/ledger?platform=instagram")
    payload = res.get_json()
    assert payload["count"] == 1
    assert payload["files"][0]["filename"] == "legacy.zip"
