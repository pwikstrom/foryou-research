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
    from web_interface.services.correlations_service import format_week_value

    assert format_week_value("2025-3") == "2025-03"
    assert format_week_value("2025-W3") == "2025-03"
    assert format_week_value("2025-W12") == "2025-12"
    assert format_week_value("2025-12") == "2025-12"
    # Non-week values pass through untouched
    assert format_week_value("hello") == "hello"
    assert format_week_value("2025-06-01") == "2025-06-01"
    assert format_week_value(42) == "42"






def test_correlation_matrix_nulls_not_zero(client, monkeypatch):
    """An undefined correlation must serialize as null, not r = 0."""
    from web_interface.routes import api_correlations_routes as routes
    from web_interface.services import correlations_service

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
    monkeypatch.setattr(correlations_service, "load_interpretations", lambda study: {})

    _login(client, _TEST_VIEWER)
    res = client.post("/api/correlations/correlation_matrix", json={"study": "mystudy"})
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["method"] == "pearson"
    cols = payload["columns"]
    matrix = payload["matrix"]
    ia, ib = cols.index("a"), cols.index("b")
    ic, i_d = cols.index("c"), cols.index("d")
    assert matrix[ia][ib] == pytest.approx(1.0)
    assert matrix[ic][i_d] is None






def test_corr_settings_config_overrides(monkeypatch):
    """[correlations] config values override the built-in defaults."""
    from web_interface.services import correlations_service as cs

    # Defaults when the section is absent
    monkeypatch.setattr(cs, "fyp_cf", {})
    assert cs.corr_setting("min_variance_pct") == 5.0
    assert cs.corr_setting("max_scatter_points") == 5000
    assert cs.corr_setting("factor_value_limit") == 500
    assert cs.correlation_method() == "pearson"

    # Section values win
    monkeypatch.setattr(cs, "fyp_cf", {"correlations": {
        "min_variance_pct": 20.0,
        "max_scatter_points": 100,
        "correlation_method": "spearman",
    }})
    assert cs.corr_setting("min_variance_pct") == 20.0
    assert cs.corr_setting("max_scatter_points") == 100
    assert cs.correlation_method() == "spearman"

    # Invalid method degrades to pearson
    monkeypatch.setattr(cs, "fyp_cf", {"correlations": {"correlation_method": "kendall-ish"}})
    assert cs.correlation_method() == "pearson"






def test_variance_filter_uses_config_threshold(monkeypatch):
    from web_interface.services import correlations_service as cs

    interpretations = {
        "x_C0": {"explained_variance_pct": 50.0},
        "x_C1": {"explained_variance_pct": 15.0},
        "x_C2": {"explained_variance_pct": 3.0},
    }
    cols = ["x_C0", "x_C1", "x_C2", "plain_numeric"]

    monkeypatch.setattr(cs, "fyp_cf", {"correlations": {"min_variance_pct": 10.0}})
    kept = cs.filter_components_by_variance(cols, interpretations)
    assert "x_C0" in kept and "x_C1" in kept and "plain_numeric" in kept
    assert "x_C2" not in kept

    # A higher threshold still always keeps the top component
    monkeypatch.setattr(cs, "fyp_cf", {"correlations": {"min_variance_pct": 99.0}})
    kept = cs.filter_components_by_variance(cols, interpretations)
    assert kept == sorted(["x_C0", "plain_numeric"])






def test_metadata_payload_prefs_and_views(monkeypatch):
    """The metadata payload carries the stat-view manifest and viz-prefs inputs."""
    from web_interface.services import correlations_service as cs

    df = pd.DataFrame({
        "advertising_C0": [0.1, 0.5, 0.9, 0.2],
        "collection_id": ["a", "a", "b", "b"],
    })
    monkeypatch.setattr(cs, "get_factors_and_features_from_var_schema",
                        lambda **kw: (["collection_id"], []))
    monkeypatch.setattr(cs, "load_interpretations", lambda study: {})
    monkeypatch.setattr(cs, "load_display_id_map", lambda: {})
    monkeypatch.setattr(cs, "load_schema_metadata", lambda m: {
        "viz_priority": ["advertising"],
        "all_variables_order": ["advertising", "aigc"],
        "section_order": ["Content"],
        "schema_map": {"advertising": {"section": "Content", "description": ""}},
    })

    payload = cs.build_metadata_payload(df, "mystudy")
    assert [v["key"] for v in payload["views"]] == ["scatter", "heatmap"]
    assert payload["viz_priority"] == ["advertising"]
    assert payload["all_variables_order"] == ["advertising", "aigc"]
    # The derived component maps back to its base schema variable (real
    # var_schema: 'advertising' is a contract field)
    assert payload["numeric_col_bases"].get("advertising_C0") == "advertising"
    # And the base variable's schema_map entry gained its section for the panel
    assert payload["schema_map"]["advertising"].get("section") == "Content"






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
