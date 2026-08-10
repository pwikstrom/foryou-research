"""data_io.rename(): local-mode semantics used by the study-rename endpoint."""

import pytest

import fyp.data_io as data_io

LOC = "cache"




@pytest.fixture
def local_cache(tmp_path, monkeypatch):
    """Point the 'cache' location at a temp dir (local mode)."""
    from fyp.fyp_config import fyp_cf

    monkeypatch.setitem(fyp_cf["paths"], LOC, str(tmp_path))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    return tmp_path




def test_rename_moves_existing_file(local_cache):
    (local_cache / "old_methods.json").write_text('{"a": 1}')

    assert data_io.rename(storage_location=LOC,
                          src_filename="old_methods.json",
                          dst_filename="new_methods.json") is True
    assert not (local_cache / "old_methods.json").exists()
    assert (local_cache / "new_methods.json").read_text() == '{"a": 1}'




def test_rename_missing_source_returns_false(local_cache):
    assert data_io.rename(storage_location=LOC,
                          src_filename="nope.json",
                          dst_filename="also_nope.json") is False
    assert not (local_cache / "also_nope.json").exists()




def test_rename_same_name_reports_existence(local_cache):
    assert data_io.rename(storage_location=LOC,
                          src_filename="x.json", dst_filename="x.json") is False
    (local_cache / "x.json").write_text("{}")
    assert data_io.rename(storage_location=LOC,
                          src_filename="x.json", dst_filename="x.json") is True




def test_rename_rejects_empty_arguments(local_cache):
    with pytest.raises(ValueError):
        data_io.rename(storage_location="", src_filename="a", dst_filename="b")
    with pytest.raises(ValueError):
        data_io.rename(storage_location=LOC, src_filename="", dst_filename="b")
    with pytest.raises(ValueError):
        data_io.rename(storage_location=LOC, src_filename="a", dst_filename="")
