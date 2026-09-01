"""Dispatch the downstream refresh pipeline from a consolidation impact.

One shared implementation behind every way the pipeline can start outside a
consolidation's own auto-refresh chain: the "Refresh All Affected" button and
the enrichment supervisor's deferred finalize. Both need the same sequence —
build the impact-scoped pipeline, record the plan and the in-flight flag in
process stats, dispatch (Cloud Tasks chain on Cloud Run, background thread
locally), and roll back the flag when the dispatch fails.

This module also owns the *deferred impact* ledger entry: while an enrichment
plan runs, mid-plan consolidations skip the downstream chain and accumulate
their impact here (``__meta__.deferred_impact`` in the enrichment ledger, via
``collection_enrichment.set_meta``); any successful dispatch settles that debt
and stamps ``__meta__.last_full_refresh``.
"""

from datetime import UTC, datetime

from fyp.logging_setup import get_logger

logger = get_logger(__name__)

DEFERRED_IMPACT_KEY = "deferred_impact"
LAST_FULL_REFRESH_KEY = "last_full_refresh"


def impact_union(a: dict | None, b: dict | None) -> dict | None:
    """Union two consolidation-impact payloads.

    Affected study/collection lists are set-unioned (order preserved,
    first-seen wins), the new-annotation count is summed — it drives only the
    "should embeddings top up" decision, so over-counting is harmless while
    under-counting would skip the top-up.

    Returns:
        The merged impact, or None when both inputs are empty.
    """
    if not a:
        return dict(b) if b else None
    if not b:
        return dict(a)

    def _merge_list(key):
        seen = list(dict.fromkeys(
            [str(x) for x in (a.get(key) or [])] + [str(x) for x in (b.get(key) or [])]))
        return seen

    return {
        "affected_study_names": _merge_list("affected_study_names"),
        "affected_collection_ids": _merge_list("affected_collection_ids"),
        "new_annotation_item_count": int(a.get("new_annotation_item_count") or 0)
                                     + int(b.get("new_annotation_item_count") or 0),
    }


def get_deferred_impact() -> dict | None:
    """The accumulated impact of consolidations whose refresh was deferred."""
    from web_interface.services import collection_enrichment as ce
    value = ce.get_meta(DEFERRED_IMPACT_KEY)
    return value if isinstance(value, dict) else None


def accumulate_deferred_impact(impact: dict | None) -> None:
    """Fold one core-only consolidation's impact into the deferred ledger entry.

    Called by the consolidation worker when it ran with ``auto_refresh=False``
    and produced a non-empty impact. ``deferred_since`` marks the first
    deferral (it feeds the finalize backstop), ``runs`` counts how many
    consolidations the debt spans — display only.
    """
    if not impact:
        return
    from web_interface.services import collection_enrichment as ce
    current = get_deferred_impact()
    merged = impact_union(current, impact) or {}
    merged["deferred_since"] = (current or {}).get("deferred_since") \
        or datetime.now(UTC).isoformat()
    merged["runs"] = int((current or {}).get("runs") or 0) + 1
    ce.set_meta(DEFERRED_IMPACT_KEY, merged)


def settle_deferred_impact() -> None:
    """Clear the deferred debt and stamp the full-refresh time.

    Only ever called after a downstream pipeline was SUCCESSFULLY dispatched
    (or chained by an auto-refresh consolidation) covering the deferred scope —
    clearing on a failed dispatch would silently lose the debt.
    """
    from web_interface.services import collection_enrichment as ce
    ce.set_meta(DEFERRED_IMPACT_KEY, None)
    ce.set_meta(LAST_FULL_REFRESH_KEY, datetime.now(UTC).isoformat())


def last_full_refresh() -> str | None:
    """ISO stamp of the last downstream-pipeline dispatch, or None."""
    from web_interface.services import collection_enrichment as ce
    value = ce.get_meta(LAST_FULL_REFRESH_KEY)
    return str(value) if value else None


def dispatch_downstream_refresh(impact: dict | None = None, *,
                                include_deferred: bool = True) -> tuple[str, str]:
    """Build and start the downstream pipeline for an impact.

    Args:
        impact: Impact to refresh (e.g. the stored ``consolidation_impact``).
        include_deferred: Fold the accumulated deferred impact into the scope
            (and settle it on success). The supervisor's finalize passes only
            the deferred debt (impact=None); the manual button passes the
            stored impact and settles the debt along the way.

    Returns:
        ``(status, message)`` with status one of ``"started"``, ``"noop"``
        (nothing to refresh), ``"busy"`` (a pipeline or consolidation is
        already running), ``"error"`` (dispatch failed; state rolled back).
    """
    from web_interface.process_manager import (
        load_process_stats, process_stats, processes, save_process_stats,
    )
    from web_interface.run_consolidate_enrichment import (
        _build_downstream_pipeline, build_pipeline_chain,
    )
    from web_interface.services.worker_status import (
        PIPELINE_STEPS_ORDER, _is_worker_running,
    )
    from web_interface.task_status import is_cloud_run

    effective = impact_union(get_deferred_impact(), impact) if include_deferred \
        else (dict(impact) if impact else None)
    if not effective:
        return "noop", "No consolidation impact to refresh."

    load_process_stats()
    ps_entry = process_stats.get("consolidate_enrichment", {})
    if ps_entry.get("pipeline_in_flight") or any(
        _is_worker_running(n) for n in (["consolidate_enrichment"] + PIPELINE_STEPS_ORDER)
    ):
        return "busy", "A refresh pipeline is already running."

    pipeline = _build_downstream_pipeline(effective)
    if not pipeline:
        return "noop", "Nothing to refresh."

    now_iso = datetime.now(UTC).isoformat()
    ps_entry["pipeline_plan"] = {"steps": [p["task"] for p in pipeline], "started_ts": now_iso}
    ps_entry["last_pipeline_partial"] = False
    ps_entry["last_pipeline_failed_at"] = None
    ps_entry["last_pipeline_summary"] = "Pipeline in progress — refreshing caches..."
    ps_entry["last_pipeline_summary_ts"] = now_iso
    ps_entry["pipeline_in_flight"] = True
    process_stats["consolidate_enrichment"] = ps_entry
    save_process_stats()

    # Local dev: the consolidate worker's last ::DATA:: emission lingers in
    # processes[...]["data"] and would shadow the fresh plan in the stats
    # overlay; mirror the plan so both stores agree (no-op on Cloud Run).
    mem = processes.get("consolidate_enrichment", {}).get("data")
    if isinstance(mem, dict) and not is_cloud_run():
        mem["pipeline_plan"] = ps_entry["pipeline_plan"]
        mem["last_pipeline_partial"] = False
        mem["last_pipeline_failed_at"] = None

    if is_cloud_run():
        from web_interface.process_manager import _dispatch_cloud_task, dispatch_deadline_for
        chain = build_pipeline_chain(pipeline)
        success, msg = _dispatch_cloud_task(
            chain["next_task"], chain["next_task_args"],
            dispatch_deadline_seconds=dispatch_deadline_for(
                chain["next_task"], chain["next_task_args"]))
        if not success:
            # Roll back the in-flight flag so the UI doesn't hang; the deferred
            # debt is deliberately left in place for a later retry.
            load_process_stats()
            entry = process_stats.get("consolidate_enrichment", {})
            entry.pop("pipeline_in_flight", None)
            process_stats["consolidate_enrichment"] = entry
            save_process_stats()
            return "error", f"Dispatch failed: {msg}"
    else:
        import threading

        from web_interface.process_manager import _run_local_downstream_pipeline
        threading.Thread(
            target=_run_local_downstream_pipeline, args=(effective,), daemon=True
        ).start()

    if include_deferred:
        settle_deferred_impact()
    return "started", "Downstream refresh started."
