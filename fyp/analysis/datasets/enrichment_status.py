"""The enrichment-status table and the consolidation that builds it.

``consolidate_enrichment_data`` consolidates new scrape and annotation
batches and rebuilds (or patches, when only a few items changed) the per-item
``enrichment_status.parquet`` — scraped / annotated flags and source
platform — plus the shadow check that verifies a patch against a full
rebuild.
"""

import datetime as _dt
import re
import time as _time
from collections.abc import Callable

import pandas as pd

import fyp.core.data_io as data_io
from fyp.analysis.datasets import common, refresh
from fyp.analysis.studies import init_study_defs
from fyp.annotation.annotation_refinement import consolidate_and_save_refined_annotations
from fyp.core.artifacts import ENRICHMENT_STATUS_FILE, load_enrichment_status
from fyp.core.logging_setup import get_logger

# Shared memory-probe implementations (fyp.core.memory); the module-private
# aliases keep this file's many existing call sites and the
# [RECODE][MEM]/[ENRICH PATCH][MEM] log lines unchanged.
from fyp.core.runtime import cf as _cf
from fyp.scrape import scrape_contract as _scrape_contract
from fyp.scrape.consolidate import consolidate_and_save_scrape_data
from fyp.scrape.failures import load_failed_scrapes

logger = get_logger(__name__)

# ============================================================================
# Enrichment status
# ============================================================================


def _backfill_source_platform(series: pd.Series) -> pd.Series:
    """Fill missing ``source_platform`` values with the default platform.

    Activity rows ingested before the column existed carry NA, which breaks the
    composite ``(source_platform, item_id)`` join and silently drops the rows
    from the per-platform groupbys below. All pre-column history is TikTok by
    definition — the same argument as the scrape-side backfill in
    ``fyp.scrape.consolidate.consolidate_and_save_scrape_data``. The persisted parquet is
    healed by ``fyp.ingest.ForYouCollection._backfill_source_platform``; this
    guard keeps merges correct before that refresh has run.
    """
    if not series.isna().any():
        return series
    default = _scrape_contract.default_platform(_scrape_contract.load_contract()) or "tiktok"
    return series.fillna(default).astype("string[pyarrow]")


def _merge_flag_columns(
    base_df: pd.DataFrame, frame: pd.DataFrame, cols: list[str]
) -> pd.DataFrame:
    """Left-merge ``cols`` from a consolidated enrichment frame onto base rows.

    The one merge line both the full status rebuild and the incremental patch
    use — shared so the two paths cannot diverge on merge semantics
    (item_id-keyed, left join, duplicates from the frame surface identically).
    """
    return pd.merge(left=base_df, right=frame[["item_id", *cols]], on="item_id", how="left")


_STATUS_FLAG_COLS_SCRAPE = ["scraped_ok", "video_downloaded"]
_STATUS_FLAG_COLS_ANNO = ["annotated_ok", "annotated_fail"]


def status_patch_allowed(scrape_consolidated: bool, annotations_consolidated: bool) -> bool:
    """Whether patch_enrichment_status may replace the full status rebuild.

    Safe only when every input the patch does NOT recompute is provably
    unchanged since the status file was built (per the marker written after
    its save): the collections parquet (drives membership, per-item counts,
    the modal-id-length filter), and each lane's recoded parquet UNLESS that
    lane consolidated this very run (then its fresh frame is what the patch
    merges from). scrape_fail is recomputed wholesale from the failed-scrapes
    store, so its fingerprint does not gate the patch.
    """
    try:
        if not data_io.exists(storage_location="recoded", filename=ENRICHMENT_STATUS_FILE):
            return False
        if not data_io.exists(storage_location="recoded", filename=refresh._STATUS_INPUTS_MARKER):
            return False
        marker = data_io.load_json(
            storage_location="recoded", filename=refresh._STATUS_INPUTS_MARKER
        )
        if not isinstance(marker, dict):
            return False
        fps = refresh.compute_input_fingerprints()
        if not refresh._fp_equal(marker.get("collections_fp"), fps.get("collections_fp")):
            return False
        if not scrape_consolidated and not refresh._fp_equal(
            marker.get("scrapes_fp"), fps.get("scrapes_fp")
        ):
            return False
        if not annotations_consolidated and not refresh._fp_equal(
            marker.get("annotations_fp"), fps.get("annotations_fp")
        ):
            return False
        # Regime flip: when a lane's recoded file did not exist at the last
        # status build (fp None), the whole frame carried that lane's
        # everything-False fallback columns; the first consolidation of that
        # lane switches every row to merge semantics (NA where unmatched), not
        # just the touched ones — a patch would leave the untouched rows on
        # the old regime.
        if scrape_consolidated and marker.get("scrapes_fp") is None:
            return False
        if annotations_consolidated and marker.get("annotations_fp") is None:
            return False
        return True
    except Exception as exc:
        logger.warning(f"    Status-patch eligibility check failed (forcing rebuild): {exc}")
        return False


def patch_enrichment_status(
    touched_ids: set[str],
    scrape_frame: pd.DataFrame | None = None,
    annotation_frame: pd.DataFrame | None = None,
    verbose: bool = False,
) -> pd.DataFrame | None:
    """Patch enrichment_status.parquet for the touched ids instead of rebuilding.

    Scrapes and annotations only left-merge flag columns onto the
    collections-derived base (they cannot change membership,
    nunique_collections, total_observations, source_platform, or the
    modal-id-length filter), so a consolidation whose collections input is
    unchanged only needs to re-derive the flag columns of the touched rows.
    scrape_fail is recomputed for the WHOLE frame from the failed-scrapes
    store each time — cheap, and it covers failed-set changes without delta
    bookkeeping. Callers must have checked :func:`status_patch_allowed`.

    Only the flag columns of lanes whose frame is passed are re-derived; a
    ``None`` frame means that lane did not consolidate (its recoded parquet is
    unchanged since the status build — enforced by status_patch_allowed), so
    its existing flag values are already current and are preserved.

    Returns:
        The patched frame (also saved, with the marker refreshed), or ``None``
        to decline — the caller must then run :func:`update_enrichment_status`.
    """
    _t_start = _time.perf_counter()
    try:
        status = data_io.load_parquet(
            storage_location="recoded", filename=ENRICHMENT_STATUS_FILE, verbose=verbose
        )
    except Exception as exc:
        logger.warning(f"    Could not load enrichment_status for patching: {exc}")
        return None
    if status is None or status.empty or status.index.name != "item_id":
        return None
    required = set(_STATUS_FLAG_COLS_SCRAPE) | set(_STATUS_FLAG_COLS_ANNO)
    if not required <= set(status.columns):
        return None

    ids = {str(i) for i in touched_ids}
    refresh_cols: list[str] = []
    if scrape_frame is not None:
        refresh_cols += _STATUS_FLAG_COLS_SCRAPE
    if annotation_frame is not None:
        refresh_cols += _STATUS_FLAG_COLS_ANNO

    touched_mask = status.index.isin(ids)
    n_touched = int(touched_mask.sum())
    if refresh_cols and n_touched:
        base = status.loc[touched_mask].drop(columns=refresh_cols).reset_index()
        if scrape_frame is not None:
            scr = scrape_frame
            if not scr.empty and {"item_id", *_STATUS_FLAG_COLS_SCRAPE}.issubset(scr.columns):
                scr = scr.loc[scr["item_id"].isin(ids)]
                base = _merge_flag_columns(base, scr, _STATUS_FLAG_COLS_SCRAPE)
            else:
                for col in _STATUS_FLAG_COLS_SCRAPE:
                    base[col] = pd.Series(False, index=base.index, dtype="bool[pyarrow]")
        if annotation_frame is not None:
            ann = annotation_frame
            if not ann.empty and {"item_id", *_STATUS_FLAG_COLS_ANNO}.issubset(ann.columns):
                ann = ann.loc[ann["item_id"].isin(ids)]
                base = _merge_flag_columns(base, ann, _STATUS_FLAG_COLS_ANNO)
            else:
                for col in _STATUS_FLAG_COLS_ANNO:
                    base[col] = pd.Series(False, index=base.index, dtype="bool[pyarrow]")
        base.set_index("item_id", inplace=True)
        # A duplicated item_id in a lane frame would fan the merge out and
        # change the row count — the full rebuild would surface the same
        # duplication, but splicing rows in requires a clean 1:1 patch.
        if len(base) != n_touched:
            logger.warning(
                f"    Status patch declined: merge changed the touched row count "
                f"({n_touched} -> {len(base)})."
            )
            return None
        status = pd.concat([status.loc[~touched_mask], base.reindex(columns=status.columns)])
        status = status.sort_index(kind="mergesort")

    # scrape_fail: full-column recompute. The full rebuild's left-merge yields
    # True for failed ids and NA (not False) elsewhere — reproduce exactly.
    failed_ids = {str(x) for x in load_failed_scrapes()}
    scrape_fail = pd.Series(pd.NA, index=status.index, dtype="bool[pyarrow]")
    if failed_ids:
        scrape_fail[status.index.isin(failed_ids)] = True
    status["scrape_fail"] = scrape_fail

    data_io.save_parquet(
        df=status, storage_location="recoded", filename=ENRICHMENT_STATUS_FILE, verbose=verbose
    )
    refresh._write_status_inputs_marker(verbose=verbose)
    logger.info(
        f"[CONSOLIDATE][TIMING] status PATCH touched={n_touched:,}/{len(ids):,} "
        f"rows={len(status):,} total={_time.perf_counter() - _t_start:.1f}s"
    )
    return status


def update_enrichment_status(
    all_datasets: dict = {}, save_to_disk: bool = True, verbose: bool = False
) -> pd.DataFrame:
    """Rebuild enrichment_status.parquet from collections, scrapes, and annotations."""

    _t_start = _time.perf_counter()
    activity_columns = ["item_id", common.collection_id_column]
    has_platform = "source_platform" in all_datasets[common._collections_label()].columns
    if has_platform:
        activity_columns.append("source_platform")
    combined_activity_data = all_datasets[common._collections_label()][activity_columns]
    if has_platform:
        combined_activity_data = combined_activity_data.copy()
        combined_activity_data["source_platform"] = _backfill_source_platform(
            combined_activity_data["source_platform"]
        )

    named_aggs = {
        "nunique_collections": pd.NamedAgg(column=common.collection_id_column, aggfunc="nunique"),
        "total_observations": pd.NamedAgg(column=common.collection_id_column, aggfunc="count"),
    }
    if has_platform:
        # Cheap per-item platform lookup for queue builders and the annotation
        # guard (an item_id never spans platforms, so "first" is exact).
        named_aggs["source_platform"] = pd.NamedAgg(column="source_platform", aggfunc="first")
    enrichment_status_df = combined_activity_data.groupby("item_id").agg(**named_aggs)
    _t_groupby = _time.perf_counter() - _t_start

    annotation_votes = pd.DataFrame()
    if data_io.exists(storage_location="recoded", filename=ENRICHMENT_STATUS_FILE):
        existing = data_io.load_parquet(
            storage_location="recoded", filename=ENRICHMENT_STATUS_FILE, verbose=verbose
        )
        if "annotation_votes" in existing.columns:
            annotation_votes = existing[["annotation_votes"]].copy()

    enrichment_status_df["nunique_collections"] = enrichment_status_df[
        "nunique_collections"
    ].astype("int64[pyarrow]")

    enrichment_status_df.reset_index(inplace=True)

    # Drop malformed item_ids by keeping only the modal id-length. Item-id length
    # differs by platform (TikTok ~19 digits, Instagram/YouTube ~11 chars), so a
    # single global modal length would drop every shorter-id platform's items;
    # compute the modal length per source_platform when the column is present.
    if len(enrichment_status_df):
        id_len = enrichment_status_df["item_id"].str.len()
        if "source_platform" in enrichment_status_df.columns:
            modal_len = enrichment_status_df.groupby("source_platform")["item_id"].transform(
                lambda s: s.str.len().mode().iloc[0]
            )
        else:
            modal_len = id_len.mode().iloc[0]
        enrichment_status_df = enrichment_status_df[id_len == modal_len].copy()

    scrapes_for_merge = all_datasets.get(common._scrapes_label())
    if (
        scrapes_for_merge is not None
        and not scrapes_for_merge.empty
        and {"item_id", "scraped_ok", "video_downloaded"}.issubset(scrapes_for_merge.columns)
    ):
        enrichment_status_df = _merge_flag_columns(
            enrichment_status_df, scrapes_for_merge, ["scraped_ok", "video_downloaded"]
        )
    else:
        enrichment_status_df["scraped_ok"] = pd.Series(
            False, index=enrichment_status_df.index, dtype="bool[pyarrow]"
        )
        enrichment_status_df["video_downloaded"] = pd.Series(
            False, index=enrichment_status_df.index, dtype="bool[pyarrow]"
        )

    annotations_for_merge = all_datasets.get(common._machine_annotations_label())
    if (
        annotations_for_merge is not None
        and not annotations_for_merge.empty
        and {"item_id", "annotated_ok", "annotated_fail"}.issubset(annotations_for_merge.columns)
    ):
        enrichment_status_df = _merge_flag_columns(
            enrichment_status_df, annotations_for_merge, ["annotated_ok", "annotated_fail"]
        )
    else:
        enrichment_status_df["annotated_ok"] = pd.Series(
            False, index=enrichment_status_df.index, dtype="bool[pyarrow]"
        )
        enrichment_status_df["annotated_fail"] = pd.Series(
            False, index=enrichment_status_df.index, dtype="bool[pyarrow]"
        )

    failed_scrapes = load_failed_scrapes()
    failed_scrapes = pd.DataFrame(failed_scrapes, columns=["item_id"])
    failed_scrapes["scrape_fail"] = True
    failed_scrapes = failed_scrapes.convert_dtypes(dtype_backend="pyarrow")

    enrichment_status_df = pd.merge(
        left=enrichment_status_df, right=failed_scrapes, on="item_id", how="left"
    ).copy()

    enrichment_status_df.set_index("item_id", inplace=True)

    if not annotation_votes.empty:
        enrichment_status_df = pd.merge(
            left=enrichment_status_df,
            right=annotation_votes,
            left_index=True,
            right_index=True,
            how="left",
        ).copy()
    else:
        enrichment_status_df["annotation_votes"] = pd.Series(
            0, index=enrichment_status_df.index, dtype="int64[pyarrow]"
        )

    _t_merges = _time.perf_counter() - _t_start - _t_groupby
    _t_save = 0.0
    if save_to_disk:
        _t_mark = _time.perf_counter()
        data_io.save_parquet(
            df=enrichment_status_df,
            storage_location="recoded",
            filename=ENRICHMENT_STATUS_FILE,
            verbose=verbose,
        )
        # Record what this status file was built from so an unchanged-input
        # consolidation can skip the rebuild entirely. Written AFTER the
        # parquet on purpose (a stale marker forces a rebuild; a premature
        # one could skip a needed rebuild).
        refresh._write_status_inputs_marker(verbose=verbose)
        _t_save = _time.perf_counter() - _t_mark
    logger.info(
        f"[CONSOLIDATE][TIMING] status groupby={_t_groupby:.1f}s merges={_t_merges:.1f}s "
        f"save={_t_save:.1f}s total={_time.perf_counter() - _t_start:.1f}s "
        f"rows={len(enrichment_status_df):,}"
    )

    return enrichment_status_df


def consolidate_enrichment_data(
    force_consolidation: bool = False,
    verbose: bool = False,
    progress_cb: Callable[[float, str], None] | None = None,
    incremental: bool = False,
) -> dict:
    """Consolidate annotation and scrape data from raw sources, then rebuild enrichment status.

    Args:
        force_consolidation: Rebuild from all raw files even when nothing new
            was detected.
        verbose: Emit verbose per-step logging.
        progress_cb: Optional ``(percent, message)`` callback invoked at each
            phase boundary so a caller (the Cloud Task worker) can surface live
            sub-progress instead of the step sitting frozen at 10%. Kept as a
            plain callback so this module stays web-agnostic; defaults to a
            no-op for ad-hoc/CLI callers.
        incremental: Allow the incremental paths — fold only the new batch
            files into the consolidated frames, and patch enrichment_status
            for the touched ids instead of rebuilding it. Every incremental
            path declines to the unchanged full-rebuild code whenever it
            cannot prove equality (and force_consolidation always bypasses
            them). Off by default; the worker passes the admin setting.
    """

    def _progress(pct: float, msg: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    logger.info("\n*** Annotations")
    _progress(15, "Consolidating annotation files…")
    # return_saved_data=False: a quiet lane returns (False, None, set()) instead
    # of downloading its ~0.5 GB recoded blob just to hand it back. When the
    # status rebuild below actually runs, any quiet lane's frame is loaded
    # lazily; when both lanes are quiet and the status inputs are unchanged,
    # nothing corpus-sized is read at all.
    (new_annotations, annotations, new_annotation_ids) = consolidate_and_save_refined_annotations(
        force_consolidation=force_consolidation,
        return_saved_data=False,
        verbose=verbose,
        incremental=incremental,
    )

    logger.info("\n*** Scrape")
    _progress(40, "Consolidating scrape files…")
    (new_scrape_data, scrape_data, new_scrape_ids) = consolidate_and_save_scrape_data(
        force_consolidation=force_consolidation,
        return_saved_data=False,
        verbose=verbose,
        incremental=incremental,
    )

    had_new_data = new_annotations or new_scrape_data

    if not had_new_data and refresh._status_inputs_unchanged(verbose=verbose):
        # No-op fast path: neither lane consolidated and the status file was
        # built from exactly these inputs (measured no-op runs cost 265-335 s
        # without this). The frames are deliberately None — the only prod
        # consumer (run_consolidate_enrichment) reads had_new_data and impact.
        logger.info("\n*** Enrichment status inputs unchanged — skipping status rebuild.")
        _progress(95, "Finalizing…")
        return {
            common._collections_label(): None,
            common._machine_annotations_label(): None,
            common._scrapes_label(): None,
            "had_new_data": False,
            "impact": None,
        }

    changed_item_ids = new_scrape_ids | new_annotation_ids
    collections = None
    status_patched = False

    # Incremental status patch: when the collections input (and every quiet
    # lane's recoded parquet) is provably unchanged since the status file was
    # built, only the touched ids' flag columns can have moved — patch those
    # instead of the measured 75-310 s full rebuild. Declines to the full
    # rebuild on any doubt.
    if (
        incremental
        and not force_consolidation
        and status_patch_allowed(
            scrape_consolidated=bool(new_scrape_data),
            annotations_consolidated=bool(new_annotations),
        )
    ):
        logger.info("\n*** Patching (and saving) data enrichment status...")
        _progress(65, "Patching enrichment status…")
        patched = patch_enrichment_status(
            changed_item_ids,
            scrape_frame=scrape_data if new_scrape_data else None,
            annotation_frame=annotations if new_annotations else None,
            verbose=verbose,
        )
        status_patched = patched is not None
        if status_patched:
            logger.info("...done.")
        else:
            logger.info("Status patch declined — taking the full rebuild path.")

    if not status_patched:

        def _recoded_or_empty(label: str) -> pd.DataFrame:
            fn = f"{label}_recoded.parquet"
            if data_io.exists(storage_location="recoded", filename=fn):
                return data_io.load_parquet(storage_location="recoded", filename=fn)
            return pd.DataFrame()

        collections = data_io.load_parquet(
            filename=f"{common._collections_label()}_recoded.parquet", storage_location="recoded"
        )
        if annotations is None:
            annotations = _recoded_or_empty(common._machine_annotations_label())
        if scrape_data is None:
            scrape_data = _recoded_or_empty(common._scrapes_label())

        logger.info("\n*** Updating (and saving) data enrichment status...")
        _progress(65, "Updating enrichment status…")
        update_enrichment_status(
            all_datasets={
                common._collections_label(): collections,
                common._machine_annotations_label(): annotations,
                common._scrapes_label(): scrape_data,
            },
            verbose=verbose,
        )
        logger.info("...done.")

    fine_results = {
        common._collections_label(): collections,
        common._machine_annotations_label(): annotations,
        common._scrapes_label(): scrape_data,
    }

    fine_results["had_new_data"] = had_new_data

    # Compute consolidation impact: which collections and studies are affected
    # by new data. On the patch path the full collections frame was never
    # loaded — the mapping needs just two columns, which is a fraction of the
    # ~100 MB blob.
    impact = None

    if changed_item_ids and collections is None:
        try:
            collections = data_io.load_parquet_selective(
                storage_location="recoded",
                filename=f"{common._collections_label()}_recoded.parquet",
                columns=["item_id", common.collection_id_column],
            )
        except Exception as exc:
            logger.warning(f"    Could not load the collections mapping for impact: {exc}")
            collections = data_io.load_parquet(
                filename=f"{common._collections_label()}_recoded.parquet",
                storage_location="recoded",
            )

    if changed_item_ids and collections is not None and not collections.empty:
        _progress(85, "Computing impact on studies…")
        logger.info(
            f"\n*** Computing consolidation impact for {len(changed_item_ids):,} changed items..."
        )

        # Drop NA collection_ids — legacy raw_files predating the manifest-based
        # ingest can leave orphan rows with no cid. They don't belong to any
        # collection or study so they shouldn't contribute to impact; including
        # them would also break the sorted() below (NA comparisons raise).
        affected_collection_ids = {
            cid
            for cid in collections.loc[
                collections["item_id"].isin(changed_item_ids), common.collection_id_column
            ].unique()
            if pd.notna(cid)
        }

        if "study_defs" not in _cf():
            init_study_defs()
        affected_studies = []
        for sname, sdef in _cf().get("study_defs", {}).items():
            selected = sdef.get("SELECTED_COLLECTIONS", [])
            if not selected:
                affected_studies.append(sname)
            else:
                cleaned = [
                    re.search(r"\[(.*?)\]", str(s)).group(1)
                    if re.search(r"\[(.*?)\]", str(s))
                    else str(s)
                    for s in selected
                ]
                if affected_collection_ids & set(cleaned):
                    affected_studies.append(sname)

        impact = {
            "changed_item_count": len(changed_item_ids),
            "new_scrape_item_count": len(new_scrape_ids),
            "new_annotation_item_count": len(new_annotation_ids),
            "affected_collection_ids": sorted(affected_collection_ids),
            "affected_study_names": sorted(affected_studies),
            "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        }
        logger.info(
            f"    {len(affected_collection_ids)} collection(s) and {len(affected_studies)} study/studies affected."
        )

    _progress(95, "Finalizing…")
    fine_results["impact"] = impact
    return fine_results


SHADOW_CHECK_FILENAME = "consolidation_shadow_check.json"


def _per_item_signatures(
    df: pd.DataFrame, exclude: frozenset | set = frozenset()
) -> dict[str, str]:
    """Per-item content signatures of a consolidated frame (dtype-insensitive)."""
    from fyp.scrape.consolidate import scrape_value_signatures

    if df is None or df.empty:
        return {}
    if df.index.name == "item_id":
        df = df.reset_index()
    value_cols = [c for c in df.columns if c != "item_id" and c not in exclude]
    return scrape_value_signatures(df, value_cols)


def _signature_mismatch(
    live: pd.DataFrame, shadow: pd.DataFrame, exclude: frozenset | set = frozenset()
) -> dict:
    """Compare two frames per item. Returns {count, sample, column_drift}."""
    live_cols = set() if live is None else set(live.columns) - set(exclude)
    shadow_cols = set() if shadow is None else set(shadow.columns) - set(exclude)
    column_drift = sorted(live_cols ^ shadow_cols)
    shared_exclude = set(exclude) | (live_cols ^ shadow_cols)
    live_sig = _per_item_signatures(live, exclude=shared_exclude)
    shadow_sig = _per_item_signatures(shadow, exclude=shared_exclude)
    bad = sorted(
        item
        for item in set(live_sig) | set(shadow_sig)
        if live_sig.get(item) != shadow_sig.get(item)
    )
    return {"count": len(bad), "sample": bad[:10], "column_drift": column_drift}


def verify_consolidation_equivalence(
    verbose: bool = False,
    progress_cb: Callable[[float, str], None] | None = None,
) -> dict:
    """Shadow full rebuild vs the live consolidated artifacts (read-only).

    The anti-divergence backstop for incremental consolidation: rebuild the
    scrape and annotation frames the full reference path WOULD produce
    (``dry_run=True`` — nothing is persisted, though a pending raw-annotation
    refinement still runs, as it would before any consolidation), derive the
    enrichment status from them in memory, and compare all three against the
    live artifacts by per-item content signature — never by row counts (a
    self-consistent row-count check is not a completeness check). The result
    is persisted to ``recoded/consolidation_shadow_check.json`` so callers can
    schedule by age and surface failures.

    Returns:
        ``{"ok": bool, "checked_at": iso, "mismatches": {artifact: {count,
        sample, column_drift}}}``. The caller decides how to react — the
        expected reaction to ``ok=False`` is alerting plus a real
        ``force_consolidation=True`` run to promote the full rebuild.
    """
    from fyp.scrape.consolidate import SCRAPE_PROVENANCE_COLS

    def _progress(pct: float, msg: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    logger.info("\n*** Shadow-verifying consolidation equivalence...")
    _progress(15, "Shadow rebuild: annotations…")
    (_, shadow_annotations, _) = consolidate_and_save_refined_annotations(
        force_consolidation=True, verbose=verbose, dry_run=True
    )
    _progress(40, "Shadow rebuild: scrapes…")
    (_, shadow_scrapes, _) = consolidate_and_save_scrape_data(
        force_consolidation=True, verbose=verbose, dry_run=True
    )

    mismatches: dict = {}

    _progress(60, "Comparing scrape frames…")
    live_scrapes = data_io.load_parquet(
        storage_location="recoded", filename=f"{common._scrapes_label()}_recoded.parquet"
    )
    mismatches[common._scrapes_label()] = _signature_mismatch(
        live_scrapes, shadow_scrapes, exclude=SCRAPE_PROVENANCE_COLS
    )
    del live_scrapes

    _progress(72, "Comparing annotation frames…")
    live_annotations = data_io.load_parquet(
        storage_location="recoded",
        filename=f"{common._machine_annotations_label()}_recoded.parquet",
    )
    mismatches[common._machine_annotations_label()] = _signature_mismatch(
        live_annotations, shadow_annotations
    )
    del live_annotations

    _progress(85, "Comparing enrichment status…")
    collections = data_io.load_parquet(
        filename=f"{common._collections_label()}_recoded.parquet", storage_location="recoded"
    )
    shadow_status = update_enrichment_status(
        all_datasets={
            common._collections_label(): collections,
            common._machine_annotations_label(): shadow_annotations,
            common._scrapes_label(): shadow_scrapes,
        },
        save_to_disk=False,
        verbose=verbose,
    )
    live_status = load_enrichment_status()
    mismatches["enrichment_status"] = _signature_mismatch(live_status, shadow_status)

    ok = all(m["count"] == 0 and not m["column_drift"] for m in mismatches.values())
    result = {
        "ok": ok,
        "checked_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "mismatches": mismatches,
    }
    try:
        data_io.save_json(data=result, storage_location="recoded", filename=SHADOW_CHECK_FILENAME)
    except Exception as exc:
        logger.warning(f"    Could not persist the shadow-check result: {exc}")

    if ok:
        logger.info("[CONSOLIDATE][SHADOW] OK — incremental artifacts match a full rebuild.")
    else:
        for name, m in mismatches.items():
            if m["count"] or m["column_drift"]:
                logger.error(
                    f"[CONSOLIDATE][SHADOW] MISMATCH artifact={name} items={m['count']} "
                    f"column_drift={m['column_drift']} sample={m['sample']}"
                )
    _progress(95, "Finalizing…")
    return result
