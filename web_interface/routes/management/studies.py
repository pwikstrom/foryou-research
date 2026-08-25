"""Study definition / stats / preview endpoints (/api/manage/studies*)."""

from datetime import UTC, datetime

import pandas as pd
from flask import jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
)
import fyp.annotation_versioning as annotation_versioning
from fyp.studies import init_study_defs, save_study_defs

from ... import activity_log
from ...data_service import (
    study_cache,
)
from ...process_manager import (
    start_process,
)
from ...permissions import permission_required
from ...task_status import is_cloud_run



from ...services.preview_cache import (
    _collections_hash,
    get_preview_cells,
)
from ...services.stats_service import (
    _cells_for_selection,
    _derive_study_issues,
    _estimate_from_cells,
    _universe_from_cells,
    get_study_activity_cap,
)
from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


def _is_study_manager() -> bool:
    """Study managers see every study regardless of per-study USER_ACCESS."""
    from web_interface.permissions import user_has_permission
    return (
        current_user.is_admin()
        or user_has_permission(current_user, 'tab.data_management.studies')
    )


def _user_can_see_study(config: dict) -> bool:
    """True when the current user may read ``config``'s definition.

    Managers see everything; everyone else needs their role or username in the
    study's ``USER_ACCESS`` (or the wildcard ``"all"``).
    """
    if _is_study_manager():
        return True
    user_access = config.get("USER_ACCESS", [])
    return isinstance(user_access, list) and (
        current_user.role in user_access
        or current_user.username in user_access
        or 'all' in user_access
    )


@management_bp.route('/api/manage/studies', methods=['GET'])
@login_required
@permission_required('tab.data_management.studies', 'tab.my_stuff.my_studies')
def list_studies():
    # Always reload from disk/GCS to pick up changes made by the task-runner service
    init_study_defs()

    studies = fyp_cf['study_defs']

    studies_list = []

    # Visibility:
    #   - Admin / users with the Define Studies sub-page permission are "study
    #     managers" — they see every study regardless of per-study USER_ACCESS.
    #   - Users who only have the My Studies tab see the curated subset: studies
    #     where their role appears in USER_ACCESS (or USER_ACCESS contains "all").
    is_manager = _is_study_manager()

    for name, config in studies.items():
        config['STUDY_NAME'] = name

        if is_manager:
            studies_list.append(config)
        elif _user_can_see_study(config):
            # The My Studies read-only view renders from this payload, so it
            # ships the whole definition. USER_ACCESS is the one key that
            # says something about other users rather than about the study.
            shared = {k: v for k, v in config.items() if k != "USER_ACCESS"}
            studies_list.append(shared)

    return jsonify(studies_list)


@management_bp.route('/api/manage/studies/<study>/set_viz', methods=['GET'])
@login_required
@permission_required('tab.data_management.studies', 'tab.my_stuff.my_studies')
def study_set_viz(study):
    """Enrichment mosaic for ONE saved study, for the read-only My Studies modal.

    The editable modal gets this from /calculate_stats, which is Data-Management
    only and takes a client-supplied definition. Here the definition comes from
    the saved study, so a viewer can only ever ask about a study they can already
    see — no arbitrary collection selections. Used when the study's persisted
    ``stats.universe`` is missing (saved before it was recorded, or the refresh
    skipped it), which would otherwise leave the mosaic on a spinner forever.
    """
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    config = (fyp_cf.get('study_defs') or {}).get(study)
    if config is None:
        return jsonify({"error": "Unknown study"}), 404
    if not _user_can_see_study(config):
        return jsonify({"error": "Not authorised for this study"}), 403

    study_config = dict(config)
    study_config["STUDY_NAME"] = study

    try:
        cells, coll_stats = get_preview_cells()
        stats, _included_per_day, _sparse, _total, _report = _estimate_from_cells(
            cells, coll_stats, study_config)
        _pot_activities, _pot_days, universe, _has_days = _universe_from_cells(cells, study_config)
        return jsonify({
            "status": "success",
            "stats": stats,
            "universe": universe,
            "frame": study_config.get("SAMPLE_FRAME"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500







@management_bp.route('/api/manage/studies/save', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def save_study():
    global fyp_cf

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    
    # load studies from disk into memory - overwrite whatever was there previously
    init_study_defs()
    studies = fyp_cf['study_defs'].copy()

    # If updating an existing study, check for actual changes
    if study_name in studies:
        existing_config = studies[study_name]

        # Compare incoming data with existing config
        changed_keys = []
        for key, value in data.copy().items(): # Use copy to safely iterate
            # key might be REFRESH_PCA/REFRESH_METADATA - these shouldn't count as study def changes but separate flags.
            if key in ['REFRESH_PCA', 'REFRESH_METADATA', 'stats']:
                continue

            if key not in existing_config or existing_config[key] != value:
                changed_keys.append(key)
                #print(f"Change detected in {key}: {existing_config.get(key)} -> {value}") # Debug

        # Note: we deliberately do NOT short-circuit when changed_keys is empty.
        # The study's cached artifacts depend on collection/scrape/annotation
        # parquets that can change out-of-band (e.g. new activities ingested
        # into an existing collection_id). run_study_refresh fingerprints those
        # inputs via the sidecar and short-circuits itself when they really are
        # unchanged, so an unnecessary save here becomes a cheap no-op — and a
        # necessary one actually rebuilds.

        # If only USER_ACCESS changed, we don't need to recalculate anything
        if len(changed_keys) == 1 and changed_keys[0] == 'USER_ACCESS':
            print(f"Only USER_ACCESS changed for {study_name}. Saving without recalculation.")
            studies[study_name]['USER_ACCESS'] = data['USER_ACCESS']
            fyp_cf['study_defs'] = studies
            save_study_defs()
            activity_log.record(
                actor=_actor(),
                category=activity_log.CATEGORY_DATA_MANAGEMENT,
                action="study.save",
                target=study_name,
                details={"changed": ["USER_ACCESS"]},
            )
            return jsonify({"status": "success", "study": studies[study_name]})

    # A study must explicitly enumerate its collections: an empty list would
    # silently select EVERY collection at recode time (organize_datasets only
    # appends the collection_id filter when the list is non-empty), so a study
    # could silently sweep in every collection in the corpus.
    effective_collections = data.get(
        'SELECTED_COLLECTIONS',
        studies.get(study_name, {}).get('SELECTED_COLLECTIONS'),
    )
    if not isinstance(effective_collections, list) or not effective_collections:
        return jsonify({"error": "A study must explicitly list its collections"}), 400

    # Hard cap: refuse to persist a definition whose projected size exceeds
    # [studies] max_activities. Only checked when a row-shaping field changed
    # (or the study is new), so an existing over-cap study can still be
    # re-permissioned or saved untouched while its data grows out-of-band.
    _SHAPING_KEYS = (
        "SELECTED_COLLECTIONS", "START_DATE", "END_DATE", "SAMPLE_FRAME",
        "MIN_ACTIVITY_COUNT_PER_GROUP", "MAX_ACTIVITY_COUNT_PER_GROUP",
        "MIN_GROUP_COUNT_PER_COLLECTION", "MAX_GROUP_COUNT_PER_COLLECTION",
    )
    existing_def = studies.get(study_name, {})

    def _shaping_differs(key):
        if key not in data:
            return False
        new_v, old_v = data.get(key), existing_def.get(key)
        if key == "SELECTED_COLLECTIONS" and isinstance(new_v, list) and isinstance(old_v, list):
            return sorted(map(str, new_v)) != sorted(map(str, old_v))
        return new_v != old_v

    shaping_changed = study_name not in studies or any(map(_shaping_differs, _SHAPING_KEYS))
    if shaping_changed:
        try:
            effective_def = {**existing_def, **data}
            cells, coll_stats = get_preview_cells()
            est_stats, *_rest = _estimate_from_cells(cells, coll_stats, effective_def)
            cap_limit = get_study_activity_cap()
            projected = int(est_stats.get("total_activities", 0))
            if projected > cap_limit:
                return jsonify({
                    "error": (
                        f"This study would contain ~{projected:,} activities; the cap is "
                        f"{cap_limit:,}. Narrow the date range, drop collections, or enable "
                        f"sampling before saving."
                    ),
                    "cap": {"limit": cap_limit, "projected": projected, "exceeded": True},
                }), 400
        except Exception as e:
            # The refusal must never be triggered by an estimator failure; the
            # refresh pipeline remains the backstop for genuinely huge builds.
            print(f"[save_study] cap check skipped (estimator failed): {e}")

    # Update config
    if study_name not in studies:
        studies[study_name] = {}

    # Preserve existing stats and last_updated — form data doesn't include these
    # and the Cloud Task will recalculate them asynchronously.
    existing_stats = studies[study_name].get('stats')
    existing_last_updated = studies[study_name].get('last_updated')

    studies[study_name].update(data)

    if existing_stats and 'stats' not in data:
        studies[study_name]['stats'] = existing_stats
    if existing_last_updated and 'last_updated' not in data:
        studies[study_name]['last_updated'] = existing_last_updated
    
    # Extract ephemeral flags (don't save to disk)
    refresh_pca_flag = data.pop('REFRESH_PCA', True)
    refresh_meta_flag = data.pop('REFRESH_METADATA', True)
    
    # Also clean them from the study object in memory just in case 'update' put them there
    # (Since 'data' was passed to update, they ARE in studies[study_name] now)
    studies[study_name].pop('REFRESH_PCA', None)
    studies[study_name].pop('REFRESH_METADATA', None)

    # Invalidate the cached daily-activities snapshot — the saved definition
    # may now have different collections, so the cache would mislead the
    # modal on reopen until a fresh /daily_activities call refreshes it.
    studies[study_name].pop('cached_daily_activities', None)

    # Update timestamp and save definition to disk
    studies[study_name]['last_updated'] = datetime.now(UTC).isoformat()
    fyp_cf['study_defs'] = studies
    save_study_defs()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="study.save",
        target=study_name,
        details={"definition_only": bool(data.get("definition_only"))},
    )

    # Definition-only save: skip heavy refresh (used by "Check data counts" for new studies)
    if data.get("definition_only"):
        return jsonify({"status": "success", "study": studies[study_name]})

    # --- Dispatch heavy refresh work ---
    task_args = {
        "study_name": study_name,
        "refresh_pca": refresh_pca_flag,
        "refresh_metadata": refresh_meta_flag,
        # Follow the study refresh with an auto sessions refresh: a saved
        # window/collection change alters the coverage spec, so the chained
        # run rebuilds exactly the affected collections (and no-ops when
        # nothing changed). skip_if_busy keeps two study saves from killing
        # each other's sessions chains. Study DELETE is deliberately not
        # chained — the next auto run drops the departed collections' rows.
        "pipeline_remaining": [{
            "task": "sessions_refresh",
            "task_args": {"stale_only": True, "skip_if_busy": True},
        }],
    }

    if is_cloud_run():
        # On Cloud Run: dispatch as a Cloud Task and return immediately
        success, msg = start_process("study_refresh", None, task_args=task_args,
                                     started_by=_actor())
        if success:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatched",
                "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
            })
        else:
            return jsonify({
                "status": "success",
                "study": studies[study_name],
                "refresh_status": "dispatch_failed",
                "message": f"Study saved, but background refresh failed to start: {msg}",
            })
    else:
        # Local dev: dispatch to a background thread so the HTTP response can
        # return immediately. The client's `_pollStudyRefresh` will watch the
        # in-process status via `/api/status/study_refresh/<name>`.
        import threading as _threading

        from web_interface.run_study_refresh import run_study_refresh
        from web_interface.task_status import LocalThreadStatusReporter

        status_key = f"study_refresh__{study_name}"
        reporter = LocalThreadStatusReporter(status_key)
        # Captured here — _actor() reads the request context, which is gone
        # by the time the thread's follow-on dispatch runs.
        _actor_name = _actor()

        def _run_in_thread():
            try:
                run_study_refresh(reporter=reporter, task_args=task_args)
                reporter.complete()
            except Exception as e:
                print(f"Study refresh failed: {e}")
                reporter.fail(str(e))
                return
            # Local-dev counterpart of the Cloud path's pipeline_remaining:
            # the subprocess mode has no pipeline advance, so start the
            # follow-on sessions refresh here. Failure only logs — the study
            # refresh itself already succeeded.
            try:
                from fyp.fyp_config import SESSIONS_REFRESH_SCRIPT
                ok, start_msg = start_process(
                    "sessions_refresh", SESSIONS_REFRESH_SCRIPT,
                    task_args={"stale_only": True, "skip_if_busy": True},
                    started_by=_actor_name)
                if not ok:
                    print(f"Sessions auto-refresh not started: {start_msg}")
            except Exception as e:
                print(f"Sessions auto-refresh dispatch failed: {e}")

        _threading.Thread(target=_run_in_thread, daemon=True, name=status_key).start()

        return jsonify({
            "status": "success",
            "study": studies[study_name],
            "refresh_status": "dispatched",
            "message": "Study saved. Stats, PCA, and metadata refresh running in background.",
        })






@management_bp.route('/api/manage/studies/calculate_stats', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def calculate_study_stats():
    """
    On-demand calculation of stats for a study definition (without saving).
    """
    global fyp_cf
    
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
        

    study_name = data.get("STUDY_NAME")
    if not study_name:
         return jsonify({"error": "Missing STUDY_NAME"}), 400
         
    if fyp_cf.get('study_defs', None) is None:
        init_study_defs()

    # Live previews (auto-update on every slider/date tweak) pass PREVIEW_ONLY so we
    # compute-and-return without persisting: no global-config mutation (which is not
    # thread-safe under rapid concurrent calls) and no save_study_defs() write per
    # tweak. The saved stats are refreshed on an explicit Save (run_study_refresh).
    preview_only = bool(data.get("PREVIEW_ONLY"))

    # 1. Backup + install config only when we intend to persist.
    original_config = None
    if not preview_only:
        if 'study_defs' in fyp_cf and study_name in fyp_cf['study_defs']:
            original_config = fyp_cf['study_defs'][study_name].copy()
        # If this is a new study (not in defs), we add it. If existing, we overwrite.
        fyp_cf['study_defs'][study_name] = data

    stats_to_persist: dict | None = None
    try:
        # Fast preview path: approximate the sampling counts analytically instead of
        # running the full create_study_recoded_dataset (scrape/annotation merge +
        # random sample). The persisted study build still uses _calculate_stats via
        # run_study_refresh; only this on-demand check uses the heuristic.
        #
        # All numbers come from the corpus-level preview cells (one row per
        # collection-day, built once per source change), so every selection /
        # window / sampling tweak is a filter + arithmetic over a tiny table —
        # no per-selection frame rebuild.
        selected = data.get("SELECTED_COLLECTIONS") or []
        cells, coll_stats = get_preview_cells()

        stats, included_per_day, sparse_cells, total_cells, sampling_report = _estimate_from_cells(cells, coll_stats, data)
        stats_to_persist = stats

        # Pre-sampling potentials + universe mosaic, both derived from the same cells.
        #   items.potential     = activities in study (how many activities map to items)
        #   scraped.potential    = items in study
        #   annotated.potential  = scraped items in study
        pot_activities, pot_active_days, universe, has_total_days = _universe_from_cells(cells, data)
        potentials = {
            "collections": len(selected),
            "activities": pot_activities,
            "active_days": pot_active_days,
            "items": int(stats.get("total_activities", 0)),
            "scraped": int(stats.get("unique_videos", 0)),
            "annotated": int(stats.get("scraped_videos", 0)),
        }

        if isinstance(stats, dict):
            stats["universe"] = universe

        issues = _derive_study_issues(stats, sparse_cells, total_cells, has_total_days, sampling_report)

        cap_limit = get_study_activity_cap()
        projected = int(stats.get("total_activities", 0))
        return jsonify({
            "status": "success",
            "stats": stats,
            "potentials": potentials,
            "universe": universe,
            "included_per_day": included_per_day,
            "issues": issues,
            "cap": {
                "limit": cap_limit,
                "projected": projected,
                "exceeded": projected > cap_limit,
            },
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 4. Revert config + persist fresh stats (skipped entirely for live previews,
        # which never touched the global config). Persisting seeds the mosaic on reopen.
        if not preview_only:
            if original_config is not None:
                 if stats_to_persist:
                      original_config['stats'] = stats_to_persist
                 fyp_cf['study_defs'][study_name] = original_config
                 if stats_to_persist:
                      try:
                           save_study_defs()
                      except Exception as _save_err:
                           print(f"[calculate_study_stats] non-fatal: failed to persist stats for '{study_name}': {_save_err}")
            else:
                 # If it was new, remove it (it wasn't there before).
                 if study_name in fyp_cf['study_defs']:
                      del fyp_cf['study_defs'][study_name]




@management_bp.route('/api/manage/studies/prewarm_check', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def prewarm_study_check():
    """Warm the corpus preview cells ahead of the first estimate.

    The modal fires this (without awaiting it) on open and on collection changes.
    It BLOCKS server-side until the cells are warm: on Cloud Run, CPU is throttled
    to ~zero once a request returns, so a background thread would never finish —
    holding the request is the only way a warm-up actually runs (the same
    wait-with-request-CPU pattern as explore's wait_for_frame). Concurrent calls
    coalesce on the cells build lock. Warm calls return in ~ms.
    """

    cells, _coll = get_preview_cells()
    if cells is None:
        return jsonify({"status": "noop"}), 200
    return jsonify({"status": "ready"}), 200





@management_bp.route('/api/manage/studies/daily_activities', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def daily_activities():
    """Return activities-per-day across a set of collections for the modal chart.

    Served from the corpus preview cells (in-event-window play/observe counts
    per collection-day), so a warm call is pure arithmetic — no parquet scan.
    No date-range filter — the chart shows the full span so the user can pick
    a window visually.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    study_name = data.get("STUDY_NAME")

    if not selected:
        return jsonify({"status": "success", "total_per_day": []})

    try:
        cells, _coll = get_preview_cells()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Per-day in-event-window play/observe counts straight off the corpus cells
    # (the frame-based path read a 20+ MB timestamp column for the same numbers).
    df = _cells_for_selection(cells, {"SELECTED_COLLECTIONS": selected})
    potentials = {
        "collections": len(selected),
        "activities": 0,
        "active_days": 0,
    }
    total_per_day: list[dict] = []
    if df is not None:
        win = df[df["n_act_inwin"].to_numpy() > 0]
        if not win.empty:
            potentials["activities"] = int(win["n_act_inwin"].sum())
            potentials["active_days"] = int(win["day"].nunique())
            day_counts = win.groupby("day")["n_act_inwin"].sum().sort_index()
            total_per_day = [
                {"date": pd.Timestamp(d).date().isoformat(), "count": int(c)}
                for d, c in day_counts.items()
            ]
    collections_hash = _collections_hash(selected)

    # Cache on the saved study so subsequent modal opens render the chart
    # instantly. Only persist when the incoming selection matches the saved
    # SELECTED_COLLECTIONS — otherwise the user is editing an unsaved state
    # and caching that would mislead on reopen.
    if study_name:
        saved = fyp_cf.get('study_defs', {}).get(study_name)
        if isinstance(saved, dict):
            saved_hash = _collections_hash(saved.get('SELECTED_COLLECTIONS'))
            if saved_hash == collections_hash:
                saved['cached_daily_activities'] = {
                    "total_per_day": total_per_day,
                    "potentials": potentials,
                    "collections_hash": collections_hash,
                    "computed_at": datetime.now(UTC).isoformat(),
                }
                try:
                    save_study_defs()
                except Exception as _save_err:
                    print(f"[daily_activities] non-fatal: failed to persist cache for '{study_name}': {_save_err}")

    return jsonify({
        "status": "success",
        "total_per_day": total_per_day,
        "potentials": potentials,
        "collections_hash": collections_hash,
    })




# Per-study cached artifacts, all in the "cache" location. Keep in sync with
# delete_study and the run_* workers that write them (study/recode refresh,
# pca refresh, meta refresh, sequence refresh, methods note).
_STUDY_ARTIFACT_SUFFIXES = [
    "_recoded.parquet",
    "_recoded.meta.json",
    "_explorer_metadata.json",
    "_comp_interpretations.json",
    "_PCA.parquet",
    "_corr_stats.json",
    "_methods.json",
    "_sequence.parquet",
    "_sequence_summary.json",
]




@management_bp.route('/api/manage/studies/rename', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def rename_study():
    """Rename a study: move its definition key and carry its cached artifacts over.

    The artifacts are renamed in place (local move / GCS blob rename), so a
    rename needs no dataset rebuild. A missing artifact is fine — e.g. a study
    saved definition-only, or one whose sequence/correlation refresh never ran.
    """
    data = request.json or {}
    old_name = (data.get("OLD_NAME") or "").strip()
    new_name = (data.get("NEW_NAME") or "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "Missing OLD_NAME or NEW_NAME"}), 400
    if new_name == old_name:
        return jsonify({"error": "The new name is the same as the old name"}), 400

    init_study_defs()
    studies = fyp_cf.get('study_defs', {})
    if old_name not in studies:
        return jsonify({"error": f"Study not found: {old_name}"}), 404
    if new_name in studies:
        return jsonify({"error": f"A study named '{new_name}' already exists"}), 400

    # Rebuild the dict so the renamed study keeps its position in the listing.
    fyp_cf['study_defs'] = {
        (new_name if name == old_name else name): config
        for name, config in studies.items()
    }
    save_study_defs()

    moved = []
    for suffix in _STUDY_ARTIFACT_SUFFIXES:
        try:
            if data_io.rename(storage_location="cache",
                              src_filename=f"{old_name}{suffix}",
                              dst_filename=f"{new_name}{suffix}"):
                moved.append(suffix)
        except Exception as e:
            print(f"[rename_study] non-fatal: could not rename {old_name}{suffix}: {e}")

    study_cache.invalidate(old_name)

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="study.rename",
        target=old_name,
        details={"new_name": new_name, "artifacts_moved": moved},
    )

    return jsonify({"status": "success", "old_name": old_name, "new_name": new_name})




@management_bp.route('/api/manage/studies/delete', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def delete_study():
    global fyp_cf
    data = request.json
    study_name = data.get("STUDY_NAME")
    if not study_name:
        return jsonify({"error": "Missing STUDY_NAME"}), 400
        
    init_study_defs()
    if 'study_defs' not in fyp_cf:
        return jsonify({"error": "No study defs found"}), 404
        
    if study_name in fyp_cf['study_defs']:
        del fyp_cf['study_defs'][study_name]
        save_study_defs()

        for suffix in _STUDY_ARTIFACT_SUFFIXES:
            data_io.remove(storage_location="cache", filename=f"{study_name}{suffix}")

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="study.delete",
            target=study_name,
        )
        return jsonify({"status": "success", "message": f"Deleted {study_name}"})
    else:
        return jsonify({"error": "Study not found"}), 404




@management_bp.route('/api/manage/studies/<study>/annotation-version', methods=['POST'])
@permission_required('tab.admin.versions')
@login_required
def set_study_annotation_version(study):
    """Pin (or clear) a study's annotation_version for reproducibility.

    A pinned study merges against that version's annotations (read strictly from
    the archive) instead of the active dataset. Pass a falsy ``version`` to clear
    the pin. The study must be refreshed to rebuild its dataset.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        version = body.get("version")  # falsy clears the pin
        if "study_defs" not in fyp_cf:
            init_study_defs()
        if study not in fyp_cf["study_defs"]:
            return jsonify({"error": f"unknown study: {study}"}), 404
        if version:
            registry = annotation_versioning.load_registry()
            if version not in registry.get("versions", {}):
                return jsonify({"error": f"unknown version: {version}"}), 404
            fyp_cf["study_defs"][study]["annotation_version"] = version
        else:
            fyp_cf["study_defs"][study].pop("annotation_version", None)
        save_study_defs()
        study_cache.invalidate(study)
        activity_log.record(
            actor=_actor(),
            category="admin",
            action="study.pin_annotation_version",
            details={"study": study, "version": version or None},
        )
        return jsonify({
            "ok": True,
            "study": study,
            "annotation_version": version or None,
            "note": "Refresh the study to rebuild its dataset against this version.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



