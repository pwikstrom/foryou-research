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
    # Patch the two sources rather than _study_collection_ids itself, so every
    # test exercises the real selected-AND-in-frame scoping.
    monkeypatch.setattr(mod, "get_study_collections",
                        lambda study: [{"collection_id": "colA"}, {"collection_id": "colB"}])
    monkeypatch.setattr(mod, "get_study_frame_collections", lambda study: {"colA", "colB"})
    # A wide-open window by default — the date axis of study scoping is
    # exercised explicitly below, and stubbing it keeps every other test off
    # the study-defs store (which the real helper reads from disk).
    monkeypatch.setattr(mod, "get_study_date_window",
                        lambda study: (pd.Timestamp("1970-01-01"), pd.Timestamp("2100-01-01")))
    monkeypatch.setattr(mod, "load_display_id_map", lambda: {"colA": "Donor A"})
    monkeypatch.setattr(mod, "_load_meta", lambda: {
        "built_at": "2026-08-01T00:00:00+00:00", "embedding_model": "gemini-embedding-001",
        "params": {"cut": 0.5}})
    return mod






def test_study_collection_ids_intersects_the_built_frame(monkeypatch):
    """A selected collection the study's frame does not contain is not the study.

    Sampling thresholds / the date window can drop a selected collection
    entirely; the sessions artifacts are global, so without the intersection
    its sessions would surface in a study that has none of its data.
    """
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "get_study_collections",
                        lambda study: [{"collection_id": c} for c in ("colA", "colB", "colC")])

    monkeypatch.setattr(mod, "get_study_frame_collections", lambda study: {"colA", "colC", "colZ"})
    assert mod._study_collection_ids("s") == {"colA", "colC"}

    # Frame missing (study never built) => the raw selection, the only honest answer.
    monkeypatch.setattr(mod, "get_study_frame_collections", lambda study: None)
    assert mod._study_collection_ids("s") == {"colA", "colB", "colC"}

    # Frame present but empty => empty, NOT a fallback to the selection.
    monkeypatch.setattr(mod, "get_study_frame_collections", lambda study: set())
    assert mod._study_collection_ids("s") == set()






def test_overview_excludes_collections_outside_the_study_frame(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "get_study_collections",
                        lambda study: [{"collection_id": c} for c in ("colA", "colB")])
    monkeypatch.setattr(mod, "get_study_frame_collections", lambda study: {"colA"})

    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0")
    assert res.status_code == 200
    body = res.get_json()
    assert body["total_in_study"] == 2
    assert {s["collection_id"] for s in body["sessions"]} == {"colA"}






def test_overview_reports_effective_limits(client, patched_routes, monkeypatch):
    """The tab's limit copy is server-driven: built params + live context_plays."""
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_load_meta", lambda: {
        "built_at": "2026-08-01T00:00:00+00:00", "embedding_model": "gemini-embedding-001",
        "params": {"cut": 0.5, "mem": 6, "min_videos": 7, "min_minutes": 9.0,
                   "window_n": 8, "max_windows": 2}})
    monkeypatch.setattr(mod, "_context_plays", lambda: 5)

    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0")
    params = res.get_json()["params"]
    # Artifact provenance wins for the segmentation limits...
    assert params["min_videos"] == 7 and params["min_minutes"] == 9.0
    assert params["window_n"] == 8 and params["max_windows"] == 2
    # ...and the display-only knob is live.
    assert params["context_plays"] == 5






def test_config_floors_hide_short_sessions_but_still_count_them(client, patched_routes, monkeypatch):
    """The [sessions] floors apply by default and are reported, not silent."""
    import web_interface.routes.api_sessions_routes as mod

    # colA__1 has 12 plays / 10 min; colA__0 has 40 / 30; colB__0 has 80 / 60.
    monkeypatch.setattr(mod, "_session_floors", lambda: {
        "min_plays": 20, "min_session_minutes": 0.0, "min_coverage": 0.0})

    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0").get_json()
    assert body["total_in_study"] == 3          # excluded sessions still counted
    assert body["total_above_floors"] == 2
    assert [s["session_id"] for s in body["sessions"]] == ["colA__0", "colB__0"]
    assert body["floors"] == {"min_plays": 20, "min_session_minutes": 0.0, "min_coverage": 0.0}
    assert body["defaults"]["min_plays"] == 20






def test_minutes_floor_is_applied_independently(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_session_floors", lambda: {
        "min_plays": 0, "min_session_minutes": 45.0, "min_coverage": 0.0})
    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0").get_json()
    # Only colB__0 runs 60 minutes.
    assert [s["session_id"] for s in body["sessions"]] == ["colB__0"]
    assert body["total_above_floors"] == 1






def test_coverage_floor_is_applied_and_counted_as_a_floor(client, patched_routes, monkeypatch):
    """Coverage is a listing floor, so its exclusions ride in total_above_floors."""
    import web_interface.routes.api_sessions_routes as mod

    # coverage_embedded: colA__0 0.71, colA__1 0.17, colB__0 0.80.
    monkeypatch.setattr(mod, "_session_floors", lambda: {
        "min_plays": 0, "min_session_minutes": 0.0, "min_coverage": 0.75})
    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0").get_json()
    assert [s["session_id"] for s in body["sessions"]] == ["colB__0"]
    assert body["total_in_study"] == 3
    assert body["total_above_floors"] == 1
    assert body["floors"]["min_coverage"] == 0.75






def test_query_params_override_the_admin_floors(client, patched_routes, monkeypatch):
    """`min_plays=0` must still mean "show me everything"."""
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_session_floors", lambda: {
        "min_plays": 20, "min_session_minutes": 15.0, "min_coverage": 0.9})
    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0"
                      "&min_plays=0&min_session_minutes=0&min_coverage=0").get_json()
    assert body["total_above_floors"] == 3
    assert body["floors"] == {"min_plays": 0, "min_session_minutes": 0.0, "min_coverage": 0.0}
    # The admin values still ride along as the defaults the UI can show.
    assert body["defaults"]["min_session_minutes"] == 15.0
    assert body["defaults"]["min_coverage"] == 0.9






def test_session_floors_come_from_the_admin_store_in_endpoint_units(monkeypatch):
    """The route converts the admin-facing percentage to the stored fraction."""
    import web_interface.admin_settings as admin_settings
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(admin_settings, "get_session_floors", lambda: {
        "sessions_min_plays": 9, "sessions_min_minutes": 2.5,
        "sessions_min_coverage_pct": 60.0})
    assert mod._session_floors() == {
        "min_plays": 9, "min_session_minutes": 2.5, "min_coverage": 0.6}






def test_committed_config_seeds_the_session_floors():
    """The seed keys must exist in config.toml, or a fresh deploy silently
    falls back to the code defaults instead of the documented values."""
    from fyp.fyp_config import fyp_cf
    from web_interface.admin_settings import SESSION_FLOOR_KEYS

    cfg = fyp_cf.get("sessions", {})
    for cfg_key in SESSION_FLOOR_KEYS.values():
        assert cfg_key in cfg, f"config.toml [sessions] is missing {cfg_key}"
        assert float(cfg[cfg_key]) >= 0






def test_display_params_fall_back_to_config_for_an_older_artifact():
    import web_interface.routes.api_sessions_routes as mod
    from fyp.analysis import session_explorer

    assert mod._display_params(None) == {
        **session_explorer.default_params(),
        "context_plays": mod._context_plays(),
        "drift_p": mod._drift_p(),
        "trend_min_videos": mod._trend_min_videos(),
    }
    assert mod._display_params({"params": {}})["window_n"] == \
        session_explorer.default_params()["window_n"]






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
    assert body["defaults"]["min_emb_plays"] == 5
    assert body["meta"]["embedding_model"] == "gemini-embedding-001"

    # NaN focus sorts last when nothing is filtered out.
    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0")
    ids = [s["session_id"] for s in res.get_json()["sessions"]]
    assert ids[-1] == "colA__1"






def test_overview_unknown_sort_falls_back(client, patched_routes):
    res = client.get("/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0&sort=__nope__")
    assert res.status_code == 200
    assert res.get_json()["sessions"][0]["session_id"] == "colA__0"






_BASE = "/api/sessions/overview?study=s&min_coverage=0&min_emb_plays=0&min_plays=0&min_session_minutes=0"




def test_overview_reports_filter_ranges(client, patched_routes):
    """The ranges block carries slider bounds, unmoved by the user's filters."""
    body = client.get(_BASE).get_json()
    assert body["ranges"]["start_date"] == ["2026-01-01", "2026-01-03"]
    assert body["ranges"]["n_plays"] == [12.0, 80.0]
    assert body["ranges"]["min_window_cosdist"] == [0.30, 0.55]
    assert body["ranges"]["n_episodes"] == [0.0, 2.0]
    assert body["page"] == 0
    assert body["page_size"] == 200

    filtered = client.get(_BASE + "&f_plays_min=20").get_json()
    assert filtered["ranges"] == body["ranges"]






def test_overview_range_filters(client, patched_routes):
    res = client.get(_BASE + "&f_plays_min=20&f_plays_max=50")
    assert [s["session_id"] for s in res.get_json()["sessions"]] == ["colA__0"]

    res = client.get(_BASE + "&f_binges_min=1")
    assert {s["session_id"] for s in res.get_json()["sessions"]} == {"colA__0", "colB__0"}

    # A bounded numeric filter drops the session with no value for the metric.
    res = client.get(_BASE + "&f_entropy_max=0.9")
    assert {s["session_id"] for s in res.get_json()["sessions"]} == {"colA__0", "colB__0"}

    res = client.get(_BASE + "&f_length_min=25&f_coverage_min=0.75")
    assert [s["session_id"] for s in res.get_json()["sessions"]] == ["colB__0"]

    res = client.get(_BASE + "&f_plays_min=junk")
    assert res.status_code == 400






def test_overview_date_filter_is_inclusive(client, patched_routes):
    res = client.get(_BASE + "&f_start_min=2026-01-02&f_start_max=2026-01-02")
    assert [s["session_id"] for s in res.get_json()["sessions"]] == ["colA__1"]

    res = client.get(_BASE + "&f_start_max=2026-01-02")
    assert {s["session_id"] for s in res.get_json()["sessions"]} == {"colA__0", "colA__1"}

    res = client.get(_BASE + "&f_start_min=not-a-date")
    assert res.status_code == 400






def test_overview_excludes_sessions_outside_the_study_date_window(
        client, patched_routes, monkeypatch):
    """The artifact is global; a study's date window must still scope it.

    Without this the tab lists every session a study's collections ever
    recorded — a ten-day study showing years of sessions.
    """
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "get_study_date_window",
                        lambda study: (pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")))

    body = client.get(_BASE).get_json()
    assert [s["session_id"] for s in body["sessions"]] == ["colA__1"]
    # Out-of-window sessions are not "hidden by a floor" — they are not in the
    # study at all, so they must not inflate the status line's denominator.
    assert body["total_in_study"] == 1
    assert body["total_above_floors"] == 1
    assert body["total_matching"] == 1
    # The slider bounds describe the study, not the artifact's full span.
    assert body["ranges"]["start_date"] == ["2026-01-02", "2026-01-02"]






def test_study_date_window_end_is_inclusive_through_that_day(
        client, patched_routes, monkeypatch):
    """END_DATE means "through the end of that day", as in the study builder.

    A date-only upper bound implicitly means midnight, which would drop every
    session on the study's own last day.
    """
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "get_study_date_window",
                        lambda study: (pd.Timestamp("2026-01-03"), pd.Timestamp("2026-01-04")))

    # colB__0 starts 10:00 on the closing day.
    body = client.get(_BASE).get_json()
    assert [s["session_id"] for s in body["sessions"]] == ["colB__0"]






def test_detail_refuses_a_session_outside_the_study_date_window(
        client, patched_routes, monkeypatch):
    """A bookmarked link cannot open a session the study does not contain."""
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "get_study_date_window",
                        lambda study: (pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")))

    res = client.get("/api/sessions/detail?study=s&collection_id=colA&session_id=colA__0")
    assert res.status_code == 404






def test_study_date_window_matches_the_builder_convention():
    """The helper the sessions scoping is built on, against its own contract."""
    from fyp.fyp_config import fyp_cf
    from web_interface.services.study_data import get_study_date_window

    defs = fyp_cf.setdefault("study_defs", {})
    defs["__window_test__"] = {"START_DATE": "2025-03-04", "END_DATE": "2025-03-13"}
    defs["__window_test_open__"] = {}
    defs["__window_test_junk__"] = {"START_DATE": "not-a-date", "END_DATE": "   "}
    try:
        start, end_bound = get_study_date_window("__window_test__")
        assert start == pd.Timestamp("2025-03-04")
        # Exclusive bound at the following midnight => the last day is included.
        assert end_bound == pd.Timestamp("2025-03-14")

        # No bounds, and unparseable bounds, both degrade to a no-op window
        # rather than an accidental cut.
        for name in ("__window_test_open__", "__window_test_junk__", "__not_a_study__"):
            start, end_bound = get_study_date_window(name)
            assert start == pd.Timestamp("1970-01-01")
            assert end_bound == pd.Timestamp("2100-01-01")
    finally:
        for name in ("__window_test__", "__window_test_open__", "__window_test_junk__"):
            defs.pop(name, None)






def test_overview_pagination_clamps(client, patched_routes):
    body = client.get(_BASE + "&limit=2&page=0").get_json()
    assert body["total_matching"] == 3
    assert body["returned"] == 2
    assert body["page"] == 0
    assert body["page_size"] == 2

    body = client.get(_BASE + "&limit=2&page=1").get_json()
    assert body["returned"] == 1
    assert body["page"] == 1

    # A page past the end clamps to the last non-empty one.
    body = client.get(_BASE + "&limit=2&page=99").get_json()
    assert body["page"] == 1
    assert body["returned"] == 1






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






def test_spearman_exact_matches_the_combinatorial_floor():
    """The reason we do not use scipy's default p-value.

    scipy's Spearman p is a t-approximation that returns ~0 for a perfect
    ordering of 4 items, where the exact answer is 2/4! = 0.083. On the
    production corpus that approximation turned a 3.4% hit rate into 21.6%.
    """
    import math

    import numpy as np

    import web_interface.routes.api_sessions_routes as mod

    for n in (4, 5, 6, 7, 8):
        rho, p = mod._spearman_exact(np.arange(n, dtype=float))
        assert rho == pytest.approx(1.0)
        assert p == pytest.approx(2 / math.factorial(n), rel=1e-6), n

    # Above the exact cutoff the null is sampled; (1+hits)/(1+m) keeps the
    # p-value off zero, which a plain mean would not.
    _, p_big = mod._spearman_exact(np.arange(12, dtype=float))
    assert 0 < p_big < 0.001
    # Reversed data is equally extreme, and a flat variable has no trend.
    assert mod._spearman_exact(np.arange(6, dtype=float)[::-1])[1] == pytest.approx(2 / 720)
    assert np.isnan(mod._spearman_exact(np.zeros(6))[0])






def test_benjamini_hochberg_is_monotone_and_bounded():
    import web_interface.routes.api_sessions_routes as mod

    q = mod._benjamini_hochberg([0.001, 0.02, 0.3, 0.9])
    assert q == [pytest.approx(0.004), pytest.approx(0.04), pytest.approx(0.4), pytest.approx(0.9)]
    # ONE lucky variable among nine null ones pays the full multiplicity price:
    # 0.03 * 10 / 1 = 0.3, so it stops being a "finding". This is the case the
    # scan actually faces — ~9 variables tested on one short binge.
    assert mod._benjamini_hochberg([0.03] + [0.9] * 9)[0] == pytest.approx(0.3)

    # But BH is step-up, not Bonferroni: when EVERY variable is equally small,
    # none is penalised for the others. Asserting 0.3 here would be wrong.
    assert mod._benjamini_hochberg([0.03] * 10)[0] == pytest.approx(0.03)
    assert all(x <= 1.0 for x in mod._benjamini_hochberg([0.9, 0.95, 0.99]))






def _members(n, dwell=None):
    return [{"item_id": f"v{i}", "ts": f"2026-01-01T10:{i:02d}:00",
             "dwell_s": None if dwell is None else dwell[i]} for i in range(n)]






def test_scan_trend_finds_a_planted_trend_and_names_it():
    import numpy as np
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    n = 9
    feat = pd.DataFrame({
        "log_plays": np.arange(n, dtype=float),            # perfectly rising
        "faves_per_K_play": np.random.default_rng(2).normal(size=n),
    }, index=pd.Index([f"v{i}" for i in range(n)], name="item_id"))

    out = mod._scan_trend(_members(n), feat, min_n=7)
    assert out["trend"] is not None
    assert out["trend"]["variable"] == "log_plays"
    assert out["trend"]["direction"] == "rising"
    assert out["trend"]["rho"] == pytest.approx(1.0)
    assert out["trend"]["q"] < 0.05
    assert out["scanned"] >= 1






def test_scan_trend_reports_why_it_could_not_run():
    """A silent 'no trend' would read as evidence of absence."""
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    out = mod._scan_trend(_members(5), pd.DataFrame(), min_n=7)
    assert out["trend"] is None
    assert out["scanned"] == 0            # nothing was testable...
    assert out["n_members"] == 5 and out["min_n"] == 7   # ...and the UI can say why






def test_scan_trend_does_not_flag_noise():
    import numpy as np
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    rng = np.random.default_rng(0)
    n = 10
    feat = pd.DataFrame({f"var{j}": rng.normal(size=n) for j in range(8)},
                        index=pd.Index([f"v{i}" for i in range(n)], name="item_id"))
    flagged = 0
    for _ in range(20):
        feat = pd.DataFrame({f"var{j}": rng.normal(size=n) for j in range(8)},
                            index=pd.Index([f"v{i}" for i in range(n)], name="item_id"))
        if mod._scan_trend(_members(n), feat, min_n=7)["trend"] is not None:
            flagged += 1
    # BH across 8 noise variables should keep this near the nominal rate.
    assert flagged <= 3, f"{flagged}/20 noise binges flagged — correction is not biting"






def test_scan_trend_includes_per_play_dwell():
    """Dwell is per-PLAY so it is absent from the per-video map, yet it is the
    variable most likely to trend within a binge (satiation)."""
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    n = 9
    out = mod._scan_trend(_members(n, dwell=list(range(n, 0, -1))), pd.DataFrame(), min_n=7)
    assert out["trend"] is not None
    assert out["trend"]["variable"] == "dwell_s"
    assert out["trend"]["direction"] == "falling"






def test_creator_count_reports_its_denominator():
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    feat = pd.DataFrame({"author": ["a", "b", "a", None]},
                        index=pd.Index([f"v{i}" for i in range(4)], name="item_id"))
    out = mod._creator_count([f"v{i}" for i in range(4)], feat)
    # 2 distinct creators over the 3 videos that have one — not "2 of 4".
    assert out == {"n_creators": 2, "n_attributed": 3, "n_items": 4}

    # An entirely unscraped run must not read as "0 creators" without context.
    empty = mod._creator_count(["v9"], pd.DataFrame())
    assert empty == {"n_creators": 0, "n_attributed": 0, "n_items": 1}






def test_overview_marks_sessions_holding_a_directed_binge(client, patched_routes, monkeypatch):
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod

    counts = pd.Series(
        [1, 0], index=pd.MultiIndex.from_tuples([("colA", "colA__0"), ("colB", "colB__0")]))
    monkeypatch.setattr(mod, "_directed_counts", lambda: counts)

    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0&min_plays=0").get_json()
    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["colA__0"]["n_directed_episodes"] == 1
    assert by_id["colB__0"]["n_directed_episodes"] == 0
    # A session absent from the episodes artifact has no binges, hence none directed.
    assert by_id["colA__1"]["n_directed_episodes"] == 0






def test_overview_reports_null_when_directedness_was_never_computed(client, patched_routes, monkeypatch):
    """An artifact built before direction_p must not read as 'no directed binges'."""
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_directed_counts", lambda: None)
    body = client.get("/api/sessions/overview?study=s&min_emb_plays=0&min_plays=0").get_json()
    assert all(s["n_directed_episodes"] is None for s in body["sessions"])

    # Sorting by a column that artifact cannot supply must not 500.
    res = client.get("/api/sessions/overview?study=s&min_emb_plays=0&min_plays=0"
                     "&sort=n_directed_episodes")
    assert res.status_code == 200






def test_detail_attaches_creators_to_both_run_kinds(client, patched_routes, monkeypatch):
    import pandas as pd

    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.api_viewer_routes as viewer

    plays = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2", "v3"], dtype="string"),
        "_ts": pd.to_datetime(["2026-01-01T10:00:00", "2026-01-01T10:05:00",
                               "2026-01-01T10:29:00"]),
        "play_duration": [10.0, 20.0, 30.0],
        "source_platform": ["tiktok"] * 3,
    })
    members = [{"item_id": "v1", "ts": "2026-01-01T10:00:00", "dwell_s": 10.0,
                "rolling_cosdist": None},
               {"item_id": "v2", "ts": "2026-01-01T10:05:00", "dwell_s": 20.0,
                "rolling_cosdist": 0.1}]
    feat = pd.DataFrame({
        "niche_name": ["Recipes", "Recipes", None], "category": ["Food", "Food", None],
        "story": ["s1", "s2", None], "political_score": [0.0, 0.0, None],
        "sensitivity_score": [0.0, 0.0, None], "advertising": ["none", "none", None],
        "author": ["chef_a", "chef_b", None],
    }, index=pd.Index(["v1", "v2", "v3"], name="item_id"))

    monkeypatch.setattr(mod, "_session_plays", lambda cid, row: plays)
    monkeypatch.setattr(mod, "_session_episodes", lambda cid, sid: [
        {"episode_idx": 0, "start_ts": "2026-01-01T10:00:00", "end_ts": "2026-01-01T10:06:00",
         "n_distinct": 2, "direction_p": 0.01, "members": members}])
    monkeypatch.setattr(mod, "_session_windows", lambda cid, sid: [
        {"window_idx": 0, "start_ts": "2026-01-01T10:00:00", "end_ts": "2026-01-01T10:29:00",
         "n_distinct": 2, "members": [members[0], {"item_id": "v3", "ts": "x", "dwell_s": 1.0}]}])
    monkeypatch.setattr(mod, "_features", lambda: feat)
    monkeypatch.setattr(mod, "_trend_frame", lambda ids: pd.DataFrame())
    monkeypatch.setattr(mod, "_flag_sets", lambda: {
        "scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()})
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset())

    body = client.get("/api/sessions/detail?study=s&collection_id=colA"
                      "&session_id=colA__0").get_json()
    # Binge: two videos, two different creators, both attributed.
    assert body["episodes"][0]["creators"] == {
        "n_creators": 2, "n_attributed": 2, "n_items": 2}
    # Sequence: v3 has no known creator, so the denominator is reported.
    assert body["windows"][0]["creators"] == {
        "n_creators": 1, "n_attributed": 1, "n_items": 2}
    # Trend scan rides along on binges, with an honest "not testable" shape.
    assert body["episodes"][0]["trend_scan"]["trend"] is None
    assert body["episodes"][0]["trend_scan"]["n_members"] == 2
    # The client needs the thresholds even when a session is opened directly.
    assert body["params"]["drift_p"] == mod._drift_p()




def _index_df_with_extremes():
    """The base fixture plus the rebuilt-artifact columns (vmax_/vmin_/search_text)."""
    df = _index_df()
    df["vmax_sensitivity_score"] = pd.to_numeric(
        pd.Series([0.9, 0.2, None]), errors="coerce")
    df["vmin_sensitivity_score"] = pd.to_numeric(
        pd.Series([0.1, 0.0, None]), errors="coerce")
    df["search_text"] = pd.Series(
        ["sourdough bread\n#funnycats\nchef_a", "gaming clips\nstreamer_b", None],
        dtype="string")
    return df






def test_overview_sorts_by_collection_id(client, patched_routes):
    res = client.get(_BASE + "&sort=collection_id&order=asc")
    assert res.status_code == 200
    cids = [s["collection_id"] for s in res.get_json()["sessions"]]
    assert cids == sorted(cids)

    res = client.get(_BASE + "&sort=collection_id&order=desc")
    cids = [s["collection_id"] for s in res.get_json()["sessions"]]
    assert cids == sorted(cids, reverse=True)






def test_overview_varmax_filter_and_ranges(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_load_index", _index_df_with_extremes)

    body = client.get(_BASE).get_json()
    # Bounds computed over the floor-passing frame; labels ride along.
    assert body["ranges"]["var_max"]["sensitivity_score"] == [0.2, 0.9]
    assert "sensitivity_score" in body["ranges"]["var_labels"]

    # Narrowing on the session max keeps only colA__0 (0.9); the NaN row
    # (colB__0) is dropped while the filter is bounded.
    res = client.get(_BASE + "&f_varmax_col=sensitivity_score&f_varmax_min=0.5")
    assert [s["session_id"] for s in res.get_json()["sessions"]] == ["colA__0"]

    res = client.get(_BASE + "&f_varmax_col=sensitivity_score&f_varmax_max=0.5")
    assert [s["session_id"] for s in res.get_json()["sessions"]] == ["colA__1"]

    # An unknown variable is silently ignored, never a 500.
    res = client.get(_BASE + "&f_varmax_col=__nope__&f_varmax_min=0.5")
    assert res.status_code == 200
    assert res.get_json()["total_matching"] == 3






def test_overview_varmax_degrades_on_an_old_artifact(client, patched_routes):
    """No vmax_ columns: var_max is null (not {}) and the filter is a no-op."""
    body = client.get(_BASE + "&f_varmax_col=sensitivity_score&f_varmax_min=0.5").get_json()
    assert body["ranges"]["var_max"] is None
    assert body["ranges"]["var_labels"] is None
    assert body["total_matching"] == 3






def test_overview_search_matches_the_baked_blob(client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_load_index", _index_df_with_extremes)

    body = client.get(_BASE + "&q=sourdough").get_json()
    assert body["search_available"] is True
    assert [s["session_id"] for s in body["sessions"]] == ["colA__0"]

    # Case-insensitive, and multi-term is AND across terms.
    body = client.get(_BASE + "&q=SOURDOUGH+chef_a").get_json()
    assert [s["session_id"] for s in body["sessions"]] == ["colA__0"]
    body = client.get(_BASE + "&q=sourdough+gaming").get_json()
    assert body["sessions"] == []

    # A null blob never matches (and never errors).
    body = client.get(_BASE + "&q=funnycats").get_json()
    assert [s["session_id"] for s in body["sessions"]] == ["colA__0"]






def test_overview_search_is_ignored_on_an_old_artifact(client, patched_routes):
    body = client.get(_BASE + "&q=sourdough").get_json()
    assert body["search_available"] is False
    assert body["total_matching"] == 3






def test_min_max_ranges_is_descriptive_and_skips_all_nan():
    import numpy as np

    import web_interface.routes.api_sessions_routes as mod

    series = {
        "sensitivity_score": np.array([0.2, np.nan, 0.8, 0.4]),
        "log_plays": np.array([np.nan, np.nan, np.nan, np.nan]),
        "dwell_s": np.array([5.0, 40.0, 10.0, np.nan]),
    }
    out = mod._min_max_ranges(series)
    by_var = {r["variable"]: r for r in out}
    assert "log_plays" not in by_var
    assert by_var["sensitivity_score"]["min"] == pytest.approx(0.2)
    assert by_var["sensitivity_score"]["max"] == pytest.approx(0.8)
    assert by_var["sensitivity_score"]["n"] == 3
    assert by_var["dwell_s"]["label"] == "Dwell (s)"
    assert by_var["dwell_s"]["min"] == pytest.approx(5.0)
    assert by_var["dwell_s"]["max"] == pytest.approx(40.0)
    # Sorted by label for a stable display order.
    assert [r["label"] for r in out] == sorted(
        (r["label"] for r in out), key=str.lower)






def test_scan_trend_reports_ranges_even_below_min_n():
    """A binge too short to test still carries its observed min/max."""
    import web_interface.routes.api_sessions_routes as mod

    out = mod._scan_trend(_members(3, dwell=[5.0, 30.0, 10.0]), pd.DataFrame(), min_n=7)
    assert out["scanned"] == 0 and out["trend"] is None
    by_var = {r["variable"]: r for r in out["ranges"]}
    assert by_var["dwell_s"]["min"] == pytest.approx(5.0)
    assert by_var["dwell_s"]["max"] == pytest.approx(30.0)






def test_detail_carries_duration_desc_hashtags_and_session_ranges(
        client, patched_routes, monkeypatch):
    import numpy as np

    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.api_viewer_routes as viewer

    plays = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2"], dtype="string"),
        "_ts": pd.to_datetime(["2026-01-01T10:00:00", "2026-01-01T10:05:00"]),
        "play_duration": [12.0, 45.0],
        "source_platform": ["tiktok"] * 2,
    })
    feat = pd.DataFrame({
        "niche_name": ["Recipes", None], "category": ["Food", None],
        "story": [None, None], "political_score": [0.0, None],
        "sensitivity_score": [0.1, None], "advertising": ["none", None],
        "author": ["chef_a", None], "duration": [30.0, None],
    }, index=pd.Index(["v1", "v2"], name="item_id"))
    trend_feat = pd.DataFrame({
        "sensitivity_score": [0.1, 0.7],
    }, index=pd.Index(["v1", "v2"], name="item_id"))

    long_desc = "caption " * 100  # 800 chars, beyond _STORY_CAP
    monkeypatch.setattr(mod, "_session_plays", lambda cid, row: plays)
    monkeypatch.setattr(mod, "_session_episodes", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_session_windows", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_features", lambda: feat)
    monkeypatch.setattr(mod, "_trend_frame", lambda ids: trend_feat)
    monkeypatch.setattr(mod, "_story_map", lambda ids: {})
    monkeypatch.setattr(mod, "_scrape_text_map", lambda ids: {
        "v1": {"desc": long_desc, "hashtags": "#bread #sourdough"}})
    monkeypatch.setattr(mod, "_flag_sets", lambda: {
        "scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()})
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset())

    body = client.get("/api/sessions/detail?study=s&collection_id=colA"
                      "&session_id=colA__0").get_json()
    p1, p2 = body["plays"]
    assert p1["duration_s"] == pytest.approx(30.0)
    assert p1["hashtags"] == "#bread #sourdough"
    # Long captions are capped like stories.
    assert len(p1["desc"]) == mod._STORY_CAP + 1 and p1["desc"].endswith("…")
    assert p2["duration_s"] is None and p2["desc"] is None

    # Session-level observed ranges: trend variables + per-play dwell.
    by_var = {r["variable"]: r for r in body["session_ranges"]}
    assert by_var["sensitivity_score"]["min"] == pytest.approx(0.1)
    assert by_var["sensitivity_score"]["max"] == pytest.approx(0.7)
    assert by_var["dwell_s"]["min"] == pytest.approx(12.0)
    assert by_var["dwell_s"]["max"] == pytest.approx(45.0)




def test_detail_uses_baked_play_texts_and_skips_corpus_reads(
        client, patched_routes, monkeypatch):
    """A plays artifact with baked text columns answers story/desc/hashtags
    without touching the corpus annotation/scrape parquets."""
    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.api_viewer_routes as viewer

    plays = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2"], dtype="string"),
        "_ts": pd.to_datetime(["2026-01-01T10:00:00", "2026-01-01T10:05:00"]),
        "play_duration": [12.0, 45.0],
        "source_platform": ["tiktok"] * 2,
        "story": pd.Series(["a baked story", None], dtype="string"),
        "desc": pd.Series(["a baked caption", None], dtype="string"),
        "hashtags": pd.Series(["#one #two", None], dtype="string"),
    })

    def _must_not_read(ids):
        raise AssertionError("corpus pushdown read on the baked-text path")

    monkeypatch.setattr(mod, "_session_plays", lambda cid, row: plays)
    monkeypatch.setattr(mod, "_session_episodes", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_session_windows", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_features", lambda: pd.DataFrame())
    monkeypatch.setattr(mod, "_trend_frame", lambda ids: pd.DataFrame())
    monkeypatch.setattr(mod, "_story_map", _must_not_read)
    monkeypatch.setattr(mod, "_scrape_text_map", _must_not_read)
    monkeypatch.setattr(mod, "_flag_sets", lambda: {
        "scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()})
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset())

    body = client.get("/api/sessions/detail?study=s&collection_id=colA"
                      "&session_id=colA__0").get_json()
    p1, p2 = body["plays"]
    assert p1["story"] == "a baked story"
    assert p1["desc"] == "a baked caption"
    assert p1["hashtags"] == "#one #two"
    assert p2["story"] is None and p2["desc"] is None and p2["hashtags"] is None






def test_detail_includes_per_play_variable_series(client, patched_routes, monkeypatch):
    """play_variables aligns each numeric map variable with the plays order."""
    import web_interface.routes.api_sessions_routes as mod
    import web_interface.routes.api_viewer_routes as viewer

    plays = pd.DataFrame({
        "item_id": pd.Series(["v1", "v2"], dtype="string"),
        "_ts": pd.to_datetime(["2026-01-01T10:00:00", "2026-01-01T10:05:00"]),
        "play_duration": [12.0, 45.0],
        "source_platform": ["tiktok"] * 2,
    })
    trend_feat = pd.DataFrame({
        "political_score": [0.25, None],
    }, index=pd.Index(["v1", "v2"], name="item_id"))
    trend_feat["political_score"] = pd.to_numeric(trend_feat["political_score"])

    monkeypatch.setattr(mod, "_session_plays", lambda cid, row: plays)
    monkeypatch.setattr(mod, "_session_episodes", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_session_windows", lambda cid, sid: [])
    monkeypatch.setattr(mod, "_features", lambda: pd.DataFrame())
    monkeypatch.setattr(mod, "_trend_frame", lambda ids: trend_feat)
    monkeypatch.setattr(mod, "_story_map", lambda ids: {})
    monkeypatch.setattr(mod, "_scrape_text_map", lambda ids: {})
    monkeypatch.setattr(mod, "_flag_sets", lambda: {
        "scraped": set(), "downloaded": set(), "annotated": set(), "embedded": set()})
    monkeypatch.setattr(viewer, "_study_item_ids", lambda study: frozenset())

    body = client.get("/api/sessions/detail?study=s&collection_id=colA"
                      "&session_id=colA__0").get_json()
    # A missing value stays a gap (None), never interpolated to a number.
    assert body["play_variables"] == {"political_score": [0.25, None]}






def test_attach_context_distances_measures_from_the_member_centroid(monkeypatch):
    """Context plays get their distance to the sequence's member centroid."""
    import numpy as np

    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_FLAGS_CACHE", {
        "key": None, "model": "m", "flags": None, "emb_index": object()})
    monkeypatch.setattr(mod, "_corpus_mean", lambda model: np.zeros(2))
    vecs = {
        "v1": np.array([1.0, 0.0]), "v2": np.array([1.0, 0.0]),   # members
        "v0": np.array([0.0, 1.0]),                               # orthogonal before
        "v3": np.array([-1.0, 0.0]),                              # opposite after
    }

    def fake_block(model, ids, mean, index=None):
        found = [i for i in ids if i in vecs]
        block = np.stack([vecs[i] for i in found])
        return {iid: row for row, iid in enumerate(found)}, block

    monkeypatch.setattr(mod.session_explorer, "load_directional_block", fake_block)

    play_rows = [{"item_id": "v0", "ts": "t0"}, {"item_id": "v1", "ts": "t1"},
                 {"item_id": "v2", "ts": "t2"}, {"item_id": "v3", "ts": "t3"}]
    ep = {"members": [{"item_id": "v1", "ts": "t1"},
                      {"item_id": "v2", "ts": "t2"}]}
    mod._attach_context_distances([ep], play_rows, 3)
    dists = ep["context_distances"]
    # Centroid of the members is [1, 0]: the orthogonal neighbour sits at
    # distance 1, the opposite one at 2 — exactly the rolling_cosdist scale.
    assert dists["v0@t0"] == pytest.approx(1.0)
    assert dists["v3@t3"] == pytest.approx(2.0)






def test_attach_context_distances_is_a_noop_without_a_dense_store(monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    monkeypatch.setattr(mod, "_FLAGS_CACHE", {
        "key": None, "model": None, "flags": None, "emb_index": None})
    ep = {"members": [{"item_id": "v1", "ts": "t1"}]}
    mod._attach_context_distances([ep], [{"item_id": "v0", "ts": "t0"},
                                         {"item_id": "v1", "ts": "t1"}], 3)
    assert "context_distances" not in ep






def test_episode_vmax_reduces_member_lists_per_binge(monkeypatch):
    """Per-episode maxima cover the map variables AND the per-play dwell."""
    import web_interface.routes.api_sessions_routes as mod

    frame = pd.DataFrame({
        "collection_id": pd.Series(["c1", "c1"], dtype="string"),
        "session_id": pd.Series(["s1", "s2"], dtype="string"),
        "member_item_ids": [["a", "b"], ["c"]],
        "member_dwell_s": [[5.0, 30.0], [10.0]],
    })
    feat = pd.DataFrame({
        "political_score": [0.1, 0.7, None],
    }, index=pd.Index(["a", "b", "c"], name="item_id"))
    feat["political_score"] = pd.to_numeric(feat["political_score"])

    monkeypatch.setattr(mod, "_fingerprint", lambda fn, location=None: "1:1")
    monkeypatch.setattr(mod, "_artifact_frame", lambda fn, cache, lock: frame)
    monkeypatch.setattr(mod, "_trend_frame", lambda ids: feat)
    monkeypatch.setattr(mod, "_EPVMAX_CACHE", {"key": None, "df": None})

    out = mod._episode_vmax()
    assert list(out["session_id"]) == ["s1", "s2"]
    assert out.iloc[0]["dwell_s"] == pytest.approx(30.0)
    assert out.iloc[0]["political_score"] == pytest.approx(0.7)
    assert out.iloc[1]["dwell_s"] == pytest.approx(10.0)
    # A binge whose members have no value must stay NaN (it cannot pass a
    # range criterion), not silently become 0.
    assert pd.isna(out.iloc[1]["political_score"])






def test_varmax_binges_scope_keeps_only_sessions_with_a_matching_binge(
        client, patched_routes, monkeypatch):
    import web_interface.routes.api_sessions_routes as mod

    df = _index_df()
    df["vmax_political_score"] = [0.9, 0.2, 0.9]
    monkeypatch.setattr(mod, "_load_index", lambda: df)
    # Only colA__0 has a binge whose max is in the filter range.
    emax = pd.DataFrame({
        "collection_id": pd.Series(["colA", "colB"], dtype="string"),
        "session_id": pd.Series(["colA__0", "colB__0"], dtype="string"),
        "political_score": [0.85, 0.1],
    })
    monkeypatch.setattr(mod, "_episode_vmax", lambda: emax)

    base = ("/api/sessions/overview?study=s&min_plays=0&min_session_minutes=0"
            "&min_coverage=0&min_emb_plays=0"
            "&f_varmax_col=political_score&f_varmax_min=0.5")
    # Session scope: both binge-holding sessions have a session max of 0.9.
    ids = {s["session_id"] for s in client.get(base).get_json()["sessions"]}
    assert ids == {"colA__0", "colB__0"}
    # Binge scope: only the session with a binge maxing in range survives.
    ids = {s["session_id"] for s in
           client.get(base + "&f_varmax_scope=binges").get_json()["sessions"]}
    assert ids == {"colA__0"}






def test_varmax_binges_scope_degrades_to_session_scope_without_episodes(
        client, patched_routes, monkeypatch):
    """No episodes artifact (or unknown variable) → session-max semantics."""
    import web_interface.routes.api_sessions_routes as mod

    df = _index_df()
    df["vmax_political_score"] = [0.9, 0.2, 0.9]
    monkeypatch.setattr(mod, "_load_index", lambda: df)
    monkeypatch.setattr(mod, "_episode_vmax", lambda: None)

    res = client.get(
        "/api/sessions/overview?study=s&min_plays=0&min_session_minutes=0"
        "&min_coverage=0&min_emb_plays=0&f_varmax_col=political_score"
        "&f_varmax_min=0.5&f_varmax_scope=binges")
    ids = {s["session_id"] for s in res.get_json()["sessions"]}
    assert ids == {"colA__0", "colB__0"}
