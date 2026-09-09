"""Both upload routes generate the stored name and the collection id.

The browser's filename is never a storage key or an identity: two uploads of
"user_data_tiktok.json" land as two distinct stored files with two distinct
collection ids, the original name survives as provenance and as the display
label, and an admin's explicit id cannot silently append to someone else's
collection.
"""

import io
import json

import pytest

_VIEWER = "__upload_identity_viewer__"
_ADMIN = "__upload_identity_admin__"




@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """Every storage location the routes touch, as local temp dirs."""
    from fyp.fyp_config import fyp_cf
    from fyp.ingest import raw_names

    dirs = {}
    for loc in ("temp", "recoded", "archive", "ddp_raw", "zeeschuimer_raw",
                "aio_raw", "instagram_raw", "youtube_raw", "users", "cache"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
        dirs[loc] = d
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    monkeypatch.setattr(raw_names, "registered_raw_paths", lambda: ["ddp_raw"])

    from web_interface import activity_log
    monkeypatch.setattr(activity_log, "record", lambda **kw: None)
    from web_interface.services import study_data
    study_data.invalidate_collection_tags_cache()
    yield dirs
    study_data.invalidate_collection_tags_cache()




@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _VIEWER:
            return User(username=_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
        if uid == _ADMIN:
            return User(username=_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client




def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True




def _grant(monkeypatch, perms):
    from web_interface import auth
    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))




def _tags(dirs):
    from fyp.organize_datasets import COLLECTIONS_LABEL
    p = dirs["recoded"] / f"{COLLECTIONS_LABEL}_tags.json"
    return json.loads(p.read_text()) if p.exists() else {}




def _manifest(dirs):
    return json.loads((dirs["ddp_raw"] / "ingestion_manifest.json").read_text())




def _post_participant(client, name, tz="Australia/Melbourne", review=False):
    data = {"raw_path": "ddp_raw", "tz": tz,
            "files": (io.BytesIO(b'{"Your Activity": {}}'), name)}
    if review:
        data["client_review"] = "1"
    return client.post("/api/my/collections/upload", data=data,
                       content_type="multipart/form-data")




def test_participant_uploads_of_the_same_filename_get_distinct_identities(client, monkeypatch, local_store):
    _grant(monkeypatch, ["tab.my_stuff.my_collections"])
    _login(client, _VIEWER)

    r1 = _post_participant(client, "user_data_tiktok.json", review=True)
    r2 = _post_participant(client, "user_data_tiktok.json")
    assert r1.status_code == 200, r1.get_json()
    assert r2.status_code == 200, r2.get_json()
    c1 = r1.get_json()["collections"][0]
    c2 = r2.get_json()["collections"][0]

    # Generated, distinct, never the browser's name.
    assert c1["filename"] != "user_data_tiktok.json" and c1["filename"].endswith(".json")
    assert c1["filename"] != c2["filename"]
    assert c1["collection_id"] != c2["collection_id"]
    assert c1["collection_id"] == c1["filename"][:-5]
    assert c1["original_filename"] == "user_data_tiktok.json"
    assert c1["display_id"] == "user_data_tiktok"
    assert (local_store["ddp_raw"] / c1["filename"]).exists()
    assert (local_store["ddp_raw"] / c2["filename"]).exists()

    manifest = _manifest(local_store)
    e1 = manifest[c1["filename"]]
    assert e1["collection_id"] == c1["collection_id"]
    assert e1["original_filename"] == "user_data_tiktok.json"
    assert e1["display_collection_id"] == "user_data_tiktok"
    assert e1["user_id"] == _VIEWER and e1["uploaded_by"] == _VIEWER
    assert e1["tz"] == "Australia/Melbourne" and e1["client_reviewed"] is True
    assert "client_reviewed" not in manifest[c2["filename"]]

    # The label is the filename's stem, so the SECOND donor of the same export
    # cannot have it: display IDs name one collection each.
    assert c2["display_id"] == "user_data_tiktok (2)"
    assert manifest[c2["filename"]]["display_collection_id"] == "user_data_tiktok (2)"

    tags = _tags(local_store)
    assert tags[c1["collection_id"]]["user_id"] == _VIEWER
    assert tags[c1["collection_id"]]["display_collection_id"] == "user_data_tiktok"
    assert tags[c2["collection_id"]]["display_collection_id"] == "user_data_tiktok (2)"




def test_participant_upload_survives_a_name_already_used_by_an_old_collection(client, monkeypatch, local_store):
    """The 2026-09-06 case: an old collection's raw file is called
    user_data_tiktok_2.json. A participant uploading a file of that name must
    neither overwrite it nor be skipped later — the stored name is fresh."""
    (local_store["ddp_raw"] / "user_data_tiktok_2.json").write_text('{"old": true}')
    _grant(monkeypatch, ["tab.my_stuff.my_collections"])
    _login(client, _VIEWER)
    r = _post_participant(client, "user_data_tiktok_2.json")
    assert r.status_code == 200, r.get_json()
    stored = r.get_json()["collections"][0]["filename"]
    assert stored != "user_data_tiktok_2.json"
    assert json.loads((local_store["ddp_raw"] / "user_data_tiktok_2.json").read_text()) == {"old": True}
    assert (local_store["ddp_raw"] / stored).exists()




def _post_admin(client, name, **fields):
    data = {"raw_path": "ddp_raw", "files": (io.BytesIO(b'{"Your Activity": {}}'), name)}
    data.update(fields)
    return client.post("/api/manage/ingestion/upload", data=data, content_type="multipart/form-data")




def test_admin_per_file_upload_generates_ids_and_labels(client, monkeypatch, local_store):
    _grant(monkeypatch, ["tab.data_management.ingestion"])
    _login(client, _ADMIN)
    r = _post_admin(client, "user_data_tiktok.json", tags=json.dumps(["q1"]))
    assert r.status_code == 200, r.get_json()
    detail = r.get_json()["uploaded"][0]
    assert detail["original_filename"] == "user_data_tiktok.json"
    assert detail["filename"] != "user_data_tiktok.json"
    assert detail["collection_id"] == detail["filename"][:-5]
    entry = _manifest(local_store)[detail["filename"]]
    assert entry["tags"] == ["q1"] and entry["uploaded_by"] == _ADMIN
    tags = _tags(local_store)[detail["collection_id"]]
    assert tags["display_collection_id"] == "user_data_tiktok"
    assert tags["annotation_tags"] == ["q1"]




def test_admin_explicit_id_cannot_append_to_someone_elses_collection(client, monkeypatch, local_store):
    from fyp.organize_datasets import COLLECTIONS_LABEL
    (local_store["recoded"] / f"{COLLECTIONS_LABEL}_tags.json").write_text(json.dumps(
        {"theirs": {"user_id": _VIEWER, "annotation_tags": [], "hidden": False}}))
    from web_interface.services import study_data
    study_data.invalidate_collection_tags_cache()
    _grant(monkeypatch, ["tab.data_management.ingestion"])
    _login(client, _ADMIN)

    r = _post_admin(client, "user_data_tiktok.json", collection_id="theirs",
                    collection_id_mode="single")
    assert r.status_code == 409
    assert _VIEWER in r.get_json()["error"]
    assert not (local_store["ddp_raw"] / "ingestion_manifest.json").exists()

    # Same owner named explicitly: appending is the deliberate use of an
    # explicit id, so it goes through.
    r = _post_admin(client, "user_data_tiktok.json", collection_id="theirs",
                    collection_id_mode="single", user_id=_VIEWER)
    assert r.status_code == 200, r.get_json()
    entry = list(_manifest(local_store).values())[0]
    assert entry["collection_id"] == "theirs" and entry["display_collection_id"] is None
