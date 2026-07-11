"""Import shim for the split management routes (Phase 7b).

The 63 management endpoints now live in ``web_interface/routes/management/``
(one submodule per domain, all registering on the same ``management_bp``), and
the non-route helpers live in ``web_interface/services/``. This module remains
the stable import surface: ``fyp_data_hub``, other route modules, the ``run_*``
workers and the tests keep importing every historical name from here.
"""

from ..services.preview_cache import (  # noqa: F401
    _PREVIEW_CACHE_TTL_S,
    _build_lock_for,
    _cache_frame_in_memory,
    _collections_hash,
    _event_window_mask,
    _get_enrichment_status_cached,
    _get_prepared_frame_cached,
    _load_collections_window,
    _load_enrichment_status_min,
    _load_prepared_from_disk,
    _load_study_raw_window,
    _prepare_preview_frame,
    _prewarm_preview_frame,
    _preview_build_locks,
    _preview_cache_lock,
    _preview_frame_cache,
    _preview_frame_filename,
    _preview_frame_key,
    _preview_sources_mtime,
    _preview_status_cache,
    _preview_warming,
    _prune_disk_frames,
    _read_cached_frame,
    _save_prepared_to_disk,
)
from ..services.stats_service import (  # noqa: F401
    LARGE_STUDY_THRESHOLD,
    SPARSE_CELL_MIN_ACTIVITIES,
    _calculate_stats,
    _compute_universe_enrichment,
    _daily_counts,
    _derive_study_issues,
    _estimate_from_prepared,
    _evaluate_consolidation_staleness,
    _filter_to_event_windows,
    _filter_to_play_observe,
    _load_collection_event_windows,
    _universe_from_prepared,
)
from ..services.worker_status import (  # noqa: F401
    PIPELINE_STEPS_ORDER,
    _actor,
    _build_pipeline_step_view,
    _cached_cookie_health,
    _is_worker_running,
    _workers_blocking_consolidate,
)
from .management import management_bp  # noqa: F401
from .management.ab_eval import (  # noqa: F401
    activate_ab_candidate,
    activate_ab_eval_set,
    create_ab_eval_set,
    delete_ab_candidate,
    delete_ab_eval_run,
    delete_ab_eval_set,
    estimate_ab_eval,
    get_ab_candidate,
    get_ab_eval_run,
    get_ab_eval_run_rows,
    get_ab_eval_set,
    list_ab_candidates,
    list_ab_eval_runs,
    list_ab_eval_sets,
    rename_ab_eval_set,
    sample_ab_eval_set,
    save_ab_candidate,
    save_ab_eval_set,
    start_ab_eval_run,
)
from .management.collections import (  # noqa: F401
    _affected_studies_for_collection,
    _find_raw_file_locations,
    affected_studies_for_collection,
    delete_collection,
    list_collections,
    save_collection_annotation,
)
from .management.contracts import (  # noqa: F401
    _annotation_contract_impact,
    activate_annotation_version,
    download_annotation_contract,
    get_annotation_contract,
    get_annotation_contract_parsed,
    get_annotation_version,
    list_annotation_versions,
    preview_annotation_contract,
    revert_annotation_contract,
    upload_annotation_contract,
)
from .management.enrichment import (  # noqa: F401
    api_consolidate_disarm,
    api_consolidate_enrichment,
    api_refresh_downstream,
    api_refresh_staleness,
    calculate_to_annotate,
    calculate_to_scrape,
    empty_enrichment_queue,
    get_enrichment_stats,
    queue_voted_videos,
)
from .management.ingestion import (  # noqa: F401
    _prepopulate_annotations,
    clear_pending_uploads,
    fetch_aio_data,
    get_ingestion_metadata,
    get_ingestion_sources,
    refresh_collection_metadata,
    refresh_ingestion_collection,
    structure_approve,
    structure_reject,
    structure_warnings,
    unskip_ingestion_ledger_entry,
    upload_ingestion_file,
)
from .management.schema import (  # noqa: F401
    _contract_locked_map,
    _df_to_records,
    _var_schema_admin_enabled,
    get_schema,
    save_presentation_endpoint,
    save_schema_endpoint,
    validate_schema_endpoint,
)
from .management.studies import (  # noqa: F401
    calculate_study_stats,
    daily_activities,
    delete_study,
    list_studies,
    prewarm_study_check,
    save_study,
    set_study_annotation_version,
)
