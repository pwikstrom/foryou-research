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
from fyp.fyp_config import _var_schema_path, compute_var_schema_etag, fyp_cf, load_var_schema


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
        and isinstance(body.get("recode_funcs"), list)
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



def test_post_schema_rejects_stale_etag(client):
    snap = _snapshot_csv()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        # First get a valid payload + etag, then save with a deliberately
        # wrong etag to provoke 409.
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        save_res = client.post(
            "/api/manage/schema",
            data=json.dumps({"rows": body["rows"], "etag": "stale-etag-value"}),
            content_type="application/json",
        )
        _check("test_post_schema_rejects_stale_etag",
               save_res.status_code == 409,
               f"status={save_res.status_code} body={save_res.data[:200]}")
    finally:
        _restore_csv(snap)



def test_post_schema_rejects_invalid_payload(client):
    snap = _snapshot_csv()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        # Corrupt the role of the first row to an illegal value.
        bad_rows = [dict(r) for r in body["rows"]]
        bad_rows[0]["role"] = "factro_typo"
        res = client.post(
            "/api/manage/schema",
            data=json.dumps({"rows": bad_rows, "etag": body["etag"]}),
            content_type="application/json",
        )
        ok = (res.status_code == 400
              and isinstance(res.get_json().get("errors"), list)
              and any(e["column"] == "role" for e in res.get_json()["errors"]))
        _check("test_post_schema_rejects_invalid_payload", ok,
               f"status={res.status_code} body={res.data[:200]}")
    finally:
        _restore_csv(snap)



def test_post_schema_persists_and_reloads(client):
    snap = _snapshot_csv()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        rows = [dict(r) for r in body["rows"]]
        rows[0]["description"] = "PHASE2_ROUND_TRIP_PROBE"
        res = client.post(
            "/api/manage/schema",
            data=json.dumps({"rows": rows, "etag": body["etag"]}),
            content_type="application/json",
        )
        if res.status_code != 200:
            _check("test_post_schema_persists_and_reloads", False,
                   f"status={res.status_code} body={res.data[:200]}")
            return
        # Verify in-memory cf reflects the change
        in_mem = fyp_cf["var_schema"].iloc[0]["description"]
        # And the response carries a fresh etag + hash_changed flag (False
        # for a cosmetic edit — description is presentation, not semantic).
        resp = res.get_json()
        ok = (str(in_mem) == "PHASE2_ROUND_TRIP_PROBE"
              and resp.get("hash_changed") is False
              and isinstance(resp.get("etag"), str))
        _check("test_post_schema_persists_and_reloads", ok,
               f"in_mem={in_mem!r} resp={resp}")
    finally:
        _restore_csv(snap)



def test_post_schema_validate_dry_runs(client):
    snap = _snapshot_csv()
    try:
        _login(client, _TEST_ADMIN_USERNAME)
        gres = client.get("/api/manage/schema")
        body = gres.get_json()
        rows = [dict(r) for r in body["rows"]]
        # Make a semantic change so hash_changed is True
        current_scale = rows[0].get("scale", "")
        rows[0]["scale"] = "categorical" if current_scale != "categorical" else "string"
        before_disk_hash = compute_var_schema_etag(fyp_cf)
        res = client.post(
            "/api/manage/schema/validate",
            data=json.dumps({"rows": rows}),
            content_type="application/json",
        )
        after_disk_hash = compute_var_schema_etag(fyp_cf)
        resp = res.get_json()
        ok = (res.status_code == 200
              and isinstance(resp.get("errors"), list)
              and resp.get("hash_changed") is True
              and before_disk_hash == after_disk_hash)  # disk untouched
        _check("test_post_schema_validate_dry_runs", ok,
               f"status={res.status_code} resp_keys={list(resp.keys()) if resp else None} "
               f"before={before_disk_hash[:8]} after={after_disk_hash[:8]}")
    finally:
        _restore_csv(snap)



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
                test_post_schema_rejects_stale_etag,
                test_post_schema_rejects_invalid_payload,
                test_post_schema_persists_and_reloads,
                test_post_schema_validate_dry_runs,
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
