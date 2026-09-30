"""Refining raw annotation batches and consolidating them into the dataset.

A raw batch is parsed (:mod:`fyp.annotation.response_parsing`), cleaned and
recoded into refined rows; refined batches are folded into the versioned
machine-annotations dataset, which keeps every version's history and the
preferred view the app reads.
"""

import time

import numpy as np
import pandas as pd

import fyp.annotation.annotation_versioning as annotation_versioning
import fyp.core.data_io as data_io
import fyp.scrape.scrape_queues as scrape_queues
from fyp.annotation import response_parsing
from fyp.annotation.recode_variables import recode_events_df, recode_fuzzy_match, rename_columns
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import label
from fyp.core.types import convert_dtypes_to_pyarrow

logger = get_logger(__name__)


def _machine_annotations_label() -> str:
    """Lazy accessor for the config-derived machine-annotations label."""
    return label("MACHINE_ANNOTATIONS_LABEL")


def clean_up_machine_annotations(some_events, verbose=False):

    some_cleaned_up_events = some_events.copy()

    # iterate over all object type columns in the events DF that starts w G_, i.e. are machine annotations
    g_cols = [
        k
        for k in some_events.select_dtypes(exclude=["number"]).columns
        if k not in ["item_id", "annotated_ok", "annotated_fail"]
    ]

    exclude_set = {_cf()["labels"]["UNABLE_TO_DETECT"], "", _cf()["labels"]["OTHER_THINGS"]}

    for c in g_cols:
        # Step 1: Flatten and filter efficiently
        series = some_events[c]

        # explode lists to rows
        try:
            exploded = series.explode().dropna()
        except ValueError:
            # PyArrow-backed columns with all-empty lists can cause a length
            # mismatch in pandas explode(); safe to skip.
            continue

        if exploded.empty:
            continue

        # exclude set filtering
        # check against set is fast
        valid_mask = ~exploded.isin(exclude_set)
        valid_items = exploded[valid_mask]

        if valid_items.empty:
            continue

        accepted = _cf()["var_schema"].set_index("variable_name").loc[c, "accepted_labels"]
        accepted_labels = pd.NA
        if (
            pd.notna(accepted)
            and accepted.lower() != "nan"
            and accepted.startswith("[")
            and accepted.endswith("]")
        ):
            accepted = accepted[1:-1]
            accepted_labels = [
                x.strip().replace("//", "").replace("&", " and ").replace("/", " or ")
                for x in accepted.split(",")
            ]

            pre_fuzzy_nunique = valid_items.nunique()

            # Remember where scalar NAs were so we can restore them after the
            # fuzzy match. recode_fuzzy_match replaces NA values with
            # OTHER_THINGS, but downstream code (e.g. the annotated_ok /
            # annotated_fail flags built from `type_of_story.isna()` at the
            # end of refine_one_raw_annotation_batch) depends on NAs staying
            # NA.
            na_mask = series.isna()

            # Fuzzy-match the WHOLE series (handles lists, scalars, NAs) so
            # that the consolidation and the final writeback below both operate
            # on the normalized values. Running the fuzzy match only on the
            # exploded valid_items (the previous behaviour) left the original
            # series unchanged, causing any value that needed fuzzy matching to
            # fail the keep_set membership check below and be collapsed to
            # OTHER_THINGS.
            series = recode_fuzzy_match(
                list_a=series,
                list_b=accepted_labels,
                threshold=0.8,
                verbose=verbose,
            )

            # Restore NAs that fuzzy matching turned into OTHER_THINGS.
            if na_mask.any():
                series = series.astype(object)
                series[na_mask] = pd.NA

            # Write the normalized series back immediately so columns that
            # have an accepted_labels list always get their fuzzy-match output
            # preserved, even if the consolidation step below is skipped
            # (e.g. avg_len >= 60 or tail too flat).
            some_cleaned_up_events[c] = series

            # Re-derive exploded / valid_items from the now-normalized series
            # for the downstream consolidation step.
            try:
                exploded = series.explode().dropna()
            except ValueError:
                continue

            valid_mask = ~exploded.isin(exclude_set)
            valid_items = exploded[valid_mask]

            if valid_items.empty:
                continue

            if verbose:
                logger.info(
                    f"    {c}: Recoded against accepted labels with fuzzy matching... {valid_items.nunique()} ({pre_fuzzy_nunique})"
                )

        # Check mean length
        # Vectorized string length based on a sample of 500 items.
        # A fixed random_state keeps the length estimate — and the rare-label
        # consolidation decision that branches on it — reproducible run-to-run.
        # Without it the whole refinement pipeline is non-deterministic.

        sample_size = min(500, len(valid_items))
        avg_len = (
            valid_items.sample(sample_size, replace=False, random_state=0)
            .astype(str)
            .str.len()
            .mean()
        )

        if avg_len < 60:
            # Step 2: Cutoff logic
            # frequency of unique valid items
            counts = valid_items.value_counts()

            total_count = counts.sum()

            # if we have an accepted list, we want to keep all of them
            if pd.notna(accepted):
                target = total_count * 1
            else:
                target = total_count * 0.95

            # cumulative sum
            cum_counts = counts.cumsum()

            # find how many labels needed to cross target
            # we keep labels where cumsum < target, plus the one that crosses it
            cutoff_idx = cum_counts.searchsorted(target)
            # keep at least 3 labels
            num_keep = max(3, cutoff_idx + 1)
            # clamp to length
            num_keep = min(num_keep, len(counts))

            # Heuristic: If we are keeping a huge portion of the labels to satisfy the coverage,
            # or the absolute number of kept labels is huge (e.g. 90k out of 100k), then consolidation is inefficient/useless.
            # User guideline: "if the sum of occurrences of top X labels constitute more than y% ... and there still are a lot of small labels" -> consolidate.
            # But "100k rare labels -> 90k" -> don't consolidate.
            # Logic: If num_keep is > 80% of len(counts) and len(counts) > 1000, skip.

            if (len(counts) > 1000) and (num_keep > len(counts) * 0.80):
                if verbose:
                    logger.info(
                        f"    {c}: Skipping consolidation. Tail is too thick/flat (would keep {num_keep}/{len(counts)})."
                    )
                continue

            okay_list = counts.index[:num_keep].tolist()

            # fast lookup set
            keep_set = set(okay_list).union(exclude_set)

            # Step 3: Replacement
            # We need to iterate rows since we want to preserve list structure [[a, b], [c]] -> [[a, OTHER], [c]]
            # A simple map with set lookup is fastest for object columns with lists.
            # NOTE: `series` here is either the original series (when there is
            # no accepted_labels list) or the fuzzy-match-normalized series
            # (when there is). That keeps the membership check against
            # keep_set consistent with how keep_set was built.
            def _fast_replace(x, keep_set=keep_set):
                if isinstance(x, (list, np.ndarray)):
                    return [y if y in keep_set else _cf()["labels"]["OTHER_THINGS"] for y in x]
                if isinstance(x, str):
                    return x if x in keep_set else _cf()["labels"]["OTHER_THINGS"]
                return x  # keep NA or other

            some_cleaned_up_events[c] = series.apply(_fast_replace)

            if verbose:
                # approximated stats
                logger.info(f"    {c}: Cleaned up rare labels (kept top {num_keep})")

        else:
            if verbose:
                logger.info(f"    {c}: Avg string length > 60, not consolidating rare labels")

    return some_cleaned_up_events


def refine_one_raw_annotation_batch(
    raw_outputs_from_machine=None, raw_json_filename=None, verbose=False, notebook_mode=False
):

    if notebook_mode:
        verbose = True

    if raw_json_filename is None:
        raise ValueError("raw_json_filename cannot be None")

    if raw_outputs_from_machine is None:
        if verbose:
            logger.info(f"Loading raw annotations from {raw_json_filename}")
        raw_outputs_from_machine = data_io.load_json(
            storage_location="machine_annotations_raw", filename=raw_json_filename, verbose=verbose
        )

    if raw_outputs_from_machine is None:
        raise ValueError("raw_outputs_from_machine cannot be None")

    logger.info(f"Refining {len(raw_outputs_from_machine):,} raw annotations in this file...")

    # ---------------------------------------------------------------
    # 1. Flatten the json to a dataframe. Using fuzzy json for this
    # ---------------------------------------------------------------
    logger.info("Transforming the messy json into a flat dataframe")
    outputs_from_machine_df = response_parsing.flatten_and_fix_machine_outputs(
        raw_outputs_from_machine, verbose=verbose, notebook_mode=notebook_mode
    )

    if outputs_from_machine_df is None:
        logger.warning(
            "I was unable to extract a single good response from this file. Returning None."
        )
        logger.warning("Consider deleting this raw file from the raw_annotations folder.")
        return None

    # ---------------------------------------------------------------
    # 2. Consolidate rare columns
    # ---------------------------------------------------------------
    logger.info("Consolidating rare columns from machine annotations.")
    outputs_from_machine_df = response_parsing.consolidate_rare_columns_from_gemini_output(
        outputs_from_machine_df, verbose=verbose, notebook_mode=notebook_mode
    )
    logger.info("...done")

    # ---------------------------------------------------------------
    # 3. Remove repetitions from transcripts
    # ---------------------------------------------------------------
    if "transcript" in outputs_from_machine_df.columns:
        logger.info("Removing repetitions from machine annotation transcripts...")
        outputs_from_machine_df = response_parsing.remove_repetitions_from_transcripts(
            outputs_from_machine_df, verbose=verbose, notebook_mode=notebook_mode
        )
        logger.info("...done")

    # ---------------------------------------------------------------
    # implement the rules from the variable scheme - recoding lists, strings and other complex data
    # ---------------------------------------------------------------
    # (and a simple renaming of columns to make them easier to identify and read)
    outputs_from_machine_df = rename_columns(outputs_from_machine_df).copy()
    outputs_from_machine_df = recode_events_df(
        study_dataset=outputs_from_machine_df, drop_single_value_cols=False, verbose=verbose
    )

    # ---------------------------------------------------------------
    # consolidate some labels in non-numeric columns where that makes sense
    # ---------------------------------------------------------------
    outputs_from_machine_df = clean_up_machine_annotations(
        some_events=outputs_from_machine_df, verbose=verbose
    )

    # ---------------------------------------------------------------
    # add flags for annotated ok and fail
    # ---------------------------------------------------------------
    outputs_from_machine_df["annotated_ok"] = ~outputs_from_machine_df[
        "type_of_story"
    ].isna().astype("bool[pyarrow]")
    outputs_from_machine_df["annotated_fail"] = (
        outputs_from_machine_df["type_of_story"].isna().astype("bool[pyarrow]")
    )

    # ---------------------------------------------------------------
    # Stamp each row with its annotation_version. recode_events_df drops this
    # (it is not a var_schema column), so re-attach it from the raw outputs.
    # Legacy raw files predating versioning have no such field and default to
    # the legacy version.
    # ---------------------------------------------------------------
    version_by_item = {}
    platform_by_item = {}
    ts_by_item = {}
    for entry in raw_outputs_from_machine.values():
        if isinstance(entry, dict) and entry.get("item_id") is not None:
            version_by_item[str(entry["item_id"])] = entry.get(
                "annotation_version", annotation_versioning.LEGACY_VERSION
            )
            if entry.get("source_platform"):
                platform_by_item[str(entry["item_id"])] = str(entry["source_platform"])
            if entry.get("inference_ts") is not None:
                ts_by_item[str(entry["item_id"])] = entry["inference_ts"]
    outputs_from_machine_df["annotation_version"] = (
        outputs_from_machine_df["item_id"]
        .astype(str)
        .map(version_by_item)
        .fillna(annotation_versioning.LEGACY_VERSION)
    )

    # Stamp source_platform the same way (raw files predating multi-platform
    # annotation have no such key and default to the default platform).
    outputs_from_machine_df["source_platform"] = (
        outputs_from_machine_df["item_id"]
        .astype(str)
        .map(platform_by_item)
        .fillna(scrape_queues.default_platform())
    )

    # Stamp inference_ts (epoch seconds) the same way; rows from raw entries
    # lacking the key stay NA. Drives timeframe-based re-annotation selection.
    outputs_from_machine_df["inference_ts"] = pd.to_numeric(
        outputs_from_machine_df["item_id"].astype(str).map(ts_by_item),
        errors="coerce",
    ).astype("int64[pyarrow]")

    # ---------------------------------------------------------------
    # Convert dtypes to pyarrow and reset index
    # ---------------------------------------------------------------
    outputs_from_machine_df.reset_index(drop=True, inplace=True)
    outputs_from_machine_df = convert_dtypes_to_pyarrow(outputs_from_machine_df, verbose=verbose)

    if verbose:
        logger.info("Ready to save processed results")

    parquet_filename = raw_json_filename.replace(".json", ".parquet")

    data_io.save_parquet(
        df=outputs_from_machine_df,
        storage_location="machine_annotations_refined",
        filename=parquet_filename,
        verbose=verbose,
    )
    logger.info(
        f"Saved processed the df - shape {outputs_from_machine_df.shape} - results to '{parquet_filename}'"
    )
    logger.info("--" * 60)

    return outputs_from_machine_df


def refine_and_save_all_raw_annotation_files(verbose=False, notebook_mode=False, force=False):

    result = {}

    raw_annotation_files = [
        fn
        for fn in data_io.listdir(storage_location="machine_annotations_raw")
        if fn.startswith(_machine_annotations_label()) and fn.endswith(".json")
    ]
    result["raw_files"] = len(raw_annotation_files)

    refined_annotation_files = [
        fn
        for fn in data_io.listdir(storage_location="machine_annotations_refined")
        if fn.startswith(_machine_annotations_label()) and fn.endswith(".parquet")
    ]
    result["refined_files_before"] = len(refined_annotation_files)

    if force:
        # Re-refine every raw file regardless of whether a refined parquet
        # already exists. Use this after a fix to the refinement pipeline that
        # invalidates the cached refined files.
        raw_files_up_for_refinement = list(raw_annotation_files)
    else:
        raw_files_up_for_refinement = [
            g
            for g in raw_annotation_files
            if g.replace(".json", ".parquet") not in refined_annotation_files
        ]
    if verbose:
        if force:
            logger.info(
                f"Force mode: re-refining all {len(raw_files_up_for_refinement)} raw files (ignoring {len(refined_annotation_files)} existing refined files)"
            )
        else:
            logger.info(
                f"{len(refined_annotation_files)} raw annotation files have already been refined"
            )
            logger.info(f"{len(raw_files_up_for_refinement)} files are up for refinement")

    for i, fn in enumerate(raw_files_up_for_refinement):
        if verbose:
            logger.info(f"\n{i + 1}/{len(raw_files_up_for_refinement)} {fn}")
        refine_one_raw_annotation_batch(
            raw_outputs_from_machine=None,
            raw_json_filename=fn,
            verbose=verbose,
            notebook_mode=notebook_mode,
        )

    refined_annotation_files = data_io.listdir(
        storage_location="machine_annotations_refined", return_absolute_path=False, verbose=False
    )
    refined_annotation_files = [u for u in refined_annotation_files if u.endswith(".parquet")]
    result["refined_files_after"] = len(refined_annotation_files)

    return result


def _normalize_annotation_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Backfills every consolidated annotation frame gets, full path and fold.

    Rows from legacy refined files predating versioning default to the legacy
    version; rows predating multi-platform annotation are TikTok-era, and item
    ids are only guaranteed unique within a platform, so all annotation keying
    is composite (source_platform, item_id). Idempotent.
    """
    if "annotation_version" not in df.columns:
        df["annotation_version"] = annotation_versioning.LEGACY_VERSION
    df["annotation_version"] = df["annotation_version"].fillna(annotation_versioning.LEGACY_VERSION)
    if "source_platform" not in df.columns:
        df["source_platform"] = scrape_queues.default_platform()
    df["source_platform"] = df["source_platform"].fillna(scrape_queues.default_platform())
    return df


def _preferred_view_from_history(history_df: pd.DataFrame) -> pd.DataFrame:
    """Derive the active per-item view from a (subset of the) version history.

    The promoted-version view when one is promoted, else the latest annotation
    per item — the same derivation the full rebuild and
    :func:`rebuild_preferred_annotations_from_archive` apply. Per-key
    decomposable ONLY when the frame holds a key's complete history, which is
    why the incremental fold sources touched keys from the all_versions
    archive, never from the recoded view.
    """
    preferred_version = annotation_versioning.get_preferred_version()
    dedup_cols = (
        ["source_platform", "item_id"] if "source_platform" in history_df.columns else ["item_id"]
    )
    if preferred_version is None:
        return history_df.drop_duplicates(subset=dedup_cols, keep="last").reset_index(drop=True)
    return annotation_versioning.select_preferred_view(history_df, preferred_version)


def _fold_annotation_batch(
    dataset_meta: dict,
    files_to_concatenate: list[str],
    new_files: list[str],
    verbose: bool = False,
):
    """Fold only the new refined files into the archive + recoded view.

    O(batch) compute plus the archive/recoded blob IO — instead of re-reading
    every refined parquet (measured ~70 s for ~500 files) and re-deriving the
    view over the whole corpus. The archive fold is order-preserving: batch
    rows are appended AFTER the existing archive rows and the archive is never
    sorted — "keep last" per (platform, item, version) and the latest-per-item
    fallback both lean on that row order.

    Returns:
        The ``(True, frame, changed_ids)`` result tuple, or ``None`` to decline
        (missing archive/recoded, or a batch whose column set drifts from the
        recoded view) — the caller then runs the unchanged full-rebuild path.
    """
    _t_start = time.perf_counter()
    archive_fn = f"{_machine_annotations_label()}_all_versions.parquet"
    recoded_fn = f"{_machine_annotations_label()}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        return None
    if not data_io.exists(storage_location="recoded", filename=recoded_fn):
        return None

    logger.info(
        f"Folding {len(new_files)} new refined annotation file(s) into the previous consolidation..."
    )
    batch_dfs = []
    new_item_ids: set[str] = set()
    for fn in new_files:
        df = data_io.load_parquet(storage_location="machine_annotations_refined", filename=fn)
        batch_dfs.append(df)
        new_item_ids.update(df["item_id"].tolist())
        if verbose:
            logger.info(f"{fn} {df.shape}")
    if not batch_dfs:
        return None
    batch = _normalize_annotation_frame(pd.concat(batch_dfs, ignore_index=True))
    _t_load = time.perf_counter() - _t_start

    _t_mark = time.perf_counter()
    archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if archive is None or archive.empty or "item_id" not in archive.columns:
        return None
    archive = _normalize_annotation_frame(archive)
    existing_recoded = data_io.load_parquet(storage_location="recoded", filename=recoded_fn)
    if existing_recoded is None or existing_recoded.empty:
        return None
    _t_prev_load = time.perf_counter() - _t_mark

    _t_mark = time.perf_counter()
    folded_archive = (
        pd.concat([archive, batch], ignore_index=True)
        .drop_duplicates(subset=["source_platform", "item_id", "annotation_version"], keep="last")
        .reset_index(drop=True)
    )

    key_cols = ["source_platform", "item_id"]
    batch_keys = pd.MultiIndex.from_frame(batch[key_cols].astype("string[pyarrow]")).unique()
    archive_keys = pd.MultiIndex.from_frame(folded_archive[key_cols].astype("string[pyarrow]"))
    history = folded_archive[archive_keys.isin(batch_keys)]
    new_view = _preferred_view_from_history(history)

    if set(new_view.columns) != set(existing_recoded.columns):
        added = sorted(set(new_view.columns) - set(existing_recoded.columns))
        removed = sorted(set(existing_recoded.columns) - set(new_view.columns))
        logger.info(
            f"[CONSOLIDATE] annotation fold declined: column drift (+{added} / -{removed})."
        )
        return None

    recoded_keys = pd.MultiIndex.from_frame(existing_recoded[key_cols].astype("string[pyarrow]"))
    consolidated_annotations = pd.concat(
        [existing_recoded[~recoded_keys.isin(batch_keys)], new_view], ignore_index=True
    )
    _t_fold = time.perf_counter() - _t_mark

    logger.info(
        f"Shape: {consolidated_annotations.shape} | "
        f"Memory usage: {consolidated_annotations.memory_usage(deep=True).sum() / (1024**2):.2f} MB"
    )
    logger.info(
        f"Found {len(new_item_ids):,} changed/newly annotated item_ids from {len(new_files)} new file(s)."
    )

    # Archive first, then the view, then the ledger — a crash anywhere replays
    # this fold idempotently (the batch rows dedupe away on the second pass).
    logger.info("Saving consolidated annotations...")
    _t_mark = time.perf_counter()
    data_io.save_parquet(
        df=folded_archive, storage_location="recoded", filename=archive_fn, verbose=verbose
    )
    # The registry must see the WHOLE archive's version set — it feeds the
    # var_schema hash, and narrowing it to the batch would silently shrink the
    # hash and mark every study for rebuild (or worse, fail to).
    annotation_versioning.record_versions_in_data(
        folded_archive["annotation_version"].dropna().unique()
    )
    data_io.save_parquet(
        df=consolidated_annotations,
        storage_location="recoded",
        filename=recoded_fn,
        verbose=verbose,
    )
    _t_save = time.perf_counter() - _t_mark
    logger.info("...done")

    if "machine_annotations" not in dataset_meta:
        dataset_meta["machine_annotations"] = {}
    dataset_meta["machine_annotations"]["filenames"] = files_to_concatenate
    dataset_meta["machine_annotations"]["preferred_version"] = (
        annotation_versioning.get_preferred_version()
    )
    _ = data_io.save_json(
        data=dataset_meta, storage_location="recoded", filename="consolidated_enrichment_files.json"
    )

    logger.info(
        f"[CONSOLIDATE][TIMING] anno FOLD load={_t_load:.1f}s prev_load={_t_prev_load:.1f}s "
        f"fold={_t_fold:.1f}s save={_t_save:.1f}s total={time.perf_counter() - _t_start:.1f}s "
        f"new_files={len(new_files)} rows={len(consolidated_annotations):,} "
        f"changed={len(new_item_ids):,}"
    )
    return True, consolidated_annotations, new_item_ids


def consolidate_and_save_refined_annotations(
    force_consolidation=False,
    return_saved_data=True,
    verbose=False,
    incremental=False,
    dry_run=False,
):
    # dry_run: run the full-rebuild reference path but persist NOTHING (no
    # archive/recoded save, no version-registry update, no ledger) — the
    # shadow verifier uses it to build what a full rebuild WOULD produce.

    top_verbose = True

    # ---------------------------------------------------------------
    _t_start = time.perf_counter()
    if top_verbose:
        logger.info("Checking for raw annotation batches that needs refining...")
    # check if there are any raw files that need refining and refine those
    result = refine_and_save_all_raw_annotation_files(verbose=verbose, notebook_mode=False)
    _t_refine = time.perf_counter() - _t_start
    if top_verbose:
        if result["refined_files_after"] == result["refined_files_before"]:
            logger.info("    ...all files already refined.")
        else:
            logger.info(
                f"    ...refined {result['refined_files_after'] - result['refined_files_before']} files."
            )

    # ---------------------------------------------------------------
    # check if there are any changes in the relevant folder compared to last time this process was run.
    if data_io.exists(
        storage_location="recoded", filename="consolidated_enrichment_files.json", verbose=verbose
    ):
        dataset_meta = data_io.load_json(
            storage_location="recoded",
            filename="consolidated_enrichment_files.json",
            verbose=verbose,
        )
        if verbose:
            logger.info("Dataset meta loaded")
    else:
        dataset_meta = {"machine_annotations": {"filenames": []}}

    files_to_concatenate = []
    for fn in data_io.listdir(storage_location="machine_annotations_refined"):
        if fn.startswith(_machine_annotations_label()) and fn.endswith(".parquet"):
            files_to_concatenate.append(fn)
    # Deterministic chronological order (the filenames are timestamped).
    # "Keep the latest annotation per item" leans on concat order, and local
    # listdir order is arbitrary — sorted order also makes the incremental
    # fold's append-new-files-last equal to the full rebuild's concat.
    files_to_concatenate.sort()

    latest_filename_list = dataset_meta.get("machine_annotations", {}).get("filenames", [])

    # if all files found in the refine folder are already registered in the dataset meta, then no need to consolidate
    if not force_consolidation and set(files_to_concatenate) <= set(latest_filename_list):
        if top_verbose:
            logger.info("No new refined machine annotations files found. No need to consolidate.")
        logger.info(
            f"[CONSOLIDATE][TIMING] anno quiet refine={_t_refine:.1f}s "
            f"total={time.perf_counter() - _t_start:.1f}s files={len(files_to_concatenate)}"
        )
        if return_saved_data:
            if data_io.exists(
                storage_location="recoded",
                filename=f"{_machine_annotations_label()}_recoded.parquet",
            ):
                if verbose:
                    logger.info("Returning existing file.")
                return (
                    False,
                    data_io.load_parquet(
                        storage_location="recoded",
                        filename=f"{_machine_annotations_label()}_recoded.parquet",
                    ),
                    set(),
                )
            if verbose:
                logger.info("No existing consolidated file — returning empty.")
            return False, pd.DataFrame(), set()
        return False, None, set()

    # ---------------------------------------------------------------
    # Incremental fold: fold ONLY the new refined files into the archive and
    # the recoded view instead of re-reading every refined parquet. Anything
    # the fold cannot prove equal to a full rebuild declines to the full path
    # below (the unchanged reference implementation). The fold re-derives only
    # the touched keys' view rows, so it is valid only while the preferred
    # version is the one the previous consolidation was built under — a
    # promotion (or demotion) in between would leave every untouched key on
    # the old view, hence the ledger comparison.
    latest_preferred = dataset_meta.get("machine_annotations", {}).get("preferred_version")
    current_preferred = annotation_versioning.get_preferred_version()
    if incremental and not force_consolidation and not dry_run:
        # Say why the fold is not even attempted — a silent full rebuild here
        # reads as a bug. A ledger written before the ledger recorded a
        # preferred version (None ≠ the promoted one) triggers a one-time
        # bootstrap: this full rebuild records the version, and the next
        # batch folds.
        if not latest_filename_list:
            logger.info(
                "[CONSOLIDATE] annotation full rebuild: the ledger has no file list yet "
                "(first consolidation) — the fold needs one to know what is new."
            )
        elif latest_preferred != current_preferred:
            logger.info(
                "[CONSOLIDATE] annotation full rebuild: preferred version is "
                f"{current_preferred!r} but the previous consolidation was built under "
                f"{latest_preferred!r} — every item's view must be re-derived. The ledger "
                "now records the current version, so the next batch can fold."
            )
        else:
            folded = _fold_annotation_batch(
                dataset_meta=dataset_meta,
                files_to_concatenate=files_to_concatenate,
                new_files=sorted(set(files_to_concatenate) - set(latest_filename_list)),
                verbose=verbose,
            )
            if folded is not None:
                return folded
            logger.info("[CONSOLIDATE] annotation fold declined — taking the full rebuild path.")

    # ---------------------------------------------------------------
    # load all refined files
    _t_mark = time.perf_counter()
    if top_verbose:
        logger.info("Loading refined annotation files...")
    refined_annotation_dfs = []
    for fn in files_to_concatenate:
        df = data_io.load_parquet(storage_location="machine_annotations_refined", filename=fn)
        refined_annotation_dfs.append(df)
        if verbose:
            logger.info(f"{fn} {df.shape}")
    _t_load = time.perf_counter() - _t_mark

    # ---------------------------------------------------------------
    _t_mark = time.perf_counter()
    if top_verbose:
        logger.info(
            f"Consolidating {len(refined_annotation_dfs):,} refined files (keeping latest version of each item_id)..."
        )
    consolidated_annotations = pd.concat(refined_annotation_dfs, ignore_index=True)

    # ---------------------------------------------------------------
    # Version-aware consolidation. Every row carries an annotation_version; rows
    # from legacy refined files predating versioning default to the legacy
    # version. The full multi-version history is archived (queryable, never
    # overwritten); the active dataset that downstream consumers read is then
    # derived from the promoted version, or — when nothing is promoted yet —
    # the latest annotation per item (identical to the historical behaviour).
    consolidated_annotations = _normalize_annotation_frame(consolidated_annotations)

    annotation_archive = consolidated_annotations.drop_duplicates(
        subset=["source_platform", "item_id", "annotation_version"], keep="last"
    ).reset_index(drop=True)
    if not dry_run:
        data_io.save_parquet(
            df=annotation_archive,
            storage_location="recoded",
            filename=f"{_machine_annotations_label()}_all_versions.parquet",
            verbose=verbose,
        )
        # Record which annotation versions the archive actually contains, so the
        # legacy-metadata union (and therefore the var_schema hash) is pruned to
        # versions that can occur in the data. NOTE: a consolidation that shrinks
        # this set changes the schema hash and marks studies for rebuild.
        annotation_versioning.record_versions_in_data(
            annotation_archive["annotation_version"].dropna().unique()
        )

    _t_archive = time.perf_counter() - _t_mark
    _t_mark = time.perf_counter()

    # No promoted version: keep the most recent annotation per item (the
    # historical, version-agnostic behaviour); else the promoted-version view.
    consolidated_annotations = _preferred_view_from_history(consolidated_annotations)

    memory_per_column = consolidated_annotations.memory_usage(deep=True)
    total_memory_bytes = memory_per_column.sum()
    total_memory_mb = total_memory_bytes / (1024**2)
    if top_verbose:
        logger.info(
            f"Shape: {consolidated_annotations.shape} | Memory usage: {total_memory_mb:.2f} MB"
        )

    # ---------------------------------------------------------------
    # Compute changed item_ids: IDs from newly added files that were not in the
    # previous consolidation file list (even re-annotations count as changes).
    # When force_consolidation is True, treat ALL items as changed.
    existing_recoded_fn = f"{_machine_annotations_label()}_recoded.parquet"
    new_item_ids: set[str] = set()
    if force_consolidation:
        new_item_ids = set(consolidated_annotations["item_id"])
        if top_verbose:
            logger.info(
                f"Force consolidation: all {len(new_item_ids):,} item_ids treated as changed."
            )
    else:
        new_files = set(files_to_concatenate) - set(latest_filename_list)
        if new_files:
            for fn, df in zip(files_to_concatenate, refined_annotation_dfs):
                if fn in new_files:
                    new_item_ids.update(df["item_id"].tolist())
        if top_verbose and new_item_ids:
            logger.info(
                f"Found {len(new_item_ids):,} changed/newly annotated item_ids from {len(new_files)} new file(s)."
            )

    _t_view = time.perf_counter() - _t_mark

    # ---------------------------------------------------------------
    # save the consolidated annotations
    _t_save = 0.0
    if dry_run:
        logger.info("[CONSOLIDATE] dry run — skipping the annotation saves and ledger update.")
    else:
        if top_verbose:
            logger.info("Saving consolidated annotations...")
        _t_mark = time.perf_counter()
        data_io.save_parquet(
            df=consolidated_annotations,
            storage_location="recoded",
            filename=existing_recoded_fn,
            verbose=verbose,
        )
        _t_save = time.perf_counter() - _t_mark
        if top_verbose:
            logger.info("...done")
    logger.info(
        f"[CONSOLIDATE][TIMING] anno refine={_t_refine:.1f}s load={_t_load:.1f}s "
        f"concat_archive={_t_archive:.1f}s preferred_view={_t_view:.1f}s save={_t_save:.1f}s "
        f"total={time.perf_counter() - _t_start:.1f}s files={len(files_to_concatenate)} "
        f"rows={len(consolidated_annotations):,} changed={len(new_item_ids):,}"
    )

    # ---------------------------------------------------------------
    # update the dataset meta file
    if not dry_run:
        if "machine_annotations" not in dataset_meta:
            dataset_meta["machine_annotations"] = {}
        dataset_meta["machine_annotations"]["filenames"] = files_to_concatenate
        # The preferred version this view was derived under — the fold is only
        # equal to a full rebuild while this matches the current promotion.
        dataset_meta["machine_annotations"]["preferred_version"] = (
            annotation_versioning.get_preferred_version()
        )
        _ = data_io.save_json(
            data=dataset_meta,
            storage_location="recoded",
            filename="consolidated_enrichment_files.json",
        )

    return True, consolidated_annotations, new_item_ids


def rebuild_preferred_annotations_from_archive(verbose: bool = False):
    """Rebuild the preferred recoded annotations from the version archive.

    Fast path used after promoting a version: re-derives
    ``machine_annotations_recoded.parquet`` from the already-built
    ``machine_annotations_all_versions.parquet`` using the preferred
    version (or latest-per-item when nothing is promoted) — no re-refinement of
    raw files. Per-study cached datasets still need a study refresh to pick up
    the change; this only updates the global preferred dataset.

    Args:
        verbose: Whether to print I/O progress.

    Returns:
        The number of rows in the rebuilt preferred dataset, or ``None`` if the
        archive is missing/empty.
    """
    archive_fn = f"{_machine_annotations_label()}_all_versions.parquet"
    recoded_fn = f"{_machine_annotations_label()}_recoded.parquet"
    if not data_io.exists(storage_location="recoded", filename=archive_fn):
        return None
    archive = data_io.load_parquet(storage_location="recoded", filename=archive_fn)
    if archive is None or archive.empty:
        return None

    preferred_version = annotation_versioning.get_preferred_version()
    dedup_cols = (
        ["source_platform", "item_id"] if "source_platform" in archive.columns else ["item_id"]
    )
    if preferred_version is None:
        preferred_df = archive.drop_duplicates(subset=dedup_cols, keep="last").reset_index(
            drop=True
        )
    else:
        preferred_df = annotation_versioning.select_preferred_view(archive, preferred_version)

    data_io.save_parquet(
        df=preferred_df, storage_location="recoded", filename=recoded_fn, verbose=verbose
    )
    return len(preferred_df)
