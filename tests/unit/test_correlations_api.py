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
    res = client.get("/api/correlations/status?study=any")
    assert res.status_code in (302, 401)






def test_requires_tab_permission(client, monkeypatch):
    _grant_permissions(monkeypatch, [])
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "any", "x_col": "a", "y_col": "b"})
        assert res.status_code == 403, endpoint
    res = client.get("/api/correlations/status?study=any")
    assert res.status_code == 403






def test_inaccessible_study_is_403(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["other_study"])
    _login(client, _TEST_VIEWER)
    for endpoint in _ENDPOINTS:
        res = client.post(endpoint, json={"study": "secret", "x_col": "a", "y_col": "b"})
        assert res.status_code == 403, endpoint
    res = client.get("/api/correlations/status?study=secret")
    assert res.status_code == 403






def test_accessible_study_without_pca_is_404(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])
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
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])

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
    assert [v["key"] for v in payload["views"]] == ["scatter", "heatmap", "group_stats"]
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





def test_pairwise_stats_match_scipy():
    """Golden values: r/p per pair against scipy, q against manual BH."""
    import numpy as np
    from scipy import stats as sps

    from web_interface.services.correlations_service import pairwise_correlation_stats

    rng = np.random.RandomState(7)
    df = pd.DataFrame({
        "a": rng.normal(size=40),
        "b": rng.normal(size=40),
    })
    df["c"] = df["a"] * 0.8 + rng.normal(scale=0.5, size=40)
    df.loc[df.index[:5], "b"] = float("nan")  # pairwise-n differs per pair

    for method in ("pearson", "spearman"):
        r, p, q, n = pairwise_correlation_stats(df, method)
        cols = list(df.columns)
        pairs = [(0, 1), (0, 2), (1, 2)]
        raw_p = {}
        for i, j in pairs:
            x = df[cols[i]]
            y = df[cols[j]]
            mask = x.notna() & y.notna()
            assert n[i][j] == int(mask.sum())
            fn = sps.spearmanr if method == "spearman" else sps.pearsonr
            expect = fn(x[mask], y[mask])
            assert r[i][j] == pytest.approx(float(expect[0]), abs=1e-12)
            assert p[i][j] == pytest.approx(float(expect[1]), abs=1e-12)
            raw_p[(i, j)] = float(expect[1])

        # Manual Benjamini-Hochberg over the three pairs
        ordered = sorted(raw_p.items(), key=lambda kv: kv[1])
        m = len(ordered)
        bh = {}
        prev = 1.0
        for rank_idx in range(m - 1, -1, -1):
            key, pval = ordered[rank_idx]
            val = min(prev, pval * m / (rank_idx + 1))
            bh[key] = val
            prev = val
        for (i, j), qv in bh.items():
            assert q[i][j] == pytest.approx(qv, abs=1e-12)

    # Diagonal: r=1, n=full column count
    r, p, q, n = pairwise_correlation_stats(df, "pearson")
    assert r[0][0] == 1.0 and n[0][0] == 40






def test_regression_stats_match_scipy():
    import numpy as np
    from scipy import stats as sps

    from web_interface.services.correlations_service import compute_regression_stats

    rng = np.random.RandomState(3)
    x = rng.normal(size=50)
    y = 2.0 * x + rng.normal(scale=0.7, size=50)

    stats = compute_regression_stats(x, y)
    ref = sps.linregress(x, y)
    t_crit = sps.t.ppf(0.975, 48)

    assert stats["n"] == 50
    assert stats["slope"] == pytest.approx(ref.slope)
    assert stats["intercept"] == pytest.approx(ref.intercept)
    assert stats["r"] == pytest.approx(ref.rvalue)
    assert stats["r2"] == pytest.approx(ref.rvalue ** 2)
    assert stats["p"] == pytest.approx(ref.pvalue)
    assert stats["ci_low"] == pytest.approx(ref.slope - t_crit * ref.stderr)
    assert stats["ci_high"] == pytest.approx(ref.slope + t_crit * ref.stderr)

    # Degenerate inputs return None instead of nonsense
    assert compute_regression_stats([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert compute_regression_stats([1.0, 2.0], [1.0, 2.0]) is None






def test_group_ellipses_match_numpy_cov():
    import numpy as np

    from web_interface.services.correlations_service import compute_group_ellipses

    rng = np.random.RandomState(11)
    df = pd.DataFrame({
        "x": rng.normal(size=30),
        "y": rng.normal(size=30),
        "grp": ["a"] * 20 + ["b"] * 10,
    })

    ellipses = {e["group"]: e for e in compute_group_ellipses(df, "x", "y", "grp")}
    assert set(ellipses) == {"a", "b"}
    sub = df[df["grp"] == "a"]
    cov = np.cov(sub["x"], sub["y"])
    assert ellipses["a"]["n"] == 20
    assert ellipses["a"]["mean_x"] == pytest.approx(float(sub["x"].mean()))
    assert ellipses["a"]["cov"][0][1] == pytest.approx(float(cov[0, 1]))

    # No colour column -> a single "Default" group over everything
    all_e = compute_group_ellipses(df, "x", "y", None)
    assert [e["group"] for e in all_e] == ["Default"]
    assert all_e[0]["n"] == 30

    # Tiny groups (n < 3) are skipped
    tiny = df.head(2).assign(grp="t")
    assert compute_group_ellipses(tiny, "x", "y", "grp") == []






def test_within_collection_centering():
    from web_interface.services.correlations_service import apply_within_collection_centering

    df = pd.DataFrame({
        "collection_id": ["a", "a", "b", "b"],
        "v": [1.0, 3.0, 10.0, 30.0],
    })
    out, applied = apply_within_collection_centering(df, ["v"])
    assert applied
    means = out.groupby("collection_id")["v"].mean()
    assert means["a"] == pytest.approx(0.0)
    assert means["b"] == pytest.approx(0.0)
    # Within-collection differences survive
    assert out["v"].tolist() == [-1.0, 1.0, -10.0, 10.0]

    # No collection_id column -> untouched no-op
    df2 = pd.DataFrame({"v": [1.0, 2.0]})
    out2, applied2 = apply_within_collection_centering(df2, ["v"])
    assert not applied2
    assert out2 is df2






def test_matrix_payload_v2_fields(client, monkeypatch):
    """families ordering, n/p/q matrices, method override and centering flag."""
    from web_interface.routes import api_correlations_routes as routes
    from web_interface.services import correlations_service

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(correlations_service, "load_interpretations", lambda study: {})

    rng_vals = [0.1, 0.9, 0.4, 0.7, 0.2, 0.8]
    df = pd.DataFrame({
        "a": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "b": [2.0, 4.1, 5.9, 8.2, 9.9, 12.1],
        "c": rng_vals,
        "collection_id": ["x", "x", "x", "y", "y", "y"],
    })
    monkeypatch.setattr(routes, "get_pca_df", lambda study: df)

    _login(client, _TEST_VIEWER)
    res = client.post("/api/correlations/correlation_matrix",
                      json={"study": "mystudy", "method": "spearman", "center": True})
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["method"] == "spearman"
    assert payload["centered"] is True
    k = len(payload["columns"])
    assert len(payload["families"]) == k
    for key in ("matrix", "p_matrix", "q_matrix", "n_matrix"):
        assert len(payload[key]) == k and len(payload[key][0]) == k
    # Diagonal is r=1 with the column's own n
    i = payload["columns"].index("a")
    assert payload["matrix"][i][i] == pytest.approx(1.0)
    assert payload["n_matrix"][i][i] == 6
    # An invalid method degrades to the configured default
    res = client.post("/api/correlations/correlation_matrix",
                      json={"study": "mystudy", "method": "nonsense"})
    assert res.get_json()["method"] in ("pearson", "spearman")






def test_status_payload_staleness(monkeypatch):
    import fyp.data_io as data_io

    from web_interface.services.correlations_service import build_status_payload

    mtimes = {"s_PCA.parquet": 100.0, "s_recoded.parquet": 50.0}
    monkeypatch.setattr(data_io, "exists",
                        lambda storage_location, filename, **kw: filename in mtimes)
    monkeypatch.setattr(data_io, "getmtime",
                        lambda storage_location, filename, **kw: mtimes[filename])

    fresh = build_status_payload("s")
    assert fresh["has_pca"] and not fresh["stale"]

    mtimes["s_recoded.parquet"] = 200.0
    assert build_status_payload("s")["stale"]

    del mtimes["s_PCA.parquet"]
    missing = build_status_payload("s")
    assert not missing["has_pca"] and not missing["stale"]






def test_scatter_payload_stats_and_ellipses(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])

    df = pd.DataFrame({
        "x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "y": [1.1, 2.2, 2.9, 4.2, 4.8, 6.1],
        "collection_id": ["a", "a", "a", "b", "b", "b"],
    })
    monkeypatch.setattr(routes, "get_pca_df", lambda study: df)

    _login(client, _TEST_VIEWER)
    res = client.post("/api/correlations/data", json={
        "study": "mystudy", "x_col": "x", "y_col": "y", "color_col": "collection_id"})
    assert res.status_code == 200
    payload = res.get_json()
    stats = payload["stats"]
    assert stats["n"] == 6
    assert stats["ci_low"] < stats["slope"] < stats["ci_high"]
    assert 0 <= stats["p"] <= 1
    groups = {e["group"] for e in payload["group_ellipses"]}
    assert groups == {"a", "b"}
    assert payload["centered"] is False
    assert payload["ellipse_coverage"] == pytest.approx(0.95)





def test_group_stats_endpoint(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes
    from web_interface.services import correlations_service

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])
    _login(client, _TEST_VIEWER)

    # Missing artifact -> 404 with a hint
    monkeypatch.setattr(correlations_service, "load_group_stats", lambda study: None)
    res = client.post("/api/correlations/group_stats", json={"study": "mystudy"})
    assert res.status_code == 404
    assert "hint" in res.get_json()

    # Present artifact is served verbatim
    artifact = {"version": 1, "study": "mystudy", "n_groups": 12,
                "anova": [], "permanova": []}
    monkeypatch.setattr(correlations_service, "load_group_stats", lambda study: artifact)
    res = client.post("/api/correlations/group_stats", json={"study": "mystudy"})
    assert res.status_code == 200
    assert res.get_json()["n_groups"] == 12

    # Access control mirrors the other endpoints
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: [])
    res = client.post("/api/correlations/group_stats", json={"study": "mystudy"})
    assert res.status_code == 403






def test_matrix_payload_includes_reliability(client, monkeypatch):
    from web_interface.routes import api_correlations_routes as routes
    from web_interface.services import correlations_service as cs

    _grant_permissions(monkeypatch, ["tab.correlations"])
    monkeypatch.setattr("web_interface.routes._access.get_accessible_studies", lambda *a, **k: ["mystudy"])
    monkeypatch.setattr(cs, "load_interpretations", lambda study: {})
    monkeypatch.setattr(cs, "load_reliability_map", lambda: {
        "a": {"reliability": 0.6, "n": 40, "source": "machine test-retest"},
    })

    df = pd.DataFrame({
        "a": [1.0, 2.0, 3.0, 4.0],
        "b": [2.0, 4.0, 5.5, 8.5],
    })
    monkeypatch.setattr(routes, "get_pca_df", lambda study: df)

    _login(client, _TEST_VIEWER)
    res = client.post("/api/correlations/correlation_matrix", json={"study": "mystudy"})
    assert res.status_code == 200
    payload = res.get_json()
    assert "a" in payload["reliability"]
    assert payload["reliability"]["a"]["item_r"] == pytest.approx(0.6)
    assert payload["reliability"]["a"]["group_r"] > 0.6  # Spearman-Brown boost
    assert payload["reliability_k"] >= 1
