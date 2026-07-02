"""Phase 2 integration tests for the /api/manage/schema endpoints.

Covers (mirrors plan §25-31):

  25. test_get_schema_returns_etag_and_rows
  26. test_post_schema_rejects_without_permission
  27. test_post_schema_rejects_stale_etag
  28. test_post_schema_rejects_invalid_payload
  29. test_post_schema_persists_and_reloads
  30. test_post_schema_validate_dry_runs
  31. test_rebuild_preview_lists_affected_studies (subsumed by hash_changed +
      affected_studies fields on the validate/save endpoints)

All tests share a Flask test client.  Auth is bypassed by stubbing the
user_manager's lookup so any request whose session carries the magic
user id resolves to a synthetic admin in memory — no JSON-store mutation.

Run:
    python tests/unit/test_var_schema_api.py
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd

from web_interface import security
from web_interface.auth import ROLE_ADMIN, User
from fyp.fyp_config import _var_schema_path, fyp_cf, load_var_schema
from fyp.recode_variables import compute_var_schema_hash
from fyp import var_presentation as vp


PASS = 0
FAIL = 0



def _check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")



# Synthetic admin user resolved by the stubbed user_manager lookup.
_TEST_ADMIN_USERNAME = "__phase2_test_admin__"
_TEST_VIEWER_USERNAME = "__phase2_test_viewer__"



def _admin_user() -> User:
    return User(
        username=_TEST_ADMIN_USERNAME,
        role=ROLE_ADMIN,
        password_hash="",
        approved=True,
    )



def _viewer_user() -> User:
    """A non-admin user holding no permissions — should hit 403 on schema routes."""
    u = User(
        username=_TEST_VIEWER_USERNAME,
        role="viewer",
        password_hash="",
        approved=True,
    )
    # Make sure can_access('tab.admin.schema') returns False.  permissions
    # are pulled via user_has_permission, which looks at a role's perm list.
    # A user whose role isn't 'admin' and whose role isn't registered as
    # holding the key will be denied — that's the default in this codebase.
    return u



def _install_auth_stub():
    """Replace user_manager.get_user with one that resolves our test users."""
    orig = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN_USERNAME:
            return _admin_user()
        if uid == _TEST_VIEWER_USERNAME:
            return _viewer_user()
        return orig(uid)

    security.user_manager.get_user = _fake_get
    return orig



def _restore_auth(orig):
    security.user_manager.get_user = orig



def _build_app():
    """Import the Flask app lazily after the auth stub is in place."""
    from web_interface.fyp_data_hub import app
    app.testing = True
    # The production app guards POSTs with Flask-WTF CSRF.  The browser
    # picks up a token from the rendered page; in tests we'd have to
    # synthesise one per request.  Turning the protection off for the
    # test app keeps the test focused on the endpoint logic.
    app.config["WTF_CSRF_ENABLED"] = False
    return app



def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True



# ------- backup live CSV so tests are non-destructive -------

def _snapshot_csv():
    src = _var_schema_path(fyp_cf)
    if not os.path.exists(src):
        return None
    fd, tmp = tempfile.mkstemp(prefix="phase2_schema_", suffix=".csv")
    os.close(fd)
    shutil.copy2(src, tmp)
    return (src, tmp)



def _restore_csv(snap):
    if snap is None:
        return
    src, tmp = snap
    shutil.copy2(tmp, src)
    os.remove(tmp)
    load_var_schema(fyp_cf, verbose=False)
    # Remove any backups created by tests
    backup_dir = os.path.dirname(src)
    for f in os.listdir(backup_dir):
        if f.startswith("var_schema_") and f.endswith(".csv") and f != "var_schema.csv":
            try:
                os.remove(os.path.join(backup_dir, f))
            except OSError:
                pass



# ------- backup the presentation store so tests are non-destructive -------

def _snapshot_presentation():
    return vp.load_presentation()



def _restore_presentation(snap):
    if snap is not None:
        vp.save_presentation(snap.get("surfaces", {}), updated_by="test-restore")
    load_var_schema(fyp_cf, verbose=False)



# ------- tests -------

def test_get_schema_returns_etag_and_rows(client):
    _login(client, _TEST_ADMIN_USERNAME)
    res = client.get("/api/manage/schema")
    if res.status_code != 200:
        _check("test_get_schema_returns_etag_and_rows", False,
               f"status={res.status_code} body={res.data[:120]}")
        return
    body = res.get_json()
    ok = (
        isinstance(body, dict)
        and isinstance(body.get("rows"), list)
        and len(body["rows"]) > 0
        and isinstance(body.get("etag"), str) and body["etag"]
        and isinstance(body.get("columns"), list)
        and isinstance(body.get("enums"), dict)
        # The presentation store (the only editable payload) rides along.
        and isinstance(body.get("presentation"), dict)
        and set(body.get("prio_columns", {})) == {"filter", "timeline", "viz", "display"}
        and body.get("current_hash", "").startswith("v2:")
    )
    _check("test_get_schema_returns_etag_and_rows", ok,
           f"keys={list(body.keys())}")



def test_post_schema_rejects_without_permission(client):
    _login(client, _TEST_VIEWER_USERNAME)
    res = client.post(
        "/api/manage/schema",
        data=json.dumps({"rows": [], "etag": "x"}),
        content_type="application/json",
    )
    _check("test_post_schema_rejects_without_permission",
           res.status_code in (401, 403),
           f"status={res.status_code}")



def test_post_schema_retired(client):
    """The legacy row-save and validate endpoints answer 410 (contract-owned)."""
    _login(client, _TEST_ADMIN_USERNAME)
    save_res = client.post(
        "/api/manage/schema",
        data=json.dumps({"rows": [], "etag": "x"}),
        content_type="application/json",
    )
    val_res = client.post(
        "/api/manage/schema/validate",
        data=json.dumps({"rows": []}),
        content_type="application/json",
    )
    _check("test_post_schema_retired",
           save_res.status_code == 410 and val_res.status_code == 410,
           f"save={save_res.status_code} validate={val_res.status_code}")



def test_post_presentation_rejects_stale_etag(client):
    snap = _snapshot_presentation()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        res = client.post(
            "/api/manage/presentation",
            data=json.dumps({"surfaces": body["presentation"], "etag": "stale-etag-value"}),
            content_type="application/json",
        )
        _check("test_post_presentation_rejects_stale_etag",
               res.status_code == 409,
               f"status={res.status_code} body={res.data[:200]}")
    finally:
        _restore_presentation(snap)



def test_post_presentation_rejects_unknown_variable(client):
    snap = _snapshot_presentation()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        surfaces = dict(body["presentation"])
        surfaces["filter"] = list(surfaces.get("filter", [])) + ["no_such_variable_xyz"]
        res = client.post(
            "/api/manage/presentation",
            data=json.dumps({"surfaces": surfaces, "etag": body["etag"]}),
            content_type="application/json",
        )
        resp = res.get_json() or {}
        ok = res.status_code == 400 and "no_such_variable_xyz" in (resp.get("unknown") or [])
        _check("test_post_presentation_rejects_unknown_variable", ok,
               f"status={res.status_code} body={res.data[:200]}")
    finally:
        _restore_presentation(snap)



def test_post_presentation_persists_and_reloads(client):
    snap = _snapshot_presentation()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        surfaces = {k: list(v) for k, v in body["presentation"].items()}
        # Toggle one variable's filter membership.
        probe = str(fyp_cf["var_schema"]["variable_name"].iloc[0])
        if probe in surfaces.get("filter", []):
            surfaces["filter"] = [n for n in surfaces["filter"] if n != probe]
            expect_on = False
        else:
            surfaces["filter"] = surfaces.get("filter", []) + [probe]
            expect_on = True
        before_hash = compute_var_schema_hash()
        res = client.post(
            "/api/manage/presentation",
            data=json.dumps({"surfaces": surfaces, "etag": body["etag"]}),
            content_type="application/json",
        )
        if res.status_code != 200:
            _check("test_post_presentation_persists_and_reloads", False,
                   f"status={res.status_code} body={res.data[:200]}")
            return
        vs = fyp_cf["var_schema"]
        cell = vs.loc[vs["variable_name"] == probe, "web_filter_prio"].iloc[0]
        is_on = str(cell).strip() not in ("", "<NA>", "None", "nan")
        resp = res.get_json()
        ok = (is_on == expect_on
              and resp.get("hash_changed") is False
              and compute_var_schema_hash() == before_hash
              and isinstance(resp.get("etag"), str))
        _check("test_post_presentation_persists_and_reloads", ok,
               f"probe={probe!r} cell={cell!r} expect_on={expect_on} resp={resp}")
    finally:
        _restore_presentation(snap)



# ------- driver -------

def main():
    print("\nRunning Phase 2 API integration tests...\n")
    orig = _install_auth_stub()
    try:
        app = _build_app()
        with app.test_client() as client:
            tests = [
                test_get_schema_returns_etag_and_rows,
                test_post_schema_rejects_without_permission,
                test_post_schema_retired,
                test_post_presentation_rejects_stale_etag,
                test_post_presentation_rejects_unknown_variable,
                test_post_presentation_persists_and_reloads,
            ]
            for t in tests:
                try:
                    t(client)
                except Exception as e:
                    global FAIL
                    FAIL += 1
                    print(f"  ERROR {t.__name__}  ({e})")
                    traceback.print_exc()
    finally:
        _restore_auth(orig)
        # Clean any activity-log files the synthetic users created.
        from fyp import data_io
        for username in (_TEST_ADMIN_USERNAME, _TEST_VIEWER_USERNAME):
            fname = f"{username}_log.json"
            try:
                if data_io.exists(storage_location="users", filename=fname):
                    data_io.remove(storage_location="users", filename=fname)
            except Exception:
                pass
    print(f"\nSummary: {PASS} passed, {FAIL} failed\n")
    return 0 if FAIL == 0 else 1



if __name__ == "__main__":
    sys.exit(main())
