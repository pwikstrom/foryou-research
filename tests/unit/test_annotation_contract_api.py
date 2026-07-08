"""Integration tests for the /api/manage/annotation-contract endpoints.

Covers the runtime-editable annotation contract's web layer:

  * GET status (source baked, current version present)
  * dry-run upload → valid + impact (metadata-only vs prompt/schema-affecting)
  * invalid TOML → 400 with errors
  * permission gate (viewer → 403)
  * confirm upload → source becomes runtime; revert → source back to baked

Shares the Flask-test-client + auth-stub harness of ``test_var_schema_api.py``.
Writes to the local ``users`` storage but snapshots and fully restores it, so
the test is non-destructive whether or not a runtime contract already exists.

Run:
    python tests/unit/test_annotation_contract_api.py
"""

import copy
import json
import sys
import tomllib
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from web_interface import security
from web_interface.auth import ROLE_ADMIN, User
from fyp.fyp_config import fyp_cf, load_var_schema
from fyp import annotation_contract as ac
from fyp import data_io

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


_TEST_ADMIN = "__ac_test_admin__"
_TEST_VIEWER = "__ac_test_viewer__"


def _install_auth_stub():
    orig = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role="viewer", password_hash="", approved=True)
        return orig(uid)

    security.user_manager.get_user = _fake_get
    return orig


def _build_app():
    from web_interface.fyp_data_hub import app
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True


def _snapshot_runtime():
    """Capture the current runtime-contract storage state for restoration."""
    text = None
    meta = None
    if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
        text = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
    if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME):
        meta = data_io.load_json(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)
    return text, meta


def _restore_runtime(snap):
    text, meta = snap
    # Remove current runtime files + any test-created backups.
    for fname in (ac.RUNTIME_FILENAME, ac.RUNTIME_META_FILENAME):
        try:
            if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=fname):
                data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=fname)
        except Exception:
            pass
    try:
        for fname in data_io.listdir(storage_location=ac.RUNTIME_LOCATION):
            if fname.startswith(ac.BACKUP_PREFIX):
                data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=fname)
    except Exception:
        pass
    # Restore any pre-existing runtime contract verbatim.
    if text is not None:
        data_io.save_text(text, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
    if meta is not None:
        data_io.save_json(data=meta, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)
    ac.refresh_runtime_contract()
    load_var_schema(fyp_cf, verbose=False)


# ------- tests -------

def test_get_status(client):
    _login(client, _TEST_ADMIN)
    res = client.get("/api/manage/annotation-contract")
    body = res.get_json() or {}
    ok = (res.status_code == 200
          and body.get("source") in ("baked", "runtime")
          and isinstance(body.get("current_version"), str)
          and body["current_version"].startswith("av_"))
    _check("test_get_status", ok, f"status={res.status_code} body={body}")


def test_permission_gate(client):
    _login(client, _TEST_VIEWER)
    res = client.post("/api/manage/annotation-contract",
                      data={"text": ac._read_baked_text()})
    _check("test_permission_gate", res.status_code in (401, 403), f"status={res.status_code}")


def test_dry_run_metadata_only(client):
    _login(client, _TEST_ADMIN)
    # Identical contract text → same av_ → metadata_only True.
    res = client.post("/api/manage/annotation-contract",
                      data={"text": ac._read_baked_text()})
    body = res.get_json() or {}
    ok = (res.status_code == 200
          and body.get("valid") is True
          and body.get("confirm_required") is True
          and body.get("impact", {}).get("metadata_only") is True
          and body["impact"].get("version_changed") is False)
    _check("test_dry_run_metadata_only", ok, f"status={res.status_code} body={body}")


def test_dry_run_prompt_change(client):
    # A desc edit changes the generated prompt (and schema description) → the
    # impact helper must flag a new version. Exercised at the helper level since
    # a mid-contract edit is awkward to express as raw TOML over HTTP.
    edited = copy.deepcopy(tomllib.loads(ac._read_baked_text()))
    f = edited["fields"][0]
    f["desc"] = (f.get("desc") or "") + " EXTRA PROMPT WORDS FOR TEST"
    from web_interface.routes.management_routes import _annotation_contract_impact
    impact = _annotation_contract_impact(edited)
    ok = impact["version_changed"] is True and impact["metadata_only"] is False
    _check("test_dry_run_prompt_change", ok, f"impact={impact}")


def test_invalid_toml_rejected(client):
    _login(client, _TEST_ADMIN)
    res = client.post("/api/manage/annotation-contract",
                      data={"text": "this is = = not valid toml [[["})
    body = res.get_json() or {}
    ok = res.status_code == 400 and bool(body.get("errors"))
    _check("test_invalid_toml_rejected", ok, f"status={res.status_code} body={body}")


def test_confirm_and_revert(client):
    _login(client, _TEST_ADMIN)
    # Confirm a metadata-only change (baked text + a trailing newline) so the
    # source flips to runtime without minting a new version.
    upload_text = ac._read_baked_text() + "\n# runtime upload test\n"
    res = client.post("/api/manage/annotation-contract",
                      data={"text": upload_text, "confirm": "1"})
    body = res.get_json() or {}
    if not (res.status_code == 200 and body.get("ok") and body.get("source") == "runtime"):
        _check("test_confirm_and_revert", False, f"upload status={res.status_code} body={body}")
        return
    # Status now reports runtime.
    gres = client.get("/api/manage/annotation-contract")
    gbody = gres.get_json() or {}
    runtime_ok = gbody.get("source") == "runtime"
    # Revert → back to baked.
    rres = client.post("/api/manage/annotation-contract/revert")
    rbody = rres.get_json() or {}
    revert_ok = rres.status_code == 200 and rbody.get("source") == "baked"
    fres = client.get("/api/manage/annotation-contract")
    baked_again = (fres.get_json() or {}).get("source") == "baked"
    _check("test_confirm_and_revert", runtime_ok and revert_ok and baked_again,
           f"runtime_ok={runtime_ok} revert_ok={revert_ok} baked_again={baked_again}")


def main():
    print("\nRunning annotation-contract API tests...\n")
    orig = _install_auth_stub()
    snap = _snapshot_runtime()
    try:
        app = _build_app()
        with app.test_client() as client:
            tests = [
                test_get_status,
                test_permission_gate,
                test_dry_run_metadata_only,
                test_dry_run_prompt_change,
                test_invalid_toml_rejected,
                test_confirm_and_revert,
            ]
            for t in tests:
                try:
                    t(client)
                except Exception as e:
                    global FAIL
                    FAIL += 1
                    print(f"  ERROR {t.__name__}  ({e})")
                    import traceback
                    traceback.print_exc()
    finally:
        _restore_runtime(snap)
        security.user_manager.get_user = orig
        for username in (_TEST_ADMIN, _TEST_VIEWER):
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
