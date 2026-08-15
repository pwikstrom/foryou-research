"""Unit tests for the cache-refresh auto-pipeline order + fan-out concurrency.

Covers:
  Order / invariants:
    1. test_order_lists_in_sync          (_PIPELINE_STEPS_ORDER == PIPELINE_STEPS_ORDER)
    2. test_dependency_invariants        (embeddings < video_map < recode < the leaves;
                                          video_map < sessions)
    3. test_every_step_has_a_stage_label
    4. test_every_step_has_a_local_script (the bug this fix closed: video_map missing)

  Candidate builder (_build_downstream_pipeline):
    5. test_candidate_full_order
    6. test_video_map_gated_on_new_annotations
    7. test_sessions_gated_on_changed_data
    8. test_video_map_uses_empty_task_args   (no auto_refresh → no double-dispatch)
    9. test_no_candidates_when_impact_empty

  Fork chain (build_pipeline_chain):
    10. test_fork_at_recode
    11. test_no_fork_without_recode
    12. test_stage_depth_not_task_count
    13. test_manual_video_map_path_forks

  Barrier (_maybe_finish_forked_pipeline) — the race-free completion detector:
    14. test_barrier_fires_when_all_leaves_completed
    15. test_barrier_waits_for_running_leaf
    16. test_barrier_ignores_stale_status_before_fork
    17. test_barrier_marks_partial_on_leaf_failure
    18. test_barrier_waits_for_missing_status

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
from web_interface.process_manager import local_pipeline_script_map
from web_interface.routes.management_routes import PIPELINE_STEPS_ORDER
from web_interface.run_consolidate_enrichment import (
    _FORK_LEAF_TASKS,
    _FORK_PARENT,
    _PIPELINE_STAGE_LABELS,
    _PIPELINE_STEPS_ORDER,
    _build_downstream_pipeline,
    build_pipeline_chain,
)
from web_interface.run_video_map_refresh import _DOWNSTREAM_PIPELINE


PASS = 0
FAIL = 0



def _check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")



def _idx(task: str) -> int:
    return _PIPELINE_STEPS_ORDER.index(task)



def _find_step_args(chain: dict, task: str) -> dict | None:
    """Return the task_args for ``task`` within a build_pipeline_chain result.

    The fork parent may be the first dispatched task (its args live in
    next_task_args) or a later spine step (in pipeline_remaining).
    """
    nta = chain["next_task_args"]
    if chain["next_task"] == task:
        return nta
    for step in nta.get("pipeline_remaining", []):
        if step["task"] == task:
            return step["task_args"]
    return None



# -------- Order / invariants --------


def test_order_lists_in_sync():
    _check(
        "test_order_lists_in_sync",
        _PIPELINE_STEPS_ORDER == PIPELINE_STEPS_ORDER,
        f"{_PIPELINE_STEPS_ORDER} != {PIPELINE_STEPS_ORDER}",
    )


def test_dependency_invariants():
    ok = (
        _idx("embeddings_refresh") < _idx("video_map_refresh") < _idx("recode_refresh_studies")
        and _idx("recode_refresh_studies") < _idx("meta_refresh_groups")
        and _idx("recode_refresh_studies") < _idx("pca_refresh")
        and _idx("recode_refresh_studies") < _idx("timelines_refresh")
        # sessions reads the embedding store and the map's trend columns.
        and _idx("video_map_refresh") < _idx("sessions_refresh")
    )
    _check("test_dependency_invariants", ok, str(_PIPELINE_STEPS_ORDER))


def test_every_step_has_a_stage_label():
    missing = [s for s in _PIPELINE_STEPS_ORDER if s not in _PIPELINE_STAGE_LABELS]
    _check("test_every_step_has_a_stage_label", not missing, f"missing labels: {missing}")


def test_every_step_has_a_local_script():
    # The original bug: video_map_refresh was absent from the local script_map,
    # so the local-dev pipeline aborted with "Unknown step".
    script_map = local_pipeline_script_map()
    missing = [s for s in _PIPELINE_STEPS_ORDER if s not in script_map]
    _check("test_every_step_has_a_local_script", not missing, f"missing scripts: {missing}")



# -------- Candidate builder --------


def test_candidate_full_order():
    impact = {
        "affected_study_names": ["A", "B"],
        "affected_collection_ids": ["c1"],
        "new_annotation_item_count": 5,
    }
    got = [p["task"] for p in _build_downstream_pipeline(impact)]
    _check("test_candidate_full_order", got == _PIPELINE_STEPS_ORDER, str(got))


def test_video_map_gated_on_new_annotations():
    # No new annotations → no embeddings + no video_map, even with affected studies.
    impact = {
        "affected_study_names": ["A"],
        "affected_collection_ids": [],
        "new_annotation_item_count": 0,
    }
    got = [p["task"] for p in _build_downstream_pipeline(impact)]
    ok = "video_map_refresh" not in got and "embeddings_refresh" not in got
    _check("test_video_map_gated_on_new_annotations", ok, str(got))


def test_sessions_gated_on_changed_data():
    # Sessions staleness is driven by in-window play/annotated counts, so it
    # rides along whenever collections changed or annotations landed — and stays
    # out when neither did (a studies-only impact).
    changed = {
        "affected_study_names": ["A"],
        "affected_collection_ids": [],
        "new_annotation_item_count": 3,
    }
    unchanged = {
        "affected_study_names": ["A"],
        "affected_collection_ids": [],
        "new_annotation_item_count": 0,
    }
    with_sessions = _build_downstream_pipeline(changed)
    sess = next((p for p in with_sessions if p["task"] == "sessions_refresh"), None)
    ok = (
        sess is not None
        and sess["task_args"] == {"stale_only": True, "skip_if_busy": True}
        and not any(p["task"] == "sessions_refresh"
                    for p in _build_downstream_pipeline(unchanged))
    )
    _check("test_sessions_gated_on_changed_data", ok, str(sess))


def test_video_map_uses_empty_task_args():
    # video_map must carry NO auto_refresh, or it would dispatch its own
    # downstream chain on top of the consolidate pipeline (double-dispatch).
    impact = {
        "affected_study_names": [],
        "affected_collection_ids": [],
        "new_annotation_item_count": 7,
    }
    pipe = _build_downstream_pipeline(impact)
    vm = next((p for p in pipe if p["task"] == "video_map_refresh"), None)
    ok = vm is not None and vm["task_args"] == {}
    _check("test_video_map_uses_empty_task_args", ok, str(vm))


def test_no_candidates_when_impact_empty():
    ok = _build_downstream_pipeline({}) == [] and _build_downstream_pipeline(None) == []
    _check("test_no_candidates_when_impact_empty", ok)


def test_meta_refresh_scoped_to_affected_studies():
    # meta_refresh_groups used to refresh EVERY study on every pipeline run,
    # even though the impact names exactly which studies changed.
    impact = {
        "affected_study_names": ["study_a", "study_b"],
        "affected_collection_ids": [],
        "new_annotation_item_count": 0,
    }
    pipe = _build_downstream_pipeline(impact)
    meta = next((p for p in pipe if p["task"] == "meta_refresh_groups"), None)
    ok = meta is not None and meta["task_args"] == {"studies": "study_a,study_b"}
    _check("test_meta_refresh_scoped_to_affected_studies", ok, str(meta))



# -------- Fork chain --------


def test_fork_at_recode():
    impact = {
        "affected_study_names": ["A"],
        "affected_collection_ids": ["c1"],
        "new_annotation_item_count": 5,
    }
    chain = build_pipeline_chain(_build_downstream_pipeline(impact))
    recode_args = _find_step_args(chain, _FORK_PARENT)
    fanout = [c["task"] for c in (recode_args or {}).get("pipeline_fanout", [])]
    leaves = (recode_args or {}).get("pipeline_leaves")
    expected = ["meta_refresh_groups", "pca_refresh", "timelines_refresh",
                "sessions_refresh"]
    # leaves are not in the linear spine:
    spine = [chain["next_task"]] + [p["task"] for p in chain["next_task_args"]["pipeline_remaining"]]
    ok = (
        fanout == expected
        and leaves == expected
        and not any(t in spine for t in _FORK_LEAF_TASKS)
    )
    _check("test_fork_at_recode", ok, f"fanout={fanout} leaves={leaves} spine={spine}")


def test_no_fork_without_recode():
    # collections-only: no recode → no fork parent, so the leaves run as a
    # linear spine (timelines then sessions) rather than concurrently.
    impact = {
        "affected_study_names": [],
        "affected_collection_ids": ["c1"],
        "new_annotation_item_count": 0,
    }
    chain = build_pipeline_chain(_build_downstream_pipeline(impact))
    nta = chain["next_task_args"]
    remaining = [p["task"] for p in nta.get("pipeline_remaining", [])]
    ok = (
        chain["next_task"] == "timelines_refresh"
        and remaining == ["sessions_refresh"]
        and "pipeline_fanout" not in nta
        and "pipeline_leaves" not in nta
    )
    _check("test_no_fork_without_recode", ok, str(nta))


def test_stage_depth_not_task_count():
    # Full pipeline has 7 tasks but a tree DEPTH of 5 (the 4 leaves share a stage).
    impact = {
        "affected_study_names": ["A"],
        "affected_collection_ids": ["c1"],
        "new_annotation_item_count": 5,
    }
    chain = build_pipeline_chain(_build_downstream_pipeline(impact))
    depth = chain["next_task_args"]["pipeline_stage_total"]
    _check("test_stage_depth_not_task_count", depth == 5, f"depth={depth}")


def test_manual_video_map_path_forks():
    # The manual Rebuild path (run_video_map_refresh._DOWNSTREAM_PIPELINE) must
    # fork recode → {meta, pca, timelines} too.
    chain = build_pipeline_chain(list(_DOWNSTREAM_PIPELINE))
    recode_args = _find_step_args(chain, _FORK_PARENT)
    fanout = [c["task"] for c in (recode_args or {}).get("pipeline_fanout", [])]
    ok = chain["next_task"] == "recode_refresh_studies" and fanout == [
        "meta_refresh_groups", "pca_refresh", "timelines_refresh",
    ]
    _check("test_manual_video_map_path_forks", ok, f"first={chain['next_task']} fanout={fanout}")



# -------- Barrier (race-free completion detector) --------


class _BarrierHarness:
    """Monkeypatch process_routes so the barrier can be exercised in-process."""

    def __init__(self, statuses: dict):
        self._statuses = statuses
        self.in_flight_calls: list = []
        self.summary_calls: list = []
        self.stamp_calls: list = []
        self.fork_cleared = False

    def __enter__(self):
        self._orig_read = pr.read_task_status
        self._orig_flag = pr._set_pipeline_in_flight
        self._orig_summary = pr._write_pipeline_summary_cloud
        self._orig_stamp = pr.stamp_task_status
        self._orig_clear = pr._clear_pipeline_fork
        pr.read_task_status = lambda name: self._statuses.get(name)
        pr._set_pipeline_in_flight = lambda v: self.in_flight_calls.append(v)
        pr._write_pipeline_summary_cloud = (
            lambda partial=False, failed_at=None: self.summary_calls.append(
                {"partial": partial, "failed_at": failed_at}
            )
        )

        def _stamp(name, state, message="", error=None, stage=None):
            self.stamp_calls.append({"name": name, "state": state, "message": message})
        pr.stamp_task_status = _stamp

        def _clear():
            self.fork_cleared = True
        pr._clear_pipeline_fork = _clear
        return self

    def __exit__(self, *a):
        pr.read_task_status = self._orig_read
        pr._set_pipeline_in_flight = self._orig_flag
        pr._write_pipeline_summary_cloud = self._orig_summary
        pr.stamp_task_status = self._orig_stamp
        pr._clear_pipeline_fork = self._orig_clear

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
    ok = h.fired and h.summary_calls[0]["partial"] is False and h.in_flight_calls == [False]
    _check("test_barrier_fires_when_all_leaves_completed", ok, str((h.summary_calls, h.in_flight_calls)))


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
        and h.fork_cleared
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



TESTS = [
    test_order_lists_in_sync,
    test_dependency_invariants,
    test_every_step_has_a_stage_label,
    test_every_step_has_a_local_script,
    test_candidate_full_order,
    test_video_map_gated_on_new_annotations,
    test_sessions_gated_on_changed_data,
    test_video_map_uses_empty_task_args,
    test_no_candidates_when_impact_empty,
    test_fork_at_recode,
    test_no_fork_without_recode,
    test_stage_depth_not_task_count,
    test_manual_video_map_path_forks,
    test_barrier_fires_when_all_leaves_completed,
    test_barrier_waits_for_running_leaf,
    test_barrier_ignores_stale_status_before_fork,
    test_barrier_marks_partial_on_leaf_failure,
    test_barrier_waits_for_missing_status,
    test_barrier_kills_queued_leaf_after_grace,
    test_barrier_spares_running_leaf_past_grace,
    test_barrier_waits_for_queued_leaf_within_grace,
]



def main():
    print(f"\nRunning {len(TESTS)} pipeline-order tests...\n")
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
