"""Merging a study's datasets into its recoded frame.

``new_merge`` joins activity, scrapes, annotations and the niche map into
one recoded dataset with its calculated columns; the enrichment-only patch
refreshes just those columns when only enrichment changed.
"""

import datetime as _dt
import time as _time

import pandas as pd

import fyp.annotation.annotation_versioning as annotation_versioning
import fyp.core.data_io as data_io
from fyp.analysis.datasets import common, enrichment_status, loading, refresh
from fyp.analysis.studies import init_study_defs
from fyp.annotation.recode_variables import (
    derive_australian_relevance,
)
from fyp.core.activity_vocabulary import parse_extra_data_tokens
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.memory import df_size_mb as _df_size_mb
from fyp.core.memory import peak_rss_mb as _peak_rss_mb
from fyp.core.memory import rss_mb as _rss_mb
from fyp.core.polars_ops import fast_join
from fyp.core.runtime import cf as _cf
from fyp.scrape.failures import load_failed_scrapes

logger = get_logger(__name__)

# ============================================================================
# Merging datasets
# ============================================================================


def _join_niche_columns(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """Left-join the embeddings-derived niche columns onto a study dataframe.

    Adds four columns from ``video_map.parquet``, keyed on ``item_id``, so the
    embedding geometry surfaces as ordinary analysis variables: ``niche_name``
    (readable categorical) with its integer ``niche`` id, plus the two numeric
    measures ``typicality_pct`` (how mainstream the video is within the whole
    corpus) and ``niche_isolation_pct`` (how far the video's micro-genre sits
    from its nearest neighbouring genre).

    Videos absent from the map (not yet embedded/clustered) get ``"unmapped"``
    and nulls. Note the asymmetry that creates downstream: the null numerics are
    dropped row-wise by the PCA/correlations build, so an incomplete map costs
    those rows their place in *every* correlation, not just these two variables
    — which is why the coverage shortfall is logged rather than left silent.
    Idempotent: existing niche columns are dropped first so a re-merge after a
    map rebuild refreshes them cleanly, and any column the map does not provide
    (an older map file predates the numerics) is backfilled.

    Args:
        df: Merged study dataframe; a no-op when it lacks ``item_id``.
        verbose: Print join diagnostics.

    Returns:
        The dataframe with the niche columns present.
    """
    if "item_id" not in df.columns:
        return df

    df = df.drop(columns=[c for c in common._NICHE_COLUMNS if c in df.columns], errors="ignore")

    available: set[str] = set()
    if data_io.exists(storage_location=common._VIDEO_MAP_LOCATION, filename=common._VIDEO_MAP_FILE):
        available = set(
            data_io.get_parquet_columns(
                storage_location=common._VIDEO_MAP_LOCATION, filename=common._VIDEO_MAP_FILE
            )
            or []
        )

    join_cols = [c for c in common._NICHE_COLUMNS if c in available]
    if "item_id" in available and join_cols:
        niche_map = data_io.load_parquet_selective(
            storage_location=common._VIDEO_MAP_LOCATION,
            filename=common._VIDEO_MAP_FILE,
            columns=["item_id", *join_cols],
        )
        niche_map["item_id"] = niche_map["item_id"].astype("string[pyarrow]")
        # A duplicated map item_id would silently row-duplicate every play
        # matching it in the left join.
        niche_map = niche_map.drop_duplicates("item_id", keep="last")
        df["item_id"] = df["item_id"].astype("string[pyarrow]")
        df = fast_join(df, niche_map, on="item_id", how="left")

    n = len(df)
    for col in common._NICHE_COLUMNS:
        dtype, fill = common._NICHE_COLUMN_BACKFILL[col]
        if col not in df.columns:
            df[col] = pd.array([fill] * n, dtype=dtype)
        elif fill is not pd.NA:
            df[col] = df[col].astype(dtype).fillna(fill)

    mapped = int((df["niche_name"] != common._NICHE_UNMAPPED).sum())
    if verbose:
        logger.info(f"  Joined niche columns: {mapped:,}/{n:,} rows mapped to a niche")
    if n and mapped < n:
        # Unmapped rows carry null typicality/isolation, and the PCA build drops
        # any row with a null feature — so this shortfall silently shrinks the
        # correlations frame for every variable. Run an embeddings + video-map
        # refresh to close it.
        logger.warning(
            f"  {n - mapped:,}/{n:,} rows ({100 * (n - mapped) / n:.1f}%) are not in the "
            "video map, so they carry no typicality/isolation and will be dropped from "
            "the correlations frame. Refresh embeddings + the video map to close the gap."
        )
    return df


def _annotations_for_study(study_name, annotations_df):
    """Return the annotations a study should merge against.

    If the study pins a specific ``annotation_version`` in its definition, the
    pinned version's rows are read from the version archive (strict, for
    reproducibility). Otherwise the supplied active annotations are used
    unchanged.

    Args:
        study_name: The study being merged, or ``None``.
        annotations_df: The active annotations frame loaded for the merge.

    Returns:
        The annotations frame to merge against.
    """
    if not study_name:
        return annotations_df
    study_def = _cf().get("study_defs", {}).get(study_name, {}) or {}
    pin = study_def.get("annotation_version")
    if not pin:
        return annotations_df
    archive_fn = f"{common._machine_annotations_label()}_all_versions.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        logger.warning(
            f"    [new_merge] study '{study_name}' pinned to {pin} but archive missing; using active annotations."
        )
        return annotations_df
    archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if archive is None or archive.empty:
        return annotations_df
    pinned = annotation_versioning.select_version_view(archive, pin)
    logger.info(
        f"    [new_merge] study '{study_name}' pinned to annotation_version={pin}: {len(pinned):,} annotations."
    )
    return pinned


def _add_merge_calculated_columns(merged: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """Add the merge-derived columns declared in ``config/derived_contract.toml``.

    days_since_created / plays_per_day / scraped_fail / completion_rate plus the
    behavioral derivations engaged / rewatched / is_weekend.

    Each column guards on its input columns, so a study frame with no scrape
    enrichment (item-metadata like create_time / duration / play_count is
    absent) receives NA/False defaults rather than being skipped — the merged
    dataset then always carries these derived columns regardless of enrichment.

    Args:
        merged: The merged (or activity-only) study frame.
        verbose: When True, print the columns added and the resulting shape.

    Returns:
        The frame with the four calculated columns present.
    """

    def _safe_vector_divide(x, y):
        return x / y.clip(lower=1).mask(x.isna() | y.isna(), pd.NA)

    # 1. days since created (activity-local time minus upload time)
    calc_col = ["days_since_created"]
    if "local_timestamp" in merged.columns and "create_time" in merged.columns:
        merged[calc_col[-1]] = merged["local_timestamp"] - merged["create_time"]
        merged[calc_col[-1]] = (
            merged[calc_col[-1]]
            .map(lambda x: x.days if x is not pd.NA else pd.NA)
            .astype("int64[pyarrow]")
        )
        merged[calc_col[-1]] = merged[calc_col[-1]].clip(lower=0)
    else:
        merged[calc_col[-1]] = pd.Series(pd.NA, index=merged.index, dtype="int64[pyarrow]")

    # 2. plays per day — produced at scrape time (BaseScraper.derive_plays_per_day,
    # using scrape_ts); fall back to an activity-time estimate only for rows that
    # lack it (e.g. items merged in without scrape enrichment).
    calc_col += ["plays_per_day"]
    if "plays_per_day" not in merged.columns:
        merged["plays_per_day"] = pd.Series(pd.NA, index=merged.index, dtype="double[pyarrow]")
    need_ppd = merged["plays_per_day"].isna()
    if (
        need_ppd.any()
        and "play_count" in merged.columns
        and "days_since_created" in merged.columns
        and not merged["days_since_created"].isna().all()
    ):
        # Mask the -1 missing-count sentinel first, or the fallback goes negative
        # (e.g. Instagram, whose view count is never available). Zero is a real
        # value (0 plays/day) and is kept. Mirrors derive_plays_per_day.
        plays = merged["play_count"].astype("double[pyarrow]").mask(merged["play_count"] < 0, pd.NA)
        fallback = _safe_vector_divide(plays, merged["days_since_created"])
        merged.loc[need_ppd, "plays_per_day"] = fallback[need_ppd]

    # 3. scraped fail
    failed_scrapes = set(load_failed_scrapes(verbose=verbose))
    calc_col += ["scraped_fail"]
    merged[calc_col[-1]] = merged["item_id"].isin(failed_scrapes).astype("bool[pyarrow]")

    # 4. completion rate
    calc_col += ["completion_rate"]
    if "play_duration" in merged.columns and "duration" in merged.columns:
        merged[calc_col[-1]] = merged["play_duration"] / merged["duration"]
        merged[calc_col[-1]] = merged[calc_col[-1]].clip(lower=0, upper=1).astype("double[pyarrow]")
    else:
        merged[calc_col[-1]] = pd.Series(pd.NA, index=merged.index, dtype="double[pyarrow]")

    # 5. engaged — this play carries any of the account's own engagement
    # activity (the fave/comment/share/save/follow tokens folded into
    # extra_data at ingest). Group mean = the collection's own engagement rate.
    calc_col += ["engaged"]
    if "extra_data" in merged.columns:
        merged[calc_col[-1]] = (
            merged["extra_data"]
            .map(lambda s: 1.0 if parse_extra_data_tokens(s) else 0.0)
            .astype("double[pyarrow]")
        )
    else:
        merged[calc_col[-1]] = pd.Series(pd.NA, index=merged.index, dtype="double[pyarrow]")

    # 6. rewatched — played longer than the item lasts (looped/rewatched); the
    # signal completion_rate's clip at 1.0 discards. NA where either side is NA.
    calc_col += ["rewatched"]
    if "play_duration" in merged.columns and "duration" in merged.columns:
        merged[calc_col[-1]] = (
            (merged["play_duration"] > merged["duration"])
            .astype("double[pyarrow]")
            .mask(merged["play_duration"].isna() | merged["duration"].isna(), pd.NA)
        )
    else:
        merged[calc_col[-1]] = pd.Series(pd.NA, index=merged.index, dtype="double[pyarrow]")

    # 7. is_weekend — two-level factor from the ingest-derived local weekday.
    calc_col += ["is_weekend"]
    if "local_weekday" in merged.columns:
        weekday = merged["local_weekday"].astype("string[pyarrow]").str.lower()
        merged[calc_col[-1]] = (
            weekday.isin(["saturday", "sunday"])
            .map({True: "weekend", False: "weekday"})
            .astype("string[pyarrow]")
            .mask(weekday.isna(), pd.NA)
        )
    else:
        merged[calc_col[-1]] = pd.Series(pd.NA, index=merged.index, dtype="string[pyarrow]")

    if verbose:
        logger.info(f"Adding columns: {calc_col}. Resulting output log DF shape {merged.shape}")
    return merged


def _ensure_enrichment_status_columns(merged: pd.DataFrame) -> pd.DataFrame:
    """Guarantee the per-item enrichment status flags exist, defaulting to False.

    ``scraped_ok`` / ``annotated_ok`` / ``annotated_fail`` / ``video_downloaded``
    normally arrive from the scrape/annotation merge. When a study has no such
    enrichment yet (e.g. a freshly ingested platform before its scraper runs),
    defaulting them lets the explore / video-analysis tabs — which gate on these
    flags — render a clean empty result (nothing scraped yet) instead of erroring
    on a missing column. When enrichment later lands, the incremental-refresh
    patch drops these (they live in the scrape/annotation schema) and re-merges
    the real values, so the defaults never mask true enrichment.

    Args:
        merged: The merged (or activity-only) study frame.

    Returns:
        The frame with the four status flags present.
    """
    for col in ("scraped_ok", "annotated_ok", "annotated_fail", "video_downloaded"):
        if col not in merged.columns:
            merged[col] = pd.Series(False, index=merged.index, dtype="bool[pyarrow]")
    return merged


def new_merge(
    study_name: str = None,
    all_datasets: dict = {},
    verbose: bool = False,
    save_to_cache: bool = True,
) -> pd.DataFrame:
    """Merge activity data with scrape + annotation data, add calculated columns, and optionally cache."""

    logger.info("Merging all datasets...")

    if study_name is None and save_to_cache:
        raise ValueError("study_name must be specified")

    if "study_defs" not in _cf():
        init_study_defs()

    if study_name not in _cf()["study_defs"].keys() and save_to_cache:
        raise ValueError(f"study_name '{study_name}' not found in config")

    if all_datasets is None:
        raise ValueError("all_datasets must be specified")

    for k in all_datasets:
        if all_datasets[k] is None:
            logger.info(f"all_datasets['{k}'] is None")

    # merge scrape + annotations into enrichment data
    scrapes_df = all_datasets.get(common._scrapes_label())
    annotations_df = _annotations_for_study(
        study_name, all_datasets.get(common._machine_annotations_label())
    )
    has_scrapes = scrapes_df is not None and not scrapes_df.empty
    has_annotations = annotations_df is not None and not annotations_df.empty

    if has_scrapes and has_annotations:
        # Composite key whenever both sides carry the platform — annotation rows
        # are stamped with source_platform at annotation time (legacy rows are
        # backfilled at consolidation). A pre-backfill annotations frame falls
        # back to item_id and inherits the scrape side's source_platform.
        if "source_platform" in scrapes_df.columns and "source_platform" in annotations_df.columns:
            annotation_join_key = ["source_platform", "item_id"]
        else:
            annotation_join_key = "item_id"
        enriched_data = pd.merge(
            left=scrapes_df, right=annotations_df, on=annotation_join_key, how="left"
        )
    elif has_scrapes:
        enriched_data = scrapes_df
    elif has_annotations:
        enriched_data = annotations_df
    else:
        enriched_data = pd.DataFrame()

    if all_datasets.get(common._collections_label()) is not None:
        activity_data = all_datasets[common._collections_label()]
    else:
        activity_data = pd.DataFrame()

    if len(activity_data) == 0:
        logger.info("No activity data")
        return enriched_data

    if "source_platform" in activity_data.columns and activity_data["source_platform"].isna().any():
        # Pre-column activity rows carry NA and would match no enrichment under
        # the composite key below (and leave holes in the Platform factor) —
        # backfill on a copy (activity_data is a reference into all_datasets).
        activity_data = activity_data.copy()
        activity_data["source_platform"] = enrichment_status._backfill_source_platform(
            activity_data["source_platform"]
        )

    if len(enriched_data) == 0:
        logger.info(
            "No enriched data — caching activity-only dataset (no scrape/annotation enrichment yet)"
        )
        merged = activity_data.copy()
    else:
        # Biggest join in the pipeline: events × item-metadata. Composite key
        # (source_platform, item_id) whenever both sides carry the platform —
        # item ids are only guaranteed unique within a platform. Polars'
        # parallel hash join is substantially faster and more memory-efficient
        # than pandas at events-scale (tens of millions of rows).
        if (
            "source_platform" in activity_data.columns
            and "source_platform" in enriched_data.columns
        ):
            join_key = ["source_platform", "item_id"]
        else:
            join_key = "item_id"
            logger.warning(
                "WARNING: source_platform missing on one side of the activity/enrichment join — falling back to item_id only"
            )
        merged = fast_join(activity_data, enriched_data, on=join_key, how="left")

    # Release the join inputs before the calculated-column work. Peak RSS on the
    # big merge was ~3x the final frame because the sources stayed alive in
    # `all_datasets` (the caller's dict) while the result was being built, so
    # dropping the local names alone frees nothing — the dict entries have to go
    # too. The collections entry is swapped for an empty frame that keeps
    # `.attrs`: `create_study_recoded_dataset` reads its `sampling_report` after
    # this function returns.
    _collections_key = common._collections_label()
    _collections_src = all_datasets.get(_collections_key)
    if _collections_src is not None and hasattr(_collections_src, "attrs"):
        _preserved = pd.DataFrame()
        _preserved.attrs = dict(_collections_src.attrs)
        all_datasets[_collections_key] = _preserved
    for _key in (common._scrapes_label(), common._machine_annotations_label()):
        if _key in all_datasets:
            all_datasets[_key] = None
    del activity_data, enriched_data, _collections_src

    # Calculated + enrichment-status columns run for BOTH branches so a study
    # with no scrape/annotation enrichment yet (e.g. a freshly ingested platform
    # before its scraper exists) still carries the columns the explore /
    # video-analysis / timeline tabs expect. Each column defaults to NA/False
    # and is populated once enrichment lands.
    merged = _add_merge_calculated_columns(merged, verbose=verbose)
    merged = _ensure_enrichment_status_columns(merged)
    # --------------------------------------------------------------------------------------------------

    # Backfill australian_relevance from primary_country for rows annotated under
    # the generalized contract (primary_country replaced it); older-version rows
    # keep their model-output value. No-op when primary_country is absent.
    merged = derive_australian_relevance(merged)

    # Join the embeddings-derived niche columns (item_id-keyed) so they surface
    # as ordinary analysis variables in the explore / timeline / correlation
    # tabs. Runs for both the merge and activity-only branches.
    merged = _join_niche_columns(merged, verbose=verbose)

    # Row order is the product here; the index labels are whatever the last
    # upstream operation happened to leave behind, and some of those paths leave
    # a float index that is mostly NaN. That index gets written into the recoded
    # parquet and read straight back out, where the web layer treats a row's
    # label as its identity (Video Analysis names the row behind the video on
    # screen with it). Normalise it once here so what lands on disk is a clean
    # 0..n-1 and no reader inherits an ambiguous or non-serialisable label.
    merged = merged.reset_index(drop=True)

    if save_to_cache:
        t1 = _dt.datetime.now()
        if verbose:
            logger.info(f"  Saving the '{study_name}' dataset to cache...")
        merged.attrs["study_name"] = study_name
        data_io.save_parquet(
            df=merged,
            storage_location="cache",
            filename=f"{study_name}_recoded.parquet",
            asyncronous=True,
            verbose=verbose,
        )
        if verbose:
            logger.info(
                f"  ...done. Time taken to save datasets to cache: {(_dt.datetime.now() - t1).total_seconds():.1f} seconds"
            )

    logger.info(f"...done. Merged all datasets. Shape: {merged.shape}")

    return merged


# ============================================================================
# Incremental refresh — enrichment-only patch
# ============================================================================


# Columns computed by new_merge() *after* the enrichment merge. Must be dropped
# from the cached recoded dataset before re-merging, otherwise new_merge would
# produce _x/_y suffixed duplicates.
_CALCULATED_ENRICHMENT_COLUMNS = {
    "days_since_created",
    "plays_per_day",
    "scraped_fail",
    "completion_rate",
    "engaged",
    "rewatched",
    "is_weekend",
    # Niche columns are re-joined by new_merge() via _join_niche_columns(), so
    # drop the cached copies before re-merging to avoid _x/_y suffixing.
    *common._NICHE_COLUMNS,
}


def apply_enrichment_only_patch(
    study_name: str,
    verbose: bool = False,
) -> pd.DataFrame | None:
    """Re-merge fresh enrichment onto the cached activity rows of a study.

    Intended for the case where `plan_refresh` reports that only scrapes /
    annotations / failed_scrapes changed. Skips the (expensive) collections
    load and sampling entirely: reads the existing `{study}_recoded.parquet`,
    drops enrichment + calculated columns, re-loads scrapes + annotations
    filtered to the cached item_id set, then calls `new_merge` to rebuild the
    merged dataset. Writes both the new parquet and a fresh sidecar.

    Returns the new dataframe on success with ``attrs["refresh_action"] =
    "enrichment_patch"``. Returns None when the cached dataset is missing or
    unreadable so the caller can fall through to a full rebuild.
    """

    cache_filename = f"{study_name}_recoded.parquet"
    _t0 = _time.perf_counter()
    _rss_start = _rss_mb()
    _peak_start = _peak_rss_mb()
    cached_df = data_io.load_parquet(
        storage_location="cache", filename=cache_filename, verbose=verbose
    )
    if cached_df is None or cached_df.empty:
        logger.warning(
            f"    [EnrichPatch] Cached '{cache_filename}' missing/empty — aborting patch"
        )
        return None

    if "item_id" not in cached_df.columns:
        logger.warning("    [EnrichPatch] Cached dataset missing 'item_id' column — aborting patch")
        return None

    scrape_filename = f"{common._scrapes_label()}_recoded.parquet"
    annot_filename = f"{common._machine_annotations_label()}_recoded.parquet"
    scrape_schema_cols = set(
        data_io.get_parquet_columns(storage_location="recoded", filename=scrape_filename) or []
    )
    annot_schema_cols = set(
        data_io.get_parquet_columns(storage_location="recoded", filename=annot_filename) or []
    )

    # Columns we will recompute in new_merge(): anything sourced from scrapes or
    # annotations, plus the four calculated columns. item_id and source_platform
    # stay — they form the composite join key and also live in the activity
    # data (dropping source_platform here degraded the merge to item_id-only
    # and left unscraped rows with an NA platform).
    enrichment_and_calc = (
        scrape_schema_cols | annot_schema_cols | _CALCULATED_ENRICHMENT_COLUMNS
    ) - {"item_id", "source_platform"}
    activity_cols = [c for c in cached_df.columns if c not in enrichment_and_calc]

    activity_df = cached_df[activity_cols].copy()
    unique_videos = set(activity_df["item_id"].dropna().astype(str).unique().tolist())
    logger.info(
        f"    [EnrichPatch] Reusing {len(activity_df):,} activity rows / "
        f"{len(unique_videos):,} unique items; dropping "
        f"{len(cached_df.columns) - len(activity_cols)} enrichment/calc columns"
    )

    # Free the original cached dataframe before loading enrichment to cap peak memory.
    del cached_df

    core_datasets: dict = {
        common._collections_label(): activity_df,
        common._scrapes_label(): None,
        common._machine_annotations_label(): None,
    }
    _t_enrich = _time.perf_counter()
    loading._filter_enrichment_data(
        core_datasets, unique_videos, study_name=study_name, verbose=verbose
    )
    _t_enrich = _time.perf_counter() - _t_enrich

    _t_merge = _time.perf_counter()
    result = new_merge(
        study_name=study_name,
        all_datasets=core_datasets,
        save_to_cache=True,
        verbose=verbose,
    )
    _t_merge = _time.perf_counter() - _t_merge

    if result is None or result.empty:
        logger.warning(
            "    [EnrichPatch] Merge returned empty — aborting patch (caller should full-rebuild)"
        )
        return None

    # Block until the async parquet write in new_merge finishes before writing
    # the sidecar — otherwise a concurrent refresh could load a stale sidecar
    # that points at a half-written parquet.
    try:
        with data_io.file_lock:
            pass
        refresh.save_sidecar(study_name=study_name, recoded_df=result, verbose=verbose)
    except Exception as exc:
        logger.warning(
            f"    [Sidecar] Non-fatal: failed to write sidecar after enrichment patch: {exc}"
        )

    result.attrs["refresh_action"] = "enrichment_patch"
    result.attrs["study_name"] = study_name

    _t_total = _time.perf_counter() - _t0
    _rss_end = _rss_mb()
    _peak_end = _peak_rss_mb()
    logger.info(
        f"[ENRICH PATCH][TIMING] study={study_name} "
        f"enrichment_load={_t_enrich:.2f}s merge={_t_merge:.2f}s "
        f"total={_t_total:.2f}s rows={len(result):,}"
    )
    logger.info(
        f"[ENRICH PATCH][MEM] study={study_name} "
        f"rss_start={_rss_start:.0f}MB "
        f"rss_end={_rss_end:.0f}MB "
        f"peak_during={_peak_end:.0f}MB "
        f"peak_delta=+{(_peak_end - _peak_start):.0f}MB "
        f"df_size={_df_size_mb(result):.0f}MB"
    )
    return result
