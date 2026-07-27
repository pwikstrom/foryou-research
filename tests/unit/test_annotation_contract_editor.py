"""Tests for the annotation-contract form editor's backend surface.

Covers the serialization + spec helpers in ``fyp.annotation_contract``
(tomlkit round-trip with comment preservation, fresh regeneration,
``parse_key_spec``/``format_key_spec``) and the editor's web layer
(GET /parsed hydration, POST /preview rendering, JSON-contract dry-run
equivalence with a text upload, confirm + etag guard).

Shares the Flask-test-client + auth-stub harness of
``test_annotation_contract_api.py`` and is equally non-destructive (runtime
storage is snapshotted and restored).

Run:
    python tests/unit/test_annotation_contract_editor.py
"""

import copy
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


def _comment_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.lstrip().startswith("#")]


_TEST_ADMIN = "__ace_test_admin__"


def _install_auth_stub():
    orig = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
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
    text = None
    meta = None
    if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
        text = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
    if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME):
        meta = data_io.load_json(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)
    return text, meta


def _restore_runtime(snap):
    text, meta = snap
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
    if text is not None:
        data_io.save_text(text, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
    if meta is not None:
        data_io.save_json(data=meta, storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_META_FILENAME)
    ac.refresh_runtime_contract()
    load_var_schema(fyp_cf, verbose=False)


# ------- serialization tests (no web layer) -------

def test_noop_roundtrip_byte_identical():
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)
    out = ac.serialize_contract(contract, base_text=baked)
    _check("test_noop_roundtrip_byte_identical", out == baked)


def test_scalar_edit_keeps_comments():
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)
    edited = copy.deepcopy(contract)
    edited["prompt"]["footer"] = "CHANGED FOOTER FOR TEST"
    out = ac.serialize_contract(edited, base_text=baked)
    ok = (tomllib.loads(out) == edited
          and _comment_lines(out) == _comment_lines(baked))
    _check("test_scalar_edit_keeps_comments", ok)


def test_field_add_remove_reparses():
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)

    added = copy.deepcopy(contract)
    added["fields"].append({"name": "editor_test_field",
                            "desc": "test", "scale": "text", "display_name": "Editor test"})
    out_add = ac.serialize_contract(added, base_text=baked)

    removed = copy.deepcopy(contract)
    removed["fields"] = [f for f in removed["fields"] if f["name"] != "aigc"]
    out_rm = ac.serialize_contract(removed, base_text=baked)

    ok = (tomllib.loads(out_add) == added and tomllib.loads(out_rm) == removed
          and len(_comment_lines(out_add)) == len(_comment_lines(baked))
          and len(_comment_lines(out_rm)) == len(_comment_lines(baked)))
    _check("test_field_add_remove_reparses", ok)


def test_enum_and_keys_edits_reparse():
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)
    edited = copy.deepcopy(contract)
    edited["enums"]["yes_no"] = ["Yes", "No", "Unclear", "N/A"]
    faces = next(f for f in edited["fields"] if f["name"] == "faces")
    faces["keys"]["gender"]["display_name"] = "CHANGED DISPLAY NAME"
    out = ac.serialize_contract(edited, base_text=baked)
    _check("test_enum_and_keys_edits_reparse", tomllib.loads(out) == edited)


def test_fresh_regeneration():
    contract = tomllib.loads(ac._read_baked_text())
    out = ac.serialize_contract(contract)   # no base text
    ok = (tomllib.loads(out) == contract
          and "annotation_contract_help.toml" in out
          and out.lstrip().startswith("#"))
    _check("test_fresh_regeneration", ok)


def test_unknown_key_passthrough():
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)
    edited = copy.deepcopy(contract)
    edited["future_extension"] = {"some_key": "some value"}
    out = ac.serialize_contract(edited, base_text=baked)
    _check("test_unknown_key_passthrough", tomllib.loads(out) == edited)


def test_null_value_raises():
    baked = ac._read_baked_text()
    edited = tomllib.loads(baked)
    edited["prompt"]["footer"] = None
    try:
        ac.serialize_contract(edited, base_text=baked)
        _check("test_null_value_raises", False, "no ValueError")
    except ValueError:
        _check("test_null_value_raises", True)


def test_key_spec_roundtrip():
    contract = tomllib.loads(ac._read_baked_text())
    specs = []
    for f in contract["fields"]:
        for spec in f.get("keys", {}).values():
            specs.append(spec["spec"] if isinstance(spec, dict) else spec)
    specs += ["enum:gender", "list:", "int:", "int(0,100):", "free text description"]
    bad = [s for s in specs if ac.format_key_spec(ac.parse_key_spec(s)) != s]
    _check("test_key_spec_roundtrip", not bad, f"bad={bad}")


def test_help_texts_load():
    help_map = ac.contract_help()
    needed = {"overview", "fields", "fields.keys", "fields.array", "enums", "recode.drop"}
    _check("test_help_texts_load", needed <= set(help_map), f"missing={needed - set(help_map)}")


# ------- web-layer tests -------

def test_parsed_endpoint(client):
    _login(client, _TEST_ADMIN)
    res = client.get("/api/manage/annotation-contract/parsed")
    body = res.get_json() or {}
    ok = (res.status_code == 200
          and isinstance(body.get("contract"), dict)
          and body["contract"].get("fields")
          and isinstance(body.get("help"), dict)
          and body.get("etag")
          and isinstance(body.get("roles"), list) and body["roles"]
          and isinstance(body.get("scales"), list) and body["scales"])
    _check("test_parsed_endpoint", ok, f"status={res.status_code}")


def test_preview_endpoint(client):
    _login(client, _TEST_ADMIN)
    contract = tomllib.loads(ac._read_baked_text())
    res = client.post("/api/manage/annotation-contract/preview", json={"contract": contract})
    body = res.get_json() or {}
    from fyp import annotation_schema as sch
    ok = (res.status_code == 200 and body.get("valid") is True
          and body.get("prompt") == sch.build_prompt(contract)
          and body.get("schema") == sch.get_annotation_json_schema(contract))
    # Invalid candidate → 200 with errors (debounce-friendly).
    bad = copy.deepcopy(contract)
    bad["fields"][0].pop("name")
    res2 = client.post("/api/manage/annotation-contract/preview", json={"contract": bad})
    body2 = res2.get_json() or {}
    ok2 = res2.status_code == 200 and body2.get("valid") is False and body2.get("errors")
    _check("test_preview_endpoint", bool(ok and ok2),
           f"valid_ok={ok} invalid_ok={ok2}")


def test_json_dry_run_matches_text(client):
    _login(client, _TEST_ADMIN)
    baked = ac._read_baked_text()
    contract = tomllib.loads(baked)
    res_json = client.post("/api/manage/annotation-contract", json={"contract": contract})
    res_text = client.post("/api/manage/annotation-contract", data={"text": baked})
    bj, bt = res_json.get_json() or {}, res_text.get_json() or {}
    ok = (res_json.status_code == 200 and res_text.status_code == 200
          and bj.get("valid") is True and bj.get("confirm_required") is True
          and bj.get("impact") == bt.get("impact")
          and bj["impact"].get("metadata_only") is True)
    _check("test_json_dry_run_matches_text", ok,
           f"json={res_json.status_code} text={res_text.status_code}")


def test_json_confirm_and_etag_guard(client):
    _login(client, _TEST_ADMIN)
    contract = tomllib.loads(ac._read_baked_text())
    edited = copy.deepcopy(contract)
    edited["recode"]["drop"]["objects"] = ["woman", "man", "person", "people", "editor_test"]

    # Stale etag → 409.
    res409 = client.post("/api/manage/annotation-contract",
                         json={"contract": edited, "confirm": True, "expected_etag": "stale:deadbeef"})
    guard_ok = res409.status_code == 409

    # Correct etag → activates; drop-word edits are metadata-only.
    etag = ac.contract_status().get("etag")
    res = client.post("/api/manage/annotation-contract",
                      json={"contract": edited, "confirm": True, "expected_etag": etag})
    body = res.get_json() or {}
    confirm_ok = (res.status_code == 200 and body.get("ok")
                  and body.get("source") == "runtime"
                  and body.get("impact", {}).get("metadata_only") is True)
    # The stored runtime text reparses to the edited contract and keeps comments.
    stored = data_io.load_text(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME)
    stored_ok = (stored is not None and tomllib.loads(stored) == edited
                 and len(_comment_lines(stored)) > 0)
    _check("test_json_confirm_and_etag_guard", guard_ok and confirm_ok and stored_ok,
           f"guard={guard_ok} confirm={confirm_ok} stored={stored_ok}")


def main():
    print("\nRunning annotation-contract editor tests...\n")

    for t in (test_noop_roundtrip_byte_identical, test_scalar_edit_keeps_comments,
              test_field_add_remove_reparses, test_enum_and_keys_edits_reparse,
              test_fresh_regeneration, test_unknown_key_passthrough,
              test_null_value_raises, test_key_spec_roundtrip, test_help_texts_load):
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}  ({e})")
            import traceback
            traceback.print_exc()

    orig = _install_auth_stub()
    snap = _snapshot_runtime()
    try:
        app = _build_app()
        with app.test_client() as client:
            for t in (test_parsed_endpoint, test_preview_endpoint,
                      test_json_dry_run_matches_text, test_json_confirm_and_etag_guard):
                try:
                    t(client)
                except Exception as e:
                    FAIL += 1
                    print(f"  ERROR {t.__name__}  ({e})")
                    import traceback
                    traceback.print_exc()
    finally:
        _restore_runtime(snap)
        security.user_manager.get_user = orig
        fname = f"{_TEST_ADMIN}_log.json"
        try:
            if data_io.exists(storage_location="users", filename=fname):
                data_io.remove(storage_location="users", filename=fname)
        except Exception:
            pass
    print(f"\nSummary: {PASS} passed, {FAIL} failed\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
