#!/usr/bin/env python3
"""Unit tests for the per-user admin's log (``web_interface.admin_notes``).

Runs against an in-memory stand-in for the ``users`` storage location, so no
real user files are touched. Covers: add/read ordering and attribution,
validation, delete, the sidecar filename being excluded from the user-file
scan, and ``UserManager.delete_user`` removing the notes sidecar.

Run:
    source .venv/bin/activate
    PYTHONPATH=. python tests/unit/test_admin_notes.py
"""

import copy
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from web_interface import admin_notes, auth  # noqa: E402


class _FakeStore:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    def exists(self, storage_location, filename):
        return filename in self.files

    def listdir(self, storage_location, return_absolute_path=False):
        return list(self.files.keys())

    def load_json(self, storage_location, filename, **kwargs):
        return copy.deepcopy(self.files.get(filename))

    def save_json(self, data, storage_location, filename, **kwargs):
        self.files[filename] = copy.deepcopy(data)

    def remove(self, storage_location, filename):
        self.files.pop(filename, None)


def _patched(store: _FakeStore, module):
    return [
        patch.object(module.data_io, name, side_effect=getattr(store, name))
        for name in ("exists", "listdir", "load_json", "save_json", "remove")
    ]


def test_add_read_delete_lifecycle() -> None:
    store = _FakeStore()
    patches = _patched(store, admin_notes)
    for p in patches:
        p.start()
    try:
        assert admin_notes.read("bob@example.com") == []

        first, err = admin_notes.add("bob@example.com", author="admin@admin.net", text="  called re consent  ")
        assert err is None and first["text"] == "called re consent"
        assert first["author"] == "admin@admin.net"
        assert first["timestamp"] and first["id"]
        assert "bob@example.com_notes.json" in store.files

        second, err = admin_notes.add("bob@example.com", author="other@admin.net", text="second donation due")
        assert err is None

        notes = admin_notes.read("bob@example.com")
        assert [n["id"] for n in notes] == [second["id"], first["id"]], "newest first"

        removed, err = admin_notes.delete("bob@example.com", first["id"])
        assert err is None and removed["id"] == first["id"]
        assert [n["id"] for n in admin_notes.read("bob@example.com")] == [second["id"]]

        _, err = admin_notes.delete("bob@example.com", "nope")
        assert err == "Note not found"

        admin_notes.remove_all("bob@example.com")
        assert "bob@example.com_notes.json" not in store.files
    finally:
        for p in patches:
            p.stop()
    print("test_add_read_delete_lifecycle PASSED")


def test_validation() -> None:
    store = _FakeStore()
    patches = _patched(store, admin_notes)
    for p in patches:
        p.start()
    try:
        assert admin_notes.add("bob", "admin", "   ")[1] == "Note text is empty"
        assert admin_notes.add("bob", "", "x")[1] == "Missing author"
        assert admin_notes.add("", "admin", "x")[1] == "Missing username"
        assert "too long" in admin_notes.add("bob", "admin", "x" * (admin_notes.MAX_NOTE_CHARS + 1))[1]
        assert store.files == {}, "rejected notes must not touch storage"
    finally:
        for p in patches:
            p.stop()
    print("test_validation PASSED")


def test_notes_sidecar_is_not_a_user_file() -> None:
    assert not auth._is_candidate_user_file("bob@example.com_notes.json")
    assert auth._is_candidate_user_file("bob@example.com.json")
    print("test_notes_sidecar_is_not_a_user_file PASSED")


def test_delete_user_removes_notes_sidecar() -> None:
    store = _FakeStore()
    patches = _patched(store, auth)
    for p in patches:
        p.start()
    try:
        um = auth.UserManager(storage_location="users", bootstrap=True)
        ok, _ = um.add_user("alice", "pw", "viewer", approved=True)
        assert ok
        store.files["alice_notes.json"] = {"notes": [{"id": "1", "author": "a", "timestamp": "t", "text": "x"}]}
        ok, _ = um.delete_user("alice")
        assert ok
        assert "alice.json" not in store.files
        assert "alice_notes.json" not in store.files
    finally:
        for p in patches:
            p.stop()
    print("test_delete_user_removes_notes_sidecar PASSED")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:
                failed += 1
                print(f"{name} FAILED")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
