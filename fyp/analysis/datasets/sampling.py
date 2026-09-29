"""Sampling collection events for a study: day cells and per-cell / per-collection thresholds."""

import numpy as np
import pandas as pd

from fyp.analysis.datasets import common
from fyp.analysis.studies import init_study_defs
from fyp.annotation.recode_variables import (
    get_grouping_factors_from_var_schema,
)
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.runtime import cf as _cf
from fyp.core.utils import VIDEO_VIEW_TYPES

logger = get_logger(__name__)

# ============================================================================
# Sampling
# ============================================================================


def simple_sample_collection_events(
    study_name: str = None,
    all_collections_df: pd.DataFrame = None,
    enrichment_status: pd.DataFrame | None = None,
    verbose: bool = False,
) -> pd.DataFrame:
    """Sample activity events using study-defined grouping factors and thresholds.

    Separates play/non-play events, applies group-size and group-count filters with
    sampling, then recombines.
    """

    def _filter_and_sample(
        df: pd.DataFrame,
        group_cols: list[str],
        x_threshold: int,
        y_samples: int,
        rng: np.random.RandomState,
    ) -> pd.DataFrame:
        """Filters aggregation groups by size and samples rows."""
        group_sizes = df.groupby(group_cols)[group_cols[0]].transform("size")
        df_filtered = df[group_sizes >= x_threshold]

        sampled_indices = df_filtered.groupby(group_cols, group_keys=False).apply(
            lambda g: g.sample(n=min(len(g), y_samples), random_state=rng), include_groups=False
        )
        result = df_filtered.loc[sampled_indices.index]
        return result

    if all_collections_df is None:
        raise ValueError("[Sampling] all_collections_df cannot be None")

    rng = np.random.RandomState(42)
    the_df = all_collections_df

    # The grouping variables are defined in the study config with the prefixes used in the
    # final dataset; the columns have not been given those prefixes yet at this stage, so the
    # prefixes are dropped when matching.

    grouping_factors = get_grouping_factors_from_var_schema(some_events_df=the_df, verbose=False)

    if len(grouping_factors) != 2:
        raise ValueError("!!! [Sampling] Group factors must be exactly 2")

    if common.collection_id_column not in grouping_factors:
        raise ValueError(
            f"!!! [Sampling] Group factors must include '{common.collection_id_column}'"
        )

    # make sure collection_id_column is the first element
    grouping_factors.remove(common.collection_id_column)
    grouping_factors = [common.collection_id_column] + grouping_factors

    if verbose:
        logger.info(f"    [Sampling] Grouping factors: {grouping_factors}")

    if "study_defs" not in _cf():
        init_study_defs()

    _study_def = _cf()["study_defs"][study_name]
    MIN_EVENTS_REQUIRED = common.parse_sample_threshold(
        _study_def.get("MIN_ACTIVITY_COUNT_PER_GROUP"), 30
    )
    MAX_EVENTS_SELECTED = common.parse_sample_threshold(
        _study_def.get("MAX_ACTIVITY_COUNT_PER_GROUP"), 50, uncapped=True
    )
    MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION = common.parse_sample_threshold(
        _study_def.get("MIN_GROUP_COUNT_PER_COLLECTION"), 20
    )
    MAX_GROUP_COUNT_SELECTED_PER_COLLECTION = common.parse_sample_threshold(
        _study_def.get("MAX_GROUP_COUNT_PER_COLLECTION"), 200, uncapped=True
    )

    # Filter to viewing events only (play + observe). Non-viewing activity types
    # are dropped — relevant signal from them is folded into adjacent play rows
    # during ingestion (see ingest.py:1335-1407).
    all_viewing_events_df = the_df[the_df[common.event_type_column].isin(VIDEO_VIEW_TYPES)].copy()
    sample_frame_size = len(all_viewing_events_df)

    if verbose:
        n_dropped = len(the_df) - len(all_viewing_events_df)
        logger.info(
            f"    [Sampling] Viewing events (play+observe): {len(all_viewing_events_df):,}  |  Dropped non-viewing events: {n_dropped:,}"
        )

    if verbose:
        logger.info(
            f"    [Sampling] Dropping aggregation groups with less than {MIN_EVENTS_REQUIRED} events"
        )
        logger.info(
            f"    [Sampling] Sampling at most {MAX_EVENTS_SELECTED} events from each remaining group. This might take a moment..."
        )
    # select agg groups with the required number of events
    viewing_events_within_agg_group_size_limits = _filter_and_sample(
        all_viewing_events_df, grouping_factors, MIN_EVENTS_REQUIRED, MAX_EVENTS_SELECTED, rng
    )
    if verbose:
        sample_size = len(viewing_events_within_agg_group_size_limits)
        if sample_frame_size > 0:
            logger.info(
                f"    [Sampling] Viewing events after sampling: {sample_size:,} ({sample_size / sample_frame_size:.0%} of original)"
            )

    # build a df with unique pairs of the two group factors
    unique_group_factor_pairs = viewing_events_within_agg_group_size_limits[
        grouping_factors
    ].drop_duplicates()

    # Track Stage 2 selection effects for pre-check reporting:
    #   - excluded: collections with fewer than MIN post-Stage-1 cells
    #   - downsampled: collections with more than MAX post-Stage-1 cells (capped to MAX)
    cells_per_collection = unique_group_factor_pairs.groupby(grouping_factors[0]).size()
    n_excluded_collections = int(
        (cells_per_collection < MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION).sum()
    )
    n_downsampled_collections = int(
        (cells_per_collection > MAX_GROUP_COUNT_SELECTED_PER_COLLECTION).sum()
    )

    if verbose:
        logger.info(
            f"    [Sampling] Dropping collections with less than {MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION} aggregation groups within the limits"
        )
        logger.info(
            f"    [Sampling] Sampling at most {MAX_GROUP_COUNT_SELECTED_PER_COLLECTION} aggregation groups from each remaining collection. This might take a moment..."
        )
    # select collections with a required number of groups
    collections_within_group_count_limits = _filter_and_sample(
        unique_group_factor_pairs,
        grouping_factors[:1],
        MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION,
        MAX_GROUP_COUNT_SELECTED_PER_COLLECTION,
        rng,
    )
    if verbose:
        logger.info(
            f"    [Sampling] Aggregation groups remaining after sampling: {len(collections_within_group_count_limits):,}"
        )

    selected_pairs_index = collections_within_group_count_limits.set_index(grouping_factors).index

    # ----------------------------------------------------------------------
    # find the viewing events in the selected groups
    viewing_events_in_candidate_groups = viewing_events_within_agg_group_size_limits.set_index(
        grouping_factors
    )

    # use isin() boolean mask instead of .loc[MultiIndex] to avoid potential reindexing
    viewing_events_in_selected_groups = viewing_events_in_candidate_groups[
        viewing_events_in_candidate_groups.index.isin(selected_pairs_index)
    ].reset_index()
    if verbose:
        sample_size = len(viewing_events_in_selected_groups)
        if sample_frame_size > 0:
            logger.info(
                f"    [Sampling] Viewing events remaining in the sampled aggregation groups: {sample_size:,} ({sample_size / sample_frame_size:.0%} of original)"
            )

    combined = viewing_events_in_selected_groups
    if verbose:
        logger.info(
            f"    [Sampling] Sampled viewing events: {len(combined):,} in {len(combined[grouping_factors].drop_duplicates()):,} groups"
        )
    combined.drop("D_id", axis=1, inplace=True, errors="ignore")

    # Surface selection effects so callers (pre-check) can show them to the user.
    combined.attrs["sampling_report"] = {
        "n_excluded_collections": n_excluded_collections,
        "n_downsampled_collections": n_downsampled_collections,
        "min_cells_per_collection": MIN_GROUP_COUNT_REQUIRED_PER_COLLECTION,
        "max_cells_per_collection": MAX_GROUP_COUNT_SELECTED_PER_COLLECTION,
    }

    # Caller is responsible for passing enrichment_status if the summary is wanted.
    # We deliberately do not reload from GCS here — an earlier version did, which
    # caused a duplicate read of enrichment_status.parquet per study refresh.
    enrichment_status_df = enrichment_status

    combined_deduped = combined.drop_duplicates(subset="item_id", keep="first")[["item_id"]]

    if enrichment_status_df is None:
        logger.info("    [Sampling] No enrichment_status available — skipping enrichment summary")
        logger.info(
            f"    [Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[grouping_factors].drop_duplicates()):,} groups"
        )
        logger.info(f"    [Sampling] - Unique items: {len(combined_deduped):,}")
        return combined

    # Ensure item_id is the index for the merge (callers may pass it as a column)
    if "item_id" in enrichment_status_df.columns:
        enrichment_status_df = enrichment_status_df.set_index("item_id")

    combined_deduped_enrichment_status = pd.merge(
        left=combined_deduped,
        right=enrichment_status_df,
        left_on="item_id",
        right_index=True,
        how="left",
    )

    enrichment_summary = (
        combined_deduped_enrichment_status.select_dtypes(include=["bool"])
        .fillna(False)
        .sum()
        .to_dict()
    )

    mapper = (
        _cf()["var_schema"][["variable_name", "display_name"]]
        .dropna()
        .set_index("variable_name")
        .to_dict()["display_name"]
    )

    logger.info(
        f"    [Sampling] Sampling completed: {combined.shape[0]:,} events in {len(combined[grouping_factors].drop_duplicates()):,} groups"
    )
    logger.info(f"    [Sampling] - Unique items: {len(combined_deduped_enrichment_status):,}")
    for k in enrichment_summary:
        if len(combined_deduped_enrichment_status) > 0:
            logger.info(
                f"    [Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} ({enrichment_summary[k] / len(combined_deduped_enrichment_status):.0%})"
            )
        else:
            logger.info(f"    [Sampling] - {mapper.get(k, k)}: {enrichment_summary[k]:,} (N/A)")

    return combined
