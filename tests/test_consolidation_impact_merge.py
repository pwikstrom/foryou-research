"""Reproduce + verify the consolidation-impact merge bugs (local-dev shadowing).

The stats / step-view endpoints overlay the in-memory
``processes["consolidate_enrichment"]["data"]`` dict on top of ``process_stats``.
After a "Consolidate Only" run that in-memory copy carries ``pipeline_plan=None``
and a stale ``consolidation_impact``. This test drives the REAL
``_build_pipeline_step_view`` and the real stats-merge expression to show:

  * Bug 1: a stale in-memory ``pipeline_plan=None`` hides the step list.
  * Bug 1 fix: mirroring the fresh plan into the in-memory copy restores it.
  * Bug 2: a stale in-memory ``consolidation_impact`` re-shows the panel.
  * Bug 2 fix: popping it from the in-memory copy hides the panel.
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from web_interface.process_manager import process_stats, processes
from web_interface.services import worker_status as mr


def _reset_consolidate_state(ps_entry: dict, mem_data: dict) -> None:
    process_stats["consolidate_enrichment"] = ps_entry
    processes.setdefault("consolidate_enrichment", {})["data"] = mem_data
    # Ensure none of the pipeline steps look "running" locally.
    for step in ["consolidate_enrichment", "embeddings_refresh", "video_map_refresh",
                 "recode_refresh_studies", "meta_refresh_groups", "pca_refresh",
                 "timelines_refresh"]:
        processes.setdefault(step, {})["status"] = "stopped"


def main() -> None:
    assert not mr.is_cloud_run(), "test assumes local mode"

    fresh_plan = {
        "steps": ["embeddings_refresh", "video_map_refresh", "recode_refresh_studies",
                  "meta_refresh_groups", "pca_refresh", "timelines_refresh"],
        "started_ts": "2026-06-26T00:00:00+00:00",
    }
    impact = {"changed_item_count": 42, "affected_collection_ids": ["c1"],
              "affected_study_names": ["s1"]}

    failures = []

    # --- Bug 1: stale in-memory pipeline_plan=None shadows the fresh plan ---
    # State right after "Consolidate Only" then "Refresh All Affected" wrote the
    # fresh plan to process_stats ONLY (the pre-fix world).
    _reset_consolidate_state(
        ps_entry={"pipeline_plan": fresh_plan, "consolidation_impact": impact},
        mem_data={"pipeline_plan": None, "consolidation_impact": impact},
    )
    steps_buggy = mr._build_pipeline_step_view(pipeline_active=True)
    print(f"Bug 1 repro (stale mem None): {len(steps_buggy)} steps")
    if steps_buggy:
        failures.append("Bug 1 repro expected 0 steps (shadowed), got some")

    # Apply the Fix 1 mirror: refresh-downstream now also writes the plan into
    # the in-memory copy.
    processes["consolidate_enrichment"]["data"]["pipeline_plan"] = fresh_plan
    steps_fixed = mr._build_pipeline_step_view(pipeline_active=True)
    print(f"Bug 1 after fix (mem synced):  {len(steps_fixed)} steps")
    # consolidate_enrichment + 6 plan steps = 7
    if len(steps_fixed) != 7:
        failures.append(f"Bug 1 fix expected 7 steps, got {len(steps_fixed)}")
    labels = [s["step"] for s in steps_fixed]
    if labels[0] != "consolidate_enrichment" or "timelines_refresh" not in labels:
        failures.append(f"Bug 1 fix step set wrong: {labels}")

    # --- Bug 2: stale in-memory consolidation_impact re-shows the panel ---
    # State right after the pipeline completed: process_stats impact popped by
    # _run_local_pipeline, but the in-memory copy still carries it (pre-fix).
    _reset_consolidate_state(
        ps_entry={"pipeline_plan": fresh_plan},  # impact already popped
        mem_data={"pipeline_plan": fresh_plan, "consolidation_impact": impact},
    )
    consolidate_entry = process_stats.get("consolidate_enrichment", {})
    merged_buggy = {**consolidate_entry,
                    **processes.get("consolidate_enrichment", {}).get("data", {})}
    print(f"Bug 2 repro merged impact present: {bool(merged_buggy.get('consolidation_impact'))}")
    if not merged_buggy.get("consolidation_impact"):
        failures.append("Bug 2 repro expected stale impact to surface in merge")

    # Apply the Fix 2 pop: _run_local_pipeline now also pops it from in-memory.
    processes["consolidate_enrichment"]["data"].pop("consolidation_impact", None)
    merged_fixed = {**consolidate_entry,
                    **processes.get("consolidate_enrichment", {}).get("data", {})}
    print(f"Bug 2 after fix merged impact present: {bool(merged_fixed.get('consolidation_impact'))}")
    if merged_fixed.get("consolidation_impact"):
        failures.append("Bug 2 fix expected no impact in merge")

    print()
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        raise SystemExit(1)
    print("All consolidation-impact merge checks passed.")


if __name__ == "__main__":
    main()
