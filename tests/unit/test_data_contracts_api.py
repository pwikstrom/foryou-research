"""The read-only /api/manage/data-contracts/* endpoints.

Uses the Flask test client with a stubbed admin user (same approach as
``test_admin_settings_route.py``). The contracts themselves are committed
repo files, so the happy-path payload checks need no data setup; the
version-history checks monkeypatch the registries so no local registry
state is required.
"""

import pytest

from fyp import activity_versioning, scrape_versioning

_TEST_ADMIN = "__data_contracts_test_admin__"
_TEST_PLAIN = "__data_contracts_plain_user__"






@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        if uid == _TEST_PLAIN:
            return User(username=_TEST_PLAIN, role=ROLE_VIEWER, password_hash="", approved=True)
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






def test_requires_permission(client):
    _login(client, _TEST_PLAIN)
    res = client.get("/api/manage/data-contracts/scrape")
    assert res.status_code == 403






def test_unknown_kind_is_404(client):
    _login(client, _TEST_ADMIN)
    res = client.get("/api/manage/data-contracts/nonsense")
    assert res.status_code == 404






def test_versions_for_derived_is_404(client):
    _login(client, _TEST_ADMIN)
    res = client.get("/api/manage/data-contracts/derived/versions")
    assert res.status_code == 404






@pytest.mark.parametrize("kind", ["scrape", "activity", "derived"])
def test_parsed_payload_shape(client, kind):
    _login(client, _TEST_ADMIN)
    res = client.get(f"/api/manage/data-contracts/{kind}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["kind"] == kind
    assert body["path"].startswith("config/")
    assert body["validation_errors"] == []
    assert isinstance(body["fields"], list) and body["fields"]
    assert all("name" in f and "dtype" in f for f in body["fields"])
    if kind == "derived":
        assert body["active_version"] is None
    else:
        assert body["active_version"]["version"]
        # The activity contract may own zero platform-scoped fields.
        assert isinstance(body["platforms"], list)
        if kind == "scrape":
            assert body["platforms"]






def test_raw_and_download_return_toml(client):
    _login(client, _TEST_ADMIN)
    res = client.get("/api/manage/data-contracts/activity/raw")
    assert res.status_code == 200
    assert "[[fields]]" in res.get_json()["toml"]

    res = client.get("/api/manage/data-contracts/activity/download")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert b"[[fields]]" in res.data






def _fake_registry(id_key):
    return {
        "preferred": "x_old",
        "versions": {
            "x_old": {
                id_key: "x_old", "label": "old", "created_at": "2026-01-01T00:00:00",
                "platforms": ["tiktok"], "field_digest": {"a": 1}, "field_metadata": {},
            },
            "x_new": {
                id_key: "x_new", "label": "new", "created_at": "2026-06-01T00:00:00",
                "platforms": ["tiktok", "youtube"], "field_digest": {"a": 2}, "field_metadata": {},
            },
        },
    }






@pytest.mark.parametrize("kind,module,id_key", [
    ("scrape", scrape_versioning, "scrape_contract_version"),
    ("activity", activity_versioning, "activity_contract_version"),
])
def test_versions_payload(client, monkeypatch, kind, module, id_key):
    _login(client, _TEST_ADMIN)
    monkeypatch.setattr(module, "load_registry", lambda: _fake_registry(id_key))

    res = client.get(f"/api/manage/data-contracts/{kind}/versions")
    assert res.status_code == 200
    body = res.get_json()
    assert [v["version"] for v in body["versions"]] == ["x_new", "x_old"]  # newest first
    assert body["preferred"] == "x_old"
    by_version = {v["version"]: v for v in body["versions"]}
    assert by_version["x_old"]["preferred"] is True
    assert by_version["x_new"]["preferred"] is False
    # Summaries strip the bulky snapshot keys.
    assert "field_digest" not in by_version["x_new"]

    res = client.get(f"/api/manage/data-contracts/{kind}/versions/x_new")
    assert res.status_code == 200
    record = res.get_json()
    assert record["version"] == "x_new"
    assert record["field_digest"] == {"a": 2}
    assert record["preferred"] is False

    res = client.get(f"/api/manage/data-contracts/{kind}/versions/x_missing")
    assert res.status_code == 404
