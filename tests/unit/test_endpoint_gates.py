"""Access-control regression tests for the S2 permission sweep.

Covers the endpoints the 2026-07 audit found ungated: the Video Analysis
media stream + item detail (previously reachable with NO authentication),
the Explore metadata/filter endpoints and the Timelines endpoints
(previously ``login_required`` only, no permission key / study scoping),
the worker-log endpoint (now admin-only), the ``/api/status`` redaction,
and the conditional ``internal_bp`` registration.

Same stubbed-user approach as ``test_correlations_api.py``.
"""

import pytest

_TEST_VIEWER = "__gates_test_viewer__"

# (method, path, json) triples for every study-scoped endpoint fixed in the
# sweep. The study/item values are arbitrary — the tests only exercise gates.
_STUDY_ENDPOINTS = [
    ("GET", "/api/explore/metadata/base?study=secret", None),
    ("GET", "/api/explore/metadata/overlay?study=secret", None),
    ("GET", "/api/explore/metadata?study=secret", None),
    ("POST", "/api/explore/filter", {"study": "secret"}),
    ("POST", "/api/video_analysis/ids", {"study": "secret"}),
    ("GET", "/api/video_analysis/item/secret/12345", None),
    ("GET", "/api/video/secret/12345", None),
    ("POST", "/api/timelines/data", {"study": "secret", "collection_id": "c1"}),
    ("POST", "/api/timelines/collections", {}),
    ("POST", "/api/timelines/vote_annotation", {"collection_id": "c1", "period": "p"}),
]






@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client






def _login(client, username):
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True






def _grant_permissions(monkeypatch, perms):
    """Make the viewer role hold exactly ``perms``."""
    from web_interface import auth

    monkeypatch.setattr(auth.role_manager, "get_role_permissions", lambda role: list(perms))






def _request(client, method, path, payload):
    if method == "GET":
        return client.get(path)
    return client.post(path, json=payload)






def test_unauthenticated_is_rejected(client):
    for method, path, payload in _STUDY_ENDPOINTS:
        res = _request(client, method, path, payload)
        assert res.status_code in (302, 401), path






def test_requires_tab_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    for method, path, payload in _STUDY_ENDPOINTS:
        res = _request(client, method, path, payload)
        assert res.status_code == 403, path






def test_inaccessible_study_is_403(client, monkeypatch):
    """Tab permission alone is not enough — the study must be accessible."""
    from web_interface.routes import _access

    _grant_permissions(monkeypatch, [
        "tab.explore", "tab.video_analysis", "tab.timelines"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["other_study"])
    _login(client, _TEST_VIEWER)

    # Timelines/data is excluded: its access model is collection-based (an
    # inaccessible *study* filter degrades to unscoped rather than 403).
    study_scoped = [e for e in _STUDY_ENDPOINTS
                    if ("secret" in e[1] or (e[2] or {}).get("study") == "secret")
                    and not e[1].startswith("/api/timelines/")]
    assert len(study_scoped) == 7
    for method, path, payload in study_scoped:
        res = _request(client, method, path, payload)
        assert res.status_code == 403, path






def test_video_stream_requires_item_in_study(client, monkeypatch):
    """The study segment of the media stream URL is enforced, not decorative."""
    from web_interface.routes import _access
    from web_interface.routes import api_viewer_routes as viewer

    _grant_permissions(monkeypatch, ["tab.video_analysis"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset({"111"}))
    _login(client, _TEST_VIEWER)

    # Item not in the study -> 404 before any media resolution
    res = client.get("/api/video/mystudy/999")
    assert res.status_code == 404
    assert b"not found in this study" in res.data.lower()

    # Item in the study passes the gate and proceeds to media resolution
    monkeypatch.setattr(viewer.media_paths, "resolve_media",
                        lambda item_id, platform=None: None)
    res = client.get("/api/video/mystudy/111")
    assert res.status_code == 404
    assert b"video 111 not found" in res.data.lower()






def test_eval_stream_coder_access(client, monkeypatch):
    """Invited coders may stream their tasks' eval items — nothing else."""
    from web_interface.routes import api_viewer_routes as viewer

    _grant_permissions(monkeypatch, [])
    monkeypatch.setattr(viewer.human_eval, "tasks_for_user",
                        lambda username: [{"run_id": "r1", "task_type": "coding"}])
    monkeypatch.setattr(viewer.human_eval, "load_task",
                        lambda run_id, task_type: {"item_ids": ["111"]})
    monkeypatch.setattr(viewer.media_paths, "resolve_media",
                        lambda item_id, platform=None: None)
    _login(client, _TEST_VIEWER)

    viewer._EVAL_ACCESS_CACHE.clear()
    res = client.get("/api/video/eval/111")
    assert res.status_code == 404  # passed the gate, media absent in tests
    assert b"video 111 not found" in res.data.lower()

    res = client.get("/api/video/eval/999")
    assert res.status_code == 403
    viewer._EVAL_ACCESS_CACHE.clear()






def test_worker_logs_admin_only(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/logs/queue_annotator")
    assert res.status_code == 403






def test_status_redacted_for_plain_viewers(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/status")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload  # the badge still works for every logged-in user
    for name, entry in payload.items():
        assert "task_args" not in entry, name
        assert "last_run_study" not in entry, name






def test_status_full_for_dm_permission_holders(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.data_management.refresh"])
    _login(client, _TEST_VIEWER)
    res = client.get("/api/status")
    assert res.status_code == 200
    payload = res.get_json()
    assert any("task_args" in entry for entry in payload.values())






def test_personal_write_surface_requires_permission(client, monkeypatch):
    """S4: the formerly login-only write endpoints now need a permission key."""
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)

    assert client.get("/api/studies/defined").status_code == 403
    assert client.post("/api/video_analysis/tags/save", json={}).status_code == 403
    assert client.delete("/api/video_analysis/tags/sometag").status_code == 403
    assert client.post("/api/video_analysis/vote", json={"item_id": "1"}).status_code == 403




def test_personal_write_surface_passes_with_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [
        "tab.explore", "tab.my_stuff.video_tags", "feature.annotation_votes"])
    _login(client, _TEST_VIEWER)

    assert client.get("/api/studies/defined").status_code == 200
    # Passes the gate; fails input validation before any write happens.
    res = client.post("/api/video_analysis/tags/save", json={})
    assert res.status_code == 400
    res = client.post("/api/video_analysis/vote", json={})
    assert res.status_code == 400




def test_timelines_vote_requires_votes_key(client, monkeypatch):
    """tab.timelines alone no longer suffices for vote_annotation (AND gate)."""
    from web_interface import security
    from web_interface.routes import _access

    _grant_permissions(monkeypatch, ["tab.timelines"])
    _login(client, _TEST_VIEWER)
    res = client.post("/api/timelines/vote_annotation",
                      json={"collection_id": "c1", "period": "p"})
    assert res.status_code == 403

    _grant_permissions(monkeypatch, ["tab.timelines", "feature.annotation_votes"])
    monkeypatch.setattr(_access, "get_study_collections",
                        lambda study: [{"collection_id": "c1"}])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["s1"])
    monkeypatch.setattr(security.user_manager, "register_annotation_vote",
                        lambda *a, **k: (True, "ok"))
    res = client.post("/api/timelines/vote_annotation",
                      json={"collection_id": "c1", "period": "p"})
    assert res.status_code == 200




def test_user_settings_key_whitelist(client, monkeypatch):
    from web_interface import security

    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    monkeypatch.setattr(security.user_manager, "update_user_settings",
                        lambda *a, **k: (True, "ok"))

    res = client.post("/api/user/settings", json={"arbitrary_key": {"x": 1}})
    assert res.status_code == 400
    assert b"unknown settings keys" in res.data.lower()

    res = client.post("/api/user/settings", json={"video_autostart": True})
    assert res.status_code == 200

    res = client.post("/api/user/settings", json=["not", "a", "dict"])
    assert res.status_code == 400




def test_internal_bp_only_on_task_runner(monkeypatch):
    """On Cloud Run, /internal/run-task exists only on the task-runner."""
    import web_interface.fyp_data_hub as hub

    def _rules(app):
        return {r.rule for r in app.url_map.iter_rules()}

    # Public web service: no task-execution endpoint
    monkeypatch.setenv("K_SERVICE", "fyp-data-hub")
    monkeypatch.setattr(hub, "_IS_TASK_RUNNER", False)
    web_app = hub.create_app()
    assert "/internal/run-task/<name>" not in _rules(web_app)

    # Task-runner: endpoint present
    monkeypatch.setattr(hub, "_IS_TASK_RUNNER", True)
    runner_app = hub.create_app()
    assert "/internal/run-task/<name>" in _rules(runner_app)

    # Local dev (no K_SERVICE): present for parity
    monkeypatch.delenv("K_SERVICE")
    monkeypatch.setattr(hub, "_IS_TASK_RUNNER", False)
    local_app = hub.create_app()
    assert "/internal/run-task/<name>" in _rules(local_app)
