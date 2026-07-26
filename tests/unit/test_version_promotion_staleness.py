"""Persistent "studies stale" signal after promoting a preferred annotation version.

The promote endpoint writes ``process_stats["annotation_versions"]["promotion_impact"]``;
``_evaluate_version_promotion_staleness`` reports it stale until
``recode_refresh_studies`` succeeds after the promotion timestamp, then pops
the marker (auto-clear). The staleness endpoint is additionally reachable with
only the ``tab.admin.versions`` permission (the Versions-page banner).
"""

import pytest

from web_interface.services import stats_service

_TEST_VERSIONS_USER = "__promo_stale_versions_user__"






@pytest.fixture
def stats(monkeypatch):
    """Isolate the shared process_stats dict: no disk I/O, restored after."""
    monkeypatch.setattr(stats_service, "load_process_stats", lambda: None)
    monkeypatch.setattr(stats_service, "save_process_stats", lambda: None)
    ps = stats_service.process_stats
    saved = {k: ps.get(k) for k in ("annotation_versions", "recode_refresh_studies")}
    yield ps
    for key, value in saved.items():
        if value is None:
            ps.pop(key, None)
        else:
            ps[key] = value






def test_no_marker_means_fresh(stats):
    stats.pop("annotation_versions", None)
    result = stats_service._evaluate_version_promotion_staleness()
    assert result == {"has_impact": False, "impact": None, "stale": False}






def test_stale_until_refresh_succeeds(stats):
    stats["annotation_versions"] = {"promotion_impact": {
        "timestamp": "2026-07-26T10:00:00+00:00", "version": "av_x",
        "previous_version": "av_y"}}
    stats["recode_refresh_studies"] = {"last_success": "2026-07-26T09:00:00+00:00"}
    result = stats_service._evaluate_version_promotion_staleness()
    assert result["stale"] is True and result["has_impact"] is True

    # No refresh recorded at all is also stale.
    stats["recode_refresh_studies"] = {}
    assert stats_service._evaluate_version_promotion_staleness()["stale"] is True






def test_auto_clears_after_successful_refresh(stats):
    stats["annotation_versions"] = {"promotion_impact": {
        "timestamp": "2026-07-26T10:00:00+00:00", "version": "av_x",
        "previous_version": "av_y"}}
    stats["recode_refresh_studies"] = {"last_success": "2026-07-26T11:00:00+00:00"}
    result = stats_service._evaluate_version_promotion_staleness()
    assert result["stale"] is False
    # Marker popped so the signal never lingers.
    assert "promotion_impact" not in stats.get("annotation_versions", {})






def test_staleness_endpoint_allows_versions_permission(monkeypatch, stats):
    """A user holding only tab.admin.versions can read the staleness endpoint."""
    import web_interface.permissions as permissions
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VERSIONS_USER:
            return User(username=_TEST_VERSIONS_USER, role="viewer",
                        password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    def _versions_only(user, key):
        if getattr(user, "username", "") == _TEST_VERSIONS_USER:
            return key == "tab.admin.versions"
        return orig_perm(user, key)

    orig_perm = permissions.user_has_permission
    monkeypatch.setattr(permissions, "user_has_permission", _versions_only)
    # The consolidation evaluator also runs inside the endpoint — keep it off
    # the real stats file too.
    monkeypatch.setattr(
        "web_interface.routes.management.enrichment._evaluate_consolidation_staleness",
        lambda: {"has_impact": False, "impact": None, "processes": {}})
    monkeypatch.setattr(
        "web_interface.routes.management.enrichment._evaluate_version_promotion_staleness",
        lambda: {"has_impact": True,
                 "impact": {"timestamp": "t", "version": "av_x"}, "stale": True})

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["_user_id"] = _TEST_VERSIONS_USER
            sess["_fresh"] = True
        res = client.get("/api/manage/refresh/staleness")
        assert res.status_code == 200
        body = res.get_json()
        assert body["version_promotion"]["stale"] is True
