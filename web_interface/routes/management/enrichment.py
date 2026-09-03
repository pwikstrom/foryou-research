"""Scrape/annotation queue + consolidation endpoints (/api/manage/enrichment*, refresh staleness)."""

import time
from datetime import UTC, datetime

import pandas as pd
from flask import jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
import fyp.scrape_queues as scrape_queues
from fyp.scrape import scraper_alerts
from fyp.platform_scraper import get_scraper
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
    create_study_recoded_dataset,
)

from ... import activity_log
from ...process_manager import (
    load_process_stats,
    process_stats,
    processes,
    save_process_stats,
    start_process,
)
from ...permissions import permission_required
from ...services import collection_enrichment, system_health
from ...task_status import is_cloud_run



from ...services.stats_service import (
    _evaluate_consolidation_staleness,
    _evaluate_version_promotion_staleness,
)
from ...services.worker_status import (
    PIPELINE_STEPS_ORDER,
    _actor,
    _build_pipeline_step_view,
    _cached_cookie_health,
    _is_worker_running,
    _workers_blocking_consolidate,
    consolidate_entry_view,
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






def _log_dm(action: str, target: str = "", details: dict | None = None) -> None:
    """Record a Data-Management activity-log entry for the current user."""
    activity_log.record(
        actor=getattr(current_user, "username", ""),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action=action,
        target=target,
        details=details,
    )






def _apply_queue_cap(items: list[str], queue_kind: str) -> tuple[list[str], dict]:
    """Clamp a queue-build selection to the non-admin per-request cap.

    Cost guardrail for invited users: the admin settings
    ``queue_cap_annotation_items`` / ``queue_cap_scrape_items`` bound how many
    items one queue-build request from a non-admin may add (0 = unlimited);
    admins always bypass. Truncation is deterministic (sorted) so a repeated
    request keeps selecting the same head slice.

    Args:
        items: The computed id selection.
        queue_kind: ``"annotation"`` or ``"scrape"``.

    Returns:
        ``(possibly-truncated items, cap_info)`` where ``cap_info`` is the
        ``{"capped", "cap", "requested"}`` dict merged into the JSON response.
    """
    from web_interface.admin_settings import get_queue_cap

    requested = len(items)
    cap = get_queue_cap(queue_kind)
    is_admin_attr = getattr(current_user, "is_admin", False)
    is_admin = is_admin_attr() if callable(is_admin_attr) else bool(is_admin_attr)
    if is_admin or cap <= 0 or requested <= cap:
        return items, {"capped": False, "cap": cap or None, "requested": requested}
    return sorted(str(v) for v in items)[:cap], {
        "capped": True, "cap": cap, "requested": requested}






def _annotation_cost_estimate(n_items: int) -> dict | None:
    """Rough spend estimate for annotating ``n_items`` with the active backend.

    Uses the backend's ``pricing`` config (USD per 1M tokens) and the
    ``[machine]`` per-item token estimates (calibrate them against real
    ab_eval run costs). Returns None when the active backend has no pricing
    (e.g. local backends) — the UI then shows the item count only.
    """
    from fyp.annotation.backends import variants
    from fyp.fyp_config import fyp_cf
    from web_interface.admin_settings import get_annotation_backend

    try:
        selection = get_annotation_backend()
        pricing = variants.selection_pricing(selection)
    except Exception:
        return None
    if not pricing:
        return None

    machine_cf = fyp_cf.get("machine", {})
    est_in = float(machine_cf.get("est_input_tokens_per_annotation", 15000))
    est_out = float(machine_cf.get("est_output_tokens_per_annotation", 1500))
    cost = n_items * (est_in * float(pricing.get("input", 0))
                      + est_out * float(pricing.get("output", 0))) / 1e6

    # The concrete model behind the selection, for cost lines in the UI —
    # a variant's override wins over its backend's configured model.
    try:
        spec = variants.resolve(selection)
        model = (spec.overrides.get("model")
                 or fyp_cf.get("machine", {}).get(spec.backend_id, {}).get("model")
                 or selection)
    except Exception:
        model = selection
    return {
        "backend": selection,
        "model": str(model),
        "est_cost_usd": round(cost, 2),
        "est_input_tokens_per_item": est_in,
        "est_output_tokens_per_item": est_out,
    }






@management_bp.route('/api/manage/enrichment/stats', methods=['GET'])
@permission_required('tab.data_management.scrape', 'tab.data_management.annotation', 'tab.data_management.refresh')
@login_required
def get_enrichment_stats():
    # Only admins can see enrichment stats
    # Reload process_stats from GCS so we pick up task-runner writes, and
    # drop any consolidation_impact that has already been fully resolved by
    # downstream refreshes — otherwise the impact panel lingers forever when
    # the UI never happens to call /api/manage/refresh/staleness.
    _evaluate_consolidation_staleness()
    # Same passive self-clear for the preferred-version promotion marker.
    _evaluate_version_promotion_staleness()

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

    # Videos reserved out of the queue by in-flight async batch jobs (claimed at
    # submit time, so they no longer count in annotate_queue_len). Gated on the
    # batch worker actually running: a leftover job-state file from a finished run
    # then reads 0. Format 2 holds a TABLE of concurrent jobs (sum their slices);
    # the legacy single-job shape kept the ids at the top level.
    annotate_claimed_len = 0
    if _is_worker_running("queue_annotator_batch") and \
       data_io.exists(storage_location='cache', filename='annotate_batch_job.json'):
        job = data_io.load_json(storage_location='cache', filename='annotate_batch_job.json')
        if isinstance(job, dict):
            jobs = job.get("jobs")
            if isinstance(jobs, list):
                annotate_claimed_len = sum(
                    len(j.get("submitted_ids") or []) for j in jobs if isinstance(j, dict))
            else:
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
            # Reload before mutating: this runs on every browser poll, on
            # whichever web instance answers, and save_process_stats writes the
            # WHOLE consolidate entry from this process's memory. An instance
            # whose copy predated another instance's write would put its stale
            # entry back — 2026-09-03 that erased a fresh `auto_armed` flag 47 s
            # after it was set, and the armed refresh never fired.
            load_process_stats()
            fresh_entry = process_stats.get("consolidate_enrichment", {})
            if fresh_entry.pop("pipeline_in_flight", None) is not None:
                process_stats["consolidate_enrichment"] = fresh_entry
                save_process_stats()
            consolidate_entry = fresh_entry
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
        # per platform, plus an annotation chip. The live availability makes
        # the annotation chip follow a backend switch immediately instead of
        # waiting for the next health-check run.
        "card_health": system_health.derive_card_health(
            live_cookie=cookie_health, alerts=active_alerts,
            annotation_live={"backend": annotation_backend,
                             "ok": annotation_ok,
                             "reason": annotation_reason}),
        "annotate_queue_len": annotate_queue_len,
        "annotate_claimed_len": annotate_claimed_len,
        # Fresh local-drain leases (laptop draining a queue against the shared
        # bucket) — the matching scraper start and consolidation are blocked
        # while one is held. {platform: {host, user, started_at, ...}}.
        "local_drains": _active_drain_leases(),
        # Same read rule as the step view: the in-memory ::DATA:: copy is the
        # subprocess mirror and is only authoritative in local dev.
        "consolidate_stats": consolidate_entry_view() or None,
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
@permission_required('tab.admin.backends')
@login_required
def get_annotation_backends():
    """Availability of every annotation backend, for the requirements panel.

    Returns:
        ``{backends: [{name, backend, label, model, active, implemented,
        availability: {ok, reason, checks}}]}`` — ``checks`` rows carry
        actionable ``fix`` strings for anything missing on this host.
        Config-declared variants appear after the implementation ids;
        ``backend`` is the implementing backend id, ``model`` the effective
        model the selection would annotate with.
    """
    from fyp.annotation.backends import active_backend_name, get_backend, variants
    from fyp.fyp_config import get_config

    active = active_backend_name()
    out = []
    for name in variants.selection_ids():
        spec = variants.resolve(name)
        # Effective model even for unimplemented backends: the variant's
        # override, else the backend block's configured model.
        block = get_config()["machine"].get(spec.backend_id, {}) or {}
        model = (spec.overrides.get("model") or spec.overrides.get("model_id")
                 or block.get("model") or block.get("model_id") or "")
        entry = {"name": name, "backend": spec.backend_id,
                 "label": spec.label or name, "model": model,
                 "active": name == active}
        try:
            backend = get_backend(name)
            entry["model"] = backend.effective_model_id()
            result = backend.availability()
            entry["implemented"] = True
            entry["availability"] = {"ok": result.ok, "reason": result.reason,
                                     "checks": result.checks}
        except ValueError as exc:
            # Module import failed (e.g. mlx-vlm absent) — fall back to the
            # dependency checks so the panel still shows actionable fixes.
            entry["implemented"] = False
            if spec.backend_id == "qwen_local":
                from fyp.annotation.backends import qwen_support
                result = qwen_support.availability()
                entry["availability"] = {"ok": False, "reason": result.reason,
                                         "checks": result.checks}
            elif spec.backend_id == "minicpm_local":
                from fyp.annotation.backends import minicpm_support
                result = minicpm_support.availability()
                entry["availability"] = {"ok": False, "reason": result.reason,
                                         "checks": result.checks}
            else:
                entry["availability"] = {"ok": False, "reason": str(exc), "checks": []}
        out.append(entry)
    return jsonify({"backends": out})




@management_bp.route('/api/manage/embedding/backends', methods=['GET'])
@permission_required('tab.admin.backends')
@login_required
def get_embedding_backends():
    """Availability of every embedding backend, for the requirements panel.

    Same shape as ``/api/manage/annotation/backends``:
    ``{backends: [{name, model, active, implemented, availability:
    {ok, reason, checks}}]}``.
    """
    from fyp.analysis.embedding_backends import BACKEND_IDS, active_backend_name, get_backend
    from fyp.fyp_config import get_config

    active = active_backend_name()
    out = []
    for name in BACKEND_IDS:
        # Config-derived fallback so the model shows even on import failure.
        block = get_config().get("embedding", {}).get(name, {}) or {}
        entry = {"name": name, "model": block.get("model_id", ""),
                 "active": name == active}
        try:
            backend = get_backend(name)
            entry["model"] = backend.model_id()
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
@permission_required('tab.data_management.scrape', 'tab.data_management.annotation')
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

        _log_dm("empty_queue", target=queue_type)
        return jsonify({"status": "success", "message": f"{queue_type.capitalize()} queue emptied."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/enrichment/scraper_alert/dismiss', methods=['POST'])
@permission_required('tab.data_management.scrape')
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
@permission_required('tab.data_management.scrape', 'tab.data_management.annotation')
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

        new_scrape, scrape_cap_info = _apply_queue_cap(new_scrape, "scrape")
        new_annotate, annotate_cap_info = _apply_queue_cap(new_annotate, "annotation")

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
            "added_to_annotate": len(new_annotate),
            "scrape_capped": scrape_cap_info.get("capped", False),
            "annotate_capped": annotate_cap_info.get("capped", False),
        })

    except Exception as e:
        print(f"Error queueing voted videos: {e}")
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_scrape', methods=['POST'])
@permission_required('tab.data_management.scrape')
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

        unscraped_videos, cap_info = _apply_queue_cap(unscraped_videos, "scrape")

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

        # Dry run: report what WOULD be queued per platform without appending.
        if bool(data.get("dry_run", False)):
            would_queue = {p: len(items) for p, items in by_platform.items()
                           if p in scrapeable}
            skipped = {p: len(items) for p, items in by_platform.items()
                       if p not in scrapeable}
            return jsonify({
                "status": "success",
                "dry_run": True,
                "would_queue": sum(would_queue.values()),
                "would_queue_by_platform": would_queue,
                "skipped_unscrapeable_by_platform": skipped,
                **cap_info,
            })

        queue_len_by_platform: dict[str, int] = {}
        skipped_by_platform: dict[str, int] = {}
        for platform, items in by_platform.items():
            if platform not in scrapeable:
                skipped_by_platform[platform] = len(items)
                print(f"Skipped {len(items)} '{platform}' item(s): no scraper registered for that platform yet.")
                continue
            queue_len_by_platform[platform] = scrape_queues.append_to_scrape_queue(platform, items)

        _log_dm("build_scrape_queue", target=study_name,
                details={"newly_queued": len(unscraped_videos),
                         "retry_failed": retry_failed,
                         "retry_missing_media": retry_missing_media,
                         **cap_info})

        return jsonify({
            "status": "success",
            "videos_to_scrape": sum(queue_len_by_platform.values()),
            "videos_to_scrape_by_platform": queue_len_by_platform,
            "skipped_unscrapeable_by_platform": skipped_by_platform,
            **cap_info,
        })

    except Exception as e:
        print(f"Error calculating scrape targets: {e}")
        return jsonify({"error": str(e)}), 500

def _select_annotated_item_ids(
    archive_df: pd.DataFrame,
    version: str | None = None,
    ts_from: int | None = None,
    ts_to: int | None = None,
    study_item_ids: set[str] | None = None,
) -> tuple[list[str], int]:
    """Select successfully-annotated item ids from the all-versions archive.

    Args:
        archive_df: The ``{label}_all_versions.parquet`` frame (one row per
            (source_platform, item_id, annotation_version)).
        version: Keep only rows annotated with this ``av_`` version.
        ts_from: Keep only rows with ``inference_ts`` >= this epoch second
            (inclusive).
        ts_to: Keep only rows with ``inference_ts`` < this epoch second
            (exclusive).
        study_item_ids: When given, intersect with this item-id set first.

    Returns:
        A tuple ``(item_ids, skipped_no_inference_ts)`` where the second
        element counts rows that matched the selection scope but were excluded
        from a timeframe filter because they carry no stored ``inference_ts``
        (legacy rows predating the timestamp column, before the backfill).
    """
    if archive_df is None or archive_df.empty:
        return [], 0

    if 'annotated_ok' in archive_df.columns:
        mask = archive_df['annotated_ok'].fillna(False) == True
    else:
        mask = pd.Series(True, index=archive_df.index)

    item_ids_str = archive_df['item_id'].astype(str)
    if study_item_ids is not None:
        mask = mask & item_ids_str.isin(study_item_ids)

    if version:
        mask = mask & (archive_df['annotation_version'] == version)

    skipped_no_ts = 0
    if ts_from is not None or ts_to is not None:
        if 'inference_ts' in archive_df.columns:
            ts = pd.to_numeric(archive_df['inference_ts'], errors='coerce')
            has_ts = ts.notna()
            skipped_no_ts = int((mask & ~has_ts).sum())
            mask = mask & has_ts
            if ts_from is not None:
                mask = mask & (ts >= ts_from)
            if ts_to is not None:
                mask = mask & (ts < ts_to)
        else:
            # Archive predates the inference_ts column entirely.
            skipped_no_ts = int(mask.sum())
            mask = pd.Series(False, index=archive_df.index)

    return sorted(set(item_ids_str[mask].dropna().tolist())), skipped_no_ts


def _parse_selection_date(value: str) -> int:
    """Parse an ISO date/datetime string into epoch seconds (UTC midnight for bare dates)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


@management_bp.route('/api/manage/enrichment/annotation_versions', methods=['GET'])
@permission_required('tab.data_management.annotation')
@login_required
def enrichment_annotation_versions():
    """List annotation versions for the Annotation page's selection dropdown.

    Enrichment-scoped alternative to the admin-gated
    ``/api/manage/annotation-versions``: returns only lightweight summaries
    (no prompt/schema payloads), restricted to versions that actually occur in
    the annotation archive when that snapshot is available.
    """
    try:
        from fyp.annotation import annotation_versioning

        summaries = annotation_versioning.list_versions()
        in_data = annotation_versioning.versions_in_data() or set()
        if in_data:
            known = {s.get("annotation_version") for s in summaries}
            summaries = [s for s in summaries if s.get("annotation_version") in in_data]
            # Archive versions with no registry record (e.g. the legacy
            # pre-versioning id) still need to be selectable.
            for version in sorted(in_data - known):
                summaries.append({"annotation_version": version, "label": version})
        active = annotation_versioning.get_preferred_version()
        return jsonify({"status": "success", "versions": summaries, "active": active})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@management_bp.route('/api/manage/enrichment/calculate_to_annotate', methods=['POST'])
@permission_required('tab.data_management.annotation')
@login_required
def calculate_to_annotate():
    data = request.json or {}
    study_name = data.get("study_name")
    retry_failed = bool(data.get("retry_failed", False))
    selection_mode = data.get("selection_mode") or "study"
    if selection_mode not in ("study", "version", "timeframe"):
        return jsonify({"error": f"Unknown selection_mode '{selection_mode}'"}), 400
    if not study_name:
        return jsonify({"error": "No study name provided"}), 400

    try:
        from fyp.fyp_config import fyp_cf

        # Check for cached recoded dataset first. Every selection mode operates
        # within the target study; the re-annotation modes (version/timeframe)
        # intersect their archive selection with it.
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

        if selection_mode in ("version", "timeframe"):
            return _calculate_to_annotate_reannotation(
                data, selection_mode, df_study, df_status
            )

        # The scraped-and-annotatable predicate lives in one place only — see
        # collection_enrichment.annotation_eligible for why a second copy is
        # dangerous (an unscraped id in this queue is burnt permanently).
        durations = None
        if 'duration' in df_study.columns:
            dur = df_study[['item_id', 'duration']].dropna(subset=['item_id']).copy()
            dur['item_id'] = dur['item_id'].astype(str)
            dur = dur.drop_duplicates(subset=['item_id'])
            durations = dict(zip(dur['item_id'], dur['duration']))
        unannotated_videos = collection_enrichment.annotation_eligible(
            df_study['item_id'].dropna().tolist(),
            df_status,
            durations=durations,
            retry_failed=retry_failed,
            max_duration=fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600),
        )

        unannotated_videos, cap_info = _apply_queue_cap(unannotated_videos, "annotation")
        cost = _annotation_cost_estimate(len(unannotated_videos))

        # Dry run: report what WOULD be queued (count, cap, cost) without
        # touching the queue — the UI uses it for its confirm step.
        if bool(data.get("dry_run", False)):
            return jsonify({
                "status": "success",
                "dry_run": True,
                "would_queue": len(unannotated_videos),
                "cost_estimate": cost,
                **cap_info,
            })

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

        _log_dm("build_annotation_queue", target=study_name,
                details={"newly_queued": len(unannotated_videos), **cap_info})

        return jsonify({
            "status": "success",
            "videos_to_annotate": len(current_queue),
            "newly_queued": len(unannotated_videos),
            "cost_estimate": cost,
            **cap_info,
        })

    except Exception as e:
        print(f"Error calculating annotate targets: {e}")
        return jsonify({"error": str(e)}), 500


def _calculate_to_annotate_reannotation(data, selection_mode, df_study, df_status):
    """Queue already-annotated videos for re-annotation with the active version.

    Selects from the ``{label}_all_versions.parquet`` archive by annotation
    version or by annotation timeframe, always intersected with the target
    study, then keeps only items whose media is still downloaded. The
    ``annotated_ok``/``annotated_fail`` masks of the study path are
    deliberately bypassed — these selections re-annotate on purpose. The
    duration cap is only enforceable when the study frame carries ``duration``
    (the archive/status carry none), which is safe because these items passed
    the cap when first queued.
    """
    from fyp.fyp_config import fyp_cf

    version = None
    ts_from = None
    ts_to = None
    if selection_mode == "version":
        version = data.get("annotation_version")
        if not version:
            return jsonify({"error": "No annotation_version provided"}), 400
    else:
        try:
            if data.get("annotated_from"):
                ts_from = _parse_selection_date(data["annotated_from"])
            if data.get("annotated_to"):
                ts_to = _parse_selection_date(data["annotated_to"])
        except ValueError as e:
            return jsonify({"error": f"Invalid timeframe date: {e}"}), 400
        if ts_from is None and ts_to is None:
            return jsonify({"error": "No timeframe provided (annotated_from / annotated_to)"}), 400

    label = fyp_cf["labels"]["MACHINE_ANNOTATIONS_LABEL"]
    archive_fn = f"{label}_all_versions.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        return jsonify({"error": "No annotation archive found — nothing has been annotated yet."}), 400
    df_archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)

    study_item_ids = None
    if df_study is not None and not df_study.empty:
        study_item_ids = {str(v) for v in df_study['item_id'].dropna().tolist()}

    selected_ids, skipped_no_ts = _select_annotated_item_ids(
        df_archive,
        version=version,
        ts_from=ts_from,
        ts_to=ts_to,
        study_item_ids=study_item_ids,
    )

    # Annotation needs an mp4: drop items whose media is not downloaded
    # (e.g. later purged). Without a status parquet the check is skipped.
    skipped_no_media = 0
    if selected_ids and df_status is not None and not df_status.empty \
            and 'video_downloaded' in df_status.columns:
        status = df_status
        if 'item_id' not in status.columns:
            status = status.reset_index()
            if 'index' in status.columns and 'item_id' not in status.columns:
                status = status.rename(columns={'index': 'item_id'})
        downloaded = status.loc[
            status['video_downloaded'].fillna(False) == True, 'item_id'
        ].astype(str)
        downloaded_set = set(downloaded.tolist())
        before = len(selected_ids)
        selected_ids = [v for v in selected_ids if v in downloaded_set]
        skipped_no_media = before - len(selected_ids)

    # Duration cap — only when a study frame carries duration.
    if selected_ids and df_study is not None and 'duration' in getattr(df_study, 'columns', []):
        max_dur = fyp_cf.get("machine", {}).get("max_duration_for_annotation", 600)
        durations = df_study[['item_id', 'duration']].copy()
        durations['item_id'] = durations['item_id'].astype(str)
        too_long = durations.loc[
            durations['duration'].notna() & (durations['duration'] >= max_dur), 'item_id'
        ]
        too_long_set = set(too_long.tolist())
        selected_ids = [v for v in selected_ids if v not in too_long_set]

    selected_ids, cap_info = _apply_queue_cap(selected_ids, "annotation")
    cost = _annotation_cost_estimate(len(selected_ids))

    # Dry run: report the selection without touching the queue.
    if bool(data.get("dry_run", False)):
        return jsonify({
            "status": "success",
            "dry_run": True,
            "would_queue": len(selected_ids),
            "cost_estimate": cost,
            "skipped_no_media": skipped_no_media,
            "skipped_no_inference_ts": skipped_no_ts,
            **cap_info,
        })

    # Append to the global annotate queue (atomic — never clobbers ids
    # claimed/pruned meanwhile by an annotation worker).
    current_queue = data_io.update_json(
        storage_location="cache",
        filename="to_annotate.json",
        mutate=lambda current: list(
            {str(v) for v in (current if isinstance(current, list) else [])}
            | set(selected_ids)
        ),
        default=[],
    )

    _log_dm("build_annotation_queue", target=f"reannotation:{selection_mode}",
            details={"newly_queued": len(selected_ids), **cap_info})

    return jsonify({
        "status": "success",
        "videos_to_annotate": len(current_queue),
        "selected": len(selected_ids) + skipped_no_media,
        "newly_queued": len(selected_ids),
        "skipped_no_media": skipped_no_media,
        "skipped_no_inference_ts": skipped_no_ts,
        "cost_estimate": cost,
        **cap_info,
    })


@management_bp.route('/api/manage/enrichment/consolidate', methods=['POST'])
@permission_required('tab.data_management.refresh')
@login_required
def api_consolidate_enrichment():
    from fyp.fyp_config import CONSOLIDATE_ENRICHMENT_SCRIPT

    if _is_worker_running("consolidate_enrichment"):
        return jsonify({"status": "error", "message": "Consolidation already running"}), 409

    data = request.json or {}
    force = bool(data.get("force"))
    # The Dataset Assembly page sends both flags explicitly (its two checkboxes).
    # The default is for other callers: refresh unless this is a full rebuild,
    # which stays chain-free by default to keep it debuggable.
    auto_refresh = bool(data.get("auto_refresh", not force))

    blocking = _consolidate_blockers()
    if blocking:
        if force:
            return jsonify({
                "status": "error",
                "message": f"Cannot run a full rebuild while {', '.join(blocking)} running.",
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
    # the whole step list is on screen from the very first poll instead of after
    # the (long) consolidation phase. With auto_refresh the marker carries the
    # FORECAST pipeline — every downstream step, flagged provisional — so the
    # user can see what is queued up behind the consolidation; the worker
    # replaces it with the real plan once the impact tells it which steps are
    # actually needed. A consolidate-only run has no downstream plan to forecast.
    now_iso = datetime.now(UTC).isoformat()
    load_process_stats()
    entry = process_stats.get("consolidate_enrichment", {})
    entry.pop("auto_armed", None)
    entry.pop("auto_armed_force", None)
    entry.pop("auto_armed_auto_refresh", None)
    entry["pipeline_plan"] = {
        "steps": list(PIPELINE_STEPS_ORDER) if auto_refresh else [],
        "started_ts": now_iso,
        "mode": "refresh" if auto_refresh else "consolidate_only",
        "provisional": bool(auto_refresh),
    }
    entry["last_pipeline_partial"] = False
    entry["last_pipeline_failed_at"] = None
    process_stats["consolidate_enrichment"] = entry
    save_process_stats()

    success, msg = start_process("consolidate_enrichment", CONSOLIDATE_ENRICHMENT_SCRIPT,
                                 task_args=task_args if task_args else None,
                                 started_by=_actor())
    if success:
        # start_process resets the in-memory ::DATA:: copy; mirror the marker
        # there too so the local-dev overlay in consolidate_entry_view agrees
        # with process_stats. Subprocess mode only — on Cloud Run that dict is
        # never updated again (the worker runs in the other service), so a
        # marker written here would outlive the real plan and pin this web
        # instance to it.
        mem = processes.get("consolidate_enrichment", {}).get("data")
        if isinstance(mem, dict) and not is_cloud_run():
            mem["pipeline_plan"] = entry["pipeline_plan"]
        _log_dm("consolidate_enrichment", target="force" if force else "normal",
                details={"auto_refresh": auto_refresh})
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
@permission_required('tab.data_management.refresh')
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
@permission_required('tab.data_management.refresh')
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
    from ...services import downstream_refresh

    load_process_stats()
    ps_entry = process_stats.get("consolidate_enrichment", {})
    mem = processes.get("consolidate_enrichment", {}).get("data", {}) or {}
    impact = ps_entry.get("consolidation_impact") or mem.get("consolidation_impact")

    # The shared dispatcher folds in any deferred-refresh debt an enrichment
    # plan has accumulated, so the manual button also settles it.
    status, message = downstream_refresh.dispatch_downstream_refresh(impact)
    if status == "busy":
        return jsonify({"status": "error", "message": message}), 409
    if status == "error":
        return jsonify({"status": "error", "message": message}), 409
    return jsonify({"status": status, "message": message})



@management_bp.route('/api/manage/refresh/staleness', methods=['GET'])
@permission_required('tab.data_management.refresh', 'tab.admin.versions')
@login_required
def api_refresh_staleness():
    """Check which downstream processes are stale relative to the last consolidation impact.

    Also reports ``version_promotion`` staleness — a promoted preferred
    annotation version whose study refresh hasn't run yet (consumed by the
    admin Versions page banner and the Dataset Assembly page).
    """
    promotion = _evaluate_version_promotion_staleness()
    status = _evaluate_consolidation_staleness()
    if not status["has_impact"] and not status.get("impact"):
        return jsonify({"has_impact": False, "version_promotion": promotion})

    return jsonify({
        "has_impact": status["has_impact"],
        "impact": status["impact"],
        "processes": status["processes"],
        "version_promotion": promotion,
    })


