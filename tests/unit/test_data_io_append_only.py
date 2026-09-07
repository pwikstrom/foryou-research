"""Raw locations are append-only at the storage layer.

A raw donation must never be replaced: data_io.move()/rename() into an
APPEND_ONLY_LOCATION and save_json(overwrite=False) raise FileExistsError and
leave the existing object untouched. Ordinary locations keep overwrite
semantics.
"""

import json

import pytest

import fyp.data_io as data_io




@pytest.fixture
def local_locations(tmp_path, monkeypatch):
    from fyp.fyp_config import fyp_cf

    dirs = {}
    for loc in ("temp", "ddp_raw", "archive", "cache"):
        d = tmp_path / loc
        d.mkdir()
        monkeypatch.setitem(fyp_cf["paths"], loc, str(d))
        dirs[loc] = d
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_data", False)
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    return dirs




def test_raw_locations_are_declared_append_only():
    assert {"ddp_raw", "zeeschuimer_raw", "aio_raw", "instagram_raw",
            "youtube_raw", "archive"} <= set(data_io.APPEND_ONLY_LOCATIONS)
    assert "cache" not in data_io.APPEND_ONLY_LOCATIONS
    assert "recoded" not in data_io.APPEND_ONLY_LOCATIONS




def test_move_from_temp_refuses_to_clobber_a_raw_file(local_locations):
    (local_locations["ddp_raw"] / "user_data_tiktok_2.json").write_text('{"owner": "first"}')
    (local_locations["temp"] / "user_data_tiktok_2.json").write_text('{"owner": "second"}')

    with pytest.raises(FileExistsError):
        data_io.move(src_storage_location="temp", dst_storage_location="ddp_raw",
                     filename="user_data_tiktok_2.json")

    assert json.loads((local_locations["ddp_raw"] / "user_data_tiktok_2.json").read_text()) == {"owner": "first"}
    assert (local_locations["temp"] / "user_data_tiktok_2.json").exists()   # nothing consumed




def test_move_from_temp_into_a_free_raw_name_works(local_locations):
    (local_locations["temp"] / "tiktok_ddp_x.json").write_text("{}")
    data_io.move(src_storage_location="temp", dst_storage_location="ddp_raw",
                 filename="tiktok_ddp_x.json")
    assert (local_locations["ddp_raw"] / "tiktok_ddp_x.json").exists()
    assert not (local_locations["temp"] / "tiktok_ddp_x.json").exists()




def test_move_between_raw_locations_refuses_to_clobber(local_locations):
    (local_locations["ddp_raw"] / "a.json").write_text('{"v": 1}')
    (local_locations["archive"] / "a.json").write_text('{"v": "archived"}')
    with pytest.raises(FileExistsError):
        data_io.move(src_storage_location="ddp_raw", dst_storage_location="archive",
                     filename="a.json")
    assert json.loads((local_locations["archive"] / "a.json").read_text()) == {"v": "archived"}
    assert (local_locations["ddp_raw"] / "a.json").exists()




def test_rename_inside_a_raw_location_refuses_to_clobber(local_locations):
    (local_locations["ddp_raw"] / "a.json").write_text('{"v": 1}')
    (local_locations["ddp_raw"] / "b.json").write_text('{"v": 2}')
    with pytest.raises(FileExistsError):
        data_io.rename(storage_location="ddp_raw", src_filename="a.json", dst_filename="b.json")
    assert json.loads((local_locations["ddp_raw"] / "b.json").read_text()) == {"v": 2}
    assert data_io.rename(storage_location="ddp_raw", src_filename="a.json",
                          dst_filename="c.json") is True




def test_save_json_overwrite_false_refuses_and_default_still_overwrites(local_locations):
    data_io.save_json(data={"v": 1}, storage_location="ddp_raw", filename="d.json")
    with pytest.raises(FileExistsError):
        data_io.save_json(data={"v": 2}, storage_location="ddp_raw", filename="d.json",
                          overwrite=False)
    assert json.loads((local_locations["ddp_raw"] / "d.json").read_text()) == {"v": 1}
    # Manifests and other bookkeeping files legitimately get rewritten.
    data_io.save_json(data={"v": 3}, storage_location="ddp_raw", filename="d.json")
    assert json.loads((local_locations["ddp_raw"] / "d.json").read_text()) == {"v": 3}




def test_ordinary_locations_keep_overwrite_semantics(local_locations):
    (local_locations["cache"] / "x.json").write_text('{"v": 1}')
    (local_locations["temp"] / "x.json").write_text('{"v": 2}')
    data_io.move(src_storage_location="temp", dst_storage_location="cache", filename="x.json")
    assert json.loads((local_locations["cache"] / "x.json").read_text()) == {"v": 2}
