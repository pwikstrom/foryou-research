"""Route-level coverage for the collection ↔ account link and user profiles.

Flask test client with a stubbed admin session (the approach of
``test_admin_settings_route.py``); the ``users`` and ``recoded`` storage
locations are an in-memory stand-in so nothing on disk is touched.
"""

import copy
import io
from unittest.mock import patch

import pandas as pd
import pytest

from web_interface import auth, collection_accounts as ca

_ADMIN = "route.admin@example.test"


class _Store:
    def __init__(self):
        self.files = {"users": {}, "recoded": {}, "archive": {}, "temp": {}, "ddp_raw": {}}

    def exists(self, storage_location="", filename="", **kw):
        return filename in self.files.get(storage_location, {})

    def listdir(self, storage_location="", return_absolute_path=False, **kw):
        return list(self.files.get(storage_location, {}).keys())

    def load_json(self, storage_location="", filename="", **kw):
        return copy.deepcopy(self.files.get(storage_location, {}).get(filename))

    def save_json(self, data=None, storage_location="", filename="", **kw):
        self.files.setdefault(storage_location, {})[filename] = copy.deepcopy(data)

    def remove(self, storage_location="", filename="", **kw):
        self.files.get(storage_location, {}).pop(filename, None)

    def load_parquet(self, storage_location="", filename="", **kw):
        return self.files[storage_location][filename].copy()

    def save_parquet(self, df=None, storage_location="", filename="", **kw):
        self.files.setdefault(storage_location, {})[filename] = df.copy()

    def move(self, src_storage_location="", dst_storage_location="", filename="", **kw):
        self.files.setdefault(dst_storage_location, {})[filename] = self.files.get(src_storage_location, {}).pop(filename, b"")


def _metadata():
    cols = pd.MultiIndex.from_tuples([("counts", "total"), ("other", "accepted"), ("participants", "campaign")])
    return pd.DataFrame([[10, True, "qut"], [20, True, "qut"]], columns=cols,
                        index=pd.Index(["c1", "c2"], name="collection_id"))


@pytest.fixture
def env(monkeypatch):
    import fyp.core.data_io as core_io
    import web_interface.services.study_data as sd
    from web_interface import security
    from web_interface.fyp_data_hub import app

    store = _Store()
    patches = [patch.object(core_io, name, side_effect=getattr(store, name))
               for name in ("exists", "listdir", "load_json", "save_json", "remove",
                            "load_parquet", "save_parquet", "move")]
    patches.append(patch.object(ca, "placeholder_domain", return_value="example.test"))
    for p in patches:
        p.start()
    sd.invalidate_collection_tags_cache()

    # A fresh manager over the in-memory store, swapped into the security
    # singleton so every route sees the same roster.
    um = auth.UserManager(storage_location="users", bootstrap=False)
    um.add_user(_ADMIN, "pw", "admin", approved=True)
    um.add_user("member@example.test", "pw", "viewer", approved=True, display_username="Mem Ber")
    for attr in ("users", "_loaded", "storage_location"):
        monkeypatch.setattr(security.user_manager, attr, getattr(um, attr))
    security.user_manager.users = um.users

    store.files["recoded"]["collections_metadata.parquet"] = _metadata()
    store.files["recoded"]["collections_tags.json"] = {
        "c1": {"display_collection_id": "One", "annotation_tags": ["t1"], "hidden": False},
    }

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_user_id"] = _ADMIN
                sess["_fresh"] = True
            yield store, client, security.user_manager
    finally:
        for p in reversed(patches):
            p.stop()
        sd.invalidate_collection_tags_cache()


def test_accounts_list_and_collections_payload(env):
    store, client, um = env
    r = client.get("/api/manage/accounts")
    assert r.status_code == 200
    names = {row["username"]: row for row in r.get_json()}
    assert "member@example.test" in names and names["member@example.test"]["can_login"]

    r = client.get("/api/manage/collections")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.get_json()}
    assert by_id["c1"]["user_id"] is None and by_id["c1"]["user_label"] is None
    assert "email" not in by_id["c1"]["participants"]
    assert by_id["c1"]["participants"]["campaign"] == "qut"


def test_save_annotation_sets_and_preserves_link(env):
    store, client, um = env
    r = client.post("/api/manage/collection/save_annotation", json={
        "collection_id": "c1", "display_collection_id": "One", "tags": ["t1", "t2"],
        "hidden": False, "user_id": "member@example.test"})
    assert r.status_code == 200, r.get_json()
    assert store.files["recoded"]["collections_tags.json"]["c1"]["user_id"] == "member@example.test"

    # A tag-only edit (no user_id key) keeps the link.
    r = client.post("/api/manage/collection/save_annotation", json={
        "collection_id": "c1", "display_collection_id": "One", "tags": ["t1"], "hidden": True})
    assert r.status_code == 200
    entry = store.files["recoded"]["collections_tags.json"]["c1"]
    assert entry["user_id"] == "member@example.test" and entry["hidden"] is True

    # Unknown account is refused; null unassigns.
    r = client.post("/api/manage/collection/save_annotation", json={
        "collection_id": "c1", "tags": [], "user_id": "ghost@example.test"})
    assert r.status_code == 400
    r = client.post("/api/manage/collection/save_annotation", json={
        "collection_id": "c1", "tags": [], "user_id": None})
    assert r.status_code == 200
    assert store.files["recoded"]["collections_tags.json"]["c1"]["user_id"] is None

    r = client.get("/api/manage/collections")
    by_id = {c["id"]: c for c in r.get_json()}
    assert by_id["c1"]["user_id"] is None


def test_collections_payload_labels_linked_account(env):
    store, client, um = env
    ca.set_collection_owner("c2", "member@example.test")
    ca.set_collection_owner("c1", "vanished@example.test")
    by_id = {c["id"]: c for c in client.get("/api/manage/collections").get_json()}
    assert by_id["c2"]["user_label"] == "Mem Ber" and by_id["c2"]["user_known"] is True
    assert by_id["c1"]["user_label"] == "vanished@example.test" and by_id["c1"]["user_known"] is False


def test_user_profile_roundtrip(env):
    store, client, um = env
    r = client.get("/api/user/profile")
    assert r.status_code == 200
    body = r.get_json()
    assert body["email"] == _ADMIN and set(body["profile"]) == set(auth.PROFILE_FIELDS)

    r = client.post("/api/user/profile", json={"profile": {"full_name": "Route Admin", "age": "21 - 25",
                                                           "consent_to_contact": True, "postcode": ""}})
    assert r.status_code == 200, r.get_json()
    prof = client.get("/api/user/profile").get_json()["profile"]
    assert prof["full_name"] == "Route Admin" and prof["age"] == "21 - 25" and prof["consent_to_contact"] is True
    assert store.files["users"][f"{_ADMIN}.json"]["profile"]["full_name"] == "Route Admin"

    r = client.post("/api/user/profile", json={"profile": {"age": "lots"}})
    assert r.status_code == 400 and "Age" in r.get_json()["error"]
    r = client.post("/api/user/profile", json={"profile": {"shoe_size": 9}})
    assert r.status_code == 400


def test_admin_users_profile_collections_and_delete_unlinks(env):
    store, client, um = env
    ca.set_collection_owner("c1", "member@example.test")
    ca.set_collection_owner("c2", "member@example.test")

    users = {u["username"]: u for u in client.get("/api/admin/users").get_json()}
    m = users["member@example.test"]
    assert m["collections"] == ["c1", "c2"] and m["collections_count"] == 2
    assert m["can_login"] is True and m["account_kind"] == "member" and "password_hash" not in m

    r = client.put("/api/admin/users", json={"action": "set_profile", "username": "member@example.test",
                                             "profile": {"country": "Australia"}})
    assert r.status_code == 200
    assert um.get_user("member@example.test").profile["country"] == "Australia"

    with patch("web_interface.process_manager.start_process", return_value=(True, "started")) as sp:
        r = client.delete("/api/admin/users?username=member%40example.test&cascade_collections=1")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["unlinked_collections"] == ["c1", "c2"]
    assert body["cascade"]["started"] is True
    assert sp.call_args.kwargs["task_args"] == {"collection_ids": ["c1", "c2"]}
    assert um.get_user("member@example.test") is None
    tags = store.files["recoded"]["collections_tags.json"]
    assert tags["c1"]["user_id"] is None and tags["c2"]["user_id"] is None


def test_delete_refused_keeps_links(env):
    store, client, um = env
    # The only admin cannot be deleted; its links must survive the attempt.
    ca.set_collection_owner("c1", _ADMIN)
    r = client.delete(f"/api/admin/users?username={_ADMIN}")
    assert r.status_code == 400
    assert store.files["recoded"]["collections_tags.json"]["c1"]["user_id"] == _ADMIN


def test_orphan_participants_endpoint(env):
    store, client, um = env
    um.add_user("p-1@example.test", None, "viewer", placeholder=True, account_kind="participant")
    um.add_user("p-2@example.test", None, "viewer", placeholder=True, account_kind="participant")
    ca.set_collection_owner("c1", "p-1@example.test")
    assert client.get("/api/admin/users/orphan_participants").get_json() == {"orphans": ["p-2@example.test"]}
    r = client.post("/api/admin/users/orphan_participants")
    assert r.get_json()["removed"] == ["p-2@example.test"]
    assert um.get_user("p-2@example.test") is None and um.get_user("p-1@example.test") is not None


def test_upload_records_account_link(env):
    store, client, um = env
    from fyp.ingest import get_main_collection
    raw_path = next(c.raw_path for c in get_main_collection(verbose=False).collections if c.raw_path)
    store.files.setdefault(raw_path, {})

    # data_io.exists must see the moved file; the fake move() stores it under raw_path.
    data = {"files": (io.BytesIO(b"{}"), "donor_a.json"), "raw_path": raw_path,
            "collection_id_mode": "per_file", "tags": '["alpha"]', "user_id": "member@example.test"}
    def _fake_save(self, dst, *a, **k):
        store.files["temp"][dst] = b"{}"

    with patch("web_interface.routes.management.ingestion.os.path.join", side_effect=lambda *a: a[-1]), \
         patch("werkzeug.datastructures.FileStorage.save", new=_fake_save):
        r = client.post("/api/manage/ingestion/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    manifest = store.files[raw_path]["ingestion_manifest.json"]
    assert manifest["donor_a.json"]["user_id"] == "member@example.test"
    entry = store.files["recoded"]["collections_tags.json"]["donor_a"]
    assert entry["user_id"] == "member@example.test" and entry["annotation_tags"] == ["alpha"]

    r = client.post("/api/manage/ingestion/upload", data={
        "files": (io.BytesIO(b"{}"), "donor_b.json"), "raw_path": raw_path, "user_id": "ghost@example.test"},
        content_type="multipart/form-data")
    assert r.status_code == 400


def test_coverage_endpoint_is_separate_from_the_listing(env):
    """The Edit Collections coverage column has its own round trip.

    The listing reads the small metadata table; coverage needs a scan of the
    whole activity parquet, so it must not be able to slow the listing down.
    """
    store, client, um = env
    with patch("web_interface.services.collection_coverage.corpus_coverage",
               return_value={"c1": {"pct_scraped": 0.5, "pct_annotated": 0.25}}) as scan:
        r = client.get("/api/manage/collections/coverage")
        assert r.status_code == 200
        assert r.get_json()["c1"]["pct_annotated"] == 0.25
        assert scan.call_args.kwargs["force"] is False

        client.get("/api/manage/collections/coverage?fresh=1")
        assert scan.call_args.kwargs["force"] is True

        # The listing itself never triggers the scan.
        scan.reset_mock()
        assert client.get("/api/manage/collections").status_code == 200
        scan.assert_not_called()
