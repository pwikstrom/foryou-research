#!/usr/bin/env python3
"""Integration tests for UserManager mutations under lazy roster loading.

The lazy-roster refactor rewrote every mutation method: single-user operations
(add / approve / password / settings / vote / last-login / verify) now route
through ``get_user`` (single lazy load), while the last-admin guards
(``delete_user`` / ``update_user_role``) call ``_ensure_loaded`` to see the full
roster. These tests exercise the whole surface end-to-end against a stateful
in-memory storage mock, so a regression in any single-user path or in an
admin-count guard is caught without touching real user files.

Run:
    source .venv/bin/activate
    PYTHONPATH=. python tests/unit/test_user_manager_mutations.py
"""


import copy
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))


from web_interface import auth  # noqa: E402




class _FakeStore:
    """A minimal stateful stand-in for the ``users`` storage location."""

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




def _manager_over(store: _FakeStore, bootstrap: bool = True):
    """Build a UserManager whose data_io is backed by ``store``."""
    patches = [
        patch.object(auth.data_io, "exists", side_effect=store.exists),
        patch.object(auth.data_io, "listdir", side_effect=store.listdir),
        patch.object(auth.data_io, "load_json", side_effect=store.load_json),
        patch.object(auth.data_io, "save_json", side_effect=store.save_json),
        patch.object(auth.data_io, "remove", side_effect=store.remove),
    ]
    for p in patches:
        p.start()
    try:
        um = auth.UserManager(storage_location="users", bootstrap=bootstrap)
    except Exception:
        for p in patches:
            p.stop()
        raise
    return um, patches




def test_bootstrap_creates_default_admin_on_empty_store() -> None:
    store = _FakeStore()
    um, patches = _manager_over(store, bootstrap=True)
    try:
        assert "admin@admin.net.json" in store.files, "fresh store must get a default admin"
        assert store.files["admin@admin.net.json"]["role"] == "admin"
        assert store.files["admin@admin.net.json"]["approved"] is True
    finally:
        for p in patches:
            p.stop()




def test_full_mutation_lifecycle() -> None:
    store = _FakeStore()
    um, patches = _manager_over(store, bootstrap=True)
    try:
        # add a viewer, initially unapproved
        ok, _ = um.add_user("alice", "pw", "viewer", approved=False)
        assert ok and "alice.json" in store.files

        # duplicate add is rejected (single-user existence check)
        ok, _ = um.add_user("alice", "pw2", "viewer")
        assert not ok

        # unapproved user cannot log in
        assert um.verify_user("alice", "pw") is None

        # approve, then login works; wrong password rejected
        ok, _ = um.approve_user("alice")
        assert ok
        u = um.verify_user("alice", "pw")
        assert u is not None and u.username == "alice"
        assert um.verify_user("alice", "wrong") is None

        # settings + last-login persist to storage
        ok, _ = um.update_user_settings("alice", {"video_autostart": True})
        assert ok and store.files["alice.json"]["settings"]["video_autostart"] is True
        um.update_last_login("alice")
        assert store.files["alice.json"]["last_login"] is not None

        # password change: old fails, new works
        ok, _ = um.update_password("alice", "pw3")
        assert ok
        assert um.verify_user("alice", "pw") is None
        assert um.verify_user("alice", "pw3") is not None

        # promote alice to admin (now two admins)
        ok, _ = um.update_user_role("alice", "admin")
        assert ok and store.files["alice.json"]["role"] == "admin"

        # with two admins, deleting one is allowed
        ok, _ = um.delete_user("alice")
        assert ok and "alice.json" not in store.files

        # only the default admin remains — the last-admin guard must block it
        ok, msg = um.delete_user("admin@admin.net")
        assert not ok, "deleting the last admin must be blocked"
    finally:
        for p in patches:
            p.stop()




def test_last_admin_demotion_blocked() -> None:
    store = _FakeStore()
    um, patches = _manager_over(store, bootstrap=True)
    try:
        # only the default admin exists; demoting it must be refused
        ok, msg = um.update_user_role("admin@admin.net", "viewer")
        assert not ok, "demoting the last admin must be blocked"
        assert store.files["admin@admin.net.json"]["role"] == "admin"
    finally:
        for p in patches:
            p.stop()




TESTS = [
    test_bootstrap_creates_default_admin_on_empty_store,
    test_full_mutation_lifecycle,
    test_last_admin_demotion_blocked,
]




def main() -> int:
    fails = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            fails += 1
    total = len(TESTS)
    print(f"\n{total - fails}/{total} passed")
    return 0 if fails == 0 else 1




if __name__ == "__main__":
    sys.exit(main())
