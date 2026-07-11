#!/usr/bin/env python3
"""Simulate the refresh-pipeline step indicators without running consolidation.

The whole indicator path reads from only two stores — per-step GCS
``task_status/*.json`` (via ``read_task_status``) and ``process_stats``
(``pipeline_plan`` etc.). Nothing re-derives state from real data files, so we can
drive ``_build_pipeline_step_view`` through every UI scenario by seeding those two
stores and monkeypatching ``read_task_status`` / ``is_cloud_run``.

This is the regression net for the "list appears late / Consolidate-only shows no
list" fix: a plan marker seeded at dispatch (steps=[]) must make the live
"Consolidate enrichment data" step appear immediately, and downstream steps must
stream in / terminalize correctly.

Usage:
    python tests/test_pipeline_indicator_sim.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fyp.fyp_config  # noqa: F401  (init config in local mode before importing routes)
from web_interface.services import worker_status as m


# Deterministic timestamps (no wall clock — keeps the sim reproducible).
T0 = "2026-07-08T10:00:00+00:00"        # pipeline started_ts
T_STALE = "2026-07-08T09:00:00+00:00"   # predates T0 → a prior run's leftover
T_FRESH = "2026-07-08T10:05:00+00:00"   # after T0 → belongs to this run
T_END = "2026-07-08T10:03:00+00:00"     # a terminal end-time within this run

DOWNSTREAM = [
    "embeddings_refresh", "video_map_refresh", "recode_refresh_studies",
    "meta_refresh_groups", "pca_refresh", "timelines_refresh",
]

_failures: list[str] = []


def _run(name, *, plan, task_status, pipeline_active, process_stats_extra=None):
    """Seed the two stores and return {step: state} from _build_pipeline_step_view."""
    ps = {"consolidate_enrichment": {}}
    if plan is not None:
        ps["consolidate_enrichment"]["pipeline_plan"] = plan
    # Per-step terminal outcomes live in process_stats[step].
    for step, extra in (process_stats_extra or {}).items():
        ps[step] = extra

    m.is_cloud_run = lambda: True
    m.process_stats = ps
    m.processes = {}
    m.read_task_status = lambda step: task_status.get(step)

    view = m._build_pipeline_step_view(pipeline_active)
    return view, {v["step"]: v["state"] for v in view}


def _expect(scenario, cond, detail):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        _failures.append(f"{scenario}: {detail}")
    print(f"  [{tag}] {scenario}: {detail}")


# --- Scenario 1: Consolidate only — during the consolidation phase -----------
def scenario_consolidate_only_running():
    view, states = _run(
        "consolidate_only_running",
        plan={"steps": [], "started_ts": T0, "mode": "consolidate_only"},
        task_status={"consolidate_enrichment": {
            "state": "running", "updated_at": T_FRESH,
            "progress": {"percent": 40, "message": "Consolidating scrape files…"},
        }},
        pipeline_active=True,
    )
    _expect("consolidate_only_running", len(view) == 1, f"exactly one step (got {len(view)})")
    _expect("consolidate_only_running", states.get("consolidate_enrichment") == "running",
            f"consolidate step running (got {states})")
    _expect("consolidate_only_running", view and view[0].get("percent") == 40,
            "live percent surfaced (40)")


# --- Scenario 2: Consolidate only — after completion (worker cleared plan) ----
def scenario_consolidate_only_done():
    view, _ = _run(
        "consolidate_only_done",
        plan=None,  # worker emitted pipeline_plan=None
        task_status={},
        pipeline_active=False,
    )
    _expect("consolidate_only_done", view == [], "list hidden once plan cleared")


# --- Scenario 3: Consolidate & Refresh — consolidation phase (appears now) ----
def scenario_refresh_consolidating():
    view, states = _run(
        "refresh_consolidating",
        plan={"steps": [], "started_ts": T0, "mode": "refresh"},
        task_status={"consolidate_enrichment": {
            "state": "running", "updated_at": T_FRESH,
            "progress": {"percent": 15, "message": "Consolidating annotation files…"},
        }},
        pipeline_active=True,
    )
    _expect("refresh_consolidating", len(view) == 1 and states.get("consolidate_enrichment") == "running",
            f"consolidate step live before downstream planned (got {states})")


# --- Scenario 4: Consolidate & Refresh — downstream running ------------------
def scenario_refresh_downstream_running():
    view, states = _run(
        "refresh_downstream_running",
        plan={"steps": DOWNSTREAM, "started_ts": T0},
        task_status={
            "consolidate_enrichment": {"state": "completed", "updated_at": T_END},
            "embeddings_refresh": {"state": "running", "updated_at": T_FRESH,
                                   "progress": {"percent": 30, "message": "Embedding…"}},
        },
        pipeline_active=True,
        process_stats_extra={
            "consolidate_enrichment": {
                "pipeline_plan": {"steps": DOWNSTREAM, "started_ts": T0},
                "last_run_outcome": "Success", "last_run_end_time": T_END,
            },
        },
    )
    _expect("refresh_downstream_running", states.get("consolidate_enrichment") == "success",
            "consolidate → success")
    _expect("refresh_downstream_running", states.get("embeddings_refresh") == "running",
            "embeddings → running")
    _expect("refresh_downstream_running",
            all(states.get(s) == "pending" for s in DOWNSTREAM[1:]),
            f"remaining downstream pending while active (got {states})")


# --- Scenario 5: Fork leaves queued / running -------------------------------
def scenario_fork_leaves():
    _, states = _run(
        "fork_leaves",
        plan={"steps": DOWNSTREAM, "started_ts": T0},
        task_status={
            "meta_refresh_groups": {"state": "queued", "updated_at": T_FRESH},
            "pca_refresh": {"state": "running", "updated_at": T_FRESH,
                            "progress": {"percent": 10, "message": "PCA…"}},
        },
        pipeline_active=True,
        process_stats_extra={
            "consolidate_enrichment": {
                "pipeline_plan": {"steps": DOWNSTREAM, "started_ts": T0},
                "last_run_outcome": "Success", "last_run_end_time": T_END,
            },
            "embeddings_refresh": {"last_run_outcome": "Success", "last_run_end_time": T_END},
            "video_map_refresh": {"last_run_outcome": "Success", "last_run_end_time": T_END},
            "recode_refresh_studies": {"last_run_outcome": "Success", "last_run_end_time": T_END},
        },
    )
    _expect("fork_leaves", states.get("meta_refresh_groups") == "queued", "meta leaf queued")
    _expect("fork_leaves", states.get("pca_refresh") == "running", "pca leaf running")
    _expect("fork_leaves", states.get("timelines_refresh") == "pending", "timelines leaf pending")
    _expect("fork_leaves", states.get("recode_refresh_studies") == "success", "recode success")


# --- Scenario 6: Failure mid-pipeline → later steps skipped ------------------
def scenario_failure_skips_rest():
    _, states = _run(
        "failure_skips_rest",
        plan={"steps": DOWNSTREAM, "started_ts": T0},
        task_status={},  # nothing live — pipeline aborted
        pipeline_active=False,
        process_stats_extra={
            "consolidate_enrichment": {
                "pipeline_plan": {"steps": DOWNSTREAM, "started_ts": T0},
                "last_run_outcome": "Success", "last_run_end_time": T_END,
            },
            "embeddings_refresh": {"last_run_outcome": "Success", "last_run_end_time": T_END},
            "video_map_refresh": {"last_run_outcome": "Fail", "last_run_end_time": T_END},
        },
    )
    _expect("failure_skips_rest", states.get("video_map_refresh") == "failed", "failed step marked failed")
    _expect("failure_skips_rest",
            all(states.get(s) == "skipped" for s in ["recode_refresh_studies", "meta_refresh_groups",
                                                     "pca_refresh", "timelines_refresh"]),
            f"downstream-of-failure skipped (got {states})")


# --- Scenario 7: Stale status is not counted as this run --------------------
def scenario_stale_status_ignored():
    _, states = _run(
        "stale_status_ignored",
        plan={"steps": DOWNSTREAM, "started_ts": T0},
        task_status={
            # A leftover "running" from a PRIOR run (updated_at predates started_ts).
            "embeddings_refresh": {"state": "running", "updated_at": T_STALE,
                                   "progress": {"percent": 99, "message": "old"}},
        },
        pipeline_active=True,
        process_stats_extra={
            "consolidate_enrichment": {
                "pipeline_plan": {"steps": DOWNSTREAM, "started_ts": T0},
                "last_run_outcome": "Success", "last_run_end_time": T_END,
            },
        },
    )
    _expect("stale_status_ignored", states.get("embeddings_refresh") == "pending",
            f"stale running status ignored → pending (got {states.get('embeddings_refresh')})")


def main():
    print("Pipeline indicator simulation (Cloud Run path):")
    scenario_consolidate_only_running()
    scenario_consolidate_only_done()
    scenario_refresh_consolidating()
    scenario_refresh_downstream_running()
    scenario_fork_leaves()
    scenario_failure_skips_rest()
    scenario_stale_status_ignored()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All pipeline-indicator scenarios passed.")


if __name__ == "__main__":
    main()
