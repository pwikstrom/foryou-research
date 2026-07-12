#!/usr/bin/env python3
"""Tests for UserManager's lazy roster loading.

The full user roster is never eagerly loaded at cold start on either Cloud Run
service — startup stays O(1) in the number of users instead of O(N) GCS
round-trips. ``get_user()`` lazily loads a single user file on demand (the
auth/login hot path), and the full roster is loaded once, on first access, by
``get_all_users()`` (admin pages, role checks, last-admin guards).

The ``bootstrap`` flag only decides whether an instance runs the one-time
legacy-data migration and ensures a default admin exists — the web service owns
the store (``bootstrap=True``); the task-runner skips both (``bootstrap=False``).

These tests verify:

1. ``bootstrap=True`` (web) does NOT preload the roster — no per-user fan-out at
   construction; existing users mean no default admin is created.
2. ``bootstrap=False`` (task-runner) does NOT touch storage at construction —
   no listdir, no per-user loads, no default-admin creation.
3. ``get_user`` lazily reads a single file on demand and caches it.
4. Lazy lookups for missing users return None cleanly.
5. ``get_all_users`` fans out the full roster once and caches it.
6. ``get_user`` lazy-loads even in bootstrap mode (the roster is not preloaded).

Run:
    source .venv/bin/activate
    PYTHONPATH=. python tests/unit/test_user_manager_lazy.py
"""


import sys
import traceback
from pathlib import Path
from unittest.mock import patch

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))


# Import the module under test. UserManager construction touches data_io only
# for the O(1) bootstrap checks, so we patch those calls inside each test to
# avoid hitting real storage.
from web_interface import auth  # noqa: E402




def _fake_users_on_disk() -> dict[str, dict]:
    """Three synthetic users with distinct roles / settings."""
    return {
        "alice.json": {
            "username": "alice",
            "role": "admin",
            "password_hash": "x",
            "approved": True,
            "last_login": None,
            "settings": {"share_annotations": True},
        },
        "bob.json": {
            "username": "bob",
            "role": "viewer",
            "password_hash": "y",
            "approved": True,
            "last_login": None,
            "settings": {},
        },
        "carol.json": {
            "username": "carol",
            "role": "viewer",
            "password_hash": "z",
            "approved": False,
            "last_login": None,
            "settings": {},
        },
    }




def test_bootstrap_true_does_not_preload_roster() -> None:
    """Web mode runs the O(1) bootstrap checks but does NOT fan out the roster."""
    disk = _fake_users_on_disk()
    load_calls: list[str] = []

    def fake_listdir(storage_location, return_absolute_path=False):
        return list(disk.keys())

    def fake_exists(storage_location, filename):
        return filename in disk

    def fake_load_json(storage_location, filename, **kwargs):
        load_calls.append(filename)
        return disk.get(filename)

    with patch.object(auth.data_io, "listdir", side_effect=fake_listdir), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json), \
         patch.object(auth.data_io, "exists", side_effect=fake_exists), \
         patch.object(auth.data_io, "save_json") as mock_save:
        um = auth.UserManager(storage_location="users", bootstrap=True)

    assert um.users == {}, "bootstrap must not preload the full roster"
    assert load_calls == [], f"no per-user load at init, got {load_calls}"
    assert mock_save.call_count == 0, "existing users => no default admin created"




def test_bootstrap_false_does_not_hit_storage_at_init() -> None:
    """Lazy mode must NOT touch listdir / load_json / exists at construction.

    This is the whole point: task-runner cold start is O(1) in the number of
    users instead of O(N) GCS round-trips.
    """
    with patch.object(auth.data_io, "listdir") as mock_list, \
         patch.object(auth.data_io, "load_json") as mock_load, \
         patch.object(auth.data_io, "exists") as mock_exists, \
         patch.object(auth.data_io, "save_json") as mock_save:
        um = auth.UserManager(storage_location="users", bootstrap=False)

    assert mock_list.call_count == 0, (
        f"listdir called {mock_list.call_count} times in lazy mode — should be zero"
    )
    assert mock_load.call_count == 0
    assert mock_exists.call_count == 0
    assert mock_save.call_count == 0, (
        "save_json called in lazy mode — migration + default-admin must be "
        "skipped when bootstrap=False"
    )
    assert um.users == {}
    assert um.bootstrap is False




def test_lazy_get_user_reads_from_disk_and_caches() -> None:
    """`get_user` loads a single file on demand and caches it."""
    disk = _fake_users_on_disk()
    load_calls: list[str] = []

    def fake_exists(storage_location, filename):
        return filename in disk

    def fake_load_json(storage_location, filename, **kwargs):
        load_calls.append(filename)
        return disk.get(filename)

    with patch.object(auth.data_io, "exists", side_effect=fake_exists), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json):
        um = auth.UserManager(storage_location="users", bootstrap=False)

        alice = um.get_user("alice")
        assert alice is not None
        assert alice.username == "alice"
        assert alice.role == "admin"
        assert load_calls == ["alice.json"]

        # Second lookup must hit the in-memory cache (no extra load).
        alice_again = um.get_user("alice")
        assert alice_again is alice
        assert load_calls == ["alice.json"]

        # A different user triggers exactly one more load.
        bob = um.get_user("bob")
        assert bob is not None
        assert bob.role == "viewer"
        assert load_calls == ["alice.json", "bob.json"]




def test_lazy_get_user_missing_returns_none() -> None:
    """Lookups for users that don't exist on disk return None cleanly."""
    def fake_exists(storage_location, filename):
        return False

    with patch.object(auth.data_io, "exists", side_effect=fake_exists), \
         patch.object(auth.data_io, "load_json") as mock_load:
        um = auth.UserManager(storage_location="users", bootstrap=False)
        result = um.get_user("nobody")

    assert result is None
    # We shouldn't even call load_json if exists returned False.
    assert mock_load.call_count == 0




def test_get_all_users_fans_out_and_caches() -> None:
    """`get_all_users` loads the full roster once, then serves it from cache."""
    disk = _fake_users_on_disk()
    listdir_calls: list[str] = []

    def fake_listdir(storage_location, return_absolute_path=False):
        listdir_calls.append(storage_location)
        return list(disk.keys())

    def fake_load_json(storage_location, filename, **kwargs):
        return disk.get(filename)

    def fake_exists(storage_location, filename):
        return filename in disk

    with patch.object(auth.data_io, "listdir", side_effect=fake_listdir), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json), \
         patch.object(auth.data_io, "exists", side_effect=fake_exists):
        um = auth.UserManager(storage_location="users", bootstrap=False)

        roster = um.get_all_users()
        assert set(roster.keys()) == {"alice", "bob", "carol"}
        assert roster["alice"].role == "admin"
        assert roster["carol"].approved is False
        first_count = len(listdir_calls)
        assert first_count >= 1

        # Second call must hit the cache — no additional listdir/reload.
        roster2 = um.get_all_users()
        assert set(roster2.keys()) == {"alice", "bob", "carol"}
        assert len(listdir_calls) == first_count, (
            "get_all_users must not reload the roster on the second call"
        )




def test_get_user_lazy_loads_in_bootstrap_mode() -> None:
    """`get_user` lazy-loads a single file even in bootstrap mode.

    The roster is no longer preloaded on the web service either, so login must
    be able to load one user on demand.
    """
    disk = _fake_users_on_disk()
    load_calls: list[str] = []

    def fake_listdir(storage_location, return_absolute_path=False):
        return list(disk.keys())  # non-empty => default-admin no-op

    def fake_exists(storage_location, filename):
        return filename in disk

    def fake_load_json(storage_location, filename, **kwargs):
        load_calls.append(filename)
        return disk.get(filename)

    with patch.object(auth.data_io, "listdir", side_effect=fake_listdir), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json), \
         patch.object(auth.data_io, "exists", side_effect=fake_exists):
        um = auth.UserManager(storage_location="users", bootstrap=True)

        assert um.users == {}, "roster must not be preloaded"
        alice = um.get_user("alice")
        assert alice is not None
        assert alice.role == "admin"
        assert load_calls == ["alice.json"]




TESTS = [
    test_bootstrap_true_does_not_preload_roster,
    test_bootstrap_false_does_not_hit_storage_at_init,
    test_lazy_get_user_reads_from_disk_and_caches,
    test_lazy_get_user_missing_returns_none,
    test_get_all_users_fans_out_and_caches,
    test_get_user_lazy_loads_in_bootstrap_mode,
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
