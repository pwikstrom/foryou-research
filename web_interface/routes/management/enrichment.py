"""Scrape/annotation queue + consolidation endpoints (/api/manage/enrichment*, refresh staleness)."""

import time
from datetime import UTC, datetime

import pandas as pd
from flask import jsonify, request
from flask_login import login_required

import fyp.data_io as data_io
import fyp.scrape_queues as scrape_queues
from fyp.scrape import scraper_alerts
from fyp.platform_scraper import get_scraper
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    create_study_recoded_dataset,
)

from ...process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
)
from ...permissions import permission_required
from ...services import system_health
from ...task_status import is_cloud_run



from ...services.stats_service import (
    _evaluate_consolidation_staleness,
)
from ...services.worker_status import (
    PIPELINE_STEPS_ORDER,
    _build_pipeline_step_view,
    _cached_cookie_health,
    _is_worker_running,
    _workers_blocking_consolidate,
)



from ._blueprint import management_bp


_drain_lease_cache = {"ts": 0.0, "value": {}}
_DRAIN_LEASE_CACHE_TTL_S = 30


def _active_drain_leases() -> dict:
    """Fresh local-drain leases per platform (empty on any read failure).

    Cached for a short TTL so the frequently-polled stats endpoint doesn't add
    per-platform GCS reads on every tick — a drain appearing/disappearing
    within the TTL is a UI-freshness detail, not a correctness one (the
    authoritative check lives in ``process_manager.start_process``).
    """
    now = time.monotonic()
    if now - _drain_lease_cache["ts"] < _DRAIN_LEASE_CACHE_TTL_S:
        return _drain_lease_cache["value"]
    try:
        from ...drain_lease import active_drain_leases

        value = active_drain_leases()
    except Exception:
        value = {}
    _drain_lease_cache["ts"] = now
    _drain_lease_cache["value"] = value
    return value


def _consolidate_blockers() -> list[str]:
    """Everything that should defer a consolidate: running workers + drain leases.

    A drain lease (a locally-started scraper subprocess, or a laptop drain
    against the shared bucket) writes scrape data that a concurrent
    consolidation's queue prune would race — ``start_process`` refuses it
    anyway, so treating the lease as a blocker here means the endpoint arms
    instead of surfacing that refusal as an error.
    """
    blocking = _workers_blocking_consolidate()
    blocking += [f"local drain ({p})" for p in sorted(_active_drain_leases())]
    return blocking






@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@permission_required('tab.data_management.enrichment')
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    # Reload process_stats from GCS so we pick up task-runner writes, and
    # drop any consolidation_impact that has already been fully resolved by
    # downstream refreshes — otherwise the impact panel lingers forever when
    # the UI never happens to call /api/manage/refresh/staleness.
    _evaluate_consolidation_staleness()

    # 1. Load Enrichment Status
    enrichment_status = None
    if data_io.exists(storage_location="recoded", filename='enrichment_status.parquet'):
        enrichment_status = data_io.load_parquet(storage_location="recoded", filename='enrichment_status.parquet')

    total_videos = 0
    scraped_videos = 0
    annotated_videos = 0
    unique_collections = 0

    if enrichment_status is not None and not enrichment_status.empty:
        total_videos = len(enrichment_status)
        if 'scraped_ok' in enrichment_status.columns:
            scraped_videos = int(enrichment_status['scraped_ok'].sum())
        if 'annotated_ok' in enrichment_status.columns:
            annotated_videos = int(enrichment_status['annotated_ok'].sum())

    ddp_metadata = None
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
        ddp_metadata = data_io.load_parquet(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
    if ddp_metadata is not None and not ddp_metadata.empty:
        if ('other', 'accepted') in ddp_metadata.columns:
            unique_collections = int(ddp_metadata[ddp_metadata[('other','accepted')]].index.nunique())
        else:
            unique_collections = int(ddp_metadata.index.nunique())
        
    
    # 2. Get Queue Lengths (per-platform scrape queues + their total)
    scrape_queues_by_platform: dict[str, int] = {}
    annotate_queue_len = 0

    try:
        scrape_queues_by_platform = scrape_queues.queue_lengths()
    except Exception:
        pass
    scrape_queue_len = sum(scrape_queues_by_platform.values())

    if data_io.exists(storage_location='cache', filename='to_annotate.json'):
        q = data_io.load_json(storage_location='cache', filename='to_annotate.json')
        if isinstance(q, list): annotate_queue_len = len(q)

    # Videos reserved out of the queue by an in-flight async batch job (claimed at
    # submit time, so they no longer count in annotate_queue_len). Gated on the
    # batch worker actually running: a leftover job-state file from a finished run
    # then reads 0, and for multi-chunk runs the file holds only the current slice
    # so pending + in-batch always sum to the outstanding total.
    annotate_claimed_len = 0
    if _is_worker_running("queue_annotator_batch") and \
       data_io.exists(storage_location='cache', filename='annotate_batch_job.json'):
        job = data_io.load_json(storage_location='cache', filename='annotate_batch_job.json')
        if isinstance(job, dict):
            annotate_claimed_len = len(job.get("submitted_ids") or [])
        
    # Backstop: resolve a forked fan-out (meta‖pca‖timelines) whose dropped leaf
    # left it un-finalized. The event-driven barrier may miss this if every
    # surviving leaf finished before the grace window; this poll-driven call
    # flips a never-started leaf to "failed" and finalizes once grace passes.
    # No-op when no fan-out is active; Cloud Run only (local mode never forks).
    if is_cloud_run():
        try:
            from ..process_routes import resolve_forked_pipeline
            resolve_forked_pipeline()
        except Exception as e:
            print(f"[status] resolve_forked_pipeline failed: {e}")

    consolidate_entry = process_stats.get("consolidate_enrichment", {})

    # Is any consolidate-pipeline step currently running? Used by the UI to
    # pick up live stage progress after a page reload mid-pipeline. The
    # pipeline_in_flight flag covers the brief gap between one step completing
    # and the next step booting up (when no step is technically "running").
    pipeline_step_names = ["consolidate_enrichment"] + PIPELINE_STEPS_ORDER
    any_step_running = any(_is_worker_running(n) for n in pipeline_step_names)
    flag_in_flight = bool(consolidate_entry.get("pipeline_in_flight"))

    # Stale-flag cleanup: a server restart mid-pipeline leaves the flag set
    # with no orchestrator thread to clear it. If the flag is on but nothing
    # is running AND the consolidate step completed >60s ago (longer than
    # any plausible inter-step gap), treat the pipeline as abandoned and
    # clear the flag so the UI stops showing "in flight" forever.
    if flag_in_flight and not any_step_running:
        last_end = consolidate_entry.get("last_run_end_time")
        stale = False
        if last_end:
            try:
                end_dt = datetime.fromisoformat(last_end)
                if (datetime.now(UTC) - end_dt).total_seconds() > 60:
                    stale = True
            except (ValueError, TypeError):
                stale = True
        else:
            stale = True
        if stale:
            consolidate_entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = consolidate_entry
            save_process_stats()
            flag_in_flight = False

    pipeline_active = flag_in_flight or any_step_running

    cookie_health = {
        p: _cached_cookie_health(p)
        for p in scrape_queues.registered_platforms()
    }

    # Active scraper alerts (e.g. a permanent-failure storm raised by the
    # worker): shown as a banner on the platform's scraper card and folded
    # into its health chip below.
    active_alerts = scraper_alerts.load_alerts()

    # Whether annotation is configured on the ACTIVE backend (pure config
    # check). The client uses this to disable/short-circuit the annotator
    # start with a clear message instead of booting a worker that can't
    # annotate anything.
    try:
        from fyp.annotation.backends import active_backend_name
        from fyp.annotation.machine_annotation import annotation_configured
        annotation_ok, annotation_reason = annotation_configured()
        annotation_backend = active_backend_name()
    except Exception as exc:
        annotation_ok, annotation_reason = False, (
            f"Machine annotation is unavailable: the annotation backend could "
            f"not be loaded ({exc})."
        )
        annotation_backend = "gemini"

    return jsonify({
        "annotation_configured": annotation_ok,
        "annotation_config_reason": annotation_reason,
        "annotation_backend": annotation_backend,
        "total_videos": total_videos,
        "scraped_videos": scraped_videos,
        "annotated_videos": annotated_videos,
        "unique_collections": unique_collections,
        "scrape_queue_len": scrape_queue_len,
        "scrape_queues": scrape_queues_by_platform,
        "cookie_health": cookie_health,
        "scraper_alerts": active_alerts,
        # Per-card health chips: combine the last system-health check (test
        # scrape + media) with the fresh cookie status into one green/yellow/red
        # per platform, plus an annotation chip from the Gemini ping.
        "card_health": system_health.derive_card_health(
            live_cookie=cookie_health, alerts=active_alerts),
        "annotate_queue_len": annotate_queue_len,
        "annotate_claimed_len": annotate_claimed_len,
        # Fresh local-drain leases (laptop draining a queue against the shared
        # bucket) — the matching scraper start and consolidation are blocked
        # while one is held. {platform: {host, user, started_at, ...}}.
        "local_drains": _active_drain_leases(),
        "consolidate_stats": {
            **consolidate_entry,
            **processes.get("consolidate_enrichment", {}).get("data", {})
        } or None,
        "consolidate_auto_armed": bool(consolidate_entry.get("auto_armed")),
        "consolidate_auto_armed_auto_refresh": bool(consolidate_entry.get("auto_armed_auto_refresh")),
        "consolidate_pipeline_active": pipeline_active,
        "pipeline_steps": _build_pipeline_step_view(pipeline_active),
        "last_pipeline_partial": bool(consolidate_entry.get("last_pipeline_partial")),
        "last_pipeline_failed_at": consolidate_entry.get("last_pipeline_failed_at"),
        # Includes fresh drain leases: the browser's armed auto-fire keys off
        # this list, and a consolidate must defer while a drain writes scrapes.
        "workers_blocking_consolidate": _consolidate_blockers(),
        "scraper_last_success": max(
            (
                process_stats.get(f"queue_scraper_{p}", {}).get("last_success")
                or process_stats.get("queue_scraper", {}).get("last_success")
                or ""
                for p in scrape_queues_by_platform or ["tiktok"]
            ),
            default=None,
        ) or None,
        # Newest of the sync and async annotators, so a completed async batch run
        # also triggers the "consolidation needed" prompt (ISO timestamps sort
        # lexically). Without the batch key, an async run left no signal.
        "annotator_last_success": max(
            (
                process_stats.get(k, {}).get("last_success") or ""
                for k in ("queue_annotator", "queue_annotator_batch")
            ),
            default="",
        ) or None,
    })






@management_bp.route('/api/manage/annotation/backends', methods=['GET'])
@permission_required('tab.admin.general')
@login_required
def get_annotation_backends():
    """Availability of every annotation backend, for the requirements panel.

    Returns:
        ``{backends: [{name, active, implemented, availability:
        {ok, reason, checks}}]}`` — ``checks`` rows carry actionable ``fix``
        strings for anything missing on this host.
    """
    from fyp.annotation.backends import BACKEND_IDS, active_backend_name, get_backend

    active = active_backend_name()
    out = []
    for name in BACKEND_IDS:
        entry = {"name": name, "active": name == active}
        try:
            backend = get_backend(name)
            result = backend.availability()
            entry["implemented"] = True
            entry["availability"] = {"ok": result.ok, "reason": result.reason,
                                     "checks": result.checks}
        except ValueError as exc:
            # Module import failed (e.g. mlx-vlm absent) — fall back to the
            # dependency checks so the panel still shows actionable fixes.
            entry["implemented"] = False
            if name == "qwen_local":
                from fyp.annotation.backends import qwen_support
                result = qwen_support.availability()
                entry["availability"] = {"ok": False, "reason": result.reason,
                                         "checks": result.checks}
            elif name == "minicpm_local":
                from fyp.annotation.backends import minicpm_support
                result = minicpm_support.availability()
                entry["availability"] = {"ok": False, "reason": result.reason,
                                         "checks": result.checks}
            else:
                entry["availability"] = {"ok": False, "reason": str(exc), "checks": []}
        out.append(entry)
    return jsonify({"backends": out})




@management_bp.route('/api/manage/embedding/backends', methods=['GET'])
@permission_required('tab.admin.general')
@login_required
def get_embedding_backends():
    """Availability of every embedding backend, for the requirements panel.

    Same shape as ``/api/manage/annotation/backends``:
    ``{backends: [{name, active, implemented, availability:
    {ok, reason, checks}}]}``.
    """
    from fyp.analysis.embedding_backends import BACKEND_IDS, active_backend_name, get_backend

    active = active_backend_name()
    out = []
    for name in BACKEND_IDS:
        entry = {"name": name, "active": name == active}
        try:
            backend = get_backend(name)
            result = backend.availability()
            entry["implemented"] = True
            entry["availability"] = {"ok": result.ok, "reason": result.reason,
                                     "checks": result.checks}
        except ValueError as exc:
            # Module import failed — fall back to the dependency checks so the
            # panel still shows actionable fixes.
            entry["implemented"] = False
            if name == "qwen_local":
                from fyp.analysis.embedding_backends import qwen_support
                result = qwen_support.availability()
                entry["availability"] = {"ok": False, "reason": result.reason,
                                         "checks": result.checks}
            else:
                entry["availability"] = {"ok": False, "reason": str(exc), "checks": []}
        out.append(entry)
    return jsonify({"backends": out})




@management_bp.route('/api/manage/enrichment/empty_queue/<queue_type>', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def empty_enrichment_queue(queue_type):
    try:
        if queue_type == "scrape":
            # Optional {"platform": ...} in the body targets one platform's
            # queue; default empties every registered platform's queue.
            body = request.get_json(silent=True) or {}
            requested = body.get("platform")
            targets = [requested] if requested else scrape_queues.registered_platforms()
            for platform in targets:
                scrape_queues.remove_scrape_queue(platform)
            load_process_stats()
            stats_changed = False
            for platform in targets:
                entry = process_stats.get(f"queue_scraper_{platform}", {})
                if "scrape_queue_len" in entry:
                    entry["scrape_queue_len"] = 0
                    stats_changed = True
            # Legacy pre-rename entry, harmless to zero alongside.
            if "scrape_queue_len" in process_stats.get("queue_scraper", {}):
                process_stats["queue_scraper"]["scrape_queue_len"] = 0
                stats_changed = True
            if stats_changed:
                save_process_stats()
        elif queue_type == "annotate":
            if data_io.exists(storage_location='cache', filename='to_annotate.json'):
                data_io.remove(storage_location='cache', filename='to_annotate.json')
            load_process_stats()
            if "annotate_queue_len" in process_stats.get("queue_annotator", {}):
                process_stats["queue_annotator"]["annotate_queue_len"] = 0
                save_process_stats()
        else:
            return jsonify({"error": "Invalid queue type"}), 400

        return jsonify({"status": "success", "message": f"{queue_type.capitalize()} queue emptied."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/scraper_alert/dismiss', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def dismiss_scraper_alert():
    """Dismiss a platform's scraper alert ({"platform": ...} in the body).

    Manual counterpart of the auto-clear on the next healthy batch — for the
    case where the admin has investigated (or fixed the scraper) and wants the
    banner gone before a new run proves it.
    """
    body = request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "")
    if platform not in scrape_queues.registered_platforms():
        return jsonify({"error": f"Unknown platform: {platform!r}"}), 400
    scraper_alerts.clear_alert(platform, reason="dismissed by admin")
    return jsonify({"status": "success"})


@management_bp.route('/api/manage/enrichment/queue_voted', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def queue_voted_videos():
    try:
        from web_interface.security import user_manager
        
        # 1. Gather all votes across all users
        all_votes = {} # dict of collection_id -> set of periods
        for user in user_manager.get_all_users().values():
            if not user.machine_annotation_votes:
                continue
            for coll_id, periods in user.machine_annotation_votes.items():
                if coll_id not in all_votes:
                    all_votes[coll_id] = set()
                all_votes[coll_id].update(periods)
                
        if not all_votes:
            return jsonify({"status": "no_votes", "message": "No votes found for machine annotation."})

        # 2. Map periods to item_ids 
        import pandas as pd

        from fyp.organize_datasets import create_collection_unified_dataset
        target_item_ids = set()
        
        for coll_id, periods in all_votes.items():
            try:
                # Need to load using standard DDP logic since timeline cache aggregates and removes item_id
                df_collection = create_collection_unified_dataset(collection_id=coll_id, verbose=False)
                
                if df_collection is not None and not df_collection.empty and 'item_id' in df_collection.columns and 'local_date' in df_collection.columns:
                    # Time periods can be 'YYYY-MM-DD' or 'YYYY-Wxx' or 'YYYY-MM'
                    ts_series = pd.to_datetime(df_collection['local_date'], errors='coerce')
                    
                    for p in periods:
                        # yyyy-mm-dd
                        if len(p) == 10 and p.count('-') == 2:
                            match_mask = ts_series.dt.strftime('%Y-%m-%d') == p
                        # yyyy-mm
                        elif len(p) == 7 and p.count('-') == 1:
                            match_mask = ts_series.dt.strftime('%Y-%m') == p
                        # yyyy-Wxx
                        elif 'W' in p:
                            # pandas isocalendar week
                            def format_week(dt):
                                if pd.isna(dt): return ""
                                iso = dt.isocalendar()
                                return f"{iso.year}-W{iso.week:02d}"
                            match_mask = ts_series.apply(format_week) == p
                        else:
                            continue # Unknown format
                        
                        hits = df_collection.loc[match_mask, 'item_id'].dropna().unique().tolist()
                        target_item_ids.update(hits)
                        
            except Exception as e:
                print(f"Error processing timeline for collection {coll_id}: {e}")

        if not target_item_ids:
             return jsonify({"status": "no_matches", "message": "No specific videos matched the voted time periods."})

        # 3. Check Enrichment Status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
             df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        default_platform = scrape_queues.default_platform()
        new_scrape = []
        new_annotate = []
        item_platform: dict[str, str] = {}

        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                     df_status = df_status.rename(columns={'index': 'item_id'})

            # Convert status ids to set for fast lookup
            status_records = df_status.set_index('item_id').to_dict('index')

            for item in target_item_ids:
                if item in status_records:
                    rec = status_records[item]
                    is_scraped = rec.get('scraped_ok', False)
                    is_annotated = rec.get('annotated_ok', False)
                    scrape_fail = rec.get('scrape_fail', False)
                    annotated_fail = rec.get('annotated_fail', False)
                    has_media = rec.get('video_downloaded', False)
                    plat = rec.get('source_platform')
                    item_platform[item] = plat if isinstance(plat, str) and plat else default_platform

                    # Annotation needs an mp4: metadata-only items (e.g.
                    # YouTube long-form past the media duration cap) are not
                    # annotatable and stay out of the queue.
                    if not is_scraped and not scrape_fail:
                        new_scrape.append(item)
                    elif is_scraped and has_media and not is_annotated and not annotated_fail:
                        new_annotate.append(item)
                else:
                    # Item not in enrichment status -> hasn't been scraped yet
                    new_scrape.append(item)
        else:
            # No enrichment file -> everything needs scraping
            new_scrape = list(target_item_ids)

        new_scrape = list(set(new_scrape))
        new_annotate = list(set(new_annotate))

        # 4. Append to Queues (scrape queues are per-platform). Platforms
        # without a scrape-contract block have no worker to drain a queue, so
        # their items are skipped instead of stranded in an orphan file.
        added_to_scrape: dict[str, int] = {}
        if new_scrape:
            scrapeable = set(scrape_queues.registered_platforms())
            by_platform: dict[str, list[str]] = {}
            for item in new_scrape:
                by_platform.setdefault(item_platform.get(item, default_platform), []).append(item)
            for platform, items in by_platform.items():
                if platform not in scrapeable:
                    print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                    continue
                scrape_queues.append_to_scrape_queue(platform, items)
                added_to_scrape[platform] = len(items)

        if new_annotate:
            # Atomic append: ids claimed/pruned meanwhile by an annotation
            # worker are never clobbered by this write.
            data_io.update_json(
                storage_location="cache",
                filename="to_annotate.json",
                mutate=lambda current: list(
                    set(current if isinstance(current, list) else []) | set(new_annotate)
                ),
                default=[],
            )

        return jsonify({
            "status": "success",
            "added_to_scrape": len(new_scrape),
            "added_to_scrape_by_platform": added_to_scrape,
            "added_to_annotate": len(new_annotate)
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_scrape():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    retry_missing_media = bool(data.get("retry_missing_media", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None

        if data_io.exists(storage_location="cache", filename=recoded_fn):
            # Load only the required column if possible, but load_parquet loads all if columns not provided properly or we can just load the whole file.
            # Actually, calculate_to_scrape only really needs item_id. The full load is fine as the files are usually small enough, but let's just load it.
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)

        if df_study is None or df_study.empty:
            # If not cached or empty, generate from scratch
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)

        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unscraped_videos = []
        if df_status is not None and not df_status.empty:
            # item_id is usually the index in enrichment_status
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                # If index was unnamed, it might become 'index'
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            # Map enrichment_status to our study videos
            study_videos = df_study[['item_id']].copy()
            study_status = study_videos.merge(df_status, on='item_id', how='left')

            # Find videos where scraped_ok is fundamentally False or NaN AND scrape_fail is fundamentally False or NaN
            not_scraped = pd.isna(study_status['scraped_ok']) | (study_status['scraped_ok'] == False)

            # When retry_failed is set, include items that previously failed
            # by dropping the scrape_fail filter — the user is asking us to
            # re-attempt them regardless of past outcome.
            if retry_failed:
                unscraped_mask = not_scraped
            elif 'scrape_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scrape_fail']) | (study_status['scrape_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            elif 'scraped_fail' in study_status.columns:
                not_failed = pd.isna(study_status['scraped_fail']) | (study_status['scraped_fail'] == False)
                unscraped_mask = not_scraped & not_failed
            else:
                unscraped_mask = not_scraped

            unscraped_videos = study_status.loc[unscraped_mask, 'item_id'].dropna().tolist()
        else:
            unscraped_videos = df_study['item_id'].dropna().tolist()

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unscraped_videos = list({str(v) for v in unscraped_videos})

        # Media-gap backfill: items scraped OK but whose media never landed
        # (e.g. a rate-limited media phase saved metadata-only) can't be found
        # via scraped_ok — pick them straight from the study frame. Items over
        # the platform's media duration cap are metadata-only by design and
        # excluded; unknown durations pass (the media phase decides).
        if retry_missing_media and {'scraped_ok', 'video_downloaded', 'item_id'} <= set(df_study.columns):
            per_item = df_study.drop_duplicates(subset=['item_id'])
            gap_mask = (
                (per_item['scraped_ok'].fillna(False) == True)
                & ~(per_item['video_downloaded'].fillna(False) == True)
            )
            gap = per_item[gap_mask]
            gap_platforms = (
                gap['source_platform'].fillna(scrape_queues.default_platform())
                if 'source_platform' in gap.columns
                else pd.Series(scrape_queues.default_platform(), index=gap.index)
            )
            media_gap_videos: set[str] = set()
            for gap_platform, grp in gap.groupby(gap_platforms):
                try:
                    cap = get_scraper(str(gap_platform)).media_duration_cap()
                except Exception:
                    continue  # no scraper registered for this platform
                if 'duration' in grp.columns:
                    dur = pd.to_numeric(grp['duration'], errors='coerce')
                    grp = grp[dur.isna() | (dur <= cap)]
                media_gap_videos |= {str(v) for v in grp['item_id'].dropna()}
            if media_gap_videos:
                print(f"Retry-missing-media: adding {len(media_gap_videos)} scraped-ok "
                      f"items without media to the queue(s).")
            unscraped_videos = list(set(unscraped_videos) | media_gap_videos)

        # Append to the per-platform scrape queues. The study frame carries
        # source_platform per event row; an item never spans platforms.
        default_platform = scrape_queues.default_platform()
        item_platform: dict[str, str] = {}
        if 'source_platform' in df_study.columns:
            plat_map = (
                df_study[['item_id', 'source_platform']]
                .dropna(subset=['item_id'])
                .drop_duplicates(subset=['item_id'])
            )
            item_platform = {
                str(i): (str(p) if isinstance(p, str) and p else default_platform)
                for i, p in zip(plat_map['item_id'], plat_map['source_platform'])
            }

        by_platform: dict[str, list[str]] = {}
        for vid in unscraped_videos:
            by_platform.setdefault(item_platform.get(vid, default_platform), []).append(vid)

        # Platforms without a scrape-contract block have no worker to drain a
        # queue, so their items are skipped instead of stranded in an orphan file.
        scrapeable = set(scrape_queues.registered_platforms())
        queue_len_by_platform: dict[str, int] = {}
        skipped_by_platform: dict[str, int] = {}
        for platform, items in by_platform.items():
            if platform not in scrapeable:
                skipped_by_platform[platform] = len(items)
                print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                continue
            queue_len_by_platform[platform] = scrape_queues.append_to_scrape_queue(platform, items)

        return jsonify({
            "status": "success",
            "videos_to_scrape": sum(queue_len_by_platform.values()),
            "videos_to_scrape_by_platform": queue_len_by_platform,
            "skipped_unscrapeable_by_platform": skipped_by_platform,
        })

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def calculate_to_annotate():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        from fyp.fyp_config import fyp_cf

        # Check for cached recoded dataset first
        recoded_fn = f"{study_name}_recoded.parquet"
        df_study = None
        
        if data_io.exists(storage_location="cache", filename=recoded_fn):
            df_study = data_io.load_parquet(storage_location="cache", filename=recoded_fn)
            
        if df_study is None or df_study.empty:
            df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
            
        if df_study is None or df_study.empty:
            return jsonify({"error": f"Dataset for study '{study_name}' could not be generated."}), 400

        # Load global enrichment status
        df_status = None
        if data_io.exists(storage_location="recoded", filename="enrichment_status.parquet"):
            df_status = data_io.load_parquet(storage_location="recoded", filename="enrichment_status.parquet")

        unannotated_videos = []
        if df_status is not None and not df_status.empty:
            if 'item_id' not in df_status.columns:
                df_status = df_status.reset_index()
                if 'index' in df_status.columns and 'item_id' not in df_status.columns:
                    df_status = df_status.rename(columns={'index': 'item_id'})

            if 'duration' in df_study.columns:
                study_videos = df_study[['item_id', 'duration']].copy()
            else:
                study_videos = df_study[['item_id']].copy()
                
            study_status = study_videos.merge(df_status, on='item_id', how='left')
            
            is_scraped_ok = study_status['scraped_ok'].fillna(False) == True
            
            if 'annotated_ok' in study_status.columns:
                not_annotated_ok = pd.isna(study_status['annotated_ok']) | (study_status['annotated_ok'] == False)
            else:
                not_annotated_ok = True

            # When retry_failed is set, ignore the annotated_fail column so
            # items that previously failed annotation are re-queued.
            if retry_failed:
                not_annotated_fail = True
            elif 'annotated_fail' in study_status.columns:
                not_annotated_fail = pd.isna(study_status['annotated_fail']) | (study_status['annotated_fail'] == False)
            else:
                not_annotated_fail = True

            unannotated_mask = is_scraped_ok & not_annotated_ok & not_annotated_fail

            # Annotation needs an mp4: metadata-only items (e.g. YouTube
            # long-form past the media duration cap) are not annotatable.
            if 'video_downloaded' in study_status.columns:
                unannotated_mask = unannotated_mask & (study_status['video_downloaded'].fillna(False) == True)

            if 'duration' in study_status.columns:
                max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
                duration_ok = (study_status['duration'] < max_dur) | pd.isna(study_status['duration'])
                unannotated_mask = unannotated_mask & duration_ok

            unannotated_videos = study_status.loc[unannotated_mask, 'item_id'].dropna().tolist()
        else:
            unannotated_videos = []

        # Ensure all values are plain Python strings (not PyArrow scalars)
        unannotated_videos = list({str(v) for v in unannotated_videos})

        # Append target payload to global annotate queue (atomic — never
        # clobbers ids claimed/pruned meanwhile by an annotation worker).
        current_queue = data_io.update_json(
            storage_location="cache",
            filename="to_annotate.json",
            mutate=lambda current: list(
                {str(v) for v in (current if isinstance(current, list) else [])}
                | set(unannotated_videos)
            ),
            default=[],
        )

        return jsonify({
            "status": "success",
            "videos_to_annotate": len(current_queue),
        })

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_enrichment():
    from fyp.fyp_config import CONSOLIDATE_ENRICHMENT_SCRIPT

    if _is_worker_running("consolidate_enrichment"):
        return jsonify({"status": "error", "message": "Consolidation already running"}), 409

    data = request.json or {}
    force = bool(data.get("force"))
    # auto_refresh defaults to True — the button means "consolidate + fix the
    # consolidation impact automatically". Force Reconsolidate skips the
    # downstream chain by default to keep it debuggable.
    auto_refresh = bool(data.get("auto_refresh", not force))

    blocking = _consolidate_blockers()
    if blocking:
        if force:
            return jsonify({
                "status": "error",
                "message": f"Cannot force reconsolidate while {', '.join(blocking)} running.",
            }), 409

        # Arm instead of firing — pipeline kicks off when workers go idle.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry["auto_armed"] = True
        entry["auto_armed_force"] = False
        entry["auto_armed_auto_refresh"] = auto_refresh
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
        return jsonify({
            "status": "armed",
            "message": f"Waiting for {', '.join(blocking)} to finish.",
            "blocking": blocking,
        })

    task_args: dict = {}
    if force:
        task_args["force_consolidation"] = True
    if auto_refresh:
        task_args["auto_refresh"] = True

    # Firing now — clear any stale armed flag and seed a pipeline-plan marker so
    # the step list shows the live "Consolidate enrichment data" step from the
    # very first poll (steps=[] until the worker computes the real downstream
    # plan; _build_pipeline_step_view renders a present-but-empty plan). Without
    # this the list only appears after consolidation finishes and the user sees
    # only a text line during the (long) consolidation phase.
    now_iso = datetime.now(UTC).isoformat()
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    entry["pipeline_plan"] = {
        "steps": [],
        "started_ts": now_iso,
        "mode": "refresh" if auto_refresh else "consolidate_only",
    }
    entry["last_pipeline_partial"] = False
    entry["last_pipeline_failed_at"] = None
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    success, msg = start_process("consolidate_enrichment", CONSOLIDATE_ENRICHMENT_SCRIPT,
                                 task_args=task_args if task_args else None)
    if success:
        # start_process resets the in-memory ::DATA:: copy; mirror the marker
        # there too so the local-dev overlay in _build_pipeline_step_view agrees
        # with process_stats (no-op on Cloud Run, where there is no subprocess).
        mem = processes.get("consolidate_enrichment", {}).get("data")
        if isinstance(mem, dict):
            mem["pipeline_plan"] = entry["pipeline_plan"]
        return jsonify({"status": "started", "message": msg})
    else:
        # Dispatch failed — don't leave a phantom plan marker behind.
        load_process_stats()
        entry = process_stats.get("consolidate_enrichment", {})
        entry.pop("pipeline_plan", None)
        process_stats["consolidate_enrichment"] = entry
        save_process_stats()
        return jsonify({"status": "error", "message": msg}), 409


@management_bp.route('/api/manage/enrichment/consolidate/disarm', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_consolidate_disarm():
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    was_armed = bool(entry.get("auto_armed"))
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()
    return jsonify({"status": "disarmed", "was_armed": was_armed})


@management_bp.route('/api/manage/enrichment/refresh-downstream', methods=['POST'])
@permission_required('tab.data_management.enrichment')
@login_required
def api_refresh_downstream():
    """Re-run the downstream refresh pipeline for the stored consolidation impact.

    Powers the "Refresh All Affected" button. It runs the SAME pipeline as the
    consolidate auto-refresh — embeddings → video_map → recode → {meta ‖ pca ‖
    timelines} — against the impact recorded by a prior Consolidate Only run, so
    the niche steps the old per-button cascade skipped are now included and in
    the right order. Writes ``pipeline_plan`` so the step list renders, then
    dispatches via the Cloud Tasks chain (Cloud Run) or the local sequential
    orchestrator (dev).
    """
    from web_interface.run_consolidate_enrichment import (
        _build_downstream_pipeline,
        build_pipeline_chain,
    )

    load_process_stats()
    ps_entry = process_stats.get("consolidate_enrichment", {})
    mem = processes.get("consolidate_enrichment", {}).get("data", {}) or {}
    impact = ps_entry.get("consolidation_impact") or mem.get("consolidation_impact")
    if not impact:
        return jsonify({"status": "noop", "message": "No consolidation impact to refresh."})

    # Don't start on top of a running pipeline.
    if ps_entry.get("pipeline_in_flight") or any(
        _is_worker_running(n) for n in (["consolidate_enrichment"] + PIPELINE_STEPS_ORDER)
    ):
        return jsonify({"status": "error", "message": "A refresh pipeline is already running."}), 409

    pipeline = _build_downstream_pipeline(impact)
    if not pipeline:
        return jsonify({"status": "noop", "message": "Nothing to refresh."})

    now_iso = datetime.now(UTC).isoformat()
    ps_entry["pipeline_plan"] = {"steps": [p["task"] for p in pipeline], "started_ts": now_iso}
    ps_entry["last_pipeline_partial"] = False
    ps_entry["last_pipeline_failed_at"] = None
    ps_entry["last_pipeline_summary"] = "Pipeline in progress — refreshing caches..."
    ps_entry["last_pipeline_summary_ts"] = now_iso
    ps_entry["pipeline_in_flight"] = True
    process_stats["consolidate_enrichment"] = ps_entry
    save_process_stats()

    # In local dev the consolidate worker's last ::DATA:: emission lingers in
    # processes["consolidate_enrichment"]["data"] and the stats / step-view
    # endpoints overlay it on top of process_stats. After a "Consolidate Only"
    # run that emission carries pipeline_plan=None, which would shadow the fresh
    # plan just written and hide the step list. Mirror the new plan into the
    # in-memory copy so both stores agree (no-op on Cloud Run, where there is no
    # in-process consolidate subprocess).
    mem = processes.get("consolidate_enrichment", {}).get("data")
    if isinstance(mem, dict):
        mem["pipeline_plan"] = ps_entry["pipeline_plan"]
        mem["last_pipeline_partial"] = False
        mem["last_pipeline_failed_at"] = None

    if is_cloud_run():
        from ...process_manager import _dispatch_cloud_task
        chain = build_pipeline_chain(pipeline)
        success, msg = _dispatch_cloud_task(chain["next_task"], chain["next_task_args"])
        if not success:
            # Roll back the in-flight flag so the UI doesn't hang.
            load_process_stats()
            entry = process_stats.get("consolidate_enrichment", {})
            entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = entry
            save_process_stats()
            return jsonify({"status": "error", "message": f"Dispatch failed: {msg}"}), 409
    else:
        import threading

        from ...process_manager import _run_local_downstream_pipeline
        threading.Thread(
            target=_run_local_downstream_pipeline, args=(impact,), daemon=True
        ).start()

    return jsonify({"status": "started", "message": "Downstream refresh started."})



@management_bp.route('/api/manage/refresh/staleness', methods=['GET'])
@permission_required('tab.data_management.refresh')
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact."""
    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
    })


