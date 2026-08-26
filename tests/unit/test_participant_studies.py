"""Auto-managed participant studies ("Just Me" / "Everyone & Me").

Covers the lifecycle service (create / grow / shrink-to-nothing), the
endpoint guards that keep the pair pattern-only (save/rename/delete refuse
system studies and the reserved ``__`` namespace), the boot migration and
default-study picker exclusions, and the read-side composition: owner-only
visibility, the Everyone & Me listing gates, and the collection/date-window
union helpers.
"""

import pytest

_OWNER = "p-9@example.org"
_OTHER = "someone-else@example.org"


@pytest.fixture
def svc(monkeypatch):
    """participant_studies wired to an in-memory defs dict + ownership map."""
    from fyp.fyp_config import fyp_cf
    import web_interface.collection_accounts as accounts
    import web_interface.services.participant_studies as ps

    defs: dict = {}
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    monkeypatch.setattr(ps, "init_study_defs", lambda: None)
    saved = {"count": 0}
    monkeypatch.setattr(ps, "save_study_defs", lambda: saved.__setitem__("count", saved["count"] + 1))

    owners: dict[str, str | None] = {}
    monkeypatch.setattr(accounts, "collections_for_user",
                        lambda uid, fresh=False: sorted(c for c, u in owners.items() if u == uid))
    monkeypatch.setattr(accounts, "load_owner_map", lambda fresh=False: dict(owners))

    removed: list[str] = []
    monkeypatch.setattr(ps.data_io, "remove",
                        lambda storage_location, filename: removed.append(filename))

    # Fake owners have no real account records; treat them as logged-in by
    # default so the lifecycle tests exercise the normal path. The dormancy
    # test overrides this.
    monkeypatch.setattr(ps, "_account_has_logged_in", lambda username: True)

    ps._test_state = {"defs": defs, "owners": owners, "removed": removed, "saved": saved}
    return ps


def test_pair_created_updated_and_removed(svc):
    defs = svc._test_state["defs"]
    owners = svc._test_state["owners"]

    # No collections: nothing happens.
    assert svc.ensure_participant_studies(_OWNER) == {
        "me_changed": False, "removed": False, "collections": 0}
    assert defs == {}

    # First collection: the pair appears, Just Me needs a build.
    owners["c1"] = _OWNER
    result = svc.ensure_participant_studies(_OWNER)
    assert result["me_changed"] and result["collections"] == 1
    me = defs[f"__me__{_OWNER}"]
    plus = defs[f"__me_plus__{_OWNER}"]
    assert me["SELECTED_COLLECTIONS"] == ["c1"]
    assert me["USER_ACCESS"] == [_OWNER]
    assert me["SYSTEM"] == "participant" and me["OWNER"] == _OWNER
    assert plus["COMPOSE"] == {"base": "__default__", "overlay": "self"}
    assert plus["USER_ACCESS"] == [_OWNER]

    # Idempotent: a second run changes nothing.
    saved_before = svc._test_state["saved"]["count"]
    result = svc.ensure_participant_studies(_OWNER)
    assert not result["me_changed"]
    assert svc._test_state["saved"]["count"] == saved_before

    # Second collection: collections grow, refresh needed, stats preserved.
    me["stats"] = {"unique_videos": 7}
    owners["c2"] = _OWNER
    result = svc.ensure_participant_studies(_OWNER)
    assert result["me_changed"]
    assert defs[f"__me__{_OWNER}"]["SELECTED_COLLECTIONS"] == ["c1", "c2"]
    assert defs[f"__me__{_OWNER}"]["stats"] == {"unique_videos": 7}

    # All collections gone: the pair and the Just Me artifacts disappear.
    owners.clear()
    result = svc.ensure_participant_studies(_OWNER)
    assert result["removed"]
    assert defs == {}
    assert f"__me__{_OWNER}_recoded.parquet" in svc._test_state["removed"]
    # Composed study never had artifacts, so none are touched for it.
    assert not any(f.startswith(f"__me_plus__{_OWNER}") for f in svc._test_state["removed"])


def test_sync_for_cids_targets_owners_and_dispatches(svc, monkeypatch):
    owners = svc._test_state["owners"]
    owners["c1"] = _OWNER
    owners["c2"] = _OTHER

    dispatched: list[str] = []
    monkeypatch.setattr(svc, "dispatch_me_refresh",
                        lambda username, wait=False, log=None: dispatched.append(username))

    affected = svc.sync_for_cids(["c1"])
    assert affected == [_OWNER]
    assert dispatched == [_OWNER]

    # Previous owner passed explicitly (their id no longer maps from the cid).
    dispatched.clear()
    owners["c1"] = _OTHER
    affected = svc.sync_for_cids(["c1"], usernames=[_OWNER])
    assert set(affected) == {_OWNER, _OTHER}
    # _OWNER now owns nothing: pair removed, no refresh; _OTHER gained c1.
    assert dispatched == [_OTHER]


def test_dormant_accounts_get_no_pair_until_login(svc, monkeypatch):
    """Owners who never logged in are skipped by every sync path; the pair is
    created lazily by the login-time check, and an EXISTING pair keeps being
    reconciled even for a dormant account (grow/shrink/remove still work)."""
    defs = svc._test_state["defs"]
    owners = svc._test_state["owners"]
    owners["c1"] = _OWNER

    logged_in = {"value": False}
    monkeypatch.setattr(svc, "_account_has_logged_in",
                        lambda username: logged_in["value"])
    dispatched: list[str] = []
    monkeypatch.setattr(svc, "dispatch_me_refresh",
                        lambda username, wait=False, log=None: dispatched.append(username))

    # Ingest-style sync: dormant owner ⇒ nothing created, nothing dispatched.
    assert svc.sync_for_cids(["c1"]) == []
    assert defs == {} and dispatched == []

    # First login: the pair appears and the build is dispatched.
    logged_in["value"] = True
    svc.ensure_on_login(_OWNER)
    assert f"__me__{_OWNER}" in defs and f"__me_plus__{_OWNER}" in defs
    assert dispatched == [_OWNER]

    # Existing pair is reconciled even if the account later reads as dormant
    # (fail-closed gate must never freeze an already-provisioned pair).
    logged_in["value"] = False
    owners["c2"] = _OWNER
    dispatched.clear()
    assert svc.sync_for_cids(["c2"]) == [_OWNER]
    assert defs[f"__me__{_OWNER}"]["SELECTED_COLLECTIONS"] == ["c1", "c2"]
    assert dispatched == [_OWNER]


def test_migration_skips_system_studies(monkeypatch):
    import fyp.analysis.studies as studies

    defs = {
        "regular_unshared": {"USER_ACCESS": []},
        f"__me__{_OWNER}": {"SYSTEM": "participant", "USER_ACCESS": [_OWNER]},
        # Even a system def with a broken empty list must not be opened up.
        f"__me_plus__{_OWNER}": {"SYSTEM": "participant", "COMPOSE": {"base": "__default__"},
                                 "USER_ACCESS": []},
    }
    monkeypatch.setitem(studies._cf(), "study_defs", defs)
    monkeypatch.setattr(studies, "save_study_defs", lambda: None)

    migrated = studies.migrate_user_access_defaults(["viewer", "team"])
    assert migrated == 1
    assert defs["regular_unshared"]["USER_ACCESS"] == ["viewer", "team"]
    assert defs[f"__me_plus__{_OWNER}"]["USER_ACCESS"] == []


def test_default_study_picker_excludes_system_studies(monkeypatch):
    from fyp.fyp_config import fyp_cf
    import fyp.analysis.studies as fyp_studies
    from web_interface import admin_settings

    monkeypatch.setitem(fyp_cf, "study_defs", {
        "main_study": {"USER_ACCESS": ["all"]},
        f"__me__{_OWNER}": {"SYSTEM": "participant", "USER_ACCESS": [_OWNER]},
    })
    monkeypatch.setattr(fyp_studies, "init_study_defs", lambda: None)
    assert admin_settings.study_names() == ["main_study"]
    assert admin_settings.validate_setting_value("default_study", f"__me__{_OWNER}") is not None
    assert admin_settings.validate_setting_value("default_study", "main_study") is None


# ---------------------------------------------------------------------------
# Read side: visibility + composition
# ---------------------------------------------------------------------------


@pytest.fixture
def participant_defs(monkeypatch):
    """Defs with a default study and one participant's pair; storage faked so
    the base and Just Me parquets 'exist' and the composed study is listable."""
    from fyp.fyp_config import fyp_cf
    from web_interface import admin_settings
    from web_interface.services import user_variables

    defs = {
        "main_study": {"USER_ACCESS": ["all"], "SELECTED_COLLECTIONS": ["c1", "c9"],
                       "START_DATE": "2026-01-01", "END_DATE": "2026-03-31",
                       "stats": {"unique_videos": 100, "total_activities": 1000}},
        f"__me__{_OWNER}": {"SYSTEM": "participant", "OWNER": _OWNER,
                            "DISPLAY_NAME": "Just Me",
                            "SELECTED_COLLECTIONS": ["c1", "c2"],
                            "USER_ACCESS": [_OWNER],
                            "stats": {"unique_videos": 9, "total_activities": 40}},
        f"__me_plus__{_OWNER}": {"SYSTEM": "participant", "OWNER": _OWNER,
                                 "DISPLAY_NAME": "Everyone & Me",
                                 "COMPOSE": {"base": "__default__", "overlay": "self"},
                                 "SELECTED_COLLECTIONS": ["c1", "c2"],
                                 "USER_ACCESS": [_OWNER]},
    }
    monkeypatch.setitem(fyp_cf, "study_defs", defs)
    monkeypatch.setattr(admin_settings, "get_default_study", lambda: "main_study")

    cache_files = {
        "main_study_recoded.parquet",
        f"__me__{_OWNER}_recoded.parquet",
    }
    monkeypatch.setattr(user_variables.data_io, "listdir", lambda **kw: sorted(cache_files))
    monkeypatch.setattr(user_variables.data_io, "exists",
                        lambda **kw: kw.get("filename") in cache_files)
    return defs, cache_files


def test_owner_sees_pair_others_do_not(participant_defs):
    from web_interface.services.user_variables import get_accessible_studies

    owner_names = get_accessible_studies(username=_OWNER, role="viewer", is_admin=False)
    assert owner_names == [f"__me__{_OWNER}", f"__me_plus__{_OWNER}", "main_study"]

    other_names = get_accessible_studies(username=_OTHER, role="viewer", is_admin=False)
    assert other_names == ["main_study"]


def test_composed_listing_requires_both_sides(participant_defs):
    from web_interface.services.user_variables import get_accessible_studies

    _defs, cache_files = participant_defs
    cache_files.discard(f"__me__{_OWNER}_recoded.parquet")
    names = get_accessible_studies(username=_OWNER, role="viewer", is_admin=False)
    assert names == ["main_study"]  # no overlay ⇒ neither Just Me nor Everyone & Me


def test_composed_stats_and_display_names(participant_defs):
    from web_interface.services.user_variables import get_accessible_studies

    entries = get_accessible_studies(username=_OWNER, role="viewer",
                                     is_admin=False, include_stats=True)
    by_name = {e["name"]: e for e in entries}
    plus = by_name[f"__me_plus__{_OWNER}"]
    assert plus["display_name"] == "Everyone & Me"
    assert plus["system"] is True
    assert plus["stats"]["unique_videos"] == 109        # base 100 + overlay 9
    assert plus["stats"]["total_activities"] == 1040
    assert by_name[f"__me__{_OWNER}"]["display_name"] == "Just Me"

    # Anyone else who can see a system study gets the owner spelled out.
    admin_entries = get_accessible_studies(username="admin", role="admin",
                                           is_admin=True, include_stats=True)
    admin_by_name = {e["name"]: e for e in admin_entries}
    assert admin_by_name[f"__me__{_OWNER}"]["display_name"] == f"Just Me — {_OWNER}"


def test_compose_resolution_and_unions(participant_defs, monkeypatch):
    import pandas as pd
    from web_interface.services import study_data

    plus = f"__me_plus__{_OWNER}"
    assert study_data.resolve_compose(plus) == ("main_study", f"__me__{_OWNER}")
    assert study_data.resolve_artifact_study(plus) == "main_study"
    assert study_data.resolve_artifact_study("main_study") == "main_study"

    # Collections: the owner's first, then the base's (deduped: c1 is both).
    cids = [d["collection_id"] for d in study_data.get_study_collections(plus)]
    assert cids == ["c1", "c2", "c9"]

    # Date window: envelope of the base's window and the (wide) own range.
    start, end = study_data.get_study_date_window(plus)
    base_start, base_end = study_data.get_study_date_window("main_study")
    assert start <= base_start and end >= base_end
    assert start == pd.Timestamp("1970-01-01")


def test_save_rename_delete_refuse_system_studies(participant_defs, monkeypatch):
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app
    import web_interface.auth as auth_mod
    import web_interface.routes.management.studies as studies_mod

    manager = "__ps_manager__"
    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == manager:
            return User(username=manager, role="team", password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(auth_mod.role_manager, "get_role_permissions",
                        lambda role: ["tab.data_management.studies"])
    monkeypatch.setattr(studies_mod, "init_study_defs", lambda: None)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = manager
            sess["_fresh"] = True

        res = client.post("/api/manage/studies/save",
                          json={"STUDY_NAME": "__new_reserved"})
        assert res.status_code == 400 and "reserved" in res.get_json()["error"]

        res = client.post("/api/manage/studies/save",
                          json={"STUDY_NAME": f"__me__{_OWNER}",
                                "SELECTED_COLLECTIONS": ["c1"]})
        assert res.status_code == 400

        res = client.post("/api/manage/studies/rename",
                          json={"OLD_NAME": f"__me__{_OWNER}", "NEW_NAME": "renamed"})
        assert res.status_code == 400 and "renamed" not in (
            studies_mod.fyp_cf.get("study_defs") or {})

        res = client.post("/api/manage/studies/rename",
                          json={"OLD_NAME": "main_study", "NEW_NAME": "__sneaky"})
        assert res.status_code == 400

        # Non-admin manager cannot delete a system study; the def survives.
        res = client.post("/api/manage/studies/delete",
                          json={"STUDY_NAME": f"__me__{_OWNER}"})
        assert res.status_code == 403
        assert f"__me__{_OWNER}" in studies_mod.fyp_cf["study_defs"]
