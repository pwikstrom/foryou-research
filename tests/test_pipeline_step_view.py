"""Ad-hoc test for the consolidate pipeline step-view + partial-state logic.

Exercises management_routes._build_pipeline_step_view with a fabricated
process_stats (local mode, so no GCS reads) across the success / partial /
in-flight scenarios.
"""

import web_interface.services.worker_status as mr


def _stats(plan_steps, started_ts, step_outcomes):
    """Build a fake process_stats dict.

    step_outcomes: {step: (last_run_end_time, last_run_outcome)} for steps that ran.
    """
    stats = {
        "consolidate_enrichment": {
            "pipeline_plan": {"steps": plan_steps, "started_ts": started_ts},
            "last_run_end_time": "2026-06-26T01:00:01+00:00",
            "last_run_outcome": "Success",
        }
    }
    for step, (end, outcome) in step_outcomes.items():
        stats[step] = {"last_run_end_time": end, "last_run_outcome": outcome}
    return stats


def _states(view):
    return {row["step"]: row["state"] for row in view}


def run():
    mr.is_cloud_run = lambda: False  # force process_stats fallback, no GCS
    started = "2026-06-26T01:00:00+00:00"
    after = "2026-06-26T01:05:00+00:00"
    before = "2026-06-25T10:00:00+00:00"
    plan = ["embeddings_refresh", "video_map_refresh", "recode_refresh_studies",
            "meta_refresh_groups", "pca_refresh", "timelines_refresh"]

    # 1) Full success — every step ran after started_ts.
    mr.process_stats = _stats(plan, started, {s: (after, "Success") for s in plan})
    s1 = _states(mr._build_pipeline_step_view(pipeline_active=False))
    assert all(v == "success" for v in s1.values()), s1
    assert s1["consolidate_enrichment"] == "success"

    # 2) Partial — embeddings ok, video_map failed, rest never ran; pipeline done.
    mr.process_stats = _stats(plan, started, {
        "embeddings_refresh": (after, "Success"),
        "video_map_refresh": (after, "Fail"),
    })
    s2 = _states(mr._build_pipeline_step_view(pipeline_active=False))
    assert s2["embeddings_refresh"] == "success", s2
    assert s2["video_map_refresh"] == "failed", s2
    assert s2["recode_refresh_studies"] == "skipped", s2
    assert s2["timelines_refresh"] == "skipped", s2

    # 3) In-flight — embeddings done, rest not yet run; pipeline active → pending.
    mr.process_stats = _stats(plan, started, {"embeddings_refresh": (after, "Success")})
    s3 = _states(mr._build_pipeline_step_view(pipeline_active=True))
    assert s3["embeddings_refresh"] == "success", s3
    assert s3["video_map_refresh"] == "pending", s3
    assert s3["recode_refresh_studies"] == "pending", s3

    # 4) Stale prior run ignored — a step whose only end_time predates started_ts
    #    counts as not-run-this-round (skipped when pipeline settled).
    mr.process_stats = _stats(plan, started, {
        "embeddings_refresh": (after, "Success"),
        "video_map_refresh": (before, "Success"),  # stale prior success
    })
    s4 = _states(mr._build_pipeline_step_view(pipeline_active=False))
    assert s4["video_map_refresh"] == "skipped", s4

    # 5) No plan → empty list (UI hides the panel).
    mr.process_stats = {"consolidate_enrichment": {}}
    assert mr._build_pipeline_step_view(pipeline_active=False) == []

    print("ALL PIPELINE STEP-VIEW TESTS PASSED")


if __name__ == "__main__":
    run()
