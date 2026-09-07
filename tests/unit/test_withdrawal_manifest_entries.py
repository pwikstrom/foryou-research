"""A withdrawal keeps the upload's manifest entry so a restore hands the
ingester exactly what the upload did (original name, timezone, review flag),
not a bare {collection_id, user_id} that would be judged against the wrong
sentinel baseline."""

import pytest

from web_interface.services import my_collections_service as mcs




@pytest.fixture
def ledger(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(mcs, "_load_withdrawals_raw", lambda: dict(store))

    def _save(w):
        store.clear()
        store.update(w)

    monkeypatch.setattr(mcs, "_save_withdrawals", _save)
    return store




def test_ledger_manifest_entries_rebuild_from_ingestion_ledger(monkeypatch):
    ingestion_ledger = {"files": {
        "tiktok_ddp_x.json": {
            "outcome": "added_as_new", "collection_id": "tiktok_ddp_x",
            "original_filename": "user_data_tiktok.json", "user_id": "p@example.org",
            "tz": "Australia/Melbourne", "client_reviewed": True,
            "uploaded_by": "p@example.org", "uploaded_at": "2026-09-06T11:29:18+00:00",
            "display_collection_id": "user_data_tiktok",
        },
    }}
    monkeypatch.setattr(mcs.data_io, "exists", lambda **kw: kw.get("filename") == "ingestion_ledger.json")
    monkeypatch.setattr(mcs.data_io, "load_json", lambda **kw: ingestion_ledger)

    entries = mcs.ledger_manifest_entries("tiktok_ddp_x", ["tiktok_ddp_x.json", "unknown.json"])
    known = entries["tiktok_ddp_x.json"]
    assert known["collection_id"] == "tiktok_ddp_x"
    assert known["original_filename"] == "user_data_tiktok.json"
    assert known["tz"] == "Australia/Melbourne" and known["client_reviewed"] is True
    assert known["display_collection_id"] == "user_data_tiktok"
    assert known["uploaded_at"] == "2026-09-06T11:29:18+00:00"
    minimal = entries["unknown.json"]
    assert minimal["collection_id"] == "tiktok_ddp_x"
    assert minimal["original_filename"] == "unknown.json"
    assert "tz" not in minimal




def test_restore_writes_the_stored_manifest_entry_back(ledger, monkeypatch):
    stored_entry = {"collection_id": "c1", "original_filename": "user_data_tiktok.json",
                    "tz": "Australia/Melbourne", "client_reviewed": True, "tags": [],
                    "user_id": "p@example.org", "display_collection_id": "user_data_tiktok"}
    mcs.record_withdrawal("c1", "p@example.org", ["tiktok_ddp_x.json"], "ddp_raw",
                          "user_data_tiktok", "tiktok",
                          manifest_entries={"tiktok_ddp_x.json": stored_entry})
    assert ledger["c1"]["manifest_entries"] == {"tiktok_ddp_x.json": stored_entry}

    written = {}
    monkeypatch.setattr(mcs.data_io, "exists", lambda **kw: kw.get("filename") != mcs.MANIFEST_FILENAME)
    monkeypatch.setattr(mcs.data_io, "move", lambda **kw: None)
    monkeypatch.setattr(mcs.data_io, "load_json", lambda **kw: {})
    monkeypatch.setattr(mcs.data_io, "save_json", lambda **kw: written.update(kw["data"]))
    import web_interface.collection_accounts as ca
    monkeypatch.setattr(ca, "set_collection_owner", lambda *a, **k: None)
    monkeypatch.setattr(mcs, "invalidate_cache", lambda: None)

    mcs.restore_withdrawal("c1")

    assert written["tiktok_ddp_x.json"]["client_reviewed"] is True
    assert written["tiktok_ddp_x.json"]["tz"] == "Australia/Melbourne"
    assert written["tiktok_ddp_x.json"]["original_filename"] == "user_data_tiktok.json"
    assert written["tiktok_ddp_x.json"]["collection_id"] == "c1"
    assert written["tiktok_ddp_x.json"]["user_id"] == "p@example.org"
    assert "c1" not in ledger




def test_restore_of_an_old_withdrawal_falls_back_to_the_minimal_entry(ledger, monkeypatch):
    mcs.record_withdrawal("c2", "p@example.org", ["old.json"], "ddp_raw", None, "tiktok")
    ledger["c2"].pop("manifest_entries")          # a record written before this existed
    written = {}
    monkeypatch.setattr(mcs.data_io, "exists", lambda **kw: kw.get("filename") != mcs.MANIFEST_FILENAME)
    monkeypatch.setattr(mcs.data_io, "move", lambda **kw: None)
    monkeypatch.setattr(mcs.data_io, "load_json", lambda **kw: {})
    monkeypatch.setattr(mcs.data_io, "save_json", lambda **kw: written.update(kw["data"]))
    import web_interface.collection_accounts as ca
    monkeypatch.setattr(ca, "set_collection_owner", lambda *a, **k: None)
    monkeypatch.setattr(mcs, "invalidate_cache", lambda: None)
    mcs.restore_withdrawal("c2")
    assert written["old.json"] == {"collection_id": "c2", "user_id": "p@example.org", "tags": []}
