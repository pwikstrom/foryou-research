import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


# Downstream refresh steps dispatched by the auto-pipeline, in dependency
# order. Keep this in sync with PIPELINE_STEPS_ORDER in management_routes.py.
# embeddings_refresh is last: it is corpus-global and only depends on new
# annotations, so it runs after the study/collection refreshes.
_PIPELINE_STEPS_ORDER = [
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
    "embeddings_refresh",
]

_PIPELINE_STAGE_LABELS = {
    "consolidate_enrichment": "Consolidating enrichment data",
    "recode_refresh_studies": "Refreshing study definitions",
    "meta_refresh_groups": "Refreshing explore metadata",
    "pca_refresh": "Refreshing correlations",
    "timelines_refresh": "Refreshing timelines",
    "embeddings_refresh": "Refreshing semantic embeddings",
}


def build_pipeline_summary(impact: dict | None, steps_ran: list[str]) -> str:
    """Human-readable summary of what the consolidate pipeline refreshed.

    Called with the impact payload and the list of downstream step names
    that completed successfully during this pipeline run. Returns a short
    sentence suitable for rendering alongside "Last consolidation {date}"
    in the UI. When nothing ran (no impact, or auto_refresh was off), the
    summary states that positively so the user knows things are in order.
    """
    studies = (impact or {}).get("affected_study_names", []) or []
    collections = (impact or {}).get("affected_collection_ids", []) or []
    steps_set = set(steps_ran)

    parts: list[str] = []
    if "recode_refresh_studies" in steps_set and studies:
        n = len(studies)
        parts.append(f"{n} study definition{'s' if n != 1 else ''}")
    if "meta_refresh_groups" in steps_set and studies:
        parts.append(f"explore metadata ({len(studies)})")
    if "pca_refresh" in steps_set and studies:
        parts.append(f"correlations ({len(studies)})")
    if "timelines_refresh" in steps_set and collections:
        n = len(collections)
        parts.append(f"{n} timeline{'s' if n != 1 else ''}")
    if "embeddings_refresh" in steps_set:
        parts.append("semantic embeddings")

    if parts:
        return f"Refreshed {', '.join(parts)}."
    return "No cached files needed refreshing. Everything is up to date."


def _build_downstream_pipeline(impact: dict | None) -> list[dict]:
    """Compute the pipeline of stale downstream refreshes for this impact.

    Each entry is {"task": <name>, "task_args": {...}} in dispatch order.
    Returns an empty list when there is nothing to refresh (no impact, or
    impact is empty).
    """
    if not impact:
        return []

    affected_studies = impact.get("affected_study_names") or []
    affected_collections = impact.get("affected_collection_ids") or []
    new_annotation_count = int(impact.get("new_annotation_item_count") or 0)

    study_csv = ",".join(affected_studies) if affected_studies else None
    collection_csv = ",".join(affected_collections) if affected_collections else None

    candidates: list[dict] = []
    if affected_studies:
        candidates.append({
            "task": "recode_refresh_studies",
            "task_args": {"studies": study_csv} if study_csv else {},
        })
        candidates.append({
            "task": "meta_refresh_groups",
            "task_args": {},  # meta_refresh_groups refreshes all studies
        })
        candidates.append({
            "task": "pca_refresh",
            "task_args": {"studies": study_csv} if study_csv else {},
        })
    if affected_collections:
        candidates.append({
            "task": "timelines_refresh",
            "task_args": {"collections": collection_csv} if collection_csv else {},
        })
    # Semantic embeddings are corpus-global and depend only on new annotations
    # (not on which studies are affected), so they top up whenever new
    # annotation data was consolidated. The video map is deliberately NOT
    # rebuilt here — it stays a manual action so its 2D layout/niche IDs do not
    # churn on every annotation batch; the Semantic Space tab flags staleness.
    if new_annotation_count > 0:
        candidates.append({"task": "embeddings_refresh", "task_args": {}})

    if not candidates:
        return []

    # Sort into canonical dependency order (recode → meta → pca → timelines →
    # embeddings).
    by_name = {c["task"]: c for c in candidates}
    return [by_name[name] for name in _PIPELINE_STEPS_ORDER if name in by_name]


def run_consolidate_enrichment(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Consolidate enrichment data (scrapes + machine annotations).

    When ``task_args.auto_refresh`` is True and the consolidation produced a
    non-empty impact, returns a chain dict that dispatches the first stale
    downstream refresh. The chain carries ``pipeline_remaining`` and stage
    metadata so the task runner can advance the pipeline one step at a time.
    """
    import fyp.data_io as data_io
    from fyp.organize_datasets import (
        MACHINE_ANNOTATIONS_LABEL,
        SCRAPES_LABEL,
        consolidate_enrichment_data,
    )

    task_args = task_args or {}
    auto_refresh = bool(task_args.get("auto_refresh"))

    _t_run_start = time.perf_counter()

    # Stage 1 of N — the actual N is only known after we compute the pipeline.
    # We log Stage 1/? up front and re-emit with the true total once computed.
    reporter.update_progress(
        0,
        "Counting new enrichment files...",
        stage_index=1,
        stage_total=1,
        stage_name="consolidate_enrichment",
    )
    _t_phase = time.perf_counter()

    known_scrape: set[str] = set()
    known_annotation: set[str] = set()
    if data_io.exists(storage_location="recoded", filename="consolidated_enrichment_files.json"):
        meta_before = data_io.load_json(
            storage_location="recoded", filename="consolidated_enrichment_files.json"
        )
        known_scrape = set(meta_before.get(SCRAPES_LABEL, {}).get("filenames", []))
        known_annotation = set(
            meta_before.get(MACHINE_ANNOTATIONS_LABEL, {}).get("filenames", [])
        )

    current_scrape = {
        fn for fn in data_io.listdir(storage_location="scrape")
        if fn.startswith(SCRAPES_LABEL) and fn.endswith(".parquet")
    }
    current_annotation = {
        fn for fn in data_io.listdir(storage_location="machine_annotations_refined")
        if fn.startswith(MACHINE_ANNOTATIONS_LABEL) and fn.endswith(".parquet")
    }

    new_scrape_count = len(current_scrape - known_scrape)
    new_annotation_count = len(current_annotation - known_annotation)

    _t_discover = time.perf_counter() - _t_phase

    reporter.update_progress(
        10,
        f"Consolidating {new_scrape_count} new scrape and {new_annotation_count} new annotation file(s)...",
        stage_index=1,
        stage_total=1,
        stage_name="consolidate_enrichment",
    )
    _t_phase = time.perf_counter()

    force = bool(task_args.get("force_consolidation"))
    result = consolidate_enrichment_data(force_consolidation=force, verbose=False)
    had_new_data = result.get("had_new_data", False) if result else False
    impact = result.get("impact") if result else None

    _t_consolidate = time.perf_counter() - _t_phase

    now_iso = datetime.now(UTC).isoformat()

    data_payload: dict = {
        "had_new_data": had_new_data,
        "new_scrape_files": new_scrape_count,
        "new_annotation_files": new_annotation_count,
        "last_status_refresh": now_iso,
        # Always record when consolidation was last run — the UI warning uses
        # this timestamp to decide whether the scraper/annotator has completed
        # more recently than the last consolidation. had_new_data in the same
        # payload separately captures whether anything actually changed.
        "last_consolidation": now_iso,
        # Always emit consolidation_impact (None when nothing changed) so the
        # UI panel clears after a no-op run. emit_data merges into stats, so
        # omitting the key would leave the previous run's impact in place.
        "consolidation_impact": impact if impact else None,
    }

    reporter.emit_data(data_payload)
    _t_total = time.perf_counter() - _t_run_start
    reporter.log(
        f"[TIMING] consolidate_enrichment discover={_t_discover:.2f}s "
        f"consolidate={_t_consolidate:.2f}s total={_t_total:.2f}s "
        f"new_scrape={new_scrape_count} new_anno={new_annotation_count} "
        f"had_new_data={had_new_data}"
    )
    reporter.log("Consolidation finished.")

    # ---- Pipeline dispatch: chain into stale downstream refreshes ----
    # Always write a last_pipeline_summary so the UI has a definitive
    # statement of the outcome (persists alongside "Last consolidation
    # {date}" across page reloads and subsequent polls).
    if not auto_refresh:
        reporter.emit_data({
            "last_pipeline_summary": "Downstream refreshes were skipped.",
            "last_pipeline_summary_ts": now_iso,
        })
        return None

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        summary = build_pipeline_summary(impact, steps_ran=[])
        reporter.emit_data({
            "last_pipeline_summary": summary,
            "last_pipeline_summary_ts": now_iso,
        })
        reporter.log(f"Pipeline outcome: {summary}")
        return None

    # Pipeline will follow — emit a provisional summary so the UI doesn't
    # show a stale "finished" message from a previous run while we wait.
    reporter.emit_data({
        "last_pipeline_summary": "Pipeline in progress — refreshing caches...",
        "last_pipeline_summary_ts": now_iso,
    })

    first = pipeline[0]
    remaining = pipeline[1:]
    stage_total = 1 + len(pipeline)  # consolidate itself is stage 1

    next_task_args = dict(first["task_args"])
    next_task_args["pipeline_remaining"] = [
        {"task": p["task"], "task_args": p["task_args"]} for p in remaining
    ]
    next_task_args["pipeline_stage_total"] = stage_total
    next_task_args["pipeline_stage_index"] = 2

    reporter.log(
        f"Auto-refresh: dispatching {first['task']} "
        f"(stage 2/{stage_total}); remaining={[p['task'] for p in remaining]}"
    )

    return {
        "chain": True,
        "next_task": first["task"],
        "next_task_args": next_task_args,
    }




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Consolidate enrichment data")
    parser.add_argument('--force-consolidation', action='store_true',
                        help='Re-consolidate even if no new files detected.')
    parser.add_argument('--auto-refresh', action='store_true',
                        help='After consolidation, record the impact so the '
                             'web service can dispatch downstream refreshes.')
    args = parser.parse_args()

    task_args = {
        "force_consolidation": bool(args.force_consolidation),
        "auto_refresh": bool(args.auto_refresh),
    }

    reporter = LocalStatusReporter("consolidate_enrichment")
    try:
        # In subprocess mode we intentionally ignore the chain-dispatch
        # return value — the web service's monitor_process_completion
        # handles the downstream orchestration in local dev. Cloud Tasks
        # uses the chain result directly in _run_task_with_stats.
        run_consolidate_enrichment(reporter=reporter, task_args=task_args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
