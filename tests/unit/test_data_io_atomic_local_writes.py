"""Local data_io writes are atomic: a failed write keeps the previous file.

save_json, save_text, save_bytes, save_parquet, write_parquet_stream and
update_json all write a hidden temp file beside the destination and swap it
in with one os.replace, so an interrupted write never truncates the file and
a reader never sees half of one.
"""

import os
import stat
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pytest

import fyp.core.data_io as data_io


@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    def _resolve(storage_location="cache", filename=""):
        return (os.path.join(tmp_path, filename), None, "local", None)

    monkeypatch.setattr(data_io, "resolve_paths", _resolve)
    return tmp_path


def _leftovers(directory):
    return [n for n in os.listdir(directory) if n.endswith(".tmp")]


def test_failed_json_write_keeps_the_previous_file(local_dir):
    data_io.save_json({"v": 1}, storage_location="cache", filename="a.json")

    with patch.object(data_io, "_write_text_file", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError):
            data_io.save_json({"v": 2}, storage_location="cache", filename="a.json")
    assert data_io.load_json(storage_location="cache", filename="a.json") == {"v": 1}
    assert _leftovers(local_dir) == []


def test_interrupted_parquet_write_keeps_the_previous_file(local_dir):
    df = pd.DataFrame({"x": [1, 2, 3]})
    data_io.save_parquet(df, storage_location="data", filename="t.parquet")

    real_replace = os.replace

    def _interrupt(src, dst):
        raise KeyboardInterrupt

    with patch.object(data_io.os, "replace", _interrupt):
        with pytest.raises(KeyboardInterrupt):
            data_io.save_parquet(
                pd.DataFrame({"x": [9]}), storage_location="data", filename="t.parquet"
            )
    assert os.replace is real_replace
    assert pd.read_parquet(local_dir / "t.parquet")["x"].tolist() == [1, 2, 3]
    assert _leftovers(local_dir) == []


def test_every_local_writer_lands_the_file_and_leaves_no_temp(local_dir):
    data_io.save_json([1], storage_location="cache", filename="j.json")
    data_io.save_text("hi", storage_location="cache", filename="t.txt")
    data_io.save_bytes(b"\x00\x01", storage_location="cache", filename="b.bin")
    data_io.update_json(
        storage_location="cache", filename="u.json", mutate=lambda c: c + [1], default=[]
    )
    schema = pa.schema([("x", pa.int64())])
    rows = data_io.write_parquet_stream(
        storage_location="data",
        filename="s.parquet",
        batches=[pa.record_batch([pa.array([1, 2])], schema=schema)],
        schema=schema,
    )
    assert rows == 2
    assert sorted(os.listdir(local_dir)) == ["b.bin", "j.json", "s.parquet", "t.txt", "u.json"]
    assert (local_dir / "t.txt").read_text() == "hi"
    assert (local_dir / "b.bin").read_bytes() == b"\x00\x01"


def test_written_files_follow_the_umask_not_0600(local_dir):
    data_io.save_json({}, storage_location="cache", filename="p.json")
    umask = os.umask(0)
    os.umask(umask)
    mode = stat.S_IMODE(os.stat(local_dir / "p.json").st_mode)
    assert mode == 0o666 & ~umask


def test_listdir_hides_in_flight_temp_files(local_dir, monkeypatch):
    (local_dir / "real.json").write_text("{}")
    (local_dir / ".real.json.0123456789ab.tmp").write_text("{")
    monkeypatch.setitem(data_io._cf()["paths"], "scratch_loc", str(local_dir))
    monkeypatch.setitem(data_io._cf()["data_io"], "use_gcs_for_data", False)
    assert data_io.listdir("scratch_loc") == ["real.json"]
