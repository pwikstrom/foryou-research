import os
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
# The niche columns flow embeddings -> video_map -> recode: embeddings top up
# the dense vectors, video_map re-clusters them into niches (writes
# video_map.parquet), and recode_refresh_studies joins those niches into each
# study dataset. meta/pca/timelines then consume the recoded outputs. Embedding
# and map rebuilds therefore run BEFORE the study/collection refreshes, not
# after, so the niche columns are fresh when the studies recode.
# sessions_refresh reads the embedding store and the map's trend columns, so it
# must follow video_map; it needs nothing recode writes, but riding along as a
# fork leaf keeps the tree single-forked.
_PIPELINE_STEPS_ORDER = [
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
    "sessions_refresh",
]

_PIPELINE_STAGE_LABELS = {
    "consolidate_enrichment": "Consolidating enrichment data",
    "embeddings_refresh": "Refreshing semantic embeddings",
    "video_map_refresh": "Rebuilding semantic map",
    "recode_refresh_studies": "Refreshing study definitions",
    "meta_refresh_groups": "Refreshing explore metadata",
    "pca_refresh": "Refreshing correlations",
    "timelines_refresh": "Refreshing timelines",
    "sessions_refresh": "Rebuilding session index",
}

# The downstream pipeline is an out-tree: a linear spine
# (consolidate → embeddings → video_map → recode) that fans out at recode into
# the terminal leaves below, which run concurrently. meta/pca read the per-study
# recoded datasets, timelines reads the global recoded datasets, and sessions
# reads the embedding store + the consolidated activity file; none of them feed
# another step, so no join is needed. recode is their shared parent (the niche
# columns it writes are what meta/pca surface).
_FORK_PARENT = "recode_refresh_studies"
_FORK_LEAF_TASKS = ("meta_refresh_groups", "pca_refresh", "timelines_refresh",
                    "sessions_refresh")


def build_pipeline_chain(pipeline: list[dict]) -> dict | None:
    """Build the Cloud-Tasks chain dict that launches a downstream pipeline.

    Takes the dependency-ordered candidate list from
    :func:`_build_downstream_pipeline` and returns the chain dict that dispatches
    the first step. The terminal leaves (meta/pca/timelines) are forked off
    recode_refresh_studies so they run concurrently: the recode step carries
    ``pipeline_fanout`` (the leaves to dispatch on its completion) and
    ``pipeline_leaves`` (the full leaf set, so the last leaf to finish writes the
    pipeline summary — see ``_run_task_with_stats``). When recode is absent the
    pipeline stays fully linear.

    Args:
        pipeline: Dependency-ordered ``[{"task", "task_args"}, ...]`` steps.

    Returns:
        A ``{"chain": True, "next_task", "next_task_args"}`` dict, or ``None``
        when ``pipeline`` is empty.
    """
    if not pipeline:
        return None

    names = [p["task"] for p in pipeline]
    leaves: list[dict] = []
    spine: list[dict] = list(pipeline)
    if _FORK_PARENT in names:
        leaves = [p for p in pipeline if p["task"] in _FORK_LEAF_TASKS]
        if leaves:
            spine = [p for p in pipeline if p["task"] not in _FORK_LEAF_TASKS]

    leaf_names = [p["task"] for p in leaves]

    # Stage framing reflects tree DEPTH, not task count: consolidate (1) + each
    # spine step + a single stage for the parallel leaves (when there are any).
    depth = 1 + len(spine) + (1 if leaves else 0)

    # Attach the fork metadata to the recode step so it travels inside
    # pipeline_remaining and triggers the fan-out when recode completes.
    spine_steps: list[dict] = []
    for p in spine:
        step_args = dict(p.get("task_args") or {})
        if leaves and p["task"] == _FORK_PARENT:
            step_args["pipeline_fanout"] = [
                {"task": leaf["task"], "task_args": dict(leaf.get("task_args") or {})}
                for leaf in leaves
            ]
            step_args["pipeline_leaves"] = leaf_names
        spine_steps.append({"task": p["task"], "task_args": step_args})

    first = spine_steps[0]
    remaining = spine_steps[1:]

    next_task_args = dict(first["task_args"])
    next_task_args["pipeline_remaining"] = [
        {"task": p["task"], "task_args": p["task_args"]} for p in remaining
    ]
    next_task_args["pipeline_stage_total"] = depth
    next_task_args["pipeline_stage_index"] = 2

    return {
        "chain": True,
        "next_task": first["task"],
        "next_task_args": next_task_args,
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
    if "embeddings_refresh" in steps_set:
        parts.append("semantic embeddings")
    if "video_map_refresh" in steps_set:
        parts.append("semantic map")
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
    if "sessions_refresh" in steps_set:
        parts.append("session index")

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
            "task_args": {"studies": study_csv} if study_csv else {},
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
    if affected_collections or new_annotation_count > 0:
        # The sessions artifacts go stale when a covered collection's in-window
        # play or annotated count moves, which is exactly what new scrapes and
        # annotations do. stale_only lets the worker decide: it re-segments only
        # the collections whose fingerprint changed and returns immediately when
        # none did. skip_if_busy keeps it off the toes of a sessions run already
        # in flight (e.g. one chained from a study save).
        candidates.append({
            "task": "sessions_refresh",
            "task_args": {"stale_only": True, "skip_if_busy": True},
        })
    # Semantic embeddings are corpus-global and depend only on new annotations
    # (not on which studies are affected), so they top up whenever new
    # annotation data was consolidated. The video map re-clusters those
    # embeddings into niches and must run after them; it uses empty task_args so
    # it preserves the existing niche names (reset_labels stays False) and does
    # NOT trigger its own auto_refresh downstream chain (that would duplicate the
    # recode/meta/pca/timelines steps the consolidate pipeline already carries).
    # On Cloud Run a local-only embedding backend can't serve the embeddings
    # step (the pipeline dispatches Cloud Tasks directly, bypassing the
    # start_process guard) — skip embeddings + map; they run when the user
    # triggers an embeddings refresh on the host machine. The map alone is
    # skipped too: rebuilding it without the top-up would just re-cluster the
    # same vectors.
    embeddings_dispatchable = True
    if os.environ.get("K_SERVICE"):
        try:
            from fyp.analysis.embedding_backends import active_backend_name, get_backend
            embeddings_dispatchable = get_backend(active_backend_name()).cloud_run_capable
        except Exception:
            embeddings_dispatchable = False
    if new_annotation_count > 0 and embeddings_dispatchable:
        candidates.append({"task": "embeddings_refresh", "task_args": {}})
        candidates.append({"task": "video_map_refresh", "task_args": {}})
    elif new_annotation_count > 0:
        print("Skipping embeddings/video-map pipeline steps: the active "
              "embedding backend runs only on a local machine.")

    if not candidates:
        return []

    # Sort into canonical dependency order (embeddings → video_map → recode →
    # meta → pca → timelines).
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

    # Feed the reporter sub-progress from inside consolidation so the UI step
    # doesn't sit frozen at 10% for the whole run. consolidate_enrichment_data
    # takes a plain (pct, msg) callback — it stays web-agnostic; we adapt it to
    # the reporter here, keeping the same stage framing as the 10% emit above.
    def _consolidate_progress(pct: float, msg: str) -> None:
        reporter.update_progress(
            int(pct),
            msg,
            stage_index=1,
            stage_total=1,
            stage_name="consolidate_enrichment",
        )

    result = consolidate_enrichment_data(
        force_consolidation=force,
        verbose=False,
        progress_cb=_consolidate_progress,
    )
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

    # Recruitment funnel: consolidation is the moment new annotations become
    # visible in enrichment_status.parquet, so check whether any participant's
    # prioritised first batch just completed (emails the owner + arms the
    # real-data tour re-offer). Never blocks the run.
    if had_new_data:
        try:
            from web_interface.services.participant_enrichment import check_first_batch_completions

            done = check_first_batch_completions()
            if done:
                reporter.log(f"Participant first batches completed: {', '.join(done)}")
        except Exception as exc:
            reporter.log(f"First-batch completion check failed (consolidation unaffected): {exc}")

    # ---- Pipeline dispatch: chain into stale downstream refreshes ----
    # Always write a last_pipeline_summary so the UI has a definitive
    # statement of the outcome (persists alongside "Last consolidation
    # {date}" across page reloads and subsequent polls).
    if not auto_refresh:
        # No downstream pipeline runs, so clear any plan/partial flags left over
        # from a previous refresh run — the step list should not show a stale
        # chain after an incremental/force consolidation.
        reporter.emit_data({
            "last_pipeline_summary": "Downstream refreshes were skipped.",
            "last_pipeline_summary_ts": now_iso,
            "pipeline_plan": None,
            "last_pipeline_partial": False,
            "last_pipeline_failed_at": None,
        })
        return None

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        summary = build_pipeline_summary(impact, steps_ran=[])
        reporter.emit_data({
            "last_pipeline_summary": summary,
            "last_pipeline_summary_ts": now_iso,
            "pipeline_plan": None,
            "last_pipeline_partial": False,
            "last_pipeline_failed_at": None,
        })
        reporter.log(f"Pipeline outcome: {summary}")
        return None

    # Pipeline will follow — emit a provisional summary plus the ordered plan so
    # the UI can render every step (live + persistent) and reset any partial
    # flag from a previous run. started_ts lets the stats endpoint tell which
    # step runs belong to THIS pipeline (end_time >= started_ts).
    reporter.emit_data({
        "last_pipeline_summary": "Pipeline in progress — refreshing caches...",
        "last_pipeline_summary_ts": now_iso,
        "pipeline_plan": {
            "steps": [p["task"] for p in pipeline],
            "started_ts": now_iso,
        },
        "last_pipeline_partial": False,
        "last_pipeline_failed_at": None,
    })

    # Build the chain dispatch: a linear spine that fans out at recode into the
    # concurrent leaves (meta ‖ pca ‖ timelines). See build_pipeline_chain.
    chain = build_pipeline_chain(pipeline)
    next_task_args = chain["next_task_args"]

    reporter.log(
        f"Auto-refresh: dispatching {chain['next_task']} "
        f"(stage 2/{next_task_args['pipeline_stage_total']}); "
        f"pipeline={[p['task'] for p in pipeline]}"
    )

    return chain




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    # In subprocess mode the chain-dispatch return value is intentionally
    # ignored — the web service's monitor_process_completion handles the
    # downstream orchestration in local dev. Cloud Tasks uses the chain
    # result directly in _run_task_with_stats.
    run_worker(
        run_consolidate_enrichment,
        "consolidate_enrichment",
        arg_specs=[
            (('--force-consolidation',), {'action': 'store_true',
                                          'help': 'Re-consolidate even if no new files detected.'}),
            (('--auto-refresh',), {'action': 'store_true',
                                   'help': 'After consolidation, record the impact so the '
                                           'web service can dispatch downstream refreshes.'}),
        ],
        make_task_args=lambda args: {
            "force_consolidation": bool(args.force_consolidation),
            "auto_refresh": bool(args.auto_refresh),
        },
        description="Consolidate enrichment data",
    )
