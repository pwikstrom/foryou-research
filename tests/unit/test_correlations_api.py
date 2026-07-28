"""Correlations API access control + helpers + the mtime-keyed PCA cache.

Uses the Flask test client with stubbed users (same approach as
``test_data_contracts_api.py``). Role permissions and study access are
monkeypatched so no roles.json / study data is required.
"""

import pandas as pd
import pytest

_TEST_VIEWER = "__correlations_test_viewer__"

_ENDPOINTS = [
    "/api/correlations/metadata",
    "/api/correlations/data",
    "/api/correlations/correlation_matrix",
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






def test_unauthenticated_is_rejected(client):
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "any"})
        assert res.status_code in (302, 401), endpoint






def test_requires_tab_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "any", "x_col": "a", "y_col": "b"})
        assert res.status_code == 403, endpoint






def test_inaccessible_study_is_403(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr(routes, "get_accessible_studies", lambda *a, **k: ["other_study"])
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "secret", "x_col": "a", "y_col": "b"})
        assert res.status_code == 403, endpoint






def test_accessible_study_without_pca_is_404(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr(routes, "get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(routes, "get_pca_df", lambda study: None)
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "mystudy", "x_col": "a", "y_col": "b"})
        assert res.status_code == 404, endpoint






def test_missing_study_param_is_400(client, monkeypatch):
    _grant_permissions(monkeypatch, ["tab.correlations"])
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={})
        assert res.status_code == 400, endpoint






def test_format_week_value():
    from web_interface.routes.api_correlations_routes import _format_week_value

    assert _format_week_value("2025-3") == "2025-03"
    assert _format_week_value("2025-W3") == "2025-03"
    assert _format_week_value("2025-W12") == "2025-12"
    assert _format_week_value("2025-12") == "2025-12"
    # Non-week values pass through untouched
    assert _format_week_value("hello") == "hello"
    assert _format_week_value("2025-06-01") == "2025-06-01"
    assert _format_week_value(42) == "42"






def test_correlation_matrix_nulls_not_zero(client, monkeypatch):
    """An undefined correlation must serialize as null, not r = 0."""
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr(routes, "get_accessible_studies", lambda *a, **k: ["mystudy"])

    # c and d never overlap, so their pairwise correlation is undefined
    df = pd.DataFrame({
        "a": [1.0, 2.0, 3.0, 4.0],
        "b": [2.0, 4.0, 6.0, 8.0],
        "c": [1.0, 2.0, float("nan"), float("nan")],
        "d": [float("nan"), float("nan"), 3.0, 4.0],
    })
    monkeypatch.setattr(routes, "get_pca_df", lambda study: df)
    monkeypatch.setattr(routes, "_load_interpretations", lambda study: {})

    _login(client, _TEST_VIEWER)
    res = client.post("/api/correlations/correlation_matrix", json={"study": "mystudy"})
    assert res.status_code == 200
    payload = res.get_json()
    cols = payload["columns"]
    matrix = payload["matrix"]
    ia, ib = cols.index("a"), cols.index("b")
    ic, i_d = cols.index("c"), cols.index("d")
    assert matrix[ia][ib] == pytest.approx(1.0)
    assert matrix[ic][i_d] is None






def test_pca_cache_invalidates_on_mtime_change(monkeypatch):
    """get_pca_df must reload when the parquet's mtime changes on disk."""
    import fyp.data_io as data_io
    from web_interface.services import analysis_data

    state = {"mtime": 100.0, "loads": 0}
    df_v1 = pd.DataFrame({"x": [1]})
    df_v2 = pd.DataFrame({"x": [1, 2]})

    monkeypatch.setattr(data_io, "exists", lambda storage_location, filename, **kw: True)
    monkeypatch.setattr(data_io, "getmtime", lambda storage_location, filename, **kw: state["mtime"])

    def _fake_load(storage_location, filename, **kw):
        state["loads"] += 1
        return df_v1 if state["mtime"] == 100.0 else df_v2

    monkeypatch.setattr(data_io, "load_parquet", _fake_load)

    with analysis_data._pca_cache_lock:
        analysis_data._pca_cache.clear()

    # First call loads from disk, second is served from RAM
    assert analysis_data.get_pca_df("mystudy") is df_v1
    assert analysis_data.get_pca_df("mystudy") is df_v1
    assert state["loads"] == 1

    # A worker rewrites the artifact -> mtime changes -> cache invalidates
    state["mtime"] = 200.0
    assert analysis_data.get_pca_df("mystudy") is df_v2
    assert state["loads"] == 2

    with analysis_data._pca_cache_lock:
        analysis_data._pca_cache.clear()
