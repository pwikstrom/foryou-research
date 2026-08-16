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
            "annotations_fingerprint": "afp1", "collections": collections}
    meta.update(over)
    return meta






def _rec(windows, n_plays) -> dict:
    return {"windows": windows, "n_plays": n_plays,
            "built_at": "2026-08-01T00:00:00Z"}






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
              "artifacts_exist": True, "plays_schema_ok": True, "scope": None,
              "store_fp": "fp1", "annotations_fp": "afp1"}
    kwargs.update(over)
    return se.compute_refresh_plan(discovered, coverage, meta, **kwargs)




def test_missing_artifacts_forces_full():
    plan = _plan([("a", 10)], {"a": WIDE}, _meta({"a": _rec(WIDE, 10)}),
                 artifacts_exist=False)
    assert plan["mode"] == "full" and plan["refresh"] == ["a"]




def test_missing_meta_forces_full():
    assert _plan([("a", 10)], {"a": WIDE}, None)["mode"] == "full"




def test_meta_without_collections_block_forces_full():
    meta = _meta({})
    del meta["collections"]
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "full"




def test_model_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)}, embedding_model="other-model")
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "full"




def test_params_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)}, params={"cut": 0.4, "mem": 6})
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "full"




def test_trend_column_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)}, trend_vars=["log_plays"])
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "full"




def test_trend_column_order_does_not_force_full():
    meta = _meta({"a": _rec(WIDE, 10)}, trend_vars=list(reversed(TREND)))
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "noop"




def test_plays_schema_drift_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)})
    plan = _plan([("a", 10)], {"a": WIDE}, meta, plays_schema_ok=False)
    assert plan["mode"] == "full"




# ---- compute_refresh_plan: enrichment invalidators ----
#
# These replace the pre-2026-08-16 rule that tolerated store drift. The
# per-collection fingerprint is computed from the activity file, which carries
# no enrichment columns, so it cannot see a new annotation or a new vector; on
# prod that made every stale_only run a no-op while 6,000 annotations landed.


def test_embedding_store_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)}, store_fingerprint="OLD-fp")
    plan = _plan([("a", 10)], {"a": WIDE}, meta)
    assert plan["mode"] == "full" and plan["refresh"] == ["a"]




def test_annotation_corpus_change_forces_full():
    meta = _meta({"a": _rec(WIDE, 10)}, annotations_fingerprint="OLD-afp")
    plan = _plan([("a", 10)], {"a": WIDE}, meta)
    assert plan["mode"] == "full" and plan["refresh"] == ["a"]




def test_meta_without_enrichment_fingerprints_forces_full():
    """A pre-upgrade meta has neither key — migrate by rebuilding once."""
    meta = _meta({"a": _rec(WIDE, 10)})
    del meta["store_fingerprint"]
    del meta["annotations_fingerprint"]
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "full"




def test_unreadable_store_does_not_force_full():
    """An empty fingerprint means "could not read", not "changed".

    A transient corpus-mean failure yields ``store_fp=""``. Escalating there
    would rebuild with no vectors and replace real episodes with none, so an
    empty current fingerprint must never invalidate.
    """
    meta = _meta({"a": _rec(WIDE, 10)}, store_fingerprint="fp1")
    plan = _plan([("a", 10)], {"a": WIDE}, meta, store_fp="")
    assert plan["mode"] == "noop"




def test_unreadable_annotation_corpus_does_not_force_full():
    meta = _meta({"a": _rec(WIDE, 10)}, annotations_fingerprint="afp1")
    plan = _plan([("a", 10)], {"a": WIDE}, meta, annotations_fp="")
    assert plan["mode"] == "noop"




def test_unchanged_enrichment_stays_noop():
    """The invalidators must not fire when nothing moved."""
    meta = _meta({"a": _rec(WIDE, 10)})
    assert _plan([("a", 10)], {"a": WIDE}, meta)["mode"] == "noop"




# ---- compute_refresh_plan: per-collection staleness ----


def test_all_match_is_noop():
    meta = _meta({"a": _rec(WIDE, 10), "b": _rec(WIDE, 3)})
    plan = _plan([("a", 10), ("b", 3)], {"a": WIDE, "b": WIDE}, meta)
    assert plan == {"mode": "noop", "reason": "all collections up to date",
                    "refresh": [], "drop": []}




def test_window_change_marks_stale():
    old_windows = [["2026-01-01", "2026-02-01"]]
    meta = _meta({"a": _rec(old_windows, 10)})
    plan = _plan([("a", 10)], {"a": WIDE}, meta)
    assert plan["mode"] == "merge" and plan["refresh"] == ["a"]




def test_play_count_change_marks_stale():
    meta = _meta({"a": _rec(WIDE, 10)})
    plan = _plan([("a", 12)], {"a": WIDE}, meta)
    assert plan["refresh"] == ["a"]




def test_old_meta_with_annotated_count_still_matches():
    """A pre-2026-08-16 per-collection record must not read as stale.

    Those records carry a vestigial ``n_annotated``; the comparison ignores
    it, so an otherwise-unchanged collection stays out of the refresh set
    (the enrichment fingerprints, not the record shape, drive the migration
    rebuild).
    """
    rec = _rec(WIDE, 10)
    rec["n_annotated"] = 5
    plan = _plan([("a", 10)], {"a": WIDE}, _meta({"a": rec}))
    assert plan["mode"] == "noop"




def test_new_collection_marks_stale():
    meta = _meta({"a": _rec(WIDE, 10)})
    plan = _plan([("a", 10), ("b", 3)], {"a": WIDE, "b": WIDE}, meta)
    assert plan["mode"] == "merge" and plan["refresh"] == ["b"]




def test_vanished_collection_is_dropped():
    meta = _meta({"a": _rec(WIDE, 10), "gone": _rec(WIDE, 4)})
    plan = _plan([("a", 10)], {"a": WIDE}, meta)
    assert plan["mode"] == "merge"
    assert plan["refresh"] == [] and plan["drop"] == ["gone"]




def test_scope_intersects_refresh_but_not_drop():
    meta = _meta({"a": _rec(WIDE, 1), "b": _rec(WIDE, 1),
                  "gone": _rec(WIDE, 4)})
    plan = _plan([("a", 9), ("b", 9)], {"a": WIDE, "b": WIDE}, meta,
                 scope={"b"})
    assert plan["refresh"] == ["b"]
    assert plan["drop"] == ["gone"]




def test_scope_with_nothing_stale_is_noop():
    meta = _meta({"a": _rec(WIDE, 1), "b": _rec(WIDE, 2)})
    plan = _plan([("a", 9), ("b", 2)], {"a": WIDE, "b": WIDE}, meta,
                 scope={"b"})
    assert plan["mode"] == "noop"




def test_refresh_keeps_discovery_order():
    meta = _meta({})
    plan = _plan([("big", 100), ("small", 2)],
                 {"big": WIDE, "small": WIDE}, meta)
    assert plan["refresh"] == ["big", "small"]
