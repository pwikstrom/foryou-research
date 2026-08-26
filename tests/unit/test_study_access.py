"""USER_ACCESS semantics after the S4 empty-means-none flip.

Both sides of study visibility must agree: the analysis-tab side
(``services.user_variables.get_accessible_studies``) and the My Studies
listing (``GET /api/manage/studies``) deny when ``USER_ACCESS`` is
missing/empty and match on role, username, or ``'all'``. Also covers the
boot-time backfill migration and the server-side rejection of a study
with no collections.
"""

import pytest

_TEST_USER = "__access_test_user__"


@pytest.fixture
def study_defs(monkeypatch):
    """Install a synthetic study_defs dict and neutralise storage checks."""
    from fyp.fyp_config import fyp_cf
    from web_interface.services import user_variables

    defs = {
        "shared_all": {"USER_ACCESS": ["all"], "stats": {"unique_videos": 5}},
        "shared_role": {"USER_ACCESS": ["team"], "stats": {"unique_videos": 5}},
        "shared_user": {"USER_ACCESS": [_TEST_USER], "stats": {"unique_videos": 5}},
        "unshared_empty": {"USER_ACCESS": [], "stats": {"unique_videos": 5}},
        "unshared_missing": {"stats": {"unique_videos": 5}},
        "unshared_malformed": {"USER_ACCESS": "all", "stats": {"unique_videos": 5}},
    }
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    monkeypatch.setattr(user_variables.data_io, "exists", lambda **kw: True)
    # The listing now answers existence from one cache listdir; failing it
    # forces the per-study exists() fallback the line above satisfies.
    monkeypatch.setattr(user_variables.data_io, "listdir",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no cache")))
    return defs


@pytest.mark.parametrize("role,expected", [
    ("team", {"shared_all", "shared_role", "shared_user"}),
    ("viewer", {"shared_all", "shared_user"}),
    ("student", {"shared_all", "shared_user"}),
])
def test_analysis_side_requires_explicit_grant(study_defs, role, expected):
    from web_interface.services.user_variables import get_accessible_studies

    names = set(get_accessible_studies(username=_TEST_USER, role=role, is_admin=False))
    assert names == expected


def test_analysis_side_admin_sees_everything(study_defs):
    from web_interface.services.user_variables import get_accessible_studies

    names = set(get_accessible_studies(username="x", role="admin", is_admin=True))
    assert names == set(study_defs)


def test_my_studies_matches_username_and_denies_unshared(study_defs, monkeypatch):
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app
    import web_interface.auth as auth_mod
    import web_interface.routes.management.studies as studies_mod

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_USER:
            return User(username=_TEST_USER, role="student", password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    # Not a study manager: no permissions at all beyond the listing gate.
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
        res = client.get("/api/manage/studies")
        assert res.status_code == 200
        names = {s["STUDY_NAME"] for s in res.get_json()}
        assert names == {"shared_all", "shared_user"}


def test_migration_backfills_only_unshared_studies(monkeypatch):
    import fyp.analysis.studies as studies

    defs = {
        "already_shared": {"USER_ACCESS": ["team"]},
        "empty": {"USER_ACCESS": []},
        "missing": {},
        "malformed": {"USER_ACCESS": "all"},
    }
    saved = {}
    from fyp.fyp_config import fyp_cf
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    monkeypatch.setattr(studies, "save_study_defs", lambda: saved.update(done=True))

    migrated = studies.migrate_user_access_defaults(["viewer", "team"])
    assert migrated == 3
    assert defs["already_shared"]["USER_ACCESS"] == ["team"]
    for name in ("empty", "missing", "malformed"):
        assert defs[name]["USER_ACCESS"] == ["viewer", "team"]
    assert saved.get("done")

    # Second run: nothing left to do, no save.
    saved.clear()
    assert studies.migrate_user_access_defaults(["viewer", "team"]) == 0
    assert not saved


def test_save_study_rejects_empty_collections(monkeypatch):
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app
    import web_interface.auth as auth_mod
    import web_interface.routes.management.studies as studies_mod
    from fyp.fyp_config import fyp_cf

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_USER:
            return User(username=_TEST_USER, role="manager", password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(auth_mod.role_manager, "get_role_permissions",
                        lambda role: ["tab.data_management.studies"])
    monkeypatch.setattr(studies_mod, "init_study_defs", lambda: None)
    monkeypatch.setitem(fyp_cf, "study_defs", {})

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = _TEST_USER
            sess["_fresh"] = True
        for payload in (
            {"STUDY_NAME": "newstudy"},
            {"STUDY_NAME": "newstudy", "SELECTED_COLLECTIONS": []},
            {"STUDY_NAME": "newstudy", "SELECTED_COLLECTIONS": "notalist"},
        ):
            res = client.post("/api/manage/studies/save", json=payload)
            assert res.status_code == 400, payload
            assert b"explicitly list its collections" in res.data.lower()
