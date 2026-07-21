"""The /api/admin/settings route: annotation keys and validation.

Uses the Flask test client with a stubbed admin user (same approach as
``test_annotation_contract_api.py``) but pytest-native. The admin settings
file is snapshotted and restored so the local store is untouched.

The [machine] model/generation parameters are config-file-only (no runtime
overrides) — the route must reject the retired machine_* keys.
"""

import pytest

import fyp.data_io as data_io
from fyp.annotation.backends import settings as backend_settings

_TEST_ADMIN = "__settings_test_admin__"






@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    # Snapshot + restore the settings file so the local store is untouched.
    fname = backend_settings.SETTINGS_FILENAME
    had_file = data_io.exists(storage_location="users", filename=fname)
    saved_settings = data_io.load_json(storage_location="users", filename=fname) if had_file else None

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield test_client

    if saved_settings is not None:
        data_io.save_json(data=saved_settings, storage_location="users", filename=fname)
    elif data_io.exists(storage_location="users", filename=fname):
        data_io.remove(storage_location="users", filename=fname)






def test_get_includes_backend_keys_and_defaults(client):
    res = client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.get_json()
    assert body["settings"]["annotation_backend"] == "gemini"
    assert body["settings"]["embedding_backend"] == "gemini"
    assert "gemini" in body["implemented_backends"]
    # The [machine] parameters are config-only — no settings keys, no baseline.
    assert "machine_temperature" not in body["settings"]
    assert "machine_defaults" not in body






def test_put_rejects_unknown_backend(client):
    res = client.put("/api/admin/settings", json={"annotation_backend": "nope_backend"})
    assert res.status_code == 400
    assert "backend" in res.get_json()["error"]






def test_put_rejects_retired_machine_keys(client):
    for key, value in (("machine_temperature", 0.8), ("machine_model", "gemini-x"),
                       ("machine_thinking_budget", 0), ("machine_media_resolution", "LOW"),
                       ("machine_max_output_tokens", 1024)):
        res = client.put("/api/admin/settings", json={key: value})
        assert res.status_code == 400, key
        assert "Unknown settings" in res.get_json()["error"]






def test_put_valid_backend_roundtrip(client):
    res = client.put("/api/admin/settings", json={"annotation_backend": "gemini"})
    assert res.status_code == 200
    res = client.get("/api/admin/settings")
    assert res.get_json()["settings"]["annotation_backend"] == "gemini"
