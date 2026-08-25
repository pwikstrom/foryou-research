"""The ?collections= subset param on the combined-personality route.

The route imports collections_for_user and build_personality lazily inside
the handler, so both are stubbed at their home modules; the Flask test
client runs with a stubbed admin session (pattern of
test_collection_accounts_routes.py, minus the storage store — nothing here
touches data_io).
"""

from unittest.mock import patch

import pytest

from web_interface import auth

_ADMIN = "subset.admin@example.test"


@pytest.fixture
def env(monkeypatch):
    from web_interface import collection_accounts as ca
    from web_interface import security
    from web_interface.fyp_data_hub import app
    from web_interface.services import my_collections_service as svc

    calls = []
    monkeypatch.setattr(ca, "collections_for_user", lambda u, **kw: ["c1", "c2", "c3"])
    monkeypatch.setattr(svc, "build_personality",
                        lambda cids: calls.append(list(cids)) or {"ok": True, "cids": list(cids)})

    um = auth.UserManager(storage_location="users", bootstrap=False)
    with patch.object(auth, "data_io") as fake_io:
        fake_io.exists.return_value = False
        um.add_user(_ADMIN, "pw", "admin", approved=True)
    for attr in ("users", "_loaded", "storage_location"):
        monkeypatch.setattr(security.user_manager, attr, getattr(um, attr))

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = _ADMIN
            sess["_fresh"] = True
        yield client, calls


def test_no_param_uses_all_owned(env):
    client, calls = env
    r = client.get("/api/my/collections/combined/personality")
    assert r.status_code == 200
    assert calls[-1] == ["c1", "c2", "c3"]


def test_valid_subset(env):
    client, calls = env
    r = client.get("/api/my/collections/combined/personality?collections=c2,c1")
    assert r.status_code == 200
    assert calls[-1] == ["c1", "c2"]  # deduped + sorted


def test_duplicates_collapse(env):
    client, calls = env
    r = client.get("/api/my/collections/combined/personality?collections=c1,c1,c1")
    assert r.status_code == 200
    assert calls[-1] == ["c1"]


def test_unowned_id_is_403(env):
    client, calls = env
    r = client.get("/api/my/collections/combined/personality?collections=c1,evil")
    assert r.status_code == 403
    assert not calls


def test_empty_param_is_400(env):
    client, calls = env
    r = client.get("/api/my/collections/combined/personality?collections=,%20,")
    assert r.status_code == 400
    assert not calls
