import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter

# What depends on this consolidation, and what a change here makes stale, is the
# refresh pipeline's business — see web_interface/services/refresh_pipeline.
# This worker consolidates and publishes the impact; it dispatches nothing.


_SHADOW_CHECK_INTERVAL_DAYS = 7


def _shadow_check_age_days() -> float | None:
    """Days since the last recorded shadow check, or None if there is none.

    Reads ``recoded/consolidation_shadow_check.json``, the marker
    :func:`verify_consolidation_equivalence` writes after every check.
    Never raises — an unreadable marker reads as "no check on record", which
    makes the check run rather than silently skipping it.
    """
    try:
        import fyp.data_io as data_io
        from fyp.organize_datasets import _SHADOW_CHECK_FILENAME
        if not data_io.exists(storage_location="recoded", filename=_SHADOW_CHECK_FILENAME):
            return None
        payload = data_io.load_json(storage_location="recoded",
                                    filename=_SHADOW_CHECK_FILENAME)
        checked_at = (payload or {}).get("checked_at")
        if not checked_at:
            return None
        delta = datetime.now(UTC) - datetime.fromisoformat(checked_at)
        return delta.total_seconds() / 86400.0
    except Exception:
        return None


def _run_shadow_verification(reporter: TaskStatusReporter) -> None:
    """Shadow-verify the incremental artifacts against a full rebuild.

    Runs as a mode of the consolidate_enrichment worker (task_args
    ``verify_consolidation``) so it shares the status key — the supervisor's
    hard gate then serializes it against real consolidations. On divergence it
    records a task failure (visible on Admin → System info) and promotes a
    real ``force_consolidation`` run, folding the resulting impact into the
    deferred-refresh ledger so downstream caches rebuild from healed data.

    Re-checks the marker age before doing the work, because this task can
    arrive more than once for a single scheduling: Cloud Tasks re-delivers on
    any dispatch failure, and a check that already passed minutes ago has
    nothing to add. Without the guard a single re-delivery costs another full
    corpus rebuild (2026-09-02: five attempts, 66 minutes of runner).
    """
    from fyp.organize_datasets import (
        consolidate_enrichment_data,
        verify_consolidation_equivalence,
    )

    def _progress(pct: float, msg: str) -> None:
        reporter.update_progress(int(pct), msg, stage_index=1, stage_total=1,
                                 stage_name="consolidate_enrichment")

    age = _shadow_check_age_days()
    if age is not None and age < _SHADOW_CHECK_INTERVAL_DAYS:
        reporter.log(
            f"Shadow verification skipped — last check was {age * 24:.1f} h ago "
            f"(interval {_SHADOW_CHECK_INTERVAL_DAYS} d).")
        return None

    # It shares the consolidate card and log, so say plainly what this run is:
    # on 2026-09-03 the admin read it as "consolidation fired twice".
    reporter.log("Weekly shadow verification — a read-only check that the incremental "
                 "artifacts match a full rebuild. Not a consolidation; nothing is written "
                 "unless a mismatch is found. Takes ~13 min.")
    _progress(5, "Shadow-verifying incremental consolidation…")
    result = verify_consolidation_equivalence(progress_cb=_progress)
    reporter.emit_data({"shadow_check": result})

    if result.get("ok"):
        reporter.log("Shadow verification OK — incremental artifacts match a full rebuild.")
        return None

    reporter.log(f"Shadow verification found divergence: {result.get('mismatches')}")
    try:
        from web_interface import task_failures
        task_failures.record_failure(
            task="consolidate_enrichment",
            error=f"[SHADOW] incremental consolidation diverged from a full rebuild: "
                  f"{result.get('mismatches')}",
            status_key="consolidate_enrichment",
            retry_count=0,
            disposition=task_failures.DISPOSITION_DEAD,
            task_args={"verify_consolidation": True},
            phase="verify",
        )
    except Exception as exc:
        reporter.log(f"Could not record the divergence in the failure ledger: {exc}")

    reporter.log("Promoting a full rebuild over the divergent incremental artifacts…")
    promote = consolidate_enrichment_data(
        force_consolidation=True, verbose=False, progress_cb=_progress)
    impact = promote.get("impact") if promote else None
    if impact:
        try:
            from web_interface.services import downstream_refresh
            downstream_refresh.accumulate_deferred_impact(impact)
            reporter.log("Healed-data impact queued for the next full downstream refresh.")
        except Exception as exc:
            reporter.log(f"Could not record the healed-data impact: {exc}")
    reporter.log("Full rebuild promoted. Investigate the divergence before re-enabling "
                 "incremental consolidation if it recurs.")
    return None


def _maybe_schedule_shadow_check(reporter: TaskStatusReporter, incremental: bool) -> None:
    """Dispatch the weekly shadow verification when it is due. Never raises.

    Fired from the tail of a normal consolidation: the artifacts were just
    written, the runner is warm, and the shared status key keeps the next real
    consolidation gated while the check runs. Only meaningful while the
    incremental paths are enabled.
    """
    if not incremental:
        return
    try:
        age = _shadow_check_age_days()
        if age is not None and age < _SHADOW_CHECK_INTERVAL_DAYS:
            return
        from web_interface.process_manager import (
            _dispatch_cloud_task,
            dispatch_deadline_for,
            is_cloud_run,
        )
        if not is_cloud_run():
            reporter.log("Shadow verification is due — run consolidate_enrichment "
                         "with --verify-consolidation (local mode does not self-schedule).")
            return
        success, msg = _dispatch_cloud_task(
            "consolidate_enrichment", {"verify_consolidation": True},
            dispatch_deadline_seconds=dispatch_deadline_for("consolidate_enrichment", {}))
        reporter.log(f"Weekly shadow verification dispatched: {msg}" if success else
                     f"Weekly shadow verification failed to dispatch: {msg}")
    except Exception as exc:
        reporter.log(f"Shadow-check scheduling skipped (consolidation unaffected): {exc}")


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

    if task_args.get("verify_consolidation"):
        return _run_shadow_verification(reporter)

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

    # The incremental kill switch lives in admin settings (runtime-flippable,
    # no redeploy) and is threaded down as a plain parameter — the fyp library
    # must not import web_interface.
    try:
        from web_interface.admin_settings import get_setting
        incremental = bool(get_setting("incremental_consolidation"))
    except Exception as exc:
        reporter.log(f"Could not read the incremental-consolidation setting (using full rebuild): {exc}")
        incremental = False

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
        incremental=incremental,
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

    # Automatic enrichment: this consolidation is what makes the last batch's
    # scrape/annotation outcomes visible, so it is also the moment the loop can
    # take its next step. The tick is NOT fired here: last_consolidation only
    # reaches process_stats.json after this function returns (in
    # _run_task_with_stats), and a tick dispatched before that read a stale
    # timestamp and re-ran a full no-op consolidation (~5 min wasted, observed
    # most cycles in prod). The task runner ticks once, after the stats save —
    # see the consolidate_enrichment branch in process_routes.

    _maybe_schedule_shadow_check(reporter, incremental)

    # ---- Publish the impact. What depends on this consolidation, and whether
    # each of those steps has anything to do, is decided by the refresh pipeline
    # from here on. Always write a last_pipeline_summary so the card has a
    # definitive statement of the outcome (it persists alongside "Last
    # consolidation {date}" across page reloads and subsequent polls).
    if not auto_refresh:
        # No downstream refresh runs. Record the debt: the impact is folded
        # into the deferred-refresh ledger entry so the enrichment supervisor's
        # finalize (or the next full refresh) covers it — this is what lets
        # mid-plan consolidations stay cheap without ever losing scope.
        summary = "Downstream refreshes were skipped."
        if impact:
            try:
                from web_interface.services import downstream_refresh
                downstream_refresh.accumulate_deferred_impact(impact)
                summary = ("Downstream refreshes deferred — the impact is "
                           "queued for the next full refresh.")
            except Exception as exc:
                reporter.log(f"Could not record the deferred impact: {exc}")
        reporter.emit_data({
            "last_pipeline_summary": summary,
            "last_pipeline_summary_ts": now_iso,
            "pipeline_impact": None,
            "last_pipeline_partial": False,
            "last_pipeline_failed_at": None,
        })
        return None

    # A full refresh covers any deferred debt too: widen the scope to the
    # union, and settle the ledger entry now that the run will carry it.
    try:
        from web_interface.services import downstream_refresh
        effective_impact = downstream_refresh.impact_union(
            downstream_refresh.get_deferred_impact(), impact)
    except Exception as exc:
        reporter.log(f"Could not read the deferred impact: {exc}")
        downstream_refresh = None
        effective_impact = impact

    # This is the whole hand-off now. What depends on this consolidation, and
    # whether each of those steps has anything to do, is decided step by step by
    # the refresh pipeline from the signals each finished step reports — see
    # web_interface/services/refresh_pipeline. Publishing the scope is all this
    # worker owes it.
    reporter.emit_data({
        "last_pipeline_summary": "Refresh run in progress — refreshing caches...",
        "last_pipeline_summary_ts": now_iso,
        "pipeline_impact": effective_impact or None,
        "last_pipeline_partial": False,
        "last_pipeline_failed_at": None,
    })

    if effective_impact and downstream_refresh is not None:
        try:
            downstream_refresh.settle_deferred_impact()
        except Exception as exc:
            reporter.log(f"Could not settle the deferred impact: {exc}")

    studies = (effective_impact or {}).get("affected_study_names") or []
    collections = (effective_impact or {}).get("affected_collection_ids") or []
    reporter.log(
        f"Consolidation impact: {len(studies)} study(ies), "
        f"{len(collections)} collection(s), "
        f"{(effective_impact or {}).get('new_annotation_item_count') or 0} new "
        f"annotation item(s). The refresh run continues from here."
    )
    return None




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    # The dependent refreshes are dispatched by the refresh pipeline once this
    # worker finishes — process_routes on Cloud Run, monitor_process_completion
    # in local dev. This script only consolidates.
    run_worker(
        run_consolidate_enrichment,
        "consolidate_enrichment",
        arg_specs=[
            (('--force-consolidation',), {'action': 'store_true',
                                          'help': 'Re-consolidate even if no new files detected.'}),
            (('--auto-refresh',), {'action': 'store_true',
                                   'help': 'After consolidation, record the impact so the '
                                           'web service can dispatch downstream refreshes.'}),
            (('--verify-consolidation',), {'action': 'store_true',
                                           'help': 'Shadow-verify the incremental artifacts '
                                                   'against a full rebuild instead of consolidating.'}),
        ],
        make_task_args=lambda args: {
            "force_consolidation": bool(args.force_consolidation),
            "auto_refresh": bool(args.auto_refresh),
            "verify_consolidation": bool(args.verify_consolidation),
        },
        description="Consolidate enrichment data",
    )
