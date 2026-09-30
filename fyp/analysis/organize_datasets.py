"""Build the per-study and per-collection datasets: the entry points.

Loads collections, samples events per study definition, consolidates the
enrichment data (scrapes, annotations, niche map), merges it onto the events,
and writes the recoded study datasets with their refresh sidecars. The steps
live in :mod:`fyp.analysis.datasets`; this module holds the entry points
(``create_study_recoded_dataset``, ``create_collection_unified_dataset``),
video selection and the lazy label constants (``COLLECTIONS_LABEL`` ...), and
re-exports the steps' public names for callers of this path. First-party code
imports them from where they live.
"""

import time as _time

import pandas as pd

import fyp.core.data_io as data_io
from fyp.analysis.datasets import common, loading, merge, refresh

# Re-exported for callers of this module's old surface (see the docstring).
from fyp.analysis.datasets.common import (  # noqa: F401
    SAMPLE_NO_CAP,
    collection_id_column,
    event_type_column,
    parse_sample_threshold,
    timestamp_column,
)
from fyp.analysis.datasets.enrichment_status import (  # noqa: F401
    SHADOW_CHECK_FILENAME,
    consolidate_enrichment_data,
    patch_enrichment_status,
    status_patch_allowed,
    update_enrichment_status,
    verify_consolidation_equivalence,
)
from fyp.analysis.datasets.loading import (  # noqa: F401
    enrichment_preload,
    load_collection_data,
    load_collection_datasets,
    load_study_datasets,
)
from fyp.analysis.datasets.merge import (  # noqa: F401
    apply_enrichment_only_patch,
    new_merge,
)
from fyp.analysis.datasets.refresh import (  # noqa: F401
    build_sidecar,
    compute_failed_scrapes_fingerprint,
    compute_input_fingerprints,
    compute_study_config_hash,
    load_sidecar,
    plan_refresh,
    save_sidecar,
)
from fyp.analysis.datasets.sampling import (  # noqa: F401
    simple_sample_collection_events,
)
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.memory import df_size_mb as _df_size_mb
from fyp.core.memory import peak_rss_mb as _peak_rss_mb
from fyp.core.memory import rss_mb as _rss_mb
from fyp.core.runtime import cf as _cf

logger = get_logger(__name__)


_CONFIG_CONSTANT_ACCESSORS = {
    "SCRAPES_LABEL": common._scrapes_label,
    "MACHINE_ANNOTATIONS_LABEL": common._machine_annotations_label,
    "COLLECTIONS_LABEL": common._collections_label,
}


def __getattr__(name: str):
    """Serve the config-derived module constants lazily (PEP 562)."""
    accessor = _CONFIG_CONSTANT_ACCESSORS.get(name)
    if accessor is not None:
        return accessor()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ============================================================================
# Video selection helpers
# ============================================================================


def _build_agg_dict_to_generate_basic_video_stats(study_dataset: pd.DataFrame = None):
    from pandas import NamedAgg

    agg_defs = {
        "nunique_collections": ("collection_id", "nunique"),
        "total_observations": ("collection_id", "count"),
        "scraped_ok": ("scraped_ok", "first"),
        "scraped_fail": ("scraped_fail", "first"),
        "annotated_ok": ("annotated_ok", "first"),
        "annotated_fail": ("annotated_fail", "first"),
        "duration": ("duration", "max"),
    }

    if study_dataset is None:
        source_cols = list(
            set(["item_id"] + [source_col for _, (source_col, _) in agg_defs.items()])
        )
        return None, list(set(source_cols))

    agg_dict = {}
    confirmed_cols = ["item_id"]
    for target_col, (source_col, agg_func) in agg_defs.items():
        if source_col in study_dataset.columns:
            agg_dict[target_col] = NamedAgg(column=source_col, aggfunc=agg_func)
            confirmed_cols.append(source_col)
    return agg_dict, list(set(confirmed_cols))


def select_videos_from_study_dataset(
    study_dataset: pd.DataFrame = None,
    query_string: str = "",
    verbose: bool = False,
    notebook_mode: bool = False,
) -> pd.DataFrame:
    """Select and aggregate video-level stats from a merged study dataset, then filter by query."""

    if study_dataset is None:
        raise ValueError("study_dataset must be specified")

    agg_dict, confirmed_cols = _build_agg_dict_to_generate_basic_video_stats(study_dataset)

    video_stats = study_dataset[confirmed_cols].groupby("item_id").agg(**agg_dict)

    if "duration" in video_stats.columns:
        video_stats["duration_ok_to_annotate"] = (
            video_stats["duration"] <= _cf()["machine"]["max_duration_for_annotation"]
        ).fillna(False)
        video_stats.drop(columns=["duration"], inplace=True)
    else:
        video_stats["duration_ok_to_annotate"] = False

    video_stats.fillna(False, inplace=True)
    video_stats.query(query_string, inplace=True)

    return video_stats


# ============================================================================
# Entry points — create unified datasets
# ============================================================================


def create_study_recoded_dataset(
    study_name: str = None,
    all_datasets: dict | None = None,
    save_to_cache: bool = True,
    load_from_cache: bool = True,
    enrichment_status: pd.DataFrame | None = None,
    force_full_rebuild: bool = False,
    verbose: bool = False,
) -> pd.DataFrame | None:
    """Generate a unified, merged dataset for a study definition.

    Loads core datasets, applies sampling, merges activity + enrichment data, and caches the result.

    When `save_to_cache=True` and the refresh sidecar reports that no input has
    changed since the cached recoded parquet was written, this function returns
    the cached dataframe without rebuilding. Callers can inspect
    `df.attrs["refresh_action"]` to tell short-circuited loads ("short_circuit")
    from full rebuilds ("full_rebuild"). Pass `force_full_rebuild=True` to
    bypass the sidecar check.
    """
    if all_datasets is None:
        all_datasets = {}

    if study_name is None:
        raise ValueError("study_name must be specified")

    if study_name not in _cf()["study_defs"].keys():
        raise ValueError(f"study_name '{study_name}' not found in config")

    # Sidecar-guided refresh: fingerprint inputs and pick the cheapest correct
    # path. Saves both I/O and CPU for the "user clicked refresh but nothing
    # actually changed" case and for enrichment-only trickle-in updates.
    if save_to_cache and not force_full_rebuild:
        plan = refresh.plan_refresh(study_name, verbose=verbose)
        logger.info(
            f"[REFRESH PLAN] study={study_name} action={plan['action']} "
            f"reasons={'; '.join(plan['reasons']) or 'no sidecar or changed inputs'}"
        )

        if plan["action"] == "short_circuit":
            cached_df = data_io.load_parquet(
                storage_location="cache",
                filename=f"{study_name}_recoded.parquet",
                verbose=verbose,
            )
            if cached_df is not None and not cached_df.empty:
                cached_df.attrs["refresh_action"] = "short_circuit"
                cached_df.attrs["refresh_plan"] = plan
                cached_df.attrs["study_name"] = study_name
                return cached_df
            # Cache surprisingly unreadable/empty — fall through to full rebuild.
            logger.warning(
                "    [Sidecar] Short-circuit aborted: cached parquet unreadable/empty. "
                "Falling through to full rebuild."
            )

        elif plan["action"] == "enrichment_patch":
            patched_df = merge.apply_enrichment_only_patch(study_name=study_name, verbose=verbose)
            if patched_df is not None and not patched_df.empty:
                patched_df.attrs["refresh_plan"] = plan
                return patched_df
            # Patch refused (missing cache, empty merge, etc.) — fall through.
            logger.warning(
                "    [EnrichPatch] Patch path aborted — falling through to full rebuild."
            )

    logger.info(f"Generating unified dataset for study '{study_name}'")

    # Memory baseline before any heavy lifting. We sample RSS at each phase
    # so the single [RECODE][MEM] log line gives enough resolution to tell
    # whether the load, the merge, or something in between dominates peak
    # memory — critical for sizing the Cloud Run task-runner container.
    _rss_start = _rss_mb()
    _peak_start = _peak_rss_mb()

    _t_phase = _time.perf_counter()
    all_datasets = loading.load_study_datasets(
        study_name=study_name,
        all_datasets=all_datasets,
        load_from_cache=load_from_cache,
        enrichment_status=enrichment_status,
        verbose=verbose,
    )
    _t_load = _time.perf_counter() - _t_phase
    _rss_after_load = _rss_mb()

    if all_datasets is None:
        logger.warning(
            f"!!! [Core datasets] No activity data matched the study definition '{study_name}'. Returning None"
        )
        logger.info(
            f"[RECODE][TIMING] study={study_name} load={_t_load:.2f}s merge=0.00s total={_t_load:.2f}s"
        )
        return None

    _t_phase = _time.perf_counter()
    study_recoded_dataset = merge.new_merge(
        study_name=study_name,
        all_datasets=all_datasets,
        save_to_cache=save_to_cache,
        verbose=verbose,
    )
    _t_merge = _time.perf_counter() - _t_phase
    _rss_after_merge = _rss_mb()
    _peak_end = _peak_rss_mb()

    # Preserve the sampling selection-effect report so pre-check UI can surface it.
    sampling_report = None
    if isinstance(all_datasets, dict):
        collections_df = all_datasets.get("collections")
        if collections_df is not None and hasattr(collections_df, "attrs"):
            sampling_report = collections_df.attrs.get("sampling_report")
    if sampling_report and study_recoded_dataset is not None:
        study_recoded_dataset.attrs["sampling_report"] = sampling_report

    # Write the refresh sidecar alongside the recoded parquet so future refresh
    # calls can fingerprint inputs and skip redundant rebuilds. `new_merge`
    # submits the parquet save to a background thread guarded by
    # `data_io.file_lock`; acquire the same lock here to wait for the write to
    # finish before writing the sidecar. Otherwise a subsequent refresh could
    # load the sidecar, trust it, and try to read a half-written parquet.
    if save_to_cache and study_recoded_dataset is not None and not study_recoded_dataset.empty:
        try:
            with data_io.file_lock:
                pass
            refresh.save_sidecar(
                study_name=study_name, recoded_df=study_recoded_dataset, verbose=verbose
            )
        except Exception as exc:
            logger.warning(
                f"    [Sidecar] Non-fatal: failed to write sidecar for '{study_name}': {exc}"
            )

    if study_recoded_dataset is not None:
        study_recoded_dataset.attrs["refresh_action"] = "full_rebuild"

    logger.info(
        f"...done. Unified dataset for study '{study_name}' generated. Total memory used: {_df_size_mb(study_recoded_dataset):.2f} MB"
    )
    logger.info(
        f"[RECODE][TIMING] study={study_name} "
        f"load={_t_load:.2f}s merge={_t_merge:.2f}s "
        f"total={(_t_load + _t_merge):.2f}s"
    )
    # Peak-delta is the max additional RSS claimed by the process during
    # this function relative to when we entered — the number that actually
    # dictates whether the 32 GB task-runner container is enough headroom
    # for this study.
    logger.info(
        f"[RECODE][MEM] study={study_name} "
        f"rss_start={_rss_start:.0f}MB "
        f"rss_after_load={_rss_after_load:.0f}MB "
        f"rss_after_merge={_rss_after_merge:.0f}MB "
        f"peak_during={_peak_end:.0f}MB "
        f"peak_delta=+{(_peak_end - _peak_start):.0f}MB "
        f"df_size={_df_size_mb(study_recoded_dataset):.0f}MB"
    )

    return study_recoded_dataset


def create_collection_unified_dataset(
    collection_id: str = None, verbose: bool = False
) -> pd.DataFrame | None:
    """Generate a unified, merged dataset for a single collection.

    Loads core datasets filtered to collection_id, merges activity + enrichment data.
    Not cached (single-collection datasets are typically one-off).
    """

    if collection_id is None:
        raise ValueError("collection_id must be specified")

    logger.info(f"Generating unified dataset for collection '{collection_id}'")

    all_datasets = loading.load_collection_datasets(
        collection_id=collection_id, load_from_cache=True, verbose=verbose
    )

    if all_datasets is None:
        logger.warning(
            f"!!! [Core datasets] No activity data matched the collection '{collection_id}'. Returning None"
        )
        return None

    collection_dataset = merge.new_merge(
        study_name=None, all_datasets=all_datasets, save_to_cache=False, verbose=verbose
    )

    logger.info(
        f"...done. Unified dataset for collection '{collection_id}' generated. Total memory used: {_df_size_mb(collection_dataset):.2f} MB"
    )

    return collection_dataset
