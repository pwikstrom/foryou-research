#!/usr/bin/env python3
"""Tests for UserManager's lazy-load mode.

The task-runner service on Cloud Run shares the docker image with the web
service, but serves only Cloud Tasks internal routes and never authenticates
browser traffic. We skip the user bulk-load at cold start there so startup
cost and memory use stay O(1) in the number of users instead of O(N).

These tests verify:

1. ``bulk_load=True`` (default / web) still eagerly fans out and populates
   ``self.users`` — behavior unchanged for the web service.
2. ``bulk_load=False`` (task-runner) does NOT touch GCS / the storage
   location at construction time — no listdir, no per-user loads, no
   default-admin creation.
3. In lazy mode, ``get_user`` falls back to reading a single file from
   storage on demand and caches the result.
4. Lazy lookups for missing users return None cleanly.

Run:
    source .fypenv314/bin/activate
    PYTHONPATH=. python tests/unit/test_user_manager_lazy.py
"""


import sys
import traceback
from pathlib import Path
from unittest.mock import patch

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))


# Import the module under test. Instantiation of UserManager triggers
# data_io calls via `load_users()`, so we patch those calls inside each
# test to avoid hitting real storage.
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





def test_bulk_load_true_fans_out_and_populates_cache() -> None:
    """Default mode (web service) does the eager listdir + per-file load."""
    disk = _fake_users_on_disk()

    def fake_listdir(storage_location, return_absolute_path=False):
        assert storage_location == "users"
        return list(disk.keys())

    def fake_load_json(storage_location, filename, **kwargs):
        assert storage_location == "users"
        return disk.get(filename)

    def fake_exists(storage_location, filename):
        return filename in disk

    with patch.object(auth.data_io, "listdir", side_effect=fake_listdir), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json), \
         patch.object(auth.data_io, "exists", side_effect=fake_exists):
        um = auth.UserManager(storage_location="users", bulk_load=True)

    assert set(um.users.keys()) == {"alice", "bob", "carol"}
    assert um.users["alice"].role == "admin"
    assert um.users["carol"].approved is False





def test_bulk_load_false_does_not_hit_storage_at_init() -> None:
    """Lazy mode must NOT touch listdir / load_json / exists at construction.

    This is the whole point of the quick-win: task-runner cold start becomes
    O(1) in the number of users instead of O(N) GCS round-trips.
    """
    with patch.object(auth.data_io, "listdir") as mock_list, \
         patch.object(auth.data_io, "load_json") as mock_load, \
         patch.object(auth.data_io, "exists") as mock_exists, \
         patch.object(auth.data_io, "save_json") as mock_save:
        um = auth.UserManager(storage_location="users", bulk_load=False)

    assert mock_list.call_count == 0, (
        f"listdir called {mock_list.call_count} times in lazy mode — "
        "should be zero"
    )
    assert mock_load.call_count == 0
    assert mock_exists.call_count == 0
    assert mock_save.call_count == 0, (
        "save_json called in lazy mode — default-admin creation must be "
        "skipped when bulk_load=False"
    )
    assert um.users == {}
    assert um.bulk_load is False





def test_lazy_get_user_reads_from_disk_and_caches() -> None:
    """`get_user` in lazy mode loads a single file on demand and caches."""
    disk = _fake_users_on_disk()
    load_calls: list[str] = []

    def fake_exists(storage_location, filename):
        return filename in disk

    def fake_load_json(storage_location, filename, **kwargs):
        load_calls.append(filename)
        return disk.get(filename)

    with patch.object(auth.data_io, "exists", side_effect=fake_exists), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json):
        um = auth.UserManager(storage_location="users", bulk_load=False)

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
        um = auth.UserManager(storage_location="users", bulk_load=False)
        result = um.get_user("nobody")

    assert result is None
    # We shouldn't even call load_json if exists returned False.
    assert mock_load.call_count == 0





def test_bulk_load_get_user_does_not_fall_back_to_disk() -> None:
    """In bulk_load mode, a cache miss should return None without hitting
    disk — the whole point is that we loaded everything at startup, so a
    miss here means the user really doesn't exist. Falling back to disk
    would cause silent re-reads after a user is deleted mid-session."""
    disk = _fake_users_on_disk()

    def fake_listdir(storage_location, return_absolute_path=False):
        return list(disk.keys())

    def fake_load_json(storage_location, filename, **kwargs):
        return disk.get(filename)

    def fake_exists(storage_location, filename):
        return filename in disk

    with patch.object(auth.data_io, "listdir", side_effect=fake_listdir), \
         patch.object(auth.data_io, "load_json", side_effect=fake_load_json), \
         patch.object(auth.data_io, "exists", side_effect=fake_exists):
        um = auth.UserManager(storage_location="users", bulk_load=True)

    with patch.object(auth.data_io, "load_json") as mock_load, \
         patch.object(auth.data_io, "exists") as mock_exists:
        result = um.get_user("nonexistent-user")

    assert result is None
    assert mock_load.call_count == 0
    assert mock_exists.call_count == 0





TESTS = [
    test_bulk_load_true_fans_out_and_populates_cache,
    test_bulk_load_false_does_not_hit_storage_at_init,
    test_lazy_get_user_reads_from_disk_and_caches,
    test_lazy_get_user_missing_returns_none,
    test_bulk_load_get_user_does_not_fall_back_to_disk,
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
