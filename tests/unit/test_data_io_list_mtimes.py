"""data_io.list_mtimes: every file's date from one listing, local and GCS."""

import datetime
import os

from fyp.core import data_io
from fyp.core.fyp_config import fyp_cf


def test_local_lists_files_only(tmp_path, monkeypatch):
    monkeypatch.setitem(fyp_cf["paths"], "cache", str(tmp_path))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "sub").mkdir()
    os.utime(tmp_path / "a.json", (1000, 1000))

    assert data_io.list_mtimes("cache") == {"a.json": 1000.0}


def test_local_missing_folder_is_empty(tmp_path, monkeypatch):
    monkeypatch.setitem(fyp_cf["paths"], "cache", str(tmp_path / "absent"))
    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", False)

    assert data_io.list_mtimes("cache") == {}


def test_gcs_uses_one_listing(monkeypatch):
    when = datetime.datetime(2026, 11, 27, tzinfo=datetime.UTC)

    class _Blob:
        def __init__(self, name, updated):
            self.name, self.updated = name, updated

    class _Bucket:
        calls = 0

        def list_blobs(self, prefix, delimiter):
            _Bucket.calls += 1
            assert (prefix, delimiter) == ("cache_dir/", "/")
            return [_Blob("cache_dir/", when), _Blob("cache_dir/x.json", when)]

        def get_blob(self, name):  # pragma: no cover - must not be used
            raise AssertionError("one request per file")

    monkeypatch.setitem(fyp_cf["data_io"], "use_gcs_for_cache", True)
    monkeypatch.setitem(fyp_cf, "gcs_paths", {"cache": "cache_dir"})
    monkeypatch.setitem(fyp_cf["data_io"], "bucket", _Bucket())

    assert data_io.list_mtimes("cache") == {"x.json": when.timestamp()}
    assert _Bucket.calls == 1

    monkeypatch.setitem(fyp_cf["data_io"], "bucket", None)
    assert data_io.list_mtimes("cache") == {}
