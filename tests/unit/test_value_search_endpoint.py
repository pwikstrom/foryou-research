"""Per-variable value search: endpoint contract + counts cache.

The capped dropdowns only persist the top-200 count>1 values; the
``/api/explore/values/search`` endpoint scans the live column through
``search_column_value_counts`` so out-of-top-200 and single-occurrence
values stay reachable. Same stubbed-user approach as
``test_endpoint_gates.py``.
"""

import pandas as pd
import pytest

from web_interface import explorer_backend as explorer
from web_interface.services import study_data

_TEST_VIEWER = "__value_search_test_viewer__"






@pytest.fixture
def client(monkeypatch):
    from web_interface import auth, security
    from web_interface.auth import ROLE_VIEWER, User
    from web_interface.fyp_data_hub import app
    from web_interface.routes import _access

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_VIEWER:
            return User(username=_TEST_VIEWER, role=ROLE_VIEWER, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(auth.role_manager, "get_role_permissions",
                        lambda role: ["tab.explore"])
    monkeypatch.setattr(_access, "get_accessible_studies", lambda *a, **k: ["s1"])

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_VIEWER
            sess["_fresh"] = True
        yield test_client






def _entry(pairs, dtype="category"):
    """Build a counts-cache entry like ``search_column_value_counts`` does."""
    values = [k for k, _ in pairs]
    return {
        "mtime": 1.0,
        "type": dtype,
        "values": values,
        "counts": [v for _, v in pairs],
        "lowered": pd.Series(values, dtype="string[pyarrow]").str.lower(),
    }






@pytest.fixture
def stub_counts(monkeypatch):
    def _install(pairs, dtype="category"):
        entry = _entry(pairs, dtype=dtype)
        monkeypatch.setattr(
            "web_interface.routes.api_explorer_routes.search_column_value_counts",
            lambda study, column: entry,
        )
    return _install






def test_case_insensitive_substring_and_singletons(client, stub_counts):
    stub_counts([("BigAuthor", 500), ("FooBar", 3), ("tinyfoo", 1)])

    res = client.get("/api/explore/values/search?study=s1&column=author_handle&q=FOO")

    assert res.status_code == 200
    body = res.get_json()
    assert body["matches"] == [{"value": "FooBar", "count": 3},
                               {"value": "tinyfoo", "count": 1}]
    assert body["total_matches"] == 2
    assert body["truncated"] is False






def test_limit_and_truncated(client, stub_counts):
    stub_counts([(f"handle_{i:03}", 100 - i) for i in range(60)])

    res = client.get("/api/explore/values/search?study=s1&column=author_handle&q=handle&limit=10")

    body = res.get_json()
    assert len(body["matches"]) == 10
    assert body["total_matches"] == 60
    assert body["truncated"] is True
    # Frequency order preserved: most frequent first.
    assert body["matches"][0]["value"] == "handle_000"






def test_min_query_length_is_400(client, stub_counts):
    stub_counts([("aa", 2)])
    res = client.get("/api/explore/values/search?study=s1&column=author_handle&q=a")
    assert res.status_code == 400






def test_missing_params_are_400(client):
    assert client.get("/api/explore/values/search?study=s1&q=ab").status_code == 400
    assert client.get("/api/explore/values/search?column=c&q=ab").status_code == 400






def test_dynamic_column_is_400(client, stub_counts):
    stub_counts([("fave", 10)])
    for col in ("extra_data", "Collection Tags", "User Tags"):
        res = client.get(f"/api/explore/values/search?study=s1&column={col}&q=ab")
        assert res.status_code == 400, col






def test_unknown_column_is_404(client, monkeypatch):
    monkeypatch.setattr(
        "web_interface.routes.api_explorer_routes.search_column_value_counts",
        lambda study, column: None,
    )
    res = client.get("/api/explore/values/search?study=s1&column=nope&q=ab")
    assert res.status_code == 404






# --- column_value_counts + get_metadata parity ------------------------------


def _category_frame():
    return pd.DataFrame({
        "author": pd.array(["a"] * 3 + ["b"] * 2 + ["c"], dtype="string[pyarrow]"),
    })






def test_column_value_counts_includes_singletons():
    df = _category_frame()
    pairs = explorer.column_value_counts(df["author"], "category")
    assert pairs == [("a", 3), ("b", 2), ("c", 1)]






def test_column_value_counts_list_document_frequency():
    import pyarrow as pa
    tags = pd.array([["x", "y", "x"], ["y"], None, ["z"]],
                    dtype=pd.ArrowDtype(pa.list_(pa.string())))
    pairs = explorer.column_value_counts(pd.Series(tags), "list")
    # Within-row dedup: x counts once for row 0.
    assert dict(pairs) == {"x": 1, "y": 2, "z": 1}
    assert pairs[0] == ("y", 2)






def test_get_metadata_still_drops_singletons_and_caps():
    df = _category_frame()
    meta = explorer.get_metadata(df, {"author": "category"})
    assert meta["author"]["values"] == [
        {"value": "a", "count": 3}, {"value": "b", "count": 2}]
    assert meta["author"]["total_unique"] == 2






def test_date_columns_normalise_to_date_part():
    ts = pd.Series(pd.to_datetime(["2026-01-02 10:00", "2026-01-02 11:00",
                                   "2026-01-03 09:00"]))
    pairs = explorer.column_value_counts(ts, "category")
    assert pairs == [("2026-01-02", 2), ("2026-01-03", 1)]






# --- search_column_value_counts cache ---------------------------------------


def test_counts_cache_invalidates_on_mtime_change(monkeypatch):
    study = "__cache_test_study__"
    series = pd.Series(pd.array(["a", "a", "b"], dtype="string[pyarrow]"))
    calls = {"n": 0}

    def fake_get_search_column(s, c):
        calls["n"] += 1
        return series, "category"

    mtime = {"v": 1.0}
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: mtime["v"])
    monkeypatch.setattr(study_data, "get_search_column", fake_get_search_column)

    first = study_data.search_column_value_counts(study, "col")
    again = study_data.search_column_value_counts(study, "col")
    assert first["values"] == ["a", "b"]
    assert again is first
    assert calls["n"] == 1

    mtime["v"] = 2.0
    fresh = study_data.search_column_value_counts(study, "col")
    assert fresh is not first
    assert calls["n"] == 2






def test_counts_cache_rejects_unsearchable_dtype(monkeypatch):
    monkeypatch.setattr(study_data, "_get_recoded_mtime", lambda s: 1.0)
    monkeypatch.setattr(study_data, "get_search_column",
                        lambda s, c: (pd.Series([1, 2]), "number"))
    assert study_data.search_column_value_counts("__num_study__", "plays") is None
