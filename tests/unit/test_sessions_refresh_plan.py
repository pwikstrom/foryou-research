"""The sessions-refresh staleness planner and study-window coverage spec.

Pure-function tests: compute_coverage_spec turns study definitions into
per-collection date windows (padded, merged); compute_refresh_plan decides
full / merge / noop from the current fingerprints vs the meta's
per-collection block. No storage involved.
"""

from fyp.analysis import session_explorer as se

WIDE = [["1969-12-29", "2100-01-04"]]  # wide defaults ±3d pad, end+1d
PARAMS = {"cut": 0.5, "mem": 6}
MODEL = "test-model"
TREND = ["log_plays", "political_score"]






def _meta(collections: dict, **over) -> dict:
    meta = {"embedding_model": MODEL, "params": dict(PARAMS),
            "trend_vars": list(TREND), "store_fingerprint": "fp1",
            "collections": collections}
    meta.update(over)
    return meta






def _rec(windows, n_plays, n_annotated) -> dict:
    return {"windows": windows, "n_plays": n_plays,
            "n_annotated": n_annotated, "built_at": "2026-08-01T00:00:00Z"}






# ---- compute_coverage_spec ----


def test_coverage_pads_and_uses_end_of_day():
    spec = se.compute_coverage_spec({
        "S": {"SELECTED_COLLECTIONS": ["a"],
              "START_DATE": "2026-03-10", "END_DATE": "2026-03-20"}})
    # start -3d; END means through end-of-day, so bound is +1d, then +3d pad.
    assert spec == {"a": [["2026-03-07", "2026-03-24"]]}




def test_coverage_merges_overlapping_study_windows():
    spec = se.compute_coverage_spec({
        "S1": {"SELECTED_COLLECTIONS": ["a"],
               "START_DATE": "2026-03-01", "END_DATE": "2026-03-10"},
        "S2": {"SELECTED_COLLECTIONS": ["a"],
               "START_DATE": "2026-03-12", "END_DATE": "2026-03-20"}})
    # 03-10 end -> bound 03-14 after pad; S2 start pads back to 03-09: overlap.
    assert spec == {"a": [["2026-02-26", "2026-03-24"]]}




def test_coverage_keeps_disjoint_windows_separate():
    spec = se.compute_coverage_spec({
        "S1": {"SELECTED_COLLECTIONS": ["a"],
               "START_DATE": "2026-01-01", "END_DATE": "2026-01-05"},
        "S2": {"SELECTED_COLLECTIONS": ["a"],
               "START_DATE": "2026-06-01", "END_DATE": "2026-06-05"}})
    assert spec["a"] == [["2025-12-29", "2026-01-09"],
                        ["2026-05-29", "2026-06-09"]]




def test_coverage_absent_bounds_fall_back_wide():
    spec = se.compute_coverage_spec({"S": {"SELECTED_COLLECTIONS": ["a"]}})
    assert spec == {"a": WIDE}




def test_coverage_excludes_collections_in_no_study():
    spec = se.compute_coverage_spec({"S": {"SELECTED_COLLECTIONS": ["a"]}})
    assert "b" not in spec




def test_coverage_unparseable_bound_falls_back_wide():
    spec = se.compute_coverage_spec({
        "S": {"SELECTED_COLLECTIONS": ["a"], "START_DATE": "not-a-date"}})
    assert spec["a"][0][0] == "1969-12-29"




# ---- compute_refresh_plan: global invalidators ----


def _plan(discovered, coverage, meta, **over):
    kwargs = {"params": PARAMS, "model": MODEL, "trend_cols": TREND,
              "artifacts_exist": True, "plays_schema_ok": True, "scope": None}
    kwargs.update(over)
    return se.compute_refresh_plan(discovered, coverage, meta, **kwargs)




def test_missing_artifacts_forces_full():
    plan = _plan([("a", 10, 5)], {"a": WIDE}, _meta({"a": _rec(WIDE, 10, 5)}),
                 artifacts_exist=False)
    assert plan["mode"] == "full" and plan["refresh"] == ["a"]




def test_missing_meta_forces_full():
    assert _plan([("a", 10, 5)], {"a": WIDE}, None)["mode"] == "full"




def test_meta_without_collections_block_forces_full():
    meta = _meta({})
    del meta["collections"]
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "full"




def test_model_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)}, embedding_model="other-model")
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "full"




def test_params_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)}, params={"cut": 0.4, "mem": 6})
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "full"




def test_trend_column_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)}, trend_vars=["log_plays"])
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "full"




def test_trend_column_order_does_not_force_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)}, trend_vars=list(reversed(TREND)))
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "noop"




def test_plays_schema_drift_forces_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)})
    plan = _plan([("a", 10, 5)], {"a": WIDE}, meta, plays_schema_ok=False)
    assert plan["mode"] == "full"




def test_corpus_mean_drift_alone_does_not_force_full():
    meta = _meta({"a": _rec(WIDE, 10, 5)}, store_fingerprint="OLD-fp")
    assert _plan([("a", 10, 5)], {"a": WIDE}, meta)["mode"] == "noop"




# ---- compute_refresh_plan: per-collection staleness ----


def test_all_match_is_noop():
    meta = _meta({"a": _rec(WIDE, 10, 5), "b": _rec(WIDE, 3, 1)})
    plan = _plan([("a", 10, 5), ("b", 3, 1)], {"a": WIDE, "b": WIDE}, meta)
    assert plan == {"mode": "noop", "reason": "all collections up to date",
                    "refresh": [], "drop": []}




def test_window_change_marks_stale():
    old_windows = [["2026-01-01", "2026-02-01"]]
    meta = _meta({"a": _rec(old_windows, 10, 5)})
    plan = _plan([("a", 10, 5)], {"a": WIDE}, meta)
    assert plan["mode"] == "merge" and plan["refresh"] == ["a"]




def test_play_count_change_marks_stale():
    meta = _meta({"a": _rec(WIDE, 10, 5)})
    plan = _plan([("a", 12, 5)], {"a": WIDE}, meta)
    assert plan["refresh"] == ["a"]




def test_annotated_count_change_marks_stale():
    meta = _meta({"a": _rec(WIDE, 10, 5)})
    plan = _plan([("a", 10, 7)], {"a": WIDE}, meta)
    assert plan["refresh"] == ["a"]




def test_new_collection_marks_stale():
    meta = _meta({"a": _rec(WIDE, 10, 5)})
    plan = _plan([("a", 10, 5), ("b", 3, 1)], {"a": WIDE, "b": WIDE}, meta)
    assert plan["mode"] == "merge" and plan["refresh"] == ["b"]




def test_vanished_collection_is_dropped():
    meta = _meta({"a": _rec(WIDE, 10, 5), "gone": _rec(WIDE, 4, 2)})
    plan = _plan([("a", 10, 5)], {"a": WIDE}, meta)
    assert plan["mode"] == "merge"
    assert plan["refresh"] == [] and plan["drop"] == ["gone"]




def test_scope_intersects_refresh_but_not_drop():
    meta = _meta({"a": _rec(WIDE, 1, 0), "b": _rec(WIDE, 1, 0),
                  "gone": _rec(WIDE, 4, 2)})
    plan = _plan([("a", 9, 0), ("b", 9, 0)], {"a": WIDE, "b": WIDE}, meta,
                 scope={"b"})
    assert plan["refresh"] == ["b"]
    assert plan["drop"] == ["gone"]




def test_scope_with_nothing_stale_is_noop():
    meta = _meta({"a": _rec(WIDE, 1, 0), "b": _rec(WIDE, 2, 0)})
    plan = _plan([("a", 9, 0), ("b", 2, 0)], {"a": WIDE, "b": WIDE}, meta,
                 scope={"b"})
    assert plan["mode"] == "noop"




def test_refresh_keeps_discovery_order():
    meta = _meta({})
    plan = _plan([("big", 100, 50), ("small", 2, 1)],
                 {"big": WIDE, "small": WIDE}, meta)
    assert plan["refresh"] == ["big", "small"]
