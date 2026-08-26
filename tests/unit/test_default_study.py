"""The site-wide default study (Admin -> Site Settings).

Selecting a default study does two things: it shares that study with every
role regardless of its ``USER_ACCESS`` list, and it becomes the study the app
opens on for users who have not chosen one. These tests cover the server side
of both halves — the analysis-tab access path
(``services.user_variables.get_accessible_studies``), the My Studies listing
(``GET /api/manage/studies``), the ``/api/admin/settings`` round-trip, and the
rename follow-through. The client-side initial pick lives in study_state.js.

An unset default, or one naming a study that no longer exists, must leave
every one of these exactly as it was before the setting existed.
"""

import pytest

_TEST_USER = "__default_study_test_user__"
_TEST_ADMIN = "__default_study_test_admin__"


@pytest.fixture
def study_defs(monkeypatch):
    """Synthetic study defs with nothing shared with the test user's role."""
    import fyp.analysis.studies as fyp_studies
    from fyp.fyp_config import fyp_cf
    from web_interface.services import user_variables

    defs = {
        "open_study": {"USER_ACCESS": ["all"], "stats": {"unique_videos": 5}},
        "closed_study": {"USER_ACCESS": ["team"], "stats": {"unique_videos": 5}},
        "other_closed": {"USER_ACCESS": [], "stats": {"unique_videos": 5}},
    }
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    monkeypatch.setattr(user_variables.data_io, "exists", lambda **kw: True)
    # The listing now answers existence from one cache listdir; failing it
    # forces the per-study exists() fallback the line above satisfies.
    monkeypatch.setattr(user_variables.data_io, "listdir",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no cache")))
    # Both the My Studies listing and the settings picker reload studies.json
    # from disk before reading — keep the synthetic dict in place instead.
    monkeypatch.setattr(fyp_studies, "init_study_defs", lambda: None)
    return defs


def _set_default(monkeypatch, name):
    """Pin get_default_study() without touching the real settings store."""
    from web_interface import admin_settings

    monkeypatch.setattr(admin_settings, "get_default_study", lambda: name)






def test_default_study_is_readable_by_every_role(study_defs, monkeypatch):
    from web_interface.services.user_variables import get_accessible_studies

    _set_default(monkeypatch, "closed_study")
    names = set(get_accessible_studies(username=_TEST_USER, role="student", is_admin=False))
    assert names == {"open_study", "closed_study"}


def test_no_default_leaves_access_unchanged(study_defs, monkeypatch):
    from web_interface.services.user_variables import get_accessible_studies

    _set_default(monkeypatch, "")
    names = set(get_accessible_studies(username=_TEST_USER, role="student", is_admin=False))
    assert names == {"open_study"}


def test_deleted_default_study_reverts_to_the_old_behaviour(study_defs, monkeypatch):
    from web_interface.services.user_variables import get_accessible_studies

    _set_default(monkeypatch, "a_study_that_was_deleted")
    names = set(get_accessible_studies(username=_TEST_USER, role="student", is_admin=False))
    assert names == {"open_study"}






@pytest.fixture
def viewer_client(monkeypatch):
    """A non-manager user whose only permission is the My Studies listing."""
    import web_interface.auth as auth_mod
    import web_interface.routes.management.studies as studies_mod
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_USER:
            return User(username=_TEST_USER, role="student", password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(auth_mod.role_manager, "get_role_permissions",
                        lambda role: ["tab.my_stuff.my_studies"])
    # The route reloads defs from disk — keep the fixture's dict in place.
    monkeypatch.setattr(studies_mod, "init_study_defs", lambda: None)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = _TEST_USER
            sess["_fresh"] = True
        yield client


def test_my_studies_lists_the_default_study(study_defs, viewer_client, monkeypatch):
    _set_default(monkeypatch, "closed_study")
    res = viewer_client.get("/api/manage/studies")
    assert res.status_code == 200
    assert {s["STUDY_NAME"] for s in res.get_json()} == {"open_study", "closed_study"}


def test_my_studies_without_a_default_denies_unshared(study_defs, viewer_client, monkeypatch):
    _set_default(monkeypatch, "")
    res = viewer_client.get("/api/manage/studies")
    assert res.status_code == 200
    assert {s["STUDY_NAME"] for s in res.get_json()} == {"open_study"}






@pytest.fixture
def admin_client(monkeypatch):
    """Admin test client with the admin settings file snapshotted/restored."""
    import fyp.data_io as data_io
    from web_interface import admin_settings, security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    fname = admin_settings.SETTINGS_FILENAME
    had_file = data_io.exists(storage_location="users", filename=fname)
    saved = data_io.load_json(storage_location="users", filename=fname) if had_file else None
    if had_file:
        data_io.remove(storage_location="users", filename=fname)
    # The store has a 15s read cache — clear it either side of the swap.
    admin_settings._SETTINGS_CACHE.update({"ts": 0.0, "data": None})

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield client

    if saved is not None:
        data_io.save_json(data=saved, storage_location="users", filename=fname)
    elif data_io.exists(storage_location="users", filename=fname):
        data_io.remove(storage_location="users", filename=fname)
    admin_settings._SETTINGS_CACHE.update({"ts": 0.0, "data": None})


def test_get_offers_the_study_names_and_an_empty_default(study_defs, admin_client):
    res = admin_client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.get_json()
    assert body["settings"]["default_study"] == ""
    assert body["study_names"] == ["closed_study", "open_study", "other_closed"]


def test_put_rejects_an_unknown_study(study_defs, admin_client):
    res = admin_client.put("/api/admin/settings", json={"default_study": "nope"})
    assert res.status_code == 400
    assert "nope" in res.get_json()["error"]


def test_put_roundtrips_and_clears_the_default_study(study_defs, admin_client):
    from web_interface.admin_settings import get_default_study

    res = admin_client.put("/api/admin/settings", json={"default_study": "closed_study"})
    assert res.status_code == 200
    assert res.get_json()["settings"]["default_study"] == "closed_study"
    assert get_default_study() == "closed_study"

    # Empty clears it — the value that means "no default".
    res = admin_client.put("/api/admin/settings", json={"default_study": ""})
    assert res.status_code == 200
    assert get_default_study() == ""






def test_rename_follows_the_default_study(monkeypatch):
    """Renaming the default study retargets the setting, not drops it."""
    import web_interface.routes.management.studies as studies_mod

    stored = {"default_study": "old_name"}
    monkeypatch.setattr("web_interface.admin_settings.get_default_study",
                        lambda: stored.get("default_study", ""))
    monkeypatch.setattr("web_interface.admin_settings.load_admin_settings",
                        lambda: dict(stored))
    monkeypatch.setattr("web_interface.admin_settings.save_admin_settings",
                        lambda settings: stored.update(settings))

    studies_mod._retarget_default_study("old_name", "new_name")
    assert stored["default_study"] == "new_name"

    # A rename of some other study leaves the default alone.
    studies_mod._retarget_default_study("unrelated", "unrelated_2")
    assert stored["default_study"] == "new_name"
