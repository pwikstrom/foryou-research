"""Unit tests for the refresh pipeline: registry, planner, pruning, barrier.

Covers:
  Registry invariants (services/refresh_pipeline):
    1. test_order_lists_in_sync           (registry == the endpoints' liveness set)
    2. test_dependency_invariants         (every parent precedes its child;
                                           timelines and sessions read the map)
    3. test_every_step_has_a_stage_label
    4. test_every_step_has_a_local_script (the bug this closed: video_map missing)
    5. test_dependents_of_each_origin

  Planning (plan_run):
    6. test_consolidate_plans_everything
    7. test_card_origin_marks_earlier_steps_upstream
    8. test_leaf_origin_plans_nothing
    9. test_consolidate_only_plans_nothing
   10. test_stage_total_is_tree_depth

  Pruning (next_actions) — the reason a run exists rather than a fixed chain:
   11. test_map_that_moved_nothing_prunes_everything
   12. test_map_that_moved_dispatches_recode_for_all_studies
   13. test_meta_and_pca_scoped_to_changed_studies
   14. test_unchanged_studies_prune_meta_and_pca
   15. test_embeddings_that_wrote_nothing_prune_the_map
   16. test_missing_signal_never_prunes
   17. test_fork_comes_from_the_last_step_that_ran
   18. test_collection_only_impact_forks_from_consolidate

  Barrier (_maybe_finish_forked_pipeline) — the race-free completion detector:
   19. test_barrier_fires_when_all_leaves_completed
   20. test_barrier_waits_for_running_leaf
   21. test_barrier_ignores_stale_status_before_fork
   22. test_barrier_marks_partial_on_leaf_failure
   23. test_barrier_waits_for_missing_status
   24. test_barrier_kills_queued_leaf_after_grace
   25. test_barrier_spares_running_leaf_past_grace
   26. test_barrier_waits_for_queued_leaf_within_grace

Run:
    python tests/unit/test_pipeline_order.py
Exit code 0 on full pass, 1 otherwise.
"""

import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import web_interface.routes.process_routes as pr
import web_interface.services.refresh_pipeline as rp
from web_interface.process_manager import local_pipeline_script_map
from web_interface.routes.management_routes import PIPELINE_STEPS_ORDER


PASS = 0
FAIL = 0


def _check(name: str, ok: bool, detail: str = ""):
    """Record a check — and FAIL the test when it does not hold.

    This used to only print, which meant every test in this file passed under
    pytest no matter what it found: the script runner read the counters, and
    pytest read nothing. A check that cannot fail is not a test, and this file
    is where the pipeline's dependency invariants are supposed to be pinned.
    """
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
        return
    FAIL += 1
    print(f"  FAIL  {name}  {detail}")
    raise AssertionError(f"{name}: {detail}")


def _idx(task: str) -> int:
    return rp.STEP_ORDER.index(task)


def _ctx(record: dict, results: dict, impact: dict | None = None) -> rp.RunContext:
    """A context whose steps are all treated as having finished in this run."""
    return rp.RunContext(record=record, results=results, impact=impact)


def _states(record: dict) -> dict:
    return {k: v.get("state") for k, v in record["steps"].items()}


# -------- Registry invariants --------


def test_order_lists_in_sync():
    _check(
        "test_order_lists_in_sync",
        rp.DOWNSTREAM_ORDER == PIPELINE_STEPS_ORDER,
        f"{rp.DOWNSTREAM_ORDER} != {PIPELINE_STEPS_ORDER}",
    )


def test_dependency_invariants():
    # Every parent must be dispatched before its child, or a step reads a cache
    # its own input has not written yet.
    violations = [
        (step.name, parent)
        for step in rp.STEPS for parent in step.parents
        if _idx(parent) >= _idx(step.name)
    ]
    # The two multi-parent steps that a fixed chain got wrong: timelines joins
    # the niche columns (via new_merge), and sessions reads the map's trend
    # columns, so BOTH depend on the map, not only on the consolidation.
    reads_map = (
        "video_map_refresh" in rp.BY_NAME["timelines_refresh"].parents
        and "video_map_refresh" in rp.BY_NAME["sessions_refresh"].parents
    )
    _check("test_dependency_invariants", not violations and reads_map,
           f"violations={violations} reads_map={reads_map}")


def test_every_step_has_a_stage_label():
    missing = [s for s in rp.STEP_ORDER if not rp.LABELS.get(s)]
    missing += [s for s in rp.STEP_ORDER if not rp.SHORT_LABELS.get(s)]
    _check("test_every_step_has_a_stage_label", not missing, f"missing labels: {missing}")


def test_every_step_has_a_local_script():
    script_map = local_pipeline_script_map()
    missing = [s for s in rp.DOWNSTREAM_ORDER if s not in script_map]
    extra = [s for s in script_map if s not in rp.DOWNSTREAM_ORDER]
    _check("test_every_step_has_a_local_script", not missing and not extra,
           f"missing={missing} extra={extra}")


def test_dependents_of_each_origin():
    expected = {
        "consolidate_enrichment": rp.DOWNSTREAM_ORDER,
        "embeddings_refresh": ["video_map_refresh", "recode_refresh_studies",
                               "meta_refresh_groups", "pca_refresh",
                               "timelines_refresh", "sessions_refresh"],
        "video_map_refresh": ["recode_refresh_studies", "meta_refresh_groups",
                              "pca_refresh", "timelines_refresh", "sessions_refresh"],
        "recode_refresh_studies": ["meta_refresh_groups", "pca_refresh"],
        "meta_refresh_groups": [],
        "pca_refresh": [],
        "timelines_refresh": [],
        "sessions_refresh": [],
    }
    got = {o: rp.dependents_of(o) for o in rp.STEP_ORDER}
    _check("test_dependents_of_each_origin", got == expected, str(got))


# -------- Planning --------


def test_consolidate_plans_everything():
    record = rp.plan_run("consolidate_enrichment", kind="consolidate")
    states = _states(record)
    ok = (states["consolidate_enrichment"] == "origin"
          and all(states[s] == "planned" for s in rp.DOWNSTREAM_ORDER))
    _check("test_consolidate_plans_everything", ok, str(states))


def test_card_origin_marks_earlier_steps_upstream():
    # A map rebuild does not re-embed. Those rows must read "not part of this
    # run" rather than "not needed", which would imply a judgement was made.
    record = rp.plan_run("video_map_refresh", kind="card")
    states = _states(record)
    ok = (states["consolidate_enrichment"] == "upstream"
          and states["embeddings_refresh"] == "upstream"
          and states["video_map_refresh"] == "origin"
          and states["recode_refresh_studies"] == "planned"
          and states["sessions_refresh"] == "planned")
    _check("test_card_origin_marks_earlier_steps_upstream", ok, str(states))


def test_leaf_origin_plans_nothing():
    record = rp.plan_run("timelines_refresh", kind="card")
    states = _states(record)
    ok = (states["timelines_refresh"] == "origin"
          and not any(v == "planned" for v in states.values())
          # sessions comes after timelines but reads nothing it writes.
          and states["sessions_refresh"] == "not_planned")
    _check("test_leaf_origin_plans_nothing", ok, str(states))


def test_consolidate_only_plans_nothing():
    record = rp.plan_run("consolidate_enrichment", kind="consolidate",
                         mode="consolidate_only")
    states = _states(record)
    ok = all(states[s] == "not_planned" for s in rp.DOWNSTREAM_ORDER)
    _check("test_consolidate_only_plans_nothing", ok, str(states))


def test_stage_total_is_tree_depth():
    # Depth, not task count: the leaves share one stage however many they are.
    full = rp.plan_run("consolidate_enrichment", kind="consolidate")
    from_map = rp.plan_run("video_map_refresh", kind="card")
    from_recode = rp.plan_run("recode_refresh_studies", kind="card")
    ok = (full["stage_total"] == 5          # consolidate, embeddings, map, recode, leaves
          and from_map["stage_total"] == 3  # map, recode, leaves
          and from_recode["stage_total"] == 2)  # recode, leaves
    _check("test_stage_total_is_tree_depth",
           ok, f"{full['stage_total']}/{from_map['stage_total']}/{from_recode['stage_total']}")


# -------- Pruning --------


def test_map_that_moved_nothing_prunes_everything():
    # The whole point: a warm-started rebuild that moves no video between
    # niches leaves every downstream cache correct.
    record = rp.plan_run("video_map_refresh", kind="card")
    ctx = _ctx(record, {"video_map_refresh": {"map_niche_changed": 0,
                                              "map_cold_start": False}})
    action = rp.next_actions(record, ctx)
    states = _states(record)
    ok = (action["action"] == "finish"
          and all(states[s] == "pruned" for s in rp.dependents_of("video_map_refresh"))
          and "niche" in (action["prunes"].get("recode_refresh_studies") or ""))
    _check("test_map_that_moved_nothing_prunes_everything", ok,
           str((action["action"], action["prunes"])))


def test_map_that_moved_dispatches_recode_for_all_studies():
    record = rp.plan_run("video_map_refresh", kind="card")
    ctx = _ctx(record, {"video_map_refresh": {"map_niche_changed": 3120}})
    action = rp.next_actions(record, ctx)
    ok = (action["action"] == "spine"
          and action["step"] == "recode_refresh_studies"
          # No study filter: a moved partition re-niches every study.
          and action["task_args"] == {})
    _check("test_map_that_moved_dispatches_recode_for_all_studies", ok, str(action))


def test_meta_and_pca_scoped_to_changed_studies():
    record = rp.plan_run("video_map_refresh", kind="card")
    record["steps"]["recode_refresh_studies"] = {"state": "dispatched"}
    ctx = _ctx(record, {"video_map_refresh": {"map_niche_changed": 10},
                        "recode_refresh_studies": {"studies_changed": ["a", "b"],
                                                   "studies_unchanged": ["c"]}})
    action = rp.next_actions(record, ctx)
    leaves = dict(action["leaves"])
    ok = (action["action"] == "fork"
          and leaves.get("meta_refresh_groups") == {"studies": "a,b"}
          and leaves.get("pca_refresh") == {"studies": "a,b"})
    _check("test_meta_and_pca_scoped_to_changed_studies", ok, str(action["leaves"]))


def test_unchanged_studies_prune_meta_and_pca():
    record = rp.plan_run("video_map_refresh", kind="card")
    record["steps"]["recode_refresh_studies"] = {"state": "dispatched"}
    ctx = _ctx(record, {"video_map_refresh": {"map_niche_changed": 10},
                        "recode_refresh_studies": {"studies_changed": []}})
    action = rp.next_actions(record, ctx)
    leaves = dict(action["leaves"])
    states = _states(record)
    ok = (states["meta_refresh_groups"] == "pruned"
          and states["pca_refresh"] == "pruned"
          # timelines and sessions still run: they read the map, not the studies.
          and "timelines_refresh" in leaves and "sessions_refresh" in leaves)
    _check("test_unchanged_studies_prune_meta_and_pca", ok, str((states, list(leaves))))


def test_embeddings_that_wrote_nothing_prune_the_map():
    record = rp.plan_run("consolidate_enrichment", kind="consolidate")
    record["steps"]["consolidate_enrichment"] = {"state": "origin"}
    record["steps"]["embeddings_refresh"] = {"state": "dispatched"}
    ctx = _ctx(record,
               {"consolidate_enrichment": {}, "embeddings_refresh": {"embeddings_embedded_run": 0}},
               {"new_annotation_item_count": 40, "affected_study_names": [],
                "affected_collection_ids": []})
    rp.next_actions(record, ctx)
    _check("test_embeddings_that_wrote_nothing_prune_the_map",
           _states(record)["video_map_refresh"] == "pruned", str(_states(record)))


def test_missing_signal_never_prunes():
    # A worker that reports nothing means "unknown", and unknown always runs: a
    # wasted refresh costs minutes, a wrongly skipped one leaves a stale cache.
    record = rp.plan_run("video_map_refresh", kind="card")
    action = rp.next_actions(record, _ctx(record, {"video_map_refresh": {}}))
    ok = action["action"] == "spine" and action["step"] == "recode_refresh_studies"
    _check("test_missing_signal_never_prunes", ok, str(action))


def test_fork_comes_from_the_last_step_that_ran():
    # recode pruned, but the map moved — the leaves that read the map still run,
    # and they fan out from the map rather than from a step that never ran.
    record = rp.plan_run("embeddings_refresh", kind="card")
    record["steps"]["embeddings_refresh"] = {"state": "origin"}
    record["steps"]["video_map_refresh"] = {"state": "dispatched"}
    ctx = _ctx(record, {"embeddings_refresh": {"embeddings_embedded_run": 900},
                        "video_map_refresh": {"map_niche_changed": 7}})
    # recode would run here (the map moved), so prune it by hand to isolate the
    # fan-out rule: with no spine step left, the leaves go together.
    record["steps"]["recode_refresh_studies"] = {"state": "pruned"}
    action = rp.next_actions(record, ctx)
    leaves = [n for n, _ in action["leaves"]]
    ok = (action["action"] == "fork"
          and leaves == ["timelines_refresh", "sessions_refresh"]
          and _states(record)["meta_refresh_groups"] == "pruned")
    _check("test_fork_comes_from_the_last_step_that_ran", ok, str((action["action"], leaves)))


def test_collection_only_impact_forks_from_consolidate():
    record = rp.plan_run("consolidate_enrichment", kind="consolidate")
    record["steps"]["consolidate_enrichment"] = {"state": "origin"}
    ctx = _ctx(record, {"consolidate_enrichment": {}},
               {"new_annotation_item_count": 0, "affected_study_names": [],
                "affected_collection_ids": ["c1", "c2"]})
    action = rp.next_actions(record, ctx)
    leaves = dict(action["leaves"])
    ok = (action["action"] == "fork"
          and leaves.get("timelines_refresh") == {"collections": "c1,c2"}
          and "sessions_refresh" in leaves
          and _states(record)["embeddings_refresh"] == "pruned")
    _check("test_collection_only_impact_forks_from_consolidate", ok, str(action))


# -------- Barrier --------


class _BarrierHarness:
    """Monkeypatch process_routes so the barrier can be exercised in-process."""

    def __init__(self, statuses: dict):
        self._statuses = statuses
        self.summary_calls: list = []
        self.stamp_calls: list = []

    def __enter__(self):
        self._orig_read = pr.read_task_status
        self._orig_finish = rp.finish_run
        self._orig_publish = pr._publish_run_summary
        self._orig_stamp = pr.stamp_task_status
        pr.read_task_status = lambda name: self._statuses.get(name)

        def _finish(partial=False, failed_at=None, reason=None, prunes=None, run_id=None):
            self.summary_calls.append({"partial": partial, "failed_at": failed_at})
            return {"partial": partial, "failed_at": failed_at}
        rp.finish_run = _finish
        pr._publish_run_summary = lambda record: None

        def _stamp(name, state, message="", error=None, stage=None):
            self.stamp_calls.append({"name": name, "state": state, "message": message})
        pr.stamp_task_status = _stamp
        return self

    def __exit__(self, *a):
        pr.read_task_status = self._orig_read
        rp.finish_run = self._orig_finish
        pr._publish_run_summary = self._orig_publish
        pr.stamp_task_status = self._orig_stamp

    def stamped_failed(self, name: str) -> bool:
        return any(c["name"] == name and c["state"] == "failed" for c in self.stamp_calls)

    @property
    def fired(self) -> bool:
        return len(self.summary_calls) > 0


_LEAVES = ["meta_refresh_groups", "pca_refresh", "timelines_refresh"]
_GRACE = pr.FORK_START_GRACE_SECONDS


def _ts(offset_s: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


def test_barrier_fires_when_all_leaves_completed():
    fork = _ts(-10)
    statuses = {l: {"state": "completed", "updated_at": _ts(0)} for l in _LEAVES}
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    ok = h.fired and h.summary_calls[0]["partial"] is False
    _check("test_barrier_fires_when_all_leaves_completed", ok, str(h.summary_calls))


def test_barrier_waits_for_running_leaf():
    fork = _ts(-10)
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(0)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(0)},
        "timelines_refresh": {"state": "running", "updated_at": _ts(0)},
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    _check("test_barrier_waits_for_running_leaf", not h.fired, str(h.summary_calls))


def test_barrier_ignores_stale_status_before_fork():
    # A leaf shows "completed" but from a PREVIOUS run (updated_at < fork_ts):
    # the barrier must treat it as not-yet-finished and wait.
    fork = _ts(0)
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(5)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(5)},
        "timelines_refresh": {"state": "completed", "updated_at": _ts(-60)},  # stale
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    _check("test_barrier_ignores_stale_status_before_fork", not h.fired, str(h.summary_calls))


def test_barrier_marks_partial_on_leaf_failure():
    fork = _ts(-10)
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(0)},
        "pca_refresh": {"state": "failed", "updated_at": _ts(0)},
        "timelines_refresh": {"state": "completed", "updated_at": _ts(0)},
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    call = h.summary_calls[0] if h.fired else {}
    ok = h.fired and call.get("partial") is True and "pca_refresh" in (call.get("failed_at") or "")
    _check("test_barrier_marks_partial_on_leaf_failure", ok, str(h.summary_calls))


def test_barrier_waits_for_missing_status():
    # A leaf has no status file yet (dispatch in flight) → wait, do not fire.
    fork = _ts(-10)
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(0)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(0)},
        # timelines_refresh absent
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    _check("test_barrier_waits_for_missing_status", not h.fired, str(h.summary_calls))


def test_barrier_kills_queued_leaf_after_grace():
    # A leaf still "queued" past the grace window = it was dropped (429) and
    # never started → mark it failed and finalize partial, so the card stops
    # looking like it is waiting.
    fork = _ts(-(_GRACE + 30))
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(-10)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(-10)},
        "timelines_refresh": {"state": "queued", "updated_at": _ts(-(_GRACE + 30))},
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    call = h.summary_calls[0] if h.fired else {}
    ok = (
        h.fired
        and h.stamped_failed("timelines_refresh")
        and call.get("partial") is True
        and "timelines_refresh" in (call.get("failed_at") or "")
    )
    _check("test_barrier_kills_queued_leaf_after_grace", ok, str((h.summary_calls, h.stamp_calls)))


def test_barrier_spares_running_leaf_past_grace():
    # A genuinely-running (slow) leaf keeps heartbeating — never kill it, even
    # past grace; the barrier waits for it.
    fork = _ts(-(_GRACE + 30))
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(-10)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(-10)},
        "timelines_refresh": {"state": "running", "updated_at": _ts(-2)},
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    ok = (not h.fired) and (not h.stamped_failed("timelines_refresh"))
    _check("test_barrier_spares_running_leaf_past_grace", ok, str((h.summary_calls, h.stamp_calls)))


def test_barrier_waits_for_queued_leaf_within_grace():
    # Still queued but within the grace window (normal cold start) → wait.
    fork = _ts(-10)
    statuses = {
        "meta_refresh_groups": {"state": "completed", "updated_at": _ts(0)},
        "pca_refresh": {"state": "completed", "updated_at": _ts(0)},
        "timelines_refresh": {"state": "queued", "updated_at": _ts(-10)},
    }
    with _BarrierHarness(statuses) as h:
        pr._maybe_finish_forked_pipeline(_LEAVES, fork_ts=fork)
    ok = (not h.fired) and (not h.stamped_failed("timelines_refresh"))
    _check("test_barrier_waits_for_queued_leaf_within_grace", ok, str(h.summary_calls))


def test_a_replayed_refresh_acts_on_the_impact_it_inherits():
    """A deferred/replayed refresh must dispatch, not evaporate.

    Prod, 2026-09-04: the enrichment supervisor's finalize called
    dispatch_downstream_refresh(None) for a real deferred debt — 50 annotated
    items, 7 collections, 7 studies. plan_run(origin_ran=False) marked all seven
    steps "planned", then next_actions returned "finish" and dispatched nothing:
    every predicate gates on "did my upstream run in THIS run?", and a replayed
    run does not re-run the consolidation it is catching up on. The supervisor
    read that as "noop" and SETTLED the debt — dropping the work entirely, not
    merely deferring it again.
    """
    impact = {
        "new_annotation_item_count": 50,
        "affected_collection_ids": ["c1", "c2"],
        "affected_study_names": ["standard_study", "scraped_ones"],
    }
    record = rp.plan_run("consolidate_enrichment", kind="refresh_downstream",
                         started_by="supervisor", impact=impact, origin_ran=False)
    action = rp.next_actions(record)
    _check(
        "test_a_replayed_refresh_acts_on_the_impact_it_inherits",
        action["action"] != "finish",
        "a replayed refresh dispatched nothing, so its debt would be dropped",
    )


def test_a_replayed_refresh_scopes_to_what_actually_changed():
    """Inheriting the consolidation must not mean rebuilding everything: a debt
    of collections only has no study to recode and no annotation to embed."""
    record = rp.plan_run(
        "consolidate_enrichment", kind="refresh_downstream", origin_ran=False,
        impact={"new_annotation_item_count": 0,
                "affected_collection_ids": ["c1"],
                "affected_study_names": []})
    action = rp.next_actions(record)
    leaves = [s for s, _ in action.get("leaves", [])]
    _check(
        "test_a_replayed_refresh_scopes_to_what_actually_changed",
        action["action"] == "fork" and "timelines_refresh" in leaves
        and "recode_refresh_studies" not in leaves,
        f"expected a timelines-side fork, got {action['action']} {leaves}",
    )


def test_the_consolidations_scope_survives_a_moved_map():
    """The consolidation's list is the scope, even when the map moved videos.

    Regression, 2026-09-04: an earlier version of _needs_recode widened to
    every study whenever the map reported ANY niche change, on the theory that
    niche columns elsewhere go stale. A warm-started rebuild routinely moves a
    couple of percent of the corpus (15,371 videos on the run that exposed
    this), so it fired on essentially every run and turned the whole planner
    back into a full refresh — 13 studies and 114 collections rebuilt when the
    consolidation had named 8 and 36. Precision is the point of this module;
    the map's leftovers are a narrower problem than rebuilding everything
    hourly, and the code before this planner existed always scoped to the
    impact too.
    """
    record = rp.plan_run("consolidate_enrichment", kind="armed",
                         impact={"new_annotation_item_count": 202,
                                 "affected_study_names": ["s%d" % i for i in range(8)],
                                 "affected_collection_ids": ["c%d" % i for i in range(36)]})
    for step in ("consolidate_enrichment", "embeddings_refresh", "video_map_refresh"):
        record["steps"][step]["state"] = "dispatched"
    ctx = _ctx(record,
               {"consolidate_enrichment": {}, "embeddings_refresh": {},
                "video_map_refresh": {"map_niche_changed": 15371, "map_cold_start": False}},
               impact=record["impact"])

    recode = rp.BY_NAME["recode_refresh_studies"].needs(ctx)
    timelines = rp.BY_NAME["timelines_refresh"].needs(ctx)
    _check("test_the_consolidations_scope_survives_a_moved_map",
           rp.scope_note("recode_refresh_studies", recode.task_args) == "8 studies"
           and rp.scope_note("timelines_refresh", timelines.task_args) == "36 collections",
           f"recode={recode.task_args} timelines={timelines.task_args}")


def test_a_map_origin_run_has_no_impact_so_it_refreshes_everything():
    """The one case the widening was really for.

    A run started from the Semantic Map card never consolidated, so there is no
    impact to scope by — the map's own verdict is all there is, and scoping to
    an absent impact would refresh nothing at all.
    """
    record = rp.plan_run("video_map_refresh", kind="card")
    record["steps"]["video_map_refresh"]["state"] = "dispatched"
    ctx = _ctx(record,
               {"video_map_refresh": {"map_niche_changed": 15371, "map_cold_start": False}},
               impact=None)

    recode = rp.BY_NAME["recode_refresh_studies"].needs(ctx)
    timelines = rp.BY_NAME["timelines_refresh"].needs(ctx)
    _check("test_a_map_origin_run_has_no_impact_so_it_refreshes_everything",
           recode.run and not (recode.task_args or {}).get("studies")
           and timelines.run and not (timelines.task_args or {}).get("collections")
           and "15,371" in recode.reason,
           f"recode={recode.task_args} reason={recode.reason!r}")


def test_an_unmoved_map_keeps_the_consolidations_scope():
    """The flip side: without a niche change the impact's list is honoured."""
    record = rp.plan_run("consolidate_enrichment", kind="armed",
                         impact={"new_annotation_item_count": 202,
                                 "affected_study_names": ["s1", "s2"],
                                 "affected_collection_ids": ["c1"]})
    for step in ("consolidate_enrichment", "embeddings_refresh", "video_map_refresh"):
        record["steps"][step]["state"] = "dispatched"
    ctx = _ctx(record,
               {"consolidate_enrichment": {}, "embeddings_refresh": {},
                "video_map_refresh": {"map_niche_changed": 0, "map_cold_start": False}},
               impact=record["impact"])

    verdict = rp.BY_NAME["recode_refresh_studies"].needs(ctx)
    _check("test_an_unmoved_map_keeps_the_consolidations_scope",
           verdict.run and (verdict.task_args or {}).get("studies") == "s1,s2"
           and rp.scope_note("recode_refresh_studies", verdict.task_args) == "2 studies",
           f"task_args={verdict.task_args}")


TESTS = [
    test_order_lists_in_sync,
    test_dependency_invariants,
    test_every_step_has_a_stage_label,
    test_every_step_has_a_local_script,
    test_dependents_of_each_origin,
    test_consolidate_plans_everything,
    test_card_origin_marks_earlier_steps_upstream,
    test_leaf_origin_plans_nothing,
    test_consolidate_only_plans_nothing,
    test_stage_total_is_tree_depth,
    test_map_that_moved_nothing_prunes_everything,
    test_map_that_moved_dispatches_recode_for_all_studies,
    test_meta_and_pca_scoped_to_changed_studies,
    test_unchanged_studies_prune_meta_and_pca,
    test_embeddings_that_wrote_nothing_prune_the_map,
    test_missing_signal_never_prunes,
    test_fork_comes_from_the_last_step_that_ran,
    test_collection_only_impact_forks_from_consolidate,
    test_barrier_fires_when_all_leaves_completed,
    test_barrier_waits_for_running_leaf,
    test_barrier_ignores_stale_status_before_fork,
    test_barrier_marks_partial_on_leaf_failure,
    test_barrier_waits_for_missing_status,
    test_barrier_kills_queued_leaf_after_grace,
    test_barrier_spares_running_leaf_past_grace,
    test_barrier_waits_for_queued_leaf_within_grace,
    test_a_replayed_refresh_acts_on_the_impact_it_inherits,
    test_a_replayed_refresh_scopes_to_what_actually_changed,
    test_the_consolidations_scope_survives_a_moved_map,
    test_a_map_origin_run_has_no_impact_so_it_refreshes_everything,
    test_an_unmoved_map_keeps_the_consolidations_scope,
]


def main():
    print(f"\nRunning {len(TESTS)} refresh-pipeline tests...\n")
    for t in TESTS:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}  ({e})")
            traceback.print_exc()
    print(f"\nSummary: {PASS} passed, {FAIL} failed\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
