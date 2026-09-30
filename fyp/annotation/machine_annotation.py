"""Machine annotation of scraped videos with Gemini: the entry points.

Annotates a list of video ids or drains the annotation queue: calls the model
(:mod:`fyp.annotation.gemini_calls`), then refines the raw responses into
per-item annotation rows and consolidates them into the versioned
machine-annotations dataset (:mod:`fyp.annotation.annotation_refinement`,
parsing in :mod:`fyp.annotation.response_parsing`). Those modules' public
names are re-exported here for callers of this module's old surface;
first-party code imports them from where they live.
"""

import datetime as _dt
import os
import re

import numpy as np

import fyp.core.data_io as data_io
import fyp.core.utils as fyp_utils
from fyp.annotation import annotation_refinement, gemini_calls

# Re-exported for callers of this module's old surface (see the docstring).
from fyp.annotation.annotation_refinement import (  # noqa: F401
    clean_up_machine_annotations,
    consolidate_and_save_refined_annotations,
    rebuild_preferred_annotations_from_archive,
    refine_and_save_all_raw_annotation_files,
    refine_one_raw_annotation_batch,
)
from fyp.annotation.gemini_calls import (  # noqa: F401
    build_structured_generation_config,
    call_machine,
    call_machine_threads,
    initialize_machine,
)
from fyp.annotation.response_parsing import (  # noqa: F401
    RARE_COLUMN_MERGE_MIN_SIMILARITY,
    consolidate_rare_columns_from_gemini_output,
    flatten_and_fix_machine_outputs,
    flatten_one_machine_response,
    fuzzy_load_of_json_from_string,
    remove_repetitions_from_transcripts,
)
from fyp.core.artifacts import ENRICHMENT_STATUS_FILE
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import graceful_stop_requested as _check_graceful_stop

logger = get_logger(__name__)


def _machine_annotations_label() -> str:
    """Lazy accessor for the config-derived machine-annotations label."""
    return _cf()["labels"]["MACHINE_ANNOTATIONS_LABEL"]


def __getattr__(name: str):
    """Serve the config-derived module constant lazily (PEP 562)."""
    if name == "MACHINE_ANNOTATIONS_LABEL":
        return _machine_annotations_label()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def annotation_configured() -> tuple[bool, str]:
    """Whether machine annotation is configured to run on the active backend.

    A pure config/dependency check — no network, no client construction.
    Dispatches to the active backend's ``availability()`` (see
    :mod:`fyp.annotation.backends`): for Gemini that reproduces the historical
    credential + gs://-media rules byte-identically; for a local backend it
    covers platform / dependency / model-download requirements instead.

    Returns:
        ``(ok, reason)``. When ``ok`` is False, ``reason`` is a user-facing
        explanation of what to configure; an empty string otherwise.
    """
    from fyp.annotation.backends import active_backend_name, get_backend

    name = active_backend_name()
    try:
        backend = get_backend(name)
    except ValueError as exc:
        return False, str(exc)
    result = backend.availability(deep=False)
    return result.ok, result.reason


def platform_map_for(item_ids: list[str]) -> dict[str, str]:
    """Map item ids to their ``source_platform`` via enrichment_status.parquet.

    Used by the annotation entry points to resolve each queued item's platform
    (the annotation queue stores bare ids). Ids missing from the status file are
    simply absent from the map — callers fall back to the default platform, and
    ``media_paths.resolve_media`` probes the other platforms' subpaths anyway.
    Never raises.

    Args:
        item_ids: The item ids to look up.

    Returns:
        ``{item_id: source_platform}`` for the ids that could be resolved.
    """
    try:
        if not data_io.exists(storage_location="recoded", filename=ENRICHMENT_STATUS_FILE):
            return {}
        status_df = data_io.load_parquet_selective(
            storage_location="recoded",
            filename=ENRICHMENT_STATUS_FILE,
            columns=["item_id", "source_platform"],
        )
        if "source_platform" not in status_df.columns:
            return {}
        wanted = {str(i) for i in item_ids}
        ids = status_df["item_id"].astype(str)
        mask = ids.isin(wanted) & status_df["source_platform"].notna()
        return dict(zip(ids[mask], status_df.loc[mask, "source_platform"].astype(str)))
    except Exception as e:
        logger.warning(
            f"WARNING: platform map lookup failed ({e}); falling back to default platform."
        )
        return {}


def annotate_from_video_id_list(
    fine_list=None,
    max_workers=50,
    refine_after_annotation=True,
    verbose=False,
    notebook_mode=False,
    dry_run=False,
    batch_label: str | None = None,
    cumulative_done: int = 0,
    cumulative_total: int = 0,
    cumulative_ok: int = 0,
    cumulative_fail: int = 0,
    reporter=None,
    platform_by_id: dict[str, str] | None = None,
):

    if notebook_mode:
        verbose = True
    """
    This function takes a list of video IDs and calls the machine to annotate them.
    It also performs the necessary post processing of the raw outputs from the machine.
    """

    gemini_calls.initialize_machine()

    if dry_run:
        logger.info(
            "********* This is a dry run. It's all fake. No data io action at all. *********"
        )

    if isinstance(fine_list, list) and len(fine_list) > 0:
        # Sanity check against corrupt lists (NaN / paths / URLs) — id shapes
        # differ per platform (TikTok 19-digit numeric, Instagram shortcode,
        # YouTube 11-char [A-Za-z0-9_-]), so the check is deliberately permissive.
        if not all(
            (type(video_id) == str and re.fullmatch(r"[A-Za-z0-9_-]{5,40}", video_id))
            for video_id in fine_list
        ):
            raise ValueError("Some videoIDs in the list were corrupt. Cannot process this list.")

        if platform_by_id is None and not dry_run:
            platform_by_id = platform_map_for(fine_list)

        logger.info("Annotating videos...")

        raw_outputs_from_machine, raw_json_fn = gemini_calls.call_machine_threads(
            interesting_videos=fine_list,
            max_workers=max_workers,
            verbose=verbose,
            notebook_mode=notebook_mode,
            dry_run=dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=cumulative_total,
            cumulative_ok=cumulative_ok,
            cumulative_fail=cumulative_fail,
            reporter=reporter,
            platform_by_id=platform_by_id,
        )

        logger.info("...video annotation completed.")

        if dry_run:
            logger.info("Since this is a dry run I'm skipping the refinement step.")
            return [], []

        if refine_after_annotation:
            refined_df = annotation_refinement.refine_one_raw_annotation_batch(
                raw_outputs_from_machine=raw_outputs_from_machine,
                raw_json_filename=raw_json_fn,
                verbose=verbose,
                notebook_mode=notebook_mode,
            )

            # Refinement can return None when flatten_and_fix_machine_outputs
            # fails for the entire batch. In that case we cannot tell which
            # items succeeded, so return empty lists — the caller will leave
            # the queue untouched and the items will be retried next run.
            if refined_df is None or refined_df.empty:
                return [], []

            if {"item_id", "annotated_ok", "annotated_fail"}.issubset(refined_df.columns):
                ok_ids = (
                    refined_df.loc[refined_df["annotated_ok"].fillna(False).astype(bool), "item_id"]
                    .astype(str)
                    .tolist()
                )
                fail_ids = (
                    refined_df.loc[
                        refined_df["annotated_fail"].fillna(False).astype(bool), "item_id"
                    ]
                    .astype(str)
                    .tolist()
                )
                return ok_ids, fail_ids

            return [], []

        return [], []

    else:
        if verbose:
            logger.info("No videos to process")
        return [], []


def queue_annotation_loop(
    batch_size=500,
    max_batches=None,
    verbose=False,
    dry_run=False,
    reporter=None,
    cancellation_check=None,
):

    import fyp.core.data_io as data_io

    target_cache_file = "to_annotate.json"

    if not data_io.exists(storage_location="cache", filename=target_cache_file):
        logger.error(
            f"    ERROR: Could not find target file '{target_cache_file}' in cache. Make sure you calculated targets first."
        )
        return None

    video_list = data_io.load_json(storage_location="cache", filename=target_cache_file)

    if not video_list or len(video_list) == 0:
        logger.info(f"    No videos to annotate found in '{target_cache_file}'.")
        return None

    logger.info(f"    Loaded {len(video_list)} videos from queue '{target_cache_file}'")

    return annotate_videos_loop_from_list(
        video_list=video_list,
        batch_size=batch_size,
        max_batches=max_batches,
        verbose=verbose,
        dry_run=dry_run,
        reporter=reporter,
        cancellation_check=cancellation_check,
    )


def annotate_videos_loop_from_list(
    video_list=None,
    batch_size=500,
    max_batches=None,
    verbose=False,
    dry_run=False,
    reporter=None,
    cancellation_check=None,
):

    max_batches = max_batches if max_batches is not None else np.inf

    if video_list is None:
        logger.error(
            "    ERROR: The annotation loop cannot run without a video list as input. Process failed."
        )
        return None

    gemini_calls.initialize_machine()

    logger.info(
        f"    Annotating selected videos, batch size: {batch_size}, max batches: {max_batches}"
    )
    logger.info(f"    Now: {_dt.datetime.now()}")

    batch_number = 1
    cumulative_done = 0
    cumulative_ok = 0
    cumulative_fail = 0

    batch_target = min(max_batches, len(video_list) // batch_size + 1)
    total_items = min(len(video_list), batch_target * batch_size)

    logger.info(
        f"  Starting loop... There are {total_items:,} videos to process in {batch_target:,} batches"
    )

    target_cache_file = "to_annotate.json"

    for batch in fyp_utils.chunk_list(video_list, batch_size):
        batch_label = f"{batch_number}/{batch_target}"
        logger.info(f"  Batch {batch_label}")

        ok_ids, fail_ids = annotate_from_video_id_list(
            fine_list=batch,
            verbose=verbose,
            dry_run=dry_run,
            batch_label=batch_label,
            cumulative_done=cumulative_done,
            cumulative_total=total_items,
            cumulative_ok=cumulative_ok,
            cumulative_fail=cumulative_fail,
            reporter=reporter,
        )

        cumulative_done += len(batch)
        cumulative_ok += len(ok_ids)
        cumulative_fail += len(fail_ids)

        # Prune successful + failed items from the on-disk queue so it stays
        # in sync with reality. Mirrors the scraper's prune in
        # run_queue_scraper.py:133. Skipped for dry_run since nothing was
        # actually annotated.
        queue_remaining = len(video_list) - cumulative_done
        if not dry_run and data_io.exists(storage_location="cache", filename=target_cache_file):
            items_to_remove = set(ok_ids) | set(fail_ids)
            prune_counts = {}

            def _prune(fresh_queue, items_to_remove=items_to_remove, prune_counts=prune_counts):
                fresh_queue = fresh_queue if isinstance(fresh_queue, list) else []
                updated_queue = [v for v in fresh_queue if v not in items_to_remove]
                prune_counts["after"] = len(updated_queue)
                if len(updated_queue) == len(fresh_queue):
                    return None  # nothing pruned — skip the write
                return updated_queue

            # Atomic prune: ids appended by the web service while this batch
            # ran are never clobbered.
            data_io.update_json(
                storage_location="cache",
                filename=target_cache_file,
                mutate=_prune,
                default=[],
            )
            queue_remaining = prune_counts.get("after", queue_remaining)

        if reporter is not None:
            reporter.emit_data({"annotate_queue_len": max(0, queue_remaining)})
        elif "WEB_INTERFACE" in os.environ:
            # STDOUT PROTOCOL — MUST stay print(). process_manager.enqueue_output()
            # parses subprocess stdout for the ::DATA:: marker; never convert to logging.
            print(f'::DATA::{{"annotate_queue_len": {max(0, queue_remaining)}}}', flush=True)

        if max_batches is not None and batch_number >= max_batches:
            break

        # Check for graceful stop request
        if cancellation_check is not None:
            if cancellation_check():
                logger.info("  Cancellation requested. Finishing after this batch.")
                break
        elif _check_graceful_stop("queue_annotator"):
            logger.info("  Graceful stop requested. Finishing after this batch.")
            break

        batch_number += 1

        if dry_run:
            break

    logger.info(f"Loop ended: {_dt.datetime.now()}")


# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
# *********************************************************************************************************
