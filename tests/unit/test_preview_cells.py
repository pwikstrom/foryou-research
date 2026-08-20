"""Parity tests for the corpus-level preview cells against the frame-based path.

The study modal's estimates moved from a per-selection prepared frame to a
corpus-wide per-(collection, day) cells table. These tests build both from the
same synthetic corpus and assert:

  - _estimate_from_cells equals _estimate_from_prepared exactly for every
    activity-level figure (totals, per-day, cells, sampling report) whenever no
    random capping is in play, and within documented tolerance for item-level
    estimates and capped configs;
  - _universe_from_cells equals _universe_from_prepared exactly, always;
  - the daily chart numbers derived from cells equal the old
    event-window + play/observe + value_counts pipeline.
"""

import numpy as np
import pandas as pd
import pytest

from web_interface.services import preview_cache as pc
from web_interface.services import stats_service as ss


# ---------------------------------------------------------------------------
# Synthetic corpus


def _make_corpus(seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (raw activities, enrichment status, event windows) for 4 collections.

    Item ids are disjoint across collections (cross-collection reuse is exercised
    separately) but recur across days within a collection, so the per-collection
    dedup calibration matters. Collection profiles:
      c1: 30 days x ~40 rows/day  (dense)
      c2: 10 days x ~8 rows/day   (sparse -> excluded by big mins)
      c3: 60 days x ~25 rows/day  (many cells -> stage-2 downsampling target)
      c4: 5 days x ~100 rows/day  (short + heavy)
    Roughly half the rows fall outside c4's event window to exercise n_act_inwin.
    """

    rng = np.random.RandomState(seed)
    rows = []
    profiles = {
        "c1": (pd.Timestamp("2026-01-01"), 30, 40, 120),
        "c2": (pd.Timestamp("2026-01-10"), 10, 8, 30),
        "c3": (pd.Timestamp("2026-02-01"), 60, 25, 200),
        "c4": (pd.Timestamp("2026-03-01"), 5, 100, 80),
    }
    for cid, (start, n_days, per_day, n_items) in profiles.items():
        items = [f"{cid}_item_{i}" for i in range(n_items)]
        for d in range(n_days):
            day = start + pd.Timedelta(days=d)
            n = max(1, per_day + int(rng.randint(-5, 6)))
            for _ in range(n):
                rows.append({
                    "collection_id": cid,
                    "local_timestamp": day + pd.Timedelta(minutes=int(rng.randint(0, 1440))),
                    "activity_type": rng.choice(["play", "observe", "like", "search"], p=[0.6, 0.25, 0.1, 0.05]),
                    "item_id": items[int(rng.randint(0, n_items))],
                })
    raw = pd.DataFrame(rows)
    raw["local_date"] = raw["local_timestamp"].dt.normalize()

    all_items = sorted(raw["item_id"].unique())
    scraped = {i for i in all_items if hash(i) % 10 < 7}          # ~70% scraped
    annotated = {i for i in scraped if hash(i) % 10 < 3}          # annotated subset of scraped
    status = pd.DataFrame({
        "item_id": all_items,
        "scraped_ok": [i in scraped for i in all_items],
        "annotated_ok": [i in annotated for i in all_items],
    })

    windows = {
        "c1": (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-30")),
        "c2": (pd.Timestamp("2026-01-10"), pd.Timestamp("2026-01-19")),
        "c3": (pd.Timestamp("2026-02-01"), pd.Timestamp("2026-04-01")),
        "c4": (pd.Timestamp("2026-03-02"), pd.Timestamp("2026-03-03")),  # trims c4
    }
    return raw, status, windows


@pytest.fixture()
def corpus(monkeypatch):
    raw, status, windows = _make_corpus()

    def fake_selective(storage_location=None, filename=None, columns=None, filters=None, **kw):
        df = raw
        if filters:
            for col, op, vals in filters:
                assert op == "in"
                df = df[df[col].astype(str).isin([str(v) for v in vals])]
        return df[columns].copy() if columns else df.copy()

    monkeypatch.setattr(pc.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(pc.data_io, "load_parquet_selective", fake_selective)
    monkeypatch.setattr(pc, "_get_enrichment_status_cached", lambda: status)
    monkeypatch.setattr(pc, "_load_collection_event_windows", lambda ids: windows)
    return raw, status, windows


def _both_paths(corpus_data, selected):
    raw, status, windows = corpus_data
    cells, coll = pc._build_preview_cells()
    frame = pc._prepare_preview_frame(selected, status)
    return cells, coll, frame


CFG_BASE = {"STUDY_NAME": "t", "START_DATE": "", "END_DATE": ""}


def _cfg(**kw):
    cfg = dict(CFG_BASE)
    cfg.update(kw)
    return cfg


EXACT_KEYS = ["total_activities", "unique_collections", "active_days"]


def _assert_estimates_match(cells, coll, frame, cfg, item_tol=0.05, exact_subcounts=True):
    s_new, days_new, sparse_new, cells_new, rep_new = ss._estimate_from_cells(cells, coll, cfg)
    s_old, days_old, sparse_old, cells_old, rep_old = ss._estimate_from_prepared(frame, cfg)

    for k in EXACT_KEYS:
        assert s_new[k] == s_old[k], f"{k}: {s_new[k]} != {s_old[k]} for {cfg}"
    assert days_new == days_old
    assert (sparse_new, cells_new) == (sparse_old, cells_old)
    assert rep_new == rep_old

    if exact_subcounts:
        assert s_new["activities_scraped"] == s_old["activities_scraped"]
        assert s_new["activities_annotated"] == s_old["activities_annotated"]

    for k in ("unique_videos", "scraped_videos", "annotated_videos"):
        lo = s_old[k] * (1 - item_tol) - 2
        hi = s_old[k] * (1 + item_tol) + 2
        assert lo <= s_new[k] <= hi, f"{k}: {s_new[k]} vs exact {s_old[k]} for {cfg}"
    return s_new, s_old


# ---------------------------------------------------------------------------
# Estimator parity


def test_frame_off_full_range(corpus):
    selected = ["c1", "c2", "c3", "c4"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="off")
    s_new, s_old = _assert_estimates_match(cells, coll, frame, cfg, item_tol=0.0)
    # Disjoint items + full range -> the dedup calibration is exact.
    assert s_new["unique_videos"] == s_old["unique_videos"]
    assert s_new["scraped_videos"] == s_old["scraped_videos"]
    assert s_new["annotated_videos"] == s_old["annotated_videos"]
    assert s_new["total_activities"] > 0


def test_frame_off_date_window(corpus):
    selected = ["c1", "c3"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="off",
               START_DATE="2026-01-05", END_DATE="2026-02-20")
    _assert_estimates_match(cells, coll, frame, cfg)


@pytest.mark.parametrize("frame_setting", ["activities", "scraped", "annotated"])
def test_sampling_uncapped(corpus, frame_setting):
    """min thresholds only, both maxes blank (∞): deterministic, exact parity."""

    selected = ["c1", "c2", "c3", "c4"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME=frame_setting,
               MIN_ACTIVITY_COUNT_PER_GROUP=10, MAX_ACTIVITY_COUNT_PER_GROUP="",
               MIN_GROUP_COUNT_PER_COLLECTION=5, MAX_GROUP_COUNT_PER_COLLECTION="")
    _assert_estimates_match(cells, coll, frame, cfg)


def test_sampling_min_filters_exclude_collections(corpus):
    selected = ["c1", "c2", "c3", "c4"]
    cells, coll, frame = _both_paths(corpus, selected)
    # c2 (~8 rows/day) fails min_events=15; c4 (5 days) fails min_cells=8.
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="activities",
               MIN_ACTIVITY_COUNT_PER_GROUP=15, MAX_ACTIVITY_COUNT_PER_GROUP="",
               MIN_GROUP_COUNT_PER_COLLECTION=8, MAX_GROUP_COUNT_PER_COLLECTION="")
    s_new, _ = _assert_estimates_match(cells, coll, frame, cfg)
    assert s_new["unique_collections"] == 2  # c1 + c3 survive


def test_sampling_capped_events(corpus):
    """Finite max_events: per-cell kept counts are min(n, max) in both paths —
    activity totals stay exact; per-item/per-flag figures are stochastic in the
    old path (it draws actual rows), so compare within tolerance."""

    selected = ["c1", "c3"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="activities",
               MIN_ACTIVITY_COUNT_PER_GROUP=10, MAX_ACTIVITY_COUNT_PER_GROUP=15,
               MIN_GROUP_COUNT_PER_COLLECTION=0, MAX_GROUP_COUNT_PER_COLLECTION="")
    s_new, days_new, sparse_new, cells_new, rep_new = ss._estimate_from_cells(cells, coll, cfg)
    s_old, days_old, sparse_old, cells_old, rep_old = ss._estimate_from_prepared(frame, cfg)
    for k in EXACT_KEYS:
        assert s_new[k] == s_old[k]
    assert days_new == days_old
    assert (sparse_new, cells_new) == (sparse_old, cells_old)
    assert rep_new == rep_old
    for k in ("activities_scraped", "activities_annotated"):
        assert abs(s_new[k] - s_old[k]) <= max(10, 0.15 * max(s_old[k], 1))


def test_sampling_stage2_downsampling(corpus):
    """Finite max_cells: WHICH cells survive is a seeded draw that differs
    between substrates, but the structural outputs must agree."""

    selected = ["c1", "c3"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="activities",
               MIN_ACTIVITY_COUNT_PER_GROUP=10, MAX_ACTIVITY_COUNT_PER_GROUP="",
               MIN_GROUP_COUNT_PER_COLLECTION=0, MAX_GROUP_COUNT_PER_COLLECTION=20)
    s_new, days_new, _sp, cells_new, rep_new = ss._estimate_from_cells(cells, coll, cfg)
    s_old, days_old, _sp2, cells_old, rep_old = ss._estimate_from_prepared(frame, cfg)
    assert rep_new == rep_old
    assert cells_new == cells_old            # both keep exactly max_cells per big collection
    assert s_new["unique_collections"] == s_old["unique_collections"]
    # Totals differ only by which cells were drawn; sizes are same order.
    assert abs(s_new["total_activities"] - s_old["total_activities"]) \
        <= 0.35 * max(s_old["total_activities"], 1)


def test_empty_after_min_events(corpus):
    selected = ["c2"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, SAMPLE_FRAME="activities",
               MIN_ACTIVITY_COUNT_PER_GROUP=10_000, MAX_ACTIVITY_COUNT_PER_GROUP="",
               MIN_GROUP_COUNT_PER_COLLECTION=0, MAX_GROUP_COUNT_PER_COLLECTION="")
    s_new, days_new, _s, _c, rep_new = ss._estimate_from_cells(cells, coll, cfg)
    s_old, days_old, _s2, _c2, rep_old = ss._estimate_from_prepared(frame, cfg)
    assert s_new == s_old
    assert days_new == days_old == []
    assert rep_new == rep_old


def test_no_selection_and_empty_window(corpus):
    cells, coll, frame = _both_paths(corpus, ["c1"])
    cfg = _cfg(SELECTED_COLLECTIONS=[], SAMPLE_FRAME="off")
    assert ss._estimate_from_cells(cells, coll, cfg)[0]["total_activities"] == 0
    cfg = _cfg(SELECTED_COLLECTIONS=["c1"], SAMPLE_FRAME="off",
               START_DATE="2030-01-01", END_DATE="2030-01-02")
    s_new, *_ = ss._estimate_from_cells(cells, coll, cfg)
    s_old, *_ = ss._estimate_from_prepared(frame, cfg)
    assert s_new == s_old


# ---------------------------------------------------------------------------
# Universe / potentials parity (must be exact, always)


@pytest.mark.parametrize("window", [("", ""), ("2026-01-05", "2026-03-02")])
def test_universe_parity(corpus, window):
    selected = ["c1", "c2", "c3", "c4"]
    cells, coll, frame = _both_paths(corpus, selected)
    cfg = _cfg(SELECTED_COLLECTIONS=selected, START_DATE=window[0], END_DATE=window[1])
    new = ss._universe_from_cells(cells, cfg)
    old = ss._universe_from_prepared(frame, cfg)
    assert new == old
    assert new[3] is True and new[0] > 0


def test_universe_no_selection(corpus):
    cells, coll, _ = _both_paths(corpus, ["c1"])
    assert ss._universe_from_cells(cells, _cfg(SELECTED_COLLECTIONS=[])) == \
        (0, 0, {"activities": 0, "scraped": 0, "annotated": 0}, False)


# ---------------------------------------------------------------------------
# Daily chart parity


def test_daily_counts_parity(corpus):
    raw, status, windows = corpus
    selected = ["c1", "c4"]
    cells, _coll = pc._build_preview_cells()

    df = ss._filter_to_event_windows(raw[raw["collection_id"].isin(selected)], windows)
    df = ss._filter_to_play_observe(df)
    old = ss._daily_counts(df)

    sel = cells[cells["collection_id"].isin(selected)]
    win = sel[sel["n_act_inwin"] > 0]
    day_counts = win.groupby("day")["n_act_inwin"].sum().sort_index()
    new = [{"date": pd.Timestamp(d).date().isoformat(), "count": int(c)} for d, c in day_counts.items()]
    assert new == old
    assert int(win["n_act_inwin"].sum()) == len(df)


# ---------------------------------------------------------------------------
# Cells builder invariants


# ---------------------------------------------------------------------------
# Hard cap


def _huge_cells() -> pd.DataFrame:
    cells = pd.DataFrame({
        "collection_id": ["c1", "c1", "c1"],
        "day": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
    })
    for col in pc._CELLS_INT_COLS:
        cells[col] = 300_000
    return cells


def test_issues_severity_around_cap():
    cap = ss.get_study_activity_cap()
    base = {"unique_videos": 0, "scraped_videos": 0, "annotated_videos": 0,
            "activities_scraped": 0, "activities_annotated": 0,
            "unique_collections": 1, "active_days": 1}

    over = ss._derive_study_issues({**base, "total_activities": cap + 1}, 0, 10, True)
    assert any(i["severity"] == "error" and i["code"] == "too_big" for i in over)

    big = ss._derive_study_issues({**base, "total_activities": int(cap * 0.8)}, 0, 10, True)
    assert any(i["severity"] == "warn" and i["code"] == "large" for i in big)
    assert not any(i["severity"] == "error" for i in big)

    small = ss._derive_study_issues({**base, "total_activities": 1000}, 0, 10, True)
    assert not any(i["code"] in ("too_big", "large") for i in small)


def _study_client(monkeypatch, study_defs):
    from web_interface import security
    from web_interface.auth import User
    from web_interface.fyp_data_hub import app
    import web_interface.auth as auth_mod
    import web_interface.routes.management.studies as studies_mod
    from fyp.fyp_config import fyp_cf

    user = "cap_test_user"
    orig_get_user = security.user_manager.get_user

    def _fake_get(uid):
        if uid == user:
            return User(username=user, role="manager", password_hash="", approved=True)
        return orig_get_user(uid)

    monkeypatch.setattr(security.user_manager, "get_user", _fake_get)
    monkeypatch.setattr(auth_mod.role_manager, "get_role_permissions",
                        lambda role: ["tab.data_management.studies"])
    monkeypatch.setattr(studies_mod, "init_study_defs", lambda: None)
    monkeypatch.setattr(studies_mod, "save_study_defs", lambda: None)
    monkeypatch.setitem(fyp_cf, "study_defs", study_defs)
    monkeypatch.setattr(studies_mod, "get_preview_cells", lambda: (_huge_cells(), None))

    app.testing = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user
        sess["_fresh"] = True
    return client


def test_save_study_rejects_over_cap(monkeypatch):
    client = _study_client(monkeypatch, {})
    res = client.post("/api/manage/studies/save", json={
        "STUDY_NAME": "huge", "SELECTED_COLLECTIONS": ["c1"],
        "SAMPLE_FRAME": "off", "definition_only": True,
    })
    assert res.status_code == 400
    body = res.get_json()
    assert "cap" in body and body["cap"]["exceeded"] is True
    assert "cap is" in body["error"]


def test_save_study_grandfathers_unchanged_shaping(monkeypatch):
    existing = {
        "huge": {
            "SELECTED_COLLECTIONS": ["c1"], "SAMPLE_FRAME": "off",
            "START_DATE": "", "END_DATE": "",
        },
    }
    client = _study_client(monkeypatch, existing)
    # Same shaping fields -> the (over-cap) study can still be saved untouched.
    res = client.post("/api/manage/studies/save", json={
        "STUDY_NAME": "huge", "SELECTED_COLLECTIONS": ["c1"],
        "SAMPLE_FRAME": "off", "START_DATE": "", "END_DATE": "",
        "definition_only": True,
    })
    assert res.status_code == 200, res.data

    # But changing a shaping field re-triggers the check.
    res = client.post("/api/manage/studies/save", json={
        "STUDY_NAME": "huge", "SELECTED_COLLECTIONS": ["c1"],
        "SAMPLE_FRAME": "off", "START_DATE": "2026-01-01", "END_DATE": "2026-01-03",
        "definition_only": True,
    })
    assert res.status_code == 400


def test_calculate_stats_returns_cap(monkeypatch, corpus):
    from fyp.fyp_config import fyp_cf
    import web_interface.routes.management.studies as studies_mod

    cells, coll = pc._build_preview_cells()
    client = _study_client(monkeypatch, {})
    monkeypatch.setattr(studies_mod, "get_preview_cells", lambda: (cells, coll))
    res = client.post("/api/manage/studies/calculate_stats", json={
        "STUDY_NAME": "__preview__", "PREVIEW_ONLY": True,
        "SELECTED_COLLECTIONS": ["c1", "c3"], "SAMPLE_FRAME": "off",
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    cap = body["cap"]
    assert cap["limit"] >= 1
    assert cap["projected"] == body["stats"]["total_activities"]
    assert cap["exceeded"] is (cap["projected"] > cap["limit"])
    # PREVIEW_ONLY must never write the preview definition into study_defs.
    assert fyp_cf.get("study_defs") == {}


# ---------------------------------------------------------------------------
# Cells builder invariants


def test_builder_shapes_and_invariants(corpus):
    raw, status, windows = corpus
    cells, coll = pc._build_preview_cells()
    assert set(pc._CELLS_INT_COLS).issubset(cells.columns)
    assert set(pc._COLL_INT_COLS).issubset(coll.columns)
    # Annotated is a subset of scraped, in-window a subset of all.
    assert (cells["n_act_annotated"] <= cells["n_act_scraped"]).all()
    assert (cells["n_act_inwin"] <= cells["n_act"]).all()
    assert (cells["n_act_inwin_scraped"] <= cells["n_act_scraped"]).all()
    assert (cells["n_items"] <= cells["n_act"]).all()
    # Play/observe only.
    po = raw[raw["activity_type"].isin(["play", "observe"])]
    assert int(cells["n_act"].sum()) == len(po)
    # Per-collection uniques match the raw truth (the __shared__ calibration row aside).
    truth = po.groupby("collection_id")["item_id"].nunique()
    got = coll[coll["collection_id"] != "__shared__"].set_index("collection_id")["u_items"]
    assert got.sort_index().equals(truth.sort_index().astype("int64"))
    # Items are disjoint across collections in this corpus, so the shared pool is empty.
    g = coll[coll["collection_id"] == "__shared__"].iloc[0]
    assert g["u_items"] == 0
    assert g["sum_cell_items"] == 0
    assert (coll["u_items_shared"] == 0).all()


def test_cross_collection_item_overlap(monkeypatch):
    """Items shared across collections must not be double-counted at full selection."""

    rng = np.random.RandomState(3)
    shared = [f"shared_{i}" for i in range(50)]
    rows = []
    for cid, start in (("a", "2026-01-01"), ("b", "2026-01-01")):
        for d in range(20):
            day = pd.Timestamp(start) + pd.Timedelta(days=d)
            for _ in range(30):
                rows.append({
                    "collection_id": cid,
                    "local_timestamp": day + pd.Timedelta(minutes=int(rng.randint(0, 1440))),
                    "activity_type": "play",
                    "item_id": shared[int(rng.randint(0, 50))],   # every item shared
                })
    raw = pd.DataFrame(rows)
    raw["local_date"] = raw["local_timestamp"].dt.normalize()
    status = pd.DataFrame({"item_id": shared, "scraped_ok": True, "annotated_ok": False})

    def fake_selective(storage_location=None, filename=None, columns=None, filters=None, **kw):
        df = raw
        if filters:
            for col, op, vals in filters:
                df = df[df[col].astype(str).isin([str(v) for v in vals])]
        return df[columns].copy() if columns else df.copy()

    monkeypatch.setattr(pc.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(pc.data_io, "load_parquet_selective", fake_selective)
    monkeypatch.setattr(pc, "_get_enrichment_status_cached", lambda: status)
    monkeypatch.setattr(pc, "_load_collection_event_windows", lambda ids: {})

    cells, coll = pc._build_preview_cells()
    frame = pc._prepare_preview_frame(["a", "b"], status)
    cfg = _cfg(SELECTED_COLLECTIONS=["a", "b"], SAMPLE_FRAME="off")
    s_new, *_ = ss._estimate_from_cells(cells, coll, cfg)
    s_old, *_ = ss._estimate_from_prepared(frame, cfg)
    # Full selection, full range: global calibration makes uniques exact (= 50, not 100).
    assert s_old["unique_videos"] == 50
    assert s_new["unique_videos"] == 50
    assert s_new["scraped_videos"] == 50
    assert s_new["total_activities"] == s_old["total_activities"]
