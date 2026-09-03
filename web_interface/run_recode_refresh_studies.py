import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_recode_refresh_studies(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh recoded datasets and stats for studies."""
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.organize_datasets import create_study_recoded_dataset, enrichment_preload
    from fyp.studies import init_study_defs, save_study_defs
    from web_interface.services.methods_note import write_methods_note
    from web_interface.services.stats_service import compute_study_dataset_stats

    task_args = task_args or {}
    force_full_rebuild: bool = bool(task_args.get("force_full_rebuild", False))
    reporter.log("Starting Study Definitions (Recoded Data) Refresh...")
    if force_full_rebuild:
        reporter.log("force_full_rebuild=True — sidecars will be removed before each study.")
    _t_run_start = time.perf_counter()

    # Init studies
    init_study_defs()
    studies = fyp_cf.get('study_defs', {})

    # Filter to targeted studies if specified
    target_studies_str = task_args.get("studies")
    if target_studies_str:
        target_names = [s.strip() for s in target_studies_str.split(',')]
        studies = {k: v for k, v in studies.items() if k in target_names}
        reporter.log(f"Targeted refresh for {len(studies)} study/studies: {', '.join(studies.keys())}")

    # System-managed participant studies refresh only when explicitly targeted
    # (their owner's collections changed, or a consolidation impact named
    # them) — a full sweep must stay O(regular studies), not O(participants).
    # Composed ("Everyone & Me") defs store no artifacts and never run here.
    from fyp.studies import is_composed_study, is_system_study
    _skipped_system = sorted(
        k for k, v in studies.items()
        if is_composed_study(v) or (is_system_study(v) and not target_studies_str)
    )
    if _skipped_system:
        studies = {k: v for k, v in studies.items() if k not in _skipped_system}
        reporter.log(f"Skipping {len(_skipped_system)} system-managed study/studies.")

    total = len(studies)
    if total == 0:
        reporter.log("No studies found to refresh.")
        # An explicit empty list, not a missing signal: nothing was rebuilt, so
        # the metadata and correlation refreshes that read these datasets have
        # nothing to do either.
        reporter.emit_data({"studies_changed": [], "studies_unchanged": [],
                            "studies_failed": []})
        return

    # Which studies this run actually rebuilt. create_study_recoded_dataset
    # decides per study by fingerprint, so a run over 40 studies can legitimately
    # rewrite none of them; the pipeline uses these lists to scope (or skip) the
    # explore-metadata and correlations refreshes that follow.
    studies_changed: list[str] = []
    studies_unchanged: list[str] = []
    studies_failed: list[str] = []

    # Pre-load enrichment status once for all studies
    _t_phase = time.perf_counter()
    df_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')
    if df_status is not None and not df_status.empty:
        if 'item_id' not in df_status.columns and df_status.index.name == 'item_id':
            df_status = df_status.reset_index()
    _t_status_load = time.perf_counter() - _t_phase
    reporter.log(f"[TIMING] enrichment_status load={_t_status_load:.2f}s")

    # The two enrichment blobs (scrapes 327 MB, annotations 479 MB) are loaded
    # once for the whole run and filtered per study, instead of once per study
    # — see enrichment_preload. Released when the block exits.
    with enrichment_preload():
        for i, (study_name, config) in enumerate(studies.items()):
            if reporter.check_cancelled():
                reporter.log("Cancelled by user.")
                break
            reporter.update_progress(int((i / total) * 100), f"Study {i + 1}/{total}: {study_name}")
            reporter.log(f"Processing study: {study_name}")
            _t_study_start = time.perf_counter()

            try:
                if force_full_rebuild:
                    sidecar_fn = f"{study_name}_recoded.meta.json"
                    if data_io.exists(storage_location="cache", filename=sidecar_fn):
                        data_io.remove(storage_location="cache", filename=sidecar_fn)

                df_study = create_study_recoded_dataset(
                    study_name=study_name,
                    save_to_cache=True,
                    enrichment_status=df_status,
                    force_full_rebuild=force_full_rebuild,
                    verbose=False,
                )

                if df_study is None:
                    reporter.log(f"Skipping {study_name}: No data generated.")
                    studies_unchanged.append(study_name)
                    studies[study_name]['stats'] = {
                        "total_activities": 0,
                        "unique_videos": 0,
                        "scraped_videos": 0,
                        "annotated_videos": 0,
                        "activities_scraped": 0,
                        "activities_annotated": 0,
                        "unique_collections": 0,
                        "active_days": 0,
                    }
                else:
                    refresh_action = df_study.attrs.get("refresh_action", "full_rebuild")
                    if refresh_action == "short_circuit":
                        studies_unchanged.append(study_name)
                        reporter.log(f"  Short-circuit for {study_name}: cached parquet reused ({len(df_study)} rows)")
                    elif refresh_action == "enrichment_patch":
                        studies_changed.append(study_name)
                        reporter.log(f"  Enrichment patch for {study_name}: re-merged enrichment onto cached activity ({len(df_study)} rows)")
                    else:
                        studies_changed.append(study_name)
                        reporter.log(f"  Successfully refreshed data for {study_name} ({len(df_study)} rows)")

                    # Same stats definition (and full key set, incl. total_activities)
                    # as the single-study refresh — see compute_study_dataset_stats.
                    selected = config.get("SELECTED_COLLECTIONS") or []
                    studies[study_name]['stats'] = compute_study_dataset_stats(
                        df_study, df_status, selected)
                    studies[study_name]['last_updated'] = datetime.now(UTC).isoformat()

                    # Methods/provenance note — written on every refresh, even a
                    # short-circuit, so it tracks registry moves (e.g. a newly
                    # preferred annotation version) that don't rebuild the parquet.
                    write_methods_note(
                        study_name=study_name,
                        study_config=studies[study_name],
                        df_study=df_study,
                        df_status=df_status,
                        stats=studies[study_name]['stats'],
                        refresh_action=refresh_action,
                        refresh_trigger="pipeline",
                    )

            except Exception as e:
                studies_failed.append(study_name)
                reporter.log(f"Error processing {study_name}: {e}")

            _t_study = time.perf_counter() - _t_study_start
            reporter.log(f"  [TIMING] study={study_name} total={_t_study:.2f}s")
            # Same message as the emit above: advances the bar without adding a
            # second, content-free line to the run log (the reporter dedupes
            # consecutive identical progress messages).
            reporter.update_progress(int(((i + 1) / total) * 100),
                                     f"Study {i + 1}/{total}: {study_name}")

    # Persist updated stats to studies.json
    # Merge updated studies back into the full study_defs to avoid clobbering
    # non-targeted studies when a filtered refresh is run.
    for sn, sc in studies.items():
        fyp_cf['study_defs'][sn] = sc
    save_study_defs()
    reporter.log("Stats saved to studies.json.")
    _t_run = time.perf_counter() - _t_run_start
    # Safety net for the auto-managed participant pairs the sweep above
    # deliberately skipped: reconcile every pair against the ownership store
    # and rebuild only those whose collection set drifted. Untargeted runs
    # only — targeted pipeline runs already name the studies they touched.
    if not target_studies_str:
        try:
            from web_interface.services.participant_studies import (
                refresh_stale_participant_studies,
            )

            refresh_stale_participant_studies(wait=True, log=reporter.log)
        except Exception as exc:
            reporter.log(f"Participant-study reconciliation failed (sweep unaffected): {exc}")

    reporter.emit_data({"studies_changed": studies_changed,
                        "studies_unchanged": studies_unchanged,
                        "studies_failed": studies_failed})
    reporter.log(
        f"Study datasets: {len(studies_changed)} rebuilt, "
        f"{len(studies_unchanged)} already current, {len(studies_failed)} failed."
    )
    reporter.log(f"[TIMING] recode_refresh_studies wall={_t_run:.2f}s studies={total}")
    reporter.log("Study Definitions (Recoded Data) refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args = {}
        if args.studies:
            task_args["studies"] = args.studies
        elif args.study_name:
            task_args["studies"] = args.study_name
        if args.force:
            task_args["force_full_rebuild"] = True
        return task_args

    run_worker(
        run_recode_refresh_studies,
        "recode_refresh_studies",
        arg_specs=[
            (('--studies',), {'type': str, 'default': None,
                              'help': 'Comma-separated study names to refresh (default: all)'}),
            (('--force',), {'action': 'store_true',
                            'help': 'Force full rebuild of every study, ignoring sidecar fingerprints'}),
            (('study_name',), {'nargs': '?', 'default': None,
                               'help': 'Single study to refresh; ignored when --studies is given'}),
        ],
        make_task_args=_make_task_args,
        description="Refresh recoded datasets and stats for studies",
    )
