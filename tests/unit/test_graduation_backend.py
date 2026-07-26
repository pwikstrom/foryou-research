"""Backend-aware graduation of Playground candidates.

Covers the backend seam fixes:

  * ``_annotation_contract_impact`` — the plain-gemini path stays identical to
    the live descriptor (av_ hash stability) and grows backend fields; a
    non-gemini ``target_backend`` builds the candidate descriptor through the
    backend machinery.
  * ``_backend_target_info`` — unknown selections report unavailable.
  * Dry-run ``POST /api/manage/ab-candidates/<name>/activate`` — optional
    ``backend`` body, response ``backend`` block, unknown backend → 400.
  * Confirm ``POST /api/manage/annotation-contract`` with ``switch_backend`` —
    403 without the Backends permission (before any write), and the eager
    version mint on a successful confirm.

Uses the Flask test client with a stubbed admin user (same approach as
``test_admin_settings_route.py``); the runtime contract, admin settings and
version registry are snapshotted and restored.
"""

import copy
import tomllib

import pytest

import fyp.ab_eval as ab_eval
import fyp.annotation_contract as ac
import fyp.annotation_versioning as annotation_versioning
import fyp.data_io as data_io
from fyp.annotation.backends import settings as backend_settings
from web_interface.routes.management.contracts import (
    _annotation_contract_impact,
    _backend_target_info,
)

_TEST_ADMIN = "__grad_test_admin__"
_CAND_NAME = "grad-test-cand"






def _snapshot_file(location: str, filename: str):
    if data_io.exists(storage_location=location, filename=filename):
        return data_io.load_json(storage_location=location, filename=filename)
    return None






def _restore_file(location: str, filename: str, snap) -> None:
    if snap is not None:
        data_io.save_json(data=snap, storage_location=location, filename=filename)
    elif data_io.exists(storage_location=location, filename=filename):
        data_io.remove(storage_location=location, filename=filename)






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

    settings_snap = _snapshot_file("users", backend_settings.SETTINGS_FILENAME)
    registry_snap = _snapshot_file(
        annotation_versioning.REGISTRY_LOCATION, annotation_versioning.REGISTRY_FILENAME)
    runtime_text = None
    if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=ac.RUNTIME_FILENAME):
        runtime_text = data_io.load_text(storage_location=ac.RUNTIME_LOCATION,
                                         filename=ac.RUNTIME_FILENAME)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield test_client

    _restore_file("users", backend_settings.SETTINGS_FILENAME, settings_snap)
    _restore_file(annotation_versioning.REGISTRY_LOCATION,
                  annotation_versioning.REGISTRY_FILENAME, registry_snap)
    for fname in (ac.RUNTIME_FILENAME, ac.RUNTIME_META_FILENAME):
        if data_io.exists(storage_location=ac.RUNTIME_LOCATION, filename=fname):
            data_io.remove(storage_location=ac.RUNTIME_LOCATION, filename=fname)
    if runtime_text is not None:
        data_io.save_text(runtime_text, storage_location=ac.RUNTIME_LOCATION,
                          filename=ac.RUNTIME_FILENAME)
    ac.refresh_runtime_contract()
    from fyp.fyp_config import fyp_cf, load_var_schema

    load_var_schema(fyp_cf, verbose=False)
    try:
        ab_eval.delete_candidate(_CAND_NAME)
    except Exception:
        pass






def _baked_contract() -> dict:
    return tomllib.loads(ac._read_baked_text())






def _variant_contract_text(marker: str) -> str:
    """Baked contract with a prompt-affecting desc edit → a genuinely new av_.

    A comment-only edit is metadata-only and hashes to the already-registered
    current version (whose pre-existing record predates contract snapshots),
    so snapshot assertions need a version that is truly minted by the test.
    """
    contract = copy.deepcopy(_baked_contract())
    field = contract["fields"][0]
    field["desc"] = (field.get("desc") or "") + " " + marker
    return ac.serialize_contract(contract, base_text=ac._read_baked_text())






def test_impact_gemini_path_unchanged():
    """Baked contract vs itself on the gemini path → metadata-only, same av_."""
    impact = _annotation_contract_impact(_baked_contract(), target_backend="gemini")
    cur = annotation_versioning.current_version_descriptor(fresh=True)
    if cur.get("backend"):
        pytest.skip("non-gemini backend active locally — gemini identity check not applicable")
    assert impact["candidate_version"] == cur["annotation_version"]
    assert impact["metadata_only"] is True
    assert impact["target_backend"] == "gemini"
    assert impact["backend_mismatch"] is (annotation_versioning.current_version_descriptor().get("backend") is not None)






def test_impact_reports_backend_fields():
    impact = _annotation_contract_impact(_baked_contract())
    for key in ("target_backend", "target_model", "active_backend", "active_model",
                "backend_mismatch"):
        assert key in impact
    # Default target is the active backend → never a mismatch.
    assert impact["backend_mismatch"] is False
    assert impact["target_backend"] == impact["active_backend"]






def test_impact_non_gemini_target():
    from fyp.annotation.backends import get_backend

    try:
        backend = get_backend("qwen_api")
    except ValueError:
        pytest.skip("qwen_api backend not importable here")
    impact = _annotation_contract_impact(_baked_contract(), target_backend="qwen_api")
    assert impact["target_backend"] == "qwen_api"
    assert impact["target_model"] == backend.effective_model_id()
    # A different model must fork the candidate version away from the gemini one.
    gem = _annotation_contract_impact(_baked_contract(), target_backend="gemini")
    assert impact["candidate_version"] != gem["candidate_version"]






def test_backend_target_info_unknown_selection():
    info = _backend_target_info("no_such_backend_xyz")
    assert info["target_available"] is False
    assert info["target_unavailable_reason"]
    assert info["mismatch"] is True






def test_dry_run_backend_block(client):
    ab_eval.save_candidate(_CAND_NAME, ac._read_baked_text(), actor="test", overwrite=True)
    res = client.post(f"/api/manage/ab-candidates/{_CAND_NAME}/activate", json={})
    body = res.get_json()
    assert res.status_code == 200, body
    be = body["backend"]
    assert be["target"] == be["active"]
    assert be["mismatch"] is False
    assert be["can_switch_backend"] is True  # stubbed admin
    assert "impact" in body and "text" in body






def test_dry_run_unknown_backend_rejected(client):
    ab_eval.save_candidate(_CAND_NAME, ac._read_baked_text(), actor="test", overwrite=True)
    res = client.post(f"/api/manage/ab-candidates/{_CAND_NAME}/activate",
                      json={"backend": "no_such_backend_xyz"})
    assert res.status_code == 400
    assert "unknown backend" in res.get_json()["error"]






def test_confirm_switch_backend_requires_permission(client, monkeypatch):
    """A switch_backend confirm without the Backends permission → 403, no write."""
    import web_interface.permissions as permissions

    orig = permissions.user_has_permission

    def _no_backends(user, key):
        if key == "tab.admin.backends":
            return False
        return orig(user, key)

    monkeypatch.setattr(permissions, "user_has_permission", _no_backends)
    before = ac.contract_status().get("source")
    res = client.post("/api/manage/annotation-contract",
                      json={"text": ac._read_baked_text(), "confirm": True,
                            "switch_backend": "qwen_api"})
    assert res.status_code == 403
    assert ac.contract_status().get("source") == before  # nothing written






def test_confirm_unknown_switch_backend_rejected(client):
    res = client.post("/api/manage/annotation-contract",
                      json={"text": ac._read_baked_text(), "confirm": True,
                            "switch_backend": "no_such_backend_xyz"})
    assert res.status_code == 400






def test_confirm_mints_version_eagerly(client):
    """A confirmed upload registers the resulting version immediately."""
    upload_text = _variant_contract_text("EAGER MINT TEST MARKER")
    res = client.post("/api/manage/annotation-contract",
                      json={"text": upload_text, "confirm": True})
    body = res.get_json()
    assert res.status_code == 200, body
    minted = body.get("minted_version")
    assert isinstance(minted, str) and minted.startswith("av_")
    registry = annotation_versioning.load_registry()
    assert minted in registry.get("versions", {})
    # The registered snapshot carries the prompt/schema (Versions-page View)
    # and the source contract TOML (Versions-page "Make current" restore).
    record = registry["versions"][minted]
    assert record.get("prompt_text")
    assert record.get("contract_text") == upload_text






def test_version_summaries_report_restorable(client):
    """list endpoint: no bulky contract_text, but a restorable flag per version."""
    upload_text = _variant_contract_text("RESTORABLE SUMMARY TEST MARKER")
    res = client.post("/api/manage/annotation-contract",
                      json={"text": upload_text, "confirm": True})
    minted = (res.get_json() or {}).get("minted_version")
    res = client.get("/api/manage/annotation-versions")
    body = res.get_json()
    assert res.status_code == 200, body
    by_version = {v["annotation_version"]: v for v in body["versions"]}
    assert minted in by_version
    assert by_version[minted]["restorable"] is True
    assert "contract_text" not in by_version[minted]






def test_make_current_restores_exact_version(client):
    """Detail endpoint exposes the restore block; re-uploading the snapshot
    reproduces the same av_ (exact restore) while config is unchanged."""
    upload_text = _variant_contract_text("EXACT RESTORE TEST MARKER")
    res = client.post("/api/manage/annotation-contract",
                      json={"text": upload_text, "confirm": True})
    minted = (res.get_json() or {}).get("minted_version")

    res = client.get(f"/api/manage/annotation-versions/{minted}")
    body = res.get_json()
    assert res.status_code == 200, body
    restore = body["restore"]
    assert restore["restorable"] is True
    assert restore["backend"]["can_switch_backend"] is True
    snapshot = body["record"]["contract_text"]
    assert snapshot == upload_text

    # Move the live contract elsewhere, then dry-run the snapshot back:
    # the predicted candidate version must equal the recorded one.
    res = client.post("/api/manage/annotation-contract",
                      json={"text": _variant_contract_text("ELSEWHERE MARKER"), "confirm": True})
    assert res.status_code == 200
    res = client.post("/api/manage/annotation-contract", json={"text": snapshot})
    impact = (res.get_json() or {}).get("impact") or {}
    assert impact.get("candidate_version") == minted






def test_default_pseudo_candidate(client):
    """The reserved 'default' candidate serves the shipped baked contract."""
    res = client.get("/api/manage/ab-candidates")
    body = res.get_json()
    assert res.status_code == 200, body
    d = body.get("default_contract")
    assert d and d["name"] == "default"
    assert isinstance(d.get("n_fields"), int) and d["n_fields"] > 0

    res = client.get("/api/manage/ab-candidates/default")
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["builtin"] is True
    assert body["text"] == ac._read_baked_text()

    # The reserved name cannot be claimed by a stored candidate.
    res = client.post("/api/manage/ab-candidates",
                      json={"name": "default", "text": ac._read_baked_text()})
    assert res.status_code == 400
    assert "reserved" in res.get_json()["error"]

    # Graduation dry-run works and flags the builtin (client routes confirm
    # to the revert endpoint).
    res = client.post("/api/manage/ab-candidates/default/activate", json={})
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["builtin_default"] is True
    assert "impact" in body






def test_impact_helper_still_flags_prompt_change():
    """Regression guard carried from the pre-change behaviour."""
    edited = copy.deepcopy(_baked_contract())
    field = edited["fields"][0]
    field["desc"] = (field.get("desc") or "") + " EXTRA PROMPT WORDS FOR TEST"
    impact = _annotation_contract_impact(edited)
    assert impact["version_changed"] is True
    assert impact["metadata_only"] is False
