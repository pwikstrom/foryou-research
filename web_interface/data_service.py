"""Facade for the split data services (Phase 7c).

The implementation now lives in web_interface/services/ (study_data,
timeline_service, analysis_data, user_variables). This module remains the
stable import surface for the route modules, workers, scripts and tests;
every historical name — including the cache singletons, whose object
identity is preserved by these re-exports — is importable from here.
"""

from .services.analysis_data import (  # noqa: F401
    _sequence_cache,
    _sequence_cache_lock,
    _sequence_mtime,
    get_pca_df,
    get_sequence_df,
    get_sequence_summary,
    pca_df_cache,
)
from .services.study_data import (  # noqa: F401
    SECTION_ORDER,
    StudyCache,
    _CAT_SCALES,
    _COLLECTION_TAGS_TTL,
    _USER_JSON_TTL,
    _collection_tags_cache,
    _collection_tags_cache_time,
    _enrichment_status,
    _get_recoded_mtime,
    _get_sidecar_mtime,
    _prefetch_user_jsons,
    _read_user_json_uncached,
    _sidecar_cache,
    _sidecar_cache_lock,
    _user_json_cache,
    _user_json_lock,
    enrich_with_user_tags,
    get_collection_tags,
    get_explorer_data,
    get_study_collections,
    get_study_sidecar,
    get_user_json_cached,
    invalidate_collection_tags_cache,
    invalidate_user_json_cache,
    load_display_id_map,
    load_shared_tags,
    make_serializable,
    study_cache,
)
from .services.timeline_service import (  # noqa: F401
    TIMELINE_SCHEMA_VERSION,
    _TIMELINE_REQUIRED_COLUMNS,
    _inject_other_bucket,
    _remap_analysis_indices,
    check_and_update_timeline_cache,
    get_timeline_covered_vars,
    get_timeline_data,
)
from .services.user_variables import (  # noqa: F401
    compose_effective_variables,
    get_accessible_studies,
    load_schema_metadata,
)
