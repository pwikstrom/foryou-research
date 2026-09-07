"""Generated identities for raw uploads (fyp.ingest.raw_names).

Every TikTok export is called user_data_tiktok.json, so the browser's
filename must never be a storage key or a collection id. Stored names and
ids are generated and checked against everything the Hub already knows.
"""

import os

import pytest

from fyp.ingest import raw_names




@pytest.fixture
def local_raw(tmp_path, monkeypatch):
    """A local 'ddp_raw' + 'archive' + 'recoded' trio under a temp dir."""
    from fyp.fyp_config import fyp_cf

    for loc in ("ddp_raw", "archive", "recoded"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setattr(raw_names, "registered_raw_paths", lambda: ["ddp_raw"])
    return tmp_path




def test_stored_name_carries_platform_source_time_and_random_suffix():
    name = raw_names.stored_filename("tiktok", "ddp", ".JSON")
    stem, ext = os.path.splitext(name)
    assert ext == ".json"                       # validated extension, lower-cased
    parts = stem.split("_")
    assert parts[0] == "tiktok" and parts[1] == "ddp"
    assert len(parts[2]) == 16 and parts[2].endswith("Z")   # 20260906T112918Z
    assert len(parts[3]) == 8 and int(parts[3], 16) >= 0    # 8 hex digits
    assert raw_names.stored_filename("tiktok", "aio", "") .count(".") == 0




def test_stored_names_are_unique_and_never_the_original():
    names = {raw_names.stored_filename("tiktok", "ddp", ".json") for _ in range(200)}
    assert len(names) == 200
    assert "user_data_tiktok.json" not in names




def test_display_label_is_the_original_stem_with_a_fallback():
    assert raw_names.display_label("user_data_tiktok_2.json") == "user_data_tiktok_2"
    assert raw_names.display_label("  My   export .zip ") == "My export"
    assert raw_names.display_label("/tmp/some/path/file.json") == "file"
    assert raw_names.display_label("", platform="tiktok") == "Tiktok donation"
    assert len(raw_names.display_label("x" * 500 + ".json")) == 80




def test_allocate_avoids_known_ids_and_taken_names(local_raw, monkeypatch):
    # Force the same candidate twice, then a fresh one, to prove the checks
    # actually reject a taken name / a known id rather than trusting luck.
    candidates = iter(["tiktok_ddp_20260906T112918Z_aaaaaaaa.json",
                       "tiktok_ddp_20260906T112918Z_bbbbbbbb.json",
                       "tiktok_ddp_20260906T112918Z_cccccccc.json"])
    monkeypatch.setattr(raw_names, "stored_filename",
                        lambda platform, source, ext, now=None: next(candidates))
    (local_raw / "ddp_raw" / "tiktok_ddp_20260906T112918Z_aaaaaaaa.json").write_text("{}")
    known = {"tiktok_ddp_20260906T112918Z_bbbbbbbb"}

    stored, cid, display = raw_names.allocate_upload_identity(
        "tiktok", "ddp", "user_data_tiktok.json", "ddp_raw", known_ids=known)

    assert stored == "tiktok_ddp_20260906T112918Z_cccccccc.json"
    assert cid == "tiktok_ddp_20260906T112918Z_cccccccc"
    assert display == "user_data_tiktok"
    assert cid in known                          # reserved for the next file in the batch




def test_allocate_treats_archived_names_as_taken(local_raw, monkeypatch):
    candidates = iter(["tiktok_ddp_20260906T112918Z_aaaaaaaa.json",
                       "tiktok_ddp_20260906T112918Z_bbbbbbbb.json"])
    monkeypatch.setattr(raw_names, "stored_filename",
                        lambda platform, source, ext, now=None: next(candidates))
    (local_raw / "archive" / "tiktok_ddp_20260906T112918Z_aaaaaaaa.json").write_text("{}")
    stored, _cid, _d = raw_names.allocate_upload_identity(
        "tiktok", "ddp", "user_data_tiktok.json", "ddp_raw", known_ids=set())
    assert stored == "tiktok_ddp_20260906T112918Z_bbbbbbbb.json"




def test_known_collection_ids_reads_every_store(local_raw):
    import json

    import pandas as pd

    import fyp.data_io as data_io
    from fyp.organize_datasets import COLLECTIONS_LABEL

    meta = pd.DataFrame({"n": [1]}, index=pd.Index(["in_dataset"], name="collection_id"))
    meta.to_parquet(local_raw / "recoded" / f"{COLLECTIONS_LABEL}_metadata.parquet")
    (local_raw / "recoded" / f"{COLLECTIONS_LABEL}_tags.json").write_text(
        json.dumps({"tagged": {"user_id": "u"}}))
    (local_raw / "recoded" / "withdrawals.json").write_text(json.dumps({"withdrawn": {}}))
    (local_raw / "recoded" / "ingestion_ledger.json").write_text(json.dumps(
        {"schema_version": 1, "files": {"old.json": {"collection_id": "ledgered"}}}))
    data_io.save_json(data={"pending.json": {"collection_id": "pending"}},
                      storage_location="ddp_raw", filename="ingestion_manifest.json")

    ids = raw_names.known_collection_ids()
    assert {"in_dataset", "tagged", "withdrawn", "ledgered", "pending"} <= ids




def test_manifest_entry_shape_and_provenance_round_trip():
    entry = raw_names.manifest_entry(
        "tiktok_ddp_x", "user_data_tiktok.json", display_collection_id="user_data_tiktok",
        user_id="p@example.org", tz="Australia/Melbourne", client_reviewed=True,
        uploaded_by="p@example.org", uploaded_at="2026-09-06T11:29:18+00:00")
    assert entry["collection_id"] == "tiktok_ddp_x"
    assert entry["original_filename"] == "user_data_tiktok.json"
    assert entry["tags"] == [] and entry["client_reviewed"] is True
    prov = raw_names.provenance_from_manifest(entry)
    assert prov == {
        "original_filename": "user_data_tiktok.json",
        "display_collection_id": "user_data_tiktok",
        "user_id": "p@example.org", "tz": "Australia/Melbourne",
        "client_reviewed": True, "uploaded_by": "p@example.org",
        "uploaded_at": "2026-09-06T11:29:18+00:00",
    }
    # Falsy optionals are omitted so older readers see the old shape.
    lean = raw_names.manifest_entry("c", "f.json")
    assert "tz" not in lean and "client_reviewed" not in lean and "user_id" not in lean
    assert raw_names.provenance_from_manifest(None) == {}
