"""Study definition / stats / preview endpoints (/api/manage/studies*)."""

import threading
import time as _time
from datetime import UTC, datetime

import pandas as pd
from flask import jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
)
import fyp.annotation_versioning as annotation_versioning
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
)
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
    _PREVIEW_CACHE_TTL_S,
    _collections_hash,
    _get_enrichment_status_cached,
    _get_prepared_frame_cached,
    _prewarm_preview_frame,
    _preview_cache_lock,
    _preview_frame_cache,
    _preview_frame_key,
    _preview_warming,
)
from ...services.stats_service import (
    _daily_counts,
    _derive_study_issues,
    _estimate_from_prepared,
    _filter_to_event_windows,
    _filter_to_play_observe,
    _load_collection_event_windows,
    _universe_from_prepared,
)
from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


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
    from web_interface.permissions import user_has_permission
    is_manager = (
        current_user.is_admin()
        or user_has_permission(current_user, 'tab.data_management.studies')
    )

    for name, config in studies.items():
        config['STUDY_NAME'] = name

        if is_manager:
            studies_list.append(config)
        else:
            user_access = config.get("USER_ACCESS", [])
            if isinstance(user_access, list) and (
                current_user.role in user_access or 'all' in user_access
            ):
                studies_list.append(config)

    return jsonify(studies_list)







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
    }

    if is_cloud_run():
        # On Cloud Run: dispatch as a Cloud Task and return immediately
        success, msg = start_process("study_refresh", None, task_args=task_args)
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

        def _run_in_thread():
            try:
                run_study_refresh(reporter=reporter, task_args=task_args)
                reporter.complete()
            except Exception as e:
                print(f"Study refresh failed: {e}")
                reporter.fail(str(e))

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
        # A preprocessed frame (keyed by the collection set) is cached in process, so the
        # tweak-and-recheck loop (changing only the date range or sampling thresholds)
        # reuses it with no I/O and no per-row recomputation — just masks and a groupby.
        selected = data.get("SELECTED_COLLECTIONS") or []
        df_status = _get_enrichment_status_cached()
        frame = _get_prepared_frame_cached(selected, df_status)

        stats, included_per_day, sparse_cells, total_cells, sampling_report = _estimate_from_prepared(frame, data)
        stats_to_persist = stats

        # Pre-sampling potentials + universe mosaic, both derived from the same frame.
        #   items.potential     = activities in study (how many activities map to items)
        #   scraped.potential    = items in study
        #   annotated.potential  = scraped items in study
        pot_activities, pot_active_days, universe, has_total_days = _universe_from_prepared(frame, data)
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

        return jsonify({
            "status": "success",
            "stats": stats,
            "potentials": potentials,
            "universe": universe,
            "included_per_day": included_per_day,
            "issues": issues,
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
    """Warm the preview frame for a collection set ahead of the first 'Check' press.

    The modal calls this on open and whenever the collection selection changes, so the
    (possibly slow) build / disk-load happens during the user's think-time instead of on
    the first button press. Returns immediately; the work runs in a background thread.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    if not selected:
        return jsonify({"status": "noop"}), 200

    key = _preview_frame_key(selected)
    with _preview_cache_lock:
        warm = _preview_frame_cache.get(key)
        ready = warm is not None and (_time.monotonic() - warm[0]) < _PREVIEW_CACHE_TTL_S
        in_flight = key in _preview_warming
    if ready:
        return jsonify({"status": "ready"}), 200
    if in_flight:
        return jsonify({"status": "warming"}), 202

    threading.Thread(target=_prewarm_preview_frame, args=(list(selected),), daemon=True, name="prewarm_check").start()
    return jsonify({"status": "warming"}), 202





@management_bp.route('/api/manage/studies/daily_activities', methods=['POST'])
@login_required
@permission_required('tab.data_management.studies')
def daily_activities():
    """Return activities-per-day across a set of collections for the modal chart.

    Lightweight: reads only `collection_id` + `local_timestamp` columns from
    `collections_recoded.parquet` with a pushdown filter on the selected IDs.
    No date-range filter — the chart shows the full span so the user can pick
    a window visually.
    """

    data = request.json or {}
    selected = data.get("SELECTED_COLLECTIONS") or []
    study_name = data.get("STUDY_NAME")

    if not selected:
        return jsonify({"status": "success", "total_per_day": []})

    filename = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=filename):
        return jsonify({"status": "success", "total_per_day": []})

    try:
        df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=filename,
            columns=["collection_id", "local_timestamp", "activity_type"],
            filters=[("collection_id", "in", selected)],
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    windows = _load_collection_event_windows(selected)
    df = _filter_to_event_windows(df, windows)
    df = _filter_to_play_observe(df)

    potentials = {
        "collections": len(selected),
        "activities": 0,
        "active_days": 0,
    }
    if df is not None and not df.empty:
        potentials["activities"] = int(len(df))
        potentials["active_days"] = int(pd.to_datetime(df["local_timestamp"], errors="coerce").dropna().dt.date.nunique())

    total_per_day = _daily_counts(df)
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

        for cached_file in [
            f"{study_name}_recoded.parquet",
            f"{study_name}_explorer_metadata.json",
            f"{study_name}_comp_interpretations.json",
            f"{study_name}_PCA.parquet",
        ]:
            data_io.remove(storage_location="cache", filename=cached_file)

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



