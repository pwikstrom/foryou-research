"""The Explore cold-open fast path (2026-08-19).

An empty-filter /api/explore/filter on a study whose frame is not in RAM used
to block behind the full multi-GB parquet load — the stats ribbons sat empty
for 30-40s while the filter panel (served from the metadata JSON) was already
rendered. The fast path serves the snapshot's total_stats immediately and
stamps the response ``warming``; the client then sends ONE follow-up request
with ``wait_for_frame``, which must bypass the fast path and block on the
load (Cloud Run gives background threads ~no CPU, so the load has to ride a
request).

Uses the Flask test client with a stubbed admin (same approach as
``test_status_poll_cache.py``).
"""

import pytest

from web_interface.routes import api_explorer_routes as routes

_TEST_ADMIN = "__coldopen_test_admin__"


@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=uid, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(routes, "study_access_error", lambda study: None)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield test_client


def _arm(monkeypatch, cached):
    monkeypatch.setattr(routes, "is_study_frame_cached", lambda s: cached)
    monkeypatch.setattr(routes, "get_explorer_metadata_cached",
                        lambda s: {"total_stats": {"niche": {"cats": 3}},
                                   "total_rows": 1234})


def test_cold_empty_filter_serves_snapshot(client, monkeypatch):
    _arm(monkeypatch, cached=False)

    res = client.post("/api/explore/filter",
                      json={"study": "s1", "filters": {}, "trigger_slice": 1})
    payload = res.get_json()

    assert payload["warming"] is True
    assert payload["stats"] == {"niche": {"cats": 3}}
    assert payload["count"] == 1234


def test_wait_for_frame_bypasses_the_fast_path(client, monkeypatch):
    """The client's follow-up request must reach the blocking load path."""
    _arm(monkeypatch, cached=False)
    sentinel = {}
    def _fake_get_explorer_data(study, **kw):
        sentinel["hit"] = True
        return None, None
    monkeypatch.setattr(routes, "get_explorer_data", _fake_get_explorer_data)

    res = client.post("/api/explore/filter",
                      json={"study": "s1", "filters": {},
                            "wait_for_frame": True})
    assert sentinel.get("hit") is True
    assert res.get_json().get("warming") is None


def test_active_filters_never_take_the_fast_path(client, monkeypatch):
    """A real filter needs the frame — it must block on the normal path, not
    silently return unfiltered snapshot stats."""
    _arm(monkeypatch, cached=False)
    # The normal path would need real study data; stub the frame fetch to
    # prove control flow reached it (and stop there).
    sentinel = {}
    def _fake_get_explorer_data(study, **kw):
        sentinel["hit"] = True
        return None, None
    monkeypatch.setattr(routes, "get_explorer_data", _fake_get_explorer_data)

    res = client.post("/api/explore/filter",
                      json={"study": "s1",
                            "filters": {"niche": {"value": ["cats"]}}})
    assert sentinel.get("hit") is True
    assert res.get_json().get("warming") is None


def test_warm_frame_never_takes_the_fast_path(client, monkeypatch):
    _arm(monkeypatch, cached=True)
    sentinel = {}
    def _fake_get_explorer_data(study, **kw):
        sentinel["hit"] = True
        return None, None
    monkeypatch.setattr(routes, "get_explorer_data", _fake_get_explorer_data)

    res = client.post("/api/explore/filter", json={"study": "s1", "filters": {}})
    assert sentinel.get("hit") is True
    assert res.get_json().get("warming") is None
