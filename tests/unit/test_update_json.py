"""Verify data_io.update_json (atomic JSON read-modify-write) and the
process_stats key-level merge built on top of it.

Covers: local-mode temp-then-rename writes, missing-file defaults, skip-save
via a None mutate return, thread-safety of concurrent local updates, the GCS
generation-precondition retry path (faked bucket), and save_process_stats
merging only changed keys onto fresh contents so concurrent writers from the
other service are never clobbered.
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.data_io as data_io


def _local_resolve(tmp: str):
    """A _resolve_paths stand-in pinning every location to tmp (local mode)."""

    def _resolve(storage_location="cache", filename=""):
        return (os.path.join(tmp, filename), None, 'local', None)

    return _resolve






def test_local_update_creates_missing_file_from_default():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(data_io, "_resolve_paths", _local_resolve(tmp)):
            result = data_io.update_json(
                storage_location="cache", filename="q.json",
                mutate=lambda cur: cur + ["a"], default=[],
            )
            assert result == ["a"]
            with open(os.path.join(tmp, "q.json")) as f:
                assert json.load(f) == ["a"]






def test_local_update_mutates_existing_and_none_skips_save():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(data_io, "_resolve_paths", _local_resolve(tmp)):
            data_io.update_json(
                storage_location="cache", filename="q.json",
                mutate=lambda cur: ["x", "y"], default=[],
            )
            # None return: file must be left untouched.
            skipped = data_io.update_json(
                storage_location="cache", filename="q.json",
                mutate=lambda cur: None, default=[],
            )
            assert skipped is None
            with open(os.path.join(tmp, "q.json")) as f:
                assert json.load(f) == ["x", "y"]
            # No stray temp files left behind by the rename.
            assert [p for p in os.listdir(tmp) if p.endswith(".tmp")] == []






def test_local_update_default_not_shared_across_calls():
    """The default must be copied — a mutated default must not leak."""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(data_io, "_resolve_paths", _local_resolve(tmp)):
            shared_default = []
            data_io.update_json(
                storage_location="cache", filename="a.json",
                mutate=lambda cur: cur + ["one"], default=shared_default,
            )
            assert shared_default == []






def test_local_concurrent_updates_lose_nothing():
    """N threads each append a unique id — all N must survive."""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(data_io, "_resolve_paths", _local_resolve(tmp)):
            n = 50

            def _append(i):
                data_io.update_json(
                    storage_location="cache", filename="c.json",
                    mutate=lambda cur: cur + [f"id{i}"], default=[],
                )

            threads = [threading.Thread(target=_append, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with open(os.path.join(tmp, "c.json")) as f:
                items = json.load(f)
            assert sorted(items) == sorted(f"id{i}" for i in range(n))






class _FakeBlob:
    def __init__(self, bucket, name, generation):
        self._bucket = bucket
        self.name = name
        self.generation = generation

    def download_as_text(self):
        payload, _gen = self._bucket.store[self.name]
        return payload

    def upload_from_string(self, payload, if_generation_match=None):
        from google.api_core import exceptions as gcs_exceptions

        current = self._bucket.store.get(self.name)
        current_gen = current[1] if current else 0
        if if_generation_match is not None and if_generation_match != current_gen:
            raise gcs_exceptions.PreconditionFailed("generation mismatch")
        self._bucket.store[self.name] = (payload, current_gen + 1)






class _FakeBucket:
    def __init__(self):
        self.store = {}  # blob_name -> (payload, generation)

    def get_blob(self, name):
        if name not in self.store:
            return None
        return _FakeBlob(self, name, self.store[name][1])

    def blob(self, name):
        gen = self.store[name][1] if name in self.store else 0
        return _FakeBlob(self, name, gen)






def test_gcs_update_retries_on_generation_conflict():
    """A concurrent write between read and write forces a retry against the
    fresh contents — the concurrent writer's item survives."""
    bucket = _FakeBucket()
    bucket.store["cache/q.json"] = (json.dumps(["a"]), 1)

    def _gcs_resolve(storage_location="cache", filename=""):
        return (f"gs://fake/cache/{filename}", None, 'gcs', f"cache/{filename}")

    calls = {"n": 0}

    def _mutate(cur):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate another writer landing between our read and write.
            bucket.store["cache/q.json"] = (json.dumps(["a", "other"]), 2)
        return cur + ["mine"]

    with patch.object(data_io, "_resolve_paths", _gcs_resolve), \
         patch.object(data_io, "_get_bucket", return_value=bucket):
        result = data_io.update_json(
            storage_location="cache", filename="q.json",
            mutate=_mutate, default=[],
        )

    assert calls["n"] == 2, "first attempt must have hit the precondition and retried"
    assert result == ["a", "other", "mine"]
    assert json.loads(bucket.store["cache/q.json"][0]) == ["a", "other", "mine"]






def test_gcs_update_creates_missing_blob_with_default():
    bucket = _FakeBucket()

    def _gcs_resolve(storage_location="cache", filename=""):
        return (f"gs://fake/cache/{filename}", None, 'gcs', f"cache/{filename}")

    with patch.object(data_io, "_resolve_paths", _gcs_resolve), \
         patch.object(data_io, "_get_bucket", return_value=bucket):
        result = data_io.update_json(
            storage_location="cache", filename="new.json",
            mutate=lambda cur: cur + ["first"], default=[],
        )

    assert result == ["first"]
    assert json.loads(bucket.store["cache/new.json"][0]) == ["first"]






def _fake_stats_io(store: dict):
    """A data_io stand-in for process_manager backed by an in-memory dict."""

    class FakeIO:
        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            return filename in store

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            return json.loads(store[filename])

        @staticmethod
        def save_json(data=None, storage_location="cache", filename="", verbose=False):
            store[filename] = json.dumps(data)

        @staticmethod
        def update_json(storage_location="cache", filename="", mutate=None,
                        default=None, max_retries=6, verbose=False):
            current = json.loads(json.dumps(default)) if default is not None else None
            if filename in store:
                current = json.loads(store[filename])
            new_value = mutate(current)
            if new_value is None:
                return None
            store[filename] = json.dumps(new_value)
            return new_value

    return FakeIO






def test_save_process_stats_merges_only_changed_keys():
    """A key written by the other service between our load and save survives."""
    from web_interface import process_manager as pm

    store = {"process_stats.json": json.dumps({"alpha": {"v": 1}, "beta": {"v": 1}})}
    saved_stats = dict(pm.process_stats)
    saved_snapshot = dict(pm._process_stats_snapshot)
    try:
        with patch.object(pm, "data_io", _fake_stats_io(store)):
            pm.load_process_stats()

            # The other service updates beta and adds gamma AFTER our load.
            store["process_stats.json"] = json.dumps(
                {"alpha": {"v": 1}, "beta": {"v": 99}, "gamma": {"v": 7}}
            )

            # This process only touches alpha.
            pm.process_stats["alpha"] = {"v": 2}
            pm.save_process_stats()

            on_disk = json.loads(store["process_stats.json"])
            assert on_disk["alpha"] == {"v": 2}, "our change must persist"
            assert on_disk["beta"] == {"v": 99}, "other service's beta must survive"
            assert on_disk["gamma"] == {"v": 7}, "other service's new key must survive"
            # The in-memory dict is resynced to the merged contents.
            assert pm.process_stats["beta"] == {"v": 99}
            assert pm.process_stats["gamma"] == {"v": 7}
    finally:
        pm.process_stats.clear()
        pm.process_stats.update(saved_stats)
        pm._process_stats_snapshot.clear()
        pm._process_stats_snapshot.update(saved_snapshot)






def test_save_process_stats_propagates_deletions():
    """A key this process deliberately removed is deleted on disk too."""
    from web_interface import process_manager as pm

    store = {"process_stats.json": json.dumps({"alpha": {"v": 1}, "beta": {"v": 1}})}
    saved_stats = dict(pm.process_stats)
    saved_snapshot = dict(pm._process_stats_snapshot)
    try:
        with patch.object(pm, "data_io", _fake_stats_io(store)):
            pm.load_process_stats()
            del pm.process_stats["beta"]
            pm.save_process_stats()
            on_disk = json.loads(store["process_stats.json"])
            assert "beta" not in on_disk
            assert on_disk["alpha"] == {"v": 1}
    finally:
        pm.process_stats.clear()
        pm.process_stats.update(saved_stats)
        pm._process_stats_snapshot.clear()
        pm._process_stats_snapshot.update(saved_snapshot)
