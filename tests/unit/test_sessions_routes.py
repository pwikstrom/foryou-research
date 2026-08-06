"""API contract tests for the Sessions tab routes (/api/sessions/*).

Uses the Flask test client with a stubbed admin user (same approach as
``test_admin_settings_route.py``); the artifact reads are monkeypatched so no
real sessions index is required.
"""

import pandas as pd
import pytest

_TEST_ADMIN = "__sessions_test_admin__"






@pytest.fixture
def client(monkeypatch):
    from web_interface import security
    from web_interface.auth import ROLE_ADMIN, User
    from web_interface.fyp_data_hub import app

    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == _TEST_ADMIN:
            return User(username=_TEST_ADMIN, role=ROLE_ADMIN, password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["_user_id"] = _TEST_ADMIN
            sess["_fresh"] = True
        yield test_client






def _index_df():
    rows = [
        {"collection_id": "colA", "session_id": "colA__0", "start_ts": "2026-01-01T10:00:00",
         "end_ts": "2026-01-01T10:30:00", "duration_min": 30.0, "n_plays": 40, "n_distinct": 35,
         "total_watch_s": 900.0, "median_dwell_s": 20.0, "n_scraped": 30, "n_annotated": 28,
         "n_embedded": 25, "coverage_scraped": 0.86, "coverage_annotated": 0.8,
         "coverage_embedded": 0.71, "emb_play_coverage": 0.7, "min_window_cosdist": 0.30,
         "min_window_entropy_norm": 0.5, "n_episodes": 2, "episode_play_frac": 0.3,
         "dominant_niche": "Recipes", "n_niches": 4},
        {"collection_id": "colA", "session_id": "colA__1", "start_ts": "2026-01-02T10:00:00",
         "end_ts": "2026-01-02T10:10:00", "duration_min": 10.0, "n_plays": 12, "n_distinct": 12,
         "total_watch_s": 200.0, "median_dwell_s": 15.0, "n_scraped": 4, "n_annotated": 3,
         "n_embedded": 2, "coverage_scraped": 0.33, "coverage_annotated": 0.25,
         "coverage_embedded": 0.17, "emb_play_coverage": 0.15, "min_window_cosdist": None,
         "min_window_entropy_norm": None, "n_episodes": 0, "episode_play_frac": 0.0,
         "dominant_niche": None, "n_niches": 1},
        {"collection_id": "colB", "session_id": "colB__0", "start_ts": "2026-01-03T10:00:00",
         "end_ts": "2026-01-03T11:00:00", "duration_min": 60.0, "n_plays": 80, "n_distinct": 70,
         "total_watch_s": 2000.0, "median_dwell_s": 22.0, "n_scraped": 65, "n_annotated": 60,
         "n_embedded": 56, "coverage_scraped": 0.93, "coverage_annotated": 0.86,
         "coverage_embedded": 0.8, "emb_play_coverage": 0.78, "min_window_cosdist": 0.55,
         "min_window_entropy_norm": 0.8, "n_episodes": 1, "episode_play_frac": 0.1,
         "dominant_niche": "News", "n_niches": 9},
    ]
    df = pd.DataFrame(rows)
    df["collection_id"] = df["collection_id"].astype("string")
    df["session_id"] = df["session_id"].astype("string")
    df["min_window_cosdist"] = pd.to_numeric(df["min_window_cosdist"], errors="coerce")
    return df






@pytest.fixture
def patched_routes(monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "study_access_error", lambda study: None)
    monkeypatch.setattr(mod, "_load_index", _index_df)
    monkeypatch.setattr(mod, "_study_collection_ids", lambda study: {"colA", "colB"})
    monkeypatch.setattr(mod, "load_display_id_map", lambda: {"colA": "Donor A"})
    monkeypatch.setattr(mod, "_load_meta", lambda: {
        "built_at": "2026-08-01T00:00:00+00:00", "embedding_model": "gemini-embedding-001",
        "params": {"cut": 0.5}})
    return mod






def test_overview_requires_study(client, patched_routes):
    res = client.get("/api/sessions/overview")
    assert res.status_code == 400






def test_overview_requires_login():
    from web_interface.fyp_data_hub import app
    app.testing = True
    with app.test_client() as anon:
        res = anon.get("/api/sessions/overview?study=x")
        assert res.status_code in (302, 401)






def test_overview_filters_and_sorts(client, patched_routes):
    res = client.get("/api/sessions/overview?study=s&min_coverage=0.5&min_emb_plays=5")
    assert res.status_code == 200
    body = res.get_json()
    # colA__1 fails both floors; the two survivors sort by focus asc, NaN last.
    assert body["total_in_study"] == 3
    assert body["total_matching"] == 2
    ids = [s["session_id"] for s in body["sessions"]]
    assert ids == ["colA__0", "colB__0"]
    assert body["sessions"][0]["collection_label"] == "Donor A"
    assert body["defaults"]["min_coverage"] == 0.5
    assert body["meta"]["embedding_model"] == "gemini-embedding-001"

    # NaN focus sorts last when nothing is filtered out.
    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0")
    ids = [s["session_id"] for s in res.get_json()["sessions"]]
    assert ids[-1] == "colA__1"






def test_overview_unknown_sort_falls_back(client, patched_routes):
    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0&sort=__nope__")
    assert res.status_code == 200
    assert res.get_json()["sessions"][0]["session_id"] == "colA__0"






def test_overview_404_without_artifact(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod
    monkeypatch.setattr(mod, "_load_index", lambda: None)
    res = client.get("/api/sessions/overview?study=s")
    assert res.status_code == 404






def test_detail_validates_params_and_membership(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod
    res = client.get("/api/sessions/detail?study=s&collection_id=colA")
    assert res.status_code == 400

    monkeypatch.setattr(mod, "_study_collection_ids", lambda study: {"colB"})
    res = client.get("/api/sessions/detail?study=s&collection_id=colA&session_id=colA__0")
    assert res.status_code == 403






def test_detail_payload_flags_and_episode_assignment(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.api_viewer_routes as viewer

    plays = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2", "v3"], dtype="string"),
        "_ts": pd.to_datetime(["2026-01-01T10:00:00", "2026-01-01T10:05:00",
                               "2026-01-01T10:29:00"]),
        "play_duration": [10.0, 20.0, 30.0],
        "source_platform": ["tiktok"] * 3,
    })
    episodes = [{
        "episode_idx": 0, "start_ts": "2026-01-01T10:00:00", "end_ts": "2026-01-01T10:06:00",
        "duration_min": 6.0, "n_plays": 2, "n_distinct": 2, "repeat_rate": 1.0,
        "n_interleaved": 0, "focus": 0.2, "diameter": 0.3, "step_mean": 0.1,
        "straightness": 0.2, "spectral_entropy_bits": 1.0, "effective_rank": 2.0,
        "dominant_niche": "Recipes", "dominant_niche_share": 1.0, "n_niches": 1,
        "n_authors": 1, "dominant_author_share": 1.0, "advertising": None,
        "advertising_share": 0.0, "mean_political": 0.0, "mean_sensitivity": 0.0,
        "members": [
            {"item_id": "v1", "ts": "2026-01-01T10:00:00", "dwell_s": 10.0, "rolling_cosdist": None},
            {"item_id": "v2", "ts": "2026-01-01T10:05:00", "dwell_s": 20.0, "rolling_cosdist": 0.1},
        ],
    }]
    feat = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2"], dtype="string"),
        "niche_name": ["Recipes", "Recipes"], "category": ["Food", "Food"],
        "story": ["s1", "s2"], "political_score": [0.0, 0.0],
        "sensitivity_score": [0.0, 0.0], "advertising": ["none", "none"],
        "author": ["a", "a"],
    }).set_index("item_id")

    windows = [{
        "window_idx": 0, "start_ts": "2026-01-01T10:00:00", "end_ts": "2026-01-01T10:29:00",
        "duration_min": 29.0, "n_distinct": 2, "mean_cosdist": 0.21, "entropy_norm": 0.4,
        "dominant_niche": "Recipes",
        "members": [
            {"item_id": "v1", "ts": "2026-01-01T10:00:00", "dwell_s": 10.0},
            {"item_id": "v3", "ts": "2026-01-01T10:29:00", "dwell_s": 30.0},
        ],
    }]

    monkeypatch.setattr(mod, "_session_plays", lambda cid, row: plays)
    monkeypatch.setattr(mod, "_session_episodes", lambda cid, sid: episodes)
    monkeypatch.setattr(mod, "_session_windows", lambda cid, sid: windows)
    monkeypatch.setattr(mod, "_features", lambda: feat)
    monkeypatch.setattr(mod, "_flag_sets", lambda: {
        "scraped": {"v1", "v2"}, "downloaded": {"v1"},
        "annotated": {"v1", "v2"}, "embedded": {"v1", "v2"}})
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset({"v1", "v3"}))

    res = client.get("/api/sessions/detail?study=s&collection_id=colA&session_id=colA__0")
    assert res.status_code == 200
    body = res.get_json()
    assert body["session"]["session_id"] == "colA__0"
    p1, p2, p3 = body["plays"]
    # v1: in study frame AND downloaded → streamable; episode member in span.
    assert p1["streamable"] is True and p1["episode_idx"] == 0
    # v2: annotated+embedded but not downloaded → not streamable; member → ep 0.
    assert p2["streamable"] is False and p2["episode_idx"] == 0
    assert p2["annotated"] is True and p2["embedded"] is True
    # v3: outside the episode span and not a member.
    assert p3["episode_idx"] is None
    assert body["episodes"][0]["members"][0]["item_id"] == "v1"
    # Low-entropy windows ride along with their members.
    assert body["windows"][0]["mean_cosdist"] == 0.21
    assert [m["item_id"] for m in body["windows"][0]["members"]] == ["v1", "v3"]






def test_status_reports_model_mismatch(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.management_routes as mgmt

    monkeypatch.setattr(mod, "_fingerprint", lambda fn: "1:2")
    monkeypatch.setattr(mod, "_load_meta", lambda: {
        "built_at": "2026-08-01T00:00:00+00:00", "embedding_model": "some-other-model"})
    monkeypatch.setattr(mgmt, "_is_worker_running", lambda name: False)

    res = client.get("/api/sessions/status")
    assert res.status_code == 200
    body = res.get_json()
    assert body["artifact_exists"] is True
    if body["active_embedding_model"]:
        assert body["model_mismatch"] is True
