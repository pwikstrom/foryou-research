"""Verify the process-log endpoints (/api/logs/<name>, /api/logs/clear/<name>).

Covers the key resolution that used to make keyed statuses unreadable
(``study_refresh__<study>`` was always looked up under the bare process name
and always missed), the ``?run=`` / ``?since=`` contract the modal polls with,
the response shape the async-annotator card feed depends on, the legacy
fallback that keeps the deploy window readable, and rejection of keys that
would escape the storage root.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TEST_ADMIN = "__logs_test_admin__"
_TEST_VIEWER = "__logs_test_viewer__"


def _fake_io(store: dict):
    """A data_io stand-in backed by an in-memory dict keyed on filename."""

    class FakeIO:
        @staticmethod
        def exists(storage_location="cache", filename="", verbose=False):
            return filename in store

        @staticmethod
        def load_json(storage_location="cache", filename="", verbose=False):
            return json.loads(store[filename])

        @staticmethod
        def remove(storage_location="cache", filename="", verbose=False):
            del store[filename]

        @staticmethod
        def update_json(storage_location="cache", filename="", mutate=None,
                        default=None, max_retries=6, verbose=False):
            current = json.loads(store[filename]) if filename in store else default
            result = mutate(current)
            if result is not None:
                store[filename] = json.dumps(result)
            return result

    return FakeIO


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="",
                        approved=True)
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="",
                        approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def store():
    """An isolated in-memory run-log store, with module buffers reset."""
    from web_interface import run_logs

    data: dict = {}
    with run_logs._states_lock:
        run_logs._states.clear()
    with patch.object(run_logs, "data_io", _fake_io(data)):
        yield data
    with run_logs._states_lock:
        for state in run_logs._states.values():
            run_logs._stop_flusher(state)
        run_logs._states.clear()


def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True






def test_returns_the_current_run_with_its_banner(client, store):
    from web_interface import run_logs

    run_logs.open_run("pca_refresh", started_by="patrik", mode="subprocess")
    run_logs.append("pca_refresh", "recoding studies")
    run_logs.flush("pca_refresh")

    _login(client, _TEST_ADMIN)
    body = client.get("/api/logs/pca_refresh").get_json()

    assert "Started by patrik" in body["logs"]
    assert "recoding studies" in body["logs"]
    assert body["run_id"]
    assert body["runs"][0]["started_by"] == "patrik"


def test_keyed_status_names_are_readable(client, store):
    # Regression: the cloud lookup used the bare process name, so a per-study
    # refresh's log ("study_refresh__<study>") could never be found.
    from web_interface import run_logs

    run_logs.open_run("study_refresh__my_study", started_by="patrik")
    run_logs.append("study_refresh__my_study", "refreshing my_study")
    run_logs.flush("study_refresh__my_study")

    _login(client, _TEST_ADMIN)
    res = client.get("/api/logs/study_refresh__my_study")

    assert res.status_code == 200
    assert "refreshing my_study" in res.get_json()["logs"]


def test_since_cursor_returns_only_new_lines(client, store):
    from web_interface import run_logs

    run_logs.open_run("pca_refresh")
    run_logs.append("pca_refresh", "line one")
    run_logs.flush("pca_refresh")

    _login(client, _TEST_ADMIN)
    first = client.get("/api/logs/pca_refresh").get_json()
    assert first["reset"] is True

    run_logs.append("pca_refresh", "line two")
    run_logs.flush("pca_refresh")
    second = client.get(
        f"/api/logs/pca_refresh?since={first['next_since']}").get_json()

    assert "line two" in second["logs"]
    assert "line one" not in second["logs"]
    assert second["reset"] is False


def test_a_previous_run_can_be_requested_by_id(client, store):
    from web_interface import run_logs

    old_id = run_logs.open_run("pca_refresh", started_by="alice")
    run_logs.append("pca_refresh", "the old run")
    run_logs.finalize("pca_refresh", run_logs.STATE_COMPLETED)
    run_logs.open_run("pca_refresh", started_by="bob")
    run_logs.append("pca_refresh", "the new run")
    run_logs.flush("pca_refresh")

    _login(client, _TEST_ADMIN)
    body = client.get(f"/api/logs/pca_refresh?run={old_id}").get_json()

    assert "the old run" in body["logs"]
    assert "the new run" not in body["logs"]
    assert body["run"]["started_by"] == "alice"
    assert len(body["runs"]) == 2


def test_logs_stays_a_newline_joined_string(client, store):
    # The async-annotator card feed reads this endpoint and splits on '\n'.
    from web_interface import run_logs

    run_logs.open_run("queue_annotator_batch")
    run_logs.append("queue_annotator_batch", "batch 1 submitted")
    run_logs.append("queue_annotator_batch", "batch 2 submitted")
    run_logs.flush("queue_annotator_batch")

    _login(client, _TEST_ADMIN)
    body = client.get("/api/logs/queue_annotator_batch").get_json()

    assert isinstance(body["logs"], str)
    lines = [ln for ln in body["logs"].split("\n") if "submitted" in ln]
    assert len(lines) == 2


def test_falls_back_to_the_in_memory_deque_before_any_run_exists(client, store):
    # Covers the deploy window: an older worker is still writing its log the
    # old way, and pre-migration runs have nothing in the new store.
    from web_interface.process_manager import processes

    processes["pca_refresh"]["logs"].append("legacy line\n")
    try:
        _login(client, _TEST_ADMIN)
        body = client.get("/api/logs/pca_refresh").get_json()
    finally:
        processes["pca_refresh"]["logs"].clear()

    assert "legacy line" in body["logs"]
    assert body["runs"] == []


def test_timestamps_are_present_on_every_line(client, store):
    import re

    from web_interface import run_logs

    run_logs.open_run("pca_refresh", started_by="patrik")
    run_logs.append("pca_refresh", "doing the work")
    run_logs.flush("pca_refresh")

    _login(client, _TEST_ADMIN)
    body = client.get("/api/logs/pca_refresh").get_json()

    lines = [ln for ln in body["logs"].split("\n") if ln.strip()]
    assert lines
    assert all(re.match(r"^\[\d\d:\d\d:\d\d\] ", ln) for ln in lines)






def test_unknown_process_is_rejected(client, store):
    _login(client, _TEST_ADMIN)
    assert client.get("/api/logs/not_a_process").status_code == 400


def test_traversal_keys_are_rejected(client, store):
    _login(client, _TEST_ADMIN)
    for bad in ("study_refresh__..%2f..%2fsecrets", "..", "pca_refresh%2f..%2fx"):
        assert client.get(f"/api/logs/{bad}").status_code in (400, 404), bad


def test_clear_removes_the_history_for_everyone(client, store):
    from web_interface import run_logs

    run_logs.open_run("pca_refresh")
    run_logs.append("pca_refresh", "line")
    run_logs.flush("pca_refresh")
    assert store

    _login(client, _TEST_ADMIN)
    assert client.post("/api/logs/clear/pca_refresh").status_code == 200
    assert store == {}


def test_log_endpoints_stay_admin_only(client, store):
    _login(client, _TEST_VIEWER)
    assert client.get("/api/logs/pca_refresh").status_code == 403
    assert client.post("/api/logs/clear/pca_refresh").status_code == 403


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
