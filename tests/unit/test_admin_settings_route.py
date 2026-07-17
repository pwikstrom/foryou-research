"""The /api/admin/settings route: annotation keys, validation, live apply.

Uses the Flask test client with a stubbed admin user (same approach as
``test_annotation_contract_api.py``) but pytest-native. The admin settings
file is snapshotted and restored so the local store is untouched.
"""

import pytest

import fyp.data_io as data_io
import fyp.machine_annotation as machine_annotation
from fyp.annotation.backends import settings as backend_settings
from fyp.fyp_config import get_config

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

    # Snapshot + restore the settings file and the live [machine] values.
    fname = backend_settings.SETTINGS_FILENAME
    had_file = data_io.exists(storage_location="users", filename=fname)
    saved_settings = data_io.load_json(storage_location="users", filename=fname) if had_file else None
    machine = get_config()["machine"]
    saved_machine = {key: machine[key] for key in backend_settings.MACHINE_OVERRIDE_KEYS.values()}
    monkeypatch.setattr(machine_annotation, "_MACHINE_BASE", None)

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
    machine.update(saved_machine)






def test_get_includes_annotation_keys_and_defaults(client):
    res = client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.get_json()
    assert body["settings"]["annotation_backend"] == "gemini"
    assert "machine_temperature" in body["settings"]
    assert set(body["machine_defaults"]) == {"model", "temperature", "thinking_budget",
                                             "media_resolution", "max_output_tokens"}
    assert "gemini" in body["implemented_backends"]






def test_put_rejects_unknown_backend(client):
    res = client.put("/api/admin/settings", json={"annotation_backend": "qwen_api"})
    assert res.status_code == 400
    assert "backend" in res.get_json()["error"]






def test_put_rejects_out_of_range_temperature(client):
    res = client.put("/api/admin/settings", json={"machine_temperature": 3.5})
    assert res.status_code == 400
    res = client.put("/api/admin/settings", json={"machine_temperature": True})
    assert res.status_code == 400
    res = client.put("/api/admin/settings", json={"machine_temperature": "hot"})
    assert res.status_code == 400






def test_put_valid_override_applies_to_live_config(client):
    res = client.put("/api/admin/settings", json={"machine_temperature": 0.8})
    assert res.status_code == 200
    assert get_config()["machine"]["temperature"] == 0.8

    # Clearing the override reverts to the config baseline.
    res = client.put("/api/admin/settings", json={"machine_temperature": ""})
    assert res.status_code == 200
    baseline = machine_annotation.machine_config_baseline()["temperature"]
    assert get_config()["machine"]["temperature"] == baseline






def test_put_model_override_roundtrip(client):
    res = client.put("/api/admin/settings", json={"machine_model": "gemini-test-model"})
    assert res.status_code == 200
    assert get_config()["machine"]["model"] == "gemini-test-model"
    res = client.get("/api/admin/settings")
    assert res.get_json()["settings"]["machine_model"] == "gemini-test-model"
