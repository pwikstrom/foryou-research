"""Admin → Backends → Second machine: the endpoints follow the switch and the permission."""

import pytest

_USER = "second_machine_test_user"


def _client(monkeypatch, perms, enabled):
    import web_interface.routes.management.enrichment as routes
    from web_interface.auth import accounts
    from web_interface.auth.accounts import User
    from web_interface.fyp_data_hub import app

    orig = accounts.user_manager.get_user
    monkeypatch.setattr(
        accounts.user_manager,
        "get_user",
        lambda uid: (
            User(username=_USER, role="student", password_hash="", approved=True)
            if uid == _USER
            else orig(uid)
        ),
    )
    monkeypatch.setattr(accounts.role_manager, "get_role_permissions", lambda role: perms)
    monkeypatch.setattr(routes.admin_settings, "get_setting", lambda key: enabled)
    monkeypatch.setattr(routes.admin_settings, "study_names", lambda: ["s1"])
    routes._BACKLOG_STATUS_CACHE.update(at=0.0, value=None)
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = _USER
        sess["_fresh"] = True
    return client


@pytest.fixture
def backlog(monkeypatch):
    from web_interface.services import annotation_backlog as b

    calls = []
    status = {
        "remaining": 3,
        "lanes": {"urgent": 3, "coverage": 0, "rest": 0},
        "paused": False,
        "failed": 0,
        "claimed": 0,
        "running": False,
        "worker": None,
        "exists": True,
    }
    monkeypatch.setattr(b, "read_status", lambda: status)
    monkeypatch.setattr(b, "worker_status", lambda: {"host": "mac", "running": True})
    monkeypatch.setattr(
        b,
        "add_from",
        lambda **kw: (
            calls.append(kw),
            {
                "source": "study s1",
                "candidates": 5,
                "eligible": 3,
                "added": 3,
                "lanes": status["lanes"],
            },
        )[1],
    )
    monkeypatch.setattr(b, "set_paused", lambda flag: calls.append({"paused": flag}))
    return calls


def test_switched_off_shows_and_does_nothing(monkeypatch, backlog):
    client = _client(monkeypatch, ["tab.admin.backends", "tab.data_management.annotation"], False)
    body = client.get("/api/manage/second-machine").get_json()
    assert body["enabled"] is False and body["backlog"] is None and body["studies"] == []
    res = client.post("/api/manage/second-machine/backlog", json={"action": "pause"})
    assert res.status_code == 409 and backlog == []
    import web_interface.routes.management.enrichment as routes

    assert routes._annotation_backlog_status() is None  # no line on the annotation page


def test_switched_on_steers_the_backlog(monkeypatch, backlog):
    client = _client(monkeypatch, ["tab.admin.backends"], True)
    body = client.get("/api/manage/second-machine").get_json()
    assert (
        body["enabled"] is True and body["backlog"]["remaining"] == 3 and body["studies"] == ["s1"]
    )

    res = client.post("/api/manage/second-machine/backlog", json={"action": "add", "study": "s1"})
    assert res.status_code == 200 and "3 added" in res.get_json()["message"].replace(",", "")
    assert backlog[0]["study"] == "s1" and backlog[0]["lane"] == "urgent"

    assert (
        client.post("/api/manage/second-machine/backlog", json={"action": "add"}).status_code == 400
    )
    assert (
        client.post("/api/manage/second-machine/backlog", json={"action": "pause"}).status_code
        == 200
    )
    assert backlog[-1] == {"paused": True}
    bad = client.post("/api/manage/second-machine/backlog", json={"action": "empty", "lane": "x"})
    assert bad.status_code == 400

    import web_interface.routes.management.enrichment as routes

    assert routes._annotation_backlog_status()["host"] == "mac"


def test_needs_the_backends_permission(monkeypatch, backlog):
    client = _client(monkeypatch, ["tab.data_management.annotation"], True)
    assert client.get("/api/manage/second-machine").status_code in (302, 401, 403)
    res = client.post("/api/manage/second-machine/backlog", json={"action": "pause"})
    assert res.status_code in (302, 401, 403) and backlog == []
