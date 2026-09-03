"""
Timeline refresh: recompute timeline caches and analysis for collections.

On Cloud Run this runs as a self-chaining Cloud Task — each link processes
a batch of collections, then dispatches the next batch until all collections
are done or the user cancels.

Locally it runs all collections in a single subprocess (same as before).
"""

import json
import multiprocessing
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter

# How many collections to process per Cloud Task before chaining.
COLLECTIONS_PER_BATCH = 30

# Cloud Tasks dispatch_deadline — conservative; each batch finishes in ~5 min.
_DISPATCH_DEADLINE = 1800




def _warm_worker_imports() -> None:
    """Resolve everything ``process_one_collection`` imports, single-threaded.

    The per-collection imports below are lazy, so on a cold task-runner the
    first N pool threads all execute them at once. Two threads importing
    different branches of the ``fyp`` tree can deadlock CPython's per-module
    locks, and its deadlock detector resolves that by handing back a
    PARTIALLY-INITIALIZED module rather than raising — so a thread can observe
    an alias shim (``fyp/timeline_analysis.py`` does
    ``sys.modules[__name__] = _real`` as its last statement) before the swap and
    fail with "cannot import name 'analyse_timeline'". It reproduces
    8-times-in-9 with a barrier in front of the same three imports.

    Prod lost 5-7 collections per batch to exactly this on 2026-08-15, and only
    on CHAIN LINKS: link 0 runs ``_discover_collections`` first, which resolves
    the same module single-threaded, so its pool was always warm. Links 1..n
    skip discovery and went straight to the pool. Calling this from the parent
    thread gives every link link-0's head start.

    Since the batch moved onto forked processes the same call matters for a
    second reason: a child forked from a parent whose modules are already
    initialised inherits them by copy-on-write and imports nothing itself.
    """
    import fyp.core.data_io  # noqa: F401
    from fyp.analysis import organize_datasets, timeline_analysis  # noqa: F401
    from web_interface import data_service  # noqa: F401
    from web_interface.services import timeline_service  # noqa: F401


# Whole collections are the work unit and the big ones are big (the largest
# holds ~390k plays), so the pool is capped below the core count to bound the
# batch's peak memory: each child materialises its own exploded frames.
MAX_WORKERS = 6

# Read by forked children through copy-on-write: the per-collection slices,
# the per-collection variable lists and the first-event dates. Nothing in it
# is pickled — a 150k-row slice would cost more to ship than to aggregate.
_FORK_CTX: dict = {}


def _child_init() -> None:
    """Pool-worker initialiser: one BLAS thread per process (see sessions)."""
    try:
        from threadpoolctl import threadpool_limits

        threadpool_limits(1)
    except Exception:
        pass


def _vars_with_prior_coverage(collection_id: str, viz_vars: list[str]) -> list[str]:
    """The requested vars plus whatever the collection's cache already covered.

    Regenerating with the union keeps the study-wide cache a superset, so one
    user's per-user include never evicts another's. Reads the coverage sidecar
    — storage I/O, so this runs in the parent before the pool.
    """
    from web_interface.services.timeline_service import get_timeline_covered_vars

    prior = get_timeline_covered_vars(collection_id, 'day')
    if not prior:
        return list(viz_vars)
    return list(viz_vars) + [v for v in sorted(prior) if v not in viz_vars]


def compute_collection_timeline(
    collection_id: str,
    preloaded_slice: pd.DataFrame,
    viz_vars: list[str],
    first_activity_date: str | None,
) -> tuple[pd.DataFrame, dict | None] | None:
    """Aggregate one collection and analyse it — pure compute, no storage.

    Safe to run in a forked child: everything it needs is in the slice and in
    the in-memory config. Returns the day-level frame and the analysis (None
    when the series is too short to analyse), or None when the slice has no
    aggregatable plays.
    """
    from fyp.analysis.timeline_analysis import analyse_timeline
    from web_interface.services.timeline_service import (
        aggregate_timeline_frame,
        get_timeline_data,
    )

    if preloaded_slice is None or preloaded_slice.empty:
        return None
    agg_df = aggregate_timeline_frame(preloaded_slice, viz_vars, collection_id=collection_id)
    if agg_df is None:
        return None

    analysis: dict | None = None
    try:
        tdata = get_timeline_data(collection_id, interval='day',
                                  skip_cache_check=True, preloaded_agg_df=agg_df)
        if tdata and tdata.get("dates"):
            analysis = analyse_timeline(tdata, interval='day',
                                        first_activity_date=first_activity_date) or None
    except Exception as ae:
        print(f"  Warning: Analysis failed for {collection_id}/day: {ae}")
    return agg_df, analysis


def write_collection_timeline(
    collection_id: str,
    agg_df: pd.DataFrame,
    viz_vars: list[str],
    analysis: dict | None,
) -> None:
    """Persist one collection's timeline parquet, coverage sidecar and analysis.

    The storage half of a refresh; runs in the parent. The analysis JSON is
    written or, when there is none, removed, so the two artefacts never drift.
    """
    import fyp.core.data_io as data_io
    from web_interface.services.timeline_service import save_timeline_cache

    save_timeline_cache(collection_id, agg_df, viz_vars, interval='day')
    analysis_fname = f"timeline_analysis_{collection_id}_day.json"
    if analysis:
        data_io.save_json(analysis, storage_location="cache", filename=analysis_fname)
    elif data_io.exists(storage_location="cache", filename=analysis_fname):
        data_io.remove(storage_location="cache", filename=analysis_fname)


def _run_collection(collection_id: str) -> tuple[str, tuple | None, float]:
    """Pool entry point: aggregate + analyse one collection from _FORK_CTX."""
    ctx = _FORK_CTX
    t0 = time.perf_counter()
    out = compute_collection_timeline(
        collection_id, ctx["slices"][collection_id], ctx["viz_vars"][collection_id],
        ctx["first_event"].get(collection_id))
    return collection_id, out, time.perf_counter() - t0


def process_one_collection(
    collection_id: str,
    preloaded_slice: pd.DataFrame | None,
    viz_vars: list[str],
    first_activity_date: str | None,
) -> bool:
    """Process a single collection end to end: aggregate, analyse, persist.

    The serial path (no pool, or a slice the preload could not produce, in
    which case the unified dataset is loaded from storage as before).

    Returns:
        True if the collection was processed successfully.
    """
    from fyp.analysis.organize_datasets import create_collection_unified_dataset

    viz = _vars_with_prior_coverage(collection_id, viz_vars)
    if preloaded_slice is None:
        preloaded_slice = create_collection_unified_dataset(collection_id=collection_id, verbose=False)
    out = compute_collection_timeline(collection_id, preloaded_slice, viz, first_activity_date)
    if out is None:
        return False
    write_collection_timeline(collection_id, out[0], viz, out[1])
    return True




def _discover_collections(reporter: TaskStatusReporter,
                          targeted_ids: set[str] | None,
                          ) -> tuple[list[str], dict[str, str]]:
    """Discover accepted collections and their first_event_ts.

    Returns:
        (sorted_collection_ids, {collection_id: first_event_date_str})
    """
    import fyp.data_io as data_io
    from fyp.organize_datasets import COLLECTIONS_LABEL
    from fyp.timeline_analysis import MIN_ACTIVE_DAYS_FOR_TIMELINE

    all_collections: set[str] = set()
    collection_first_event: dict[str, str] = {}

    meta_file = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if data_io.exists(storage_location="recoded", filename=meta_file):
        try:
            reporter.log(f"Loading {meta_file} to identify accepted collections...")
            # Project to only the columns this routine reads: the `accepted`
            # flag, `first_event_ts`, and `active_days` (both stored as
            # MultiIndex stringified tuples on disk, with flat-name fallbacks
            # for older files).
            df = data_io.load_parquet_selective(
                storage_location="recoded",
                filename=meta_file,
                columns=[
                    "('other', 'accepted')", "other_accepted",
                    "('personas', 'first_event_ts')", "first_event_ts",
                    "('personas', 'active_days')", "active_days",
                ],
                set_index='collection_id',
                verbose=False,
            )

            if df is not None and not df.empty:
                found_col = False
                if ('other', 'accepted') in df.columns:
                    accepted_mask = df[('other', 'accepted')] == True
                    all_collections = set(df[accepted_mask].index.astype(str))
                    found_col = True
                elif 'other_accepted' in df.columns:
                    accepted_mask = df['other_accepted'] == True
                    all_collections = set(df[accepted_mask].index.astype(str))
                    found_col = True

                if not found_col:
                    reporter.log("Warning: Could not find 'accepted' column. Processing ALL collections.")
                    if df.index.name == 'collection_id':
                        all_collections = set(df.index.astype(str))
                    elif 'collection_id' in df.columns:
                        all_collections = set(df['collection_id'].astype(str))

                # Skip collections with too few active days — not enough data
                # for the 7-day moving average, break detection, or anomaly
                # stats to produce meaningful results.
                active_days_col = None
                if ('personas', 'active_days') in df.columns:
                    active_days_col = ('personas', 'active_days')
                elif 'active_days' in df.columns:
                    active_days_col = 'active_days'

                if active_days_col is not None:
                    before = len(all_collections)
                    eligible = set()
                    skipped_low = []
                    # Column-first access: df.loc[row, <tuple>] re-interprets a
                    # tuple key as a list of column labels when the columns are
                    # flat (fresh installs without personas metadata) — select
                    # the verified column once, then index rows.
                    active_days_series = df[active_days_col]
                    for did in all_collections:
                        if did in df.index:
                            ad = active_days_series.loc[did]
                            if pd.notna(ad) and int(ad) >= MIN_ACTIVE_DAYS_FOR_TIMELINE:
                                eligible.add(did)
                            else:
                                skipped_low.append((did, None if pd.isna(ad) else int(ad)))
                        else:
                            # Without active_days data, keep the collection
                            # rather than silently drop it.
                            eligible.add(did)
                    all_collections = eligible
                    if skipped_low:
                        reporter.log(
                            f"Skipped {len(skipped_low)} of {before} collections "
                            f"with active_days < {MIN_ACTIVE_DAYS_FOR_TIMELINE}."
                        )

                # Extract first_event_ts map for analysis filtering
                first_event_col = None
                if ('personas', 'first_event_ts') in df.columns:
                    first_event_col = ('personas', 'first_event_ts')
                elif 'first_event_ts' in df.columns:
                    first_event_col = 'first_event_ts'

                if first_event_col is not None:
                    first_event_series = df[first_event_col]
                    for did in all_collections:
                        if did in df.index:
                            ts = first_event_series.loc[did]
                            if pd.notna(ts):
                                collection_first_event[did] = str(ts)[:10]
                    reporter.log(f"Loaded first_event_ts for {len(collection_first_event)} collections.")

        except Exception as e:
            reporter.log(f"Error loading metadata: {e}")

    if not all_collections:
        reporter.log("No collections found in metadata.")

    # Filter to targeted collections if specified
    if targeted_ids:
        all_collections = all_collections & targeted_ids if all_collections else targeted_ids

    return sorted(all_collections), collection_first_event




def _preload_and_slice(reporter: TaskStatusReporter,
                       collection_ids: list[str],
                       ) -> dict[str, pd.DataFrame | None]:
    """Preload core datasets, merge, and slice per collection.

    Returns:
        {collection_id: DataFrame_slice_or_None}
    """
    import fyp.data_io as data_io
    from fyp.organize_datasets import (
        COLLECTIONS_LABEL,
        MACHINE_ANNOTATIONS_LABEL,
        SCRAPES_LABEL,
        new_merge,
    )

    reporter.log("Preloading core datasets...")
    all_datasets: dict[str, pd.DataFrame] = {}
    cid_strs = [str(c) for c in collection_ids]

    # Load collections filtered to only this batch's collection_ids
    coll_file = f"{COLLECTIONS_LABEL}_recoded.parquet"
    if data_io.exists(storage_location="recoded", filename=coll_file):
        all_datasets[COLLECTIONS_LABEL] = data_io.load_parquet(
            storage_location="recoded", filename=coll_file,
            filters=[("collection_id", "in", cid_strs)], verbose=False)
    else:
        all_datasets[COLLECTIONS_LABEL] = pd.DataFrame()

    # Extract item_ids from filtered collections to narrow scrapes + annotations
    item_ids: list[str] = []
    coll_df = all_datasets[COLLECTIONS_LABEL]
    if not coll_df.empty and "item_id" in coll_df.columns:
        item_ids = coll_df["item_id"].dropna().unique().tolist()
    item_set = set(map(str, item_ids))

    reporter.log(f"Filtered collections to {len(coll_df)} rows, {len(item_ids)} unique items.")

    # Whole-file read, then filter in memory. A pyarrow pushdown of
    # `item_id in <1.5M ids>` makes the reader evaluate a million-element
    # set-membership predicate per row on every row group — the study refresh
    # dropped the same pattern for the same reason (_filter_enrichment_data).
    _t = time.perf_counter()
    for k, f in [(SCRAPES_LABEL, f"{SCRAPES_LABEL}_recoded.parquet"),
                 (MACHINE_ANNOTATIONS_LABEL, f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet")]:
        if data_io.exists(storage_location="recoded", filename=f):
            full = data_io.load_parquet(storage_location="recoded", filename=f, verbose=False)
            if full is not None and not full.empty and item_set and "item_id" in full.columns:
                all_datasets[k] = full[full["item_id"].astype(str).isin(item_set)].copy()
            else:
                all_datasets[k] = full if full is not None else pd.DataFrame()
            del full
        else:
            all_datasets[k] = pd.DataFrame()
    reporter.log(f"[TIMING] timelines_preload enrichment_load={time.perf_counter() - _t:.1f}s "
                 f"items={len(item_set):,}")

    giant_df = None
    try:
        giant_df = new_merge(study_name=None, all_datasets=all_datasets, save_to_cache=False, verbose=False)
        if giant_df is not None and not giant_df.empty and 'collection_id' in giant_df.columns:
            giant_df['collection_id'] = giant_df['collection_id'].astype(str)
        else:
            reporter.log("Warning: merged dataframe is empty or missing 'collection_id'.")
            giant_df = None
    except Exception as e:
        reporter.log(f"Error merging core datasets: {e}")
        giant_df = None

    slices: dict[str, pd.DataFrame | None] = {}
    for cid in collection_ids:
        if giant_df is not None and not giant_df.empty and 'collection_id' in giant_df.columns:
            slices[cid] = giant_df[giant_df['collection_id'] == str(cid)].copy()
        else:
            slices[cid] = None

    del giant_df
    return slices




def _process_batch(reporter: TaskStatusReporter,
                   collection_ids: list[str],
                   collection_slices: dict[str, pd.DataFrame | None],
                   viz_vars: list[str],
                   collection_first_event: dict[str, str],
                   collections_processed: int,
                   total_collections: int,
                   ) -> int:
    """Process a batch of collections, updating reporter progress.

    Returns:
        Number of successfully processed collections in this batch.
    """
    batch_total = len(collection_ids)
    valid_count = 0
    _t_batch = time.perf_counter()
    # Per-collection compute seconds, so a slow batch can be attributed. Before
    # this the log only said "Collection 9/15" at completion and the 13-minute
    # batch of 2026-09-03 could not be explained from the record.
    unit_secs: dict[str, float] = {}
    done: set[str] = set()

    def _rows(cid: str) -> int:
        s = collection_slices.get(cid)
        return len(s) if s is not None else 0

    def _log_unit(cid: str, ok: bool, write_secs: float = 0.0) -> None:
        reporter.log(f"[TIMING] timelines_collection cid={cid} rows={_rows(cid):,} "
                     f"secs={unit_secs.get(cid, 0.0):.1f} write={write_secs:.1f} ok={int(ok)}")

    def _progress(n_done: int) -> None:
        overall_done = collections_processed + n_done
        pct = int((overall_done / total_collections) * 100) if total_collections else 0
        reporter.update_progress(pct, f"Collection {overall_done}/{total_collections}")

    # Resolve every import once in the parent, before forking — children then
    # inherit fully-initialised modules (see _warm_worker_imports).
    _warm_worker_imports()

    # The coverage sidecars are storage reads: do them here, threaded (I/O
    # bound), so the children never touch storage.
    with ThreadPoolExecutor(max_workers=8) as io_pool:
        viz_by_cid = dict(zip(collection_ids, io_pool.map(
            lambda cid: _vars_with_prior_coverage(cid, viz_vars), collection_ids)))

    # Biggest first: one collection can't be split across workers, so the
    # largest sets the batch's floor — start it at t=0, not last.
    pooled = sorted((cid for cid in collection_ids if collection_slices.get(cid) is not None),
                    key=_rows, reverse=True)
    max_workers = min(len(pooled), os.cpu_count() or 1, MAX_WORKERS)
    if "fork" not in multiprocessing.get_all_start_methods():
        max_workers = 1

    if max_workers > 1:
        # Threads were what ran here before, and they delivered no
        # parallelism: the aggregation is pandas over object columns (lists,
        # strings) and holds the GIL — measured 2026-09-03, 8 threads ran 8
        # units in 27 s against 3.5 s for one (1.1×); 8 forked processes ran
        # them in 4.6 s (6.2×). The children compute only; the parent writes.
        reporter.log(f"Using {max_workers} worker processes.")
        _FORK_CTX.clear()
        _FORK_CTX.update({"slices": collection_slices, "viz_vars": viz_by_cid,
                          "first_event": collection_first_event})
        try:
            with warnings.catch_warnings():
                # Python 3.12 warns that forking a multi-threaded process may
                # deadlock the child: these children do pure compute on
                # inherited memory and take no locks, which is the safe case.
                warnings.filterwarnings("ignore", message=".*fork.*",
                                        category=DeprecationWarning)
                mp_ctx = multiprocessing.get_context("fork")
                with ProcessPoolExecutor(max_workers=max_workers, mp_context=mp_ctx,
                                         initializer=_child_init) as pool:
                    futures = [pool.submit(_run_collection, cid) for cid in pooled]
                    for future in as_completed(futures):
                        cid, out, secs = future.result()
                        unit_secs[cid] = secs
                        done.add(cid)
                        ok = out is not None
                        t_w = time.perf_counter()
                        if ok:
                            try:
                                write_collection_timeline(cid, out[0], viz_by_cid[cid], out[1])
                                valid_count += 1
                            except Exception as e:
                                ok = False
                                reporter.log(f"Error writing {cid}: {e}")
                        _log_unit(cid, ok, time.perf_counter() - t_w)
                        _progress(len(done))
                        if reporter.check_cancelled():
                            reporter.log("Cancellation requested. Shutting down workers...")
                            pool.shutdown(wait=False, cancel_futures=True)
                            return valid_count
        except Exception as e:
            reporter.log(f"[TIMELINES] worker pool failed ({type(e).__name__}: {e}) "
                         f"— finishing this batch serially")
        finally:
            _FORK_CTX.clear()

    # Serial path: everything the pool did not finish, plus any collection
    # whose slice the preload could not produce (loaded from storage inside).
    pending = [cid for cid in collection_ids if cid not in done]
    for cid in pending:
        if reporter.check_cancelled():
            reporter.log("Cancellation requested. Stopping.")
            break
        t0 = time.perf_counter()
        try:
            ok = process_one_collection(
                cid, collection_slices.get(cid), viz_vars, collection_first_event.get(cid))
            if ok:
                valid_count += 1
        except Exception as e:
            ok = False
            reporter.log(f"Error processing {cid}: {e}")
        unit_secs[cid] = time.perf_counter() - t0
        done.add(cid)
        _log_unit(cid, ok)
        _progress(len(done))

    if unit_secs:
        slowest = max(unit_secs, key=unit_secs.get)
        reporter.log(
            f"[TIMING] timelines_batch collections={len(unit_secs)} ok={valid_count} "
            f"wall={time.perf_counter() - _t_batch:.1f}s unit_sum={sum(unit_secs.values()):.1f}s "
            f"unit_max={unit_secs[slowest]:.1f}s slowest={slowest} rows_slowest={_rows(slowest):,} "
            f"workers={max_workers}")
    return valid_count




def _load_viz_vars(reporter: TaskStatusReporter) -> list[str]:
    """Load timeline viz_vars from schema metadata."""
    from web_interface.data_service import load_schema_metadata

    meta: dict = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])

    if 'machine_state' not in viz_vars:
        viz_vars = ['machine_state'] + viz_vars

    if not viz_vars:
        reporter.log("Warning: No timeline variables defined in schema (timeline_priority).")

    return viz_vars




def run_timelines_refresh(reporter: TaskStatusReporter,
                          task_args: dict | None = None,
                          batch_size: int = COLLECTIONS_PER_BATCH,
                          ) -> dict | None:
    """Refresh timeline caches for collections.

    On Cloud Run this processes one batch of collections per Cloud Task and
    returns chain info for the next batch.  Locally (via __main__) it runs
    all collections in a single invocation.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional dict with keys:
            - collections: comma-separated collection IDs (targeted refresh)
            - remaining_collections: comma-separated IDs still to process (set by chain)
            - chunk_index: which chain link (0-based)
            - collections_processed: cumulative count from prior links
            - total_collections: total across all links (set by first link)
            - first_event_map_json: JSON string of {cid: date_str} (passed through chain)

    Returns:
        dict with chain=True and next_task_args if more collections remain,
        or None when done.
    """
    from fyp.studies import init_study_defs

    task_args = task_args or {}
    chunk_index: int = int(task_args.get("chunk_index", 0))
    collections_processed: int = int(task_args.get("collections_processed", 0))

    reporter.log("Starting Timeline Refresh Process...")
    init_study_defs()

    # Load viz_vars (needed every link)
    viz_vars = _load_viz_vars(reporter)

    # --- Determine which collections to process ---
    remaining_str = task_args.get("remaining_collections")

    if remaining_str:
        # Chain continuation — collections already discovered
        all_sorted = [s.strip() for s in remaining_str.split(',') if s.strip()]
        total_collections = int(task_args.get("total_collections", len(all_sorted) + collections_processed))
        # Restore first_event map from chain args
        first_event_json = task_args.get("first_event_map_json", "{}")
        collection_first_event: dict[str, str] = json.loads(first_event_json)
        reporter.log(f"Chain link {chunk_index}: {len(all_sorted)} collections remaining.")
    else:
        # First link — discover collections
        targeted_ids = None
        collections_str = task_args.get("collections")
        if collections_str:
            targeted_ids = {s.strip() for s in collections_str.split(',') if s.strip()}
            reporter.log(f"Targeted refresh for {len(targeted_ids)} collection(s).")

        all_sorted, collection_first_event = _discover_collections(reporter, targeted_ids)
        total_collections = len(all_sorted)
        # Say why the number shrank — "Targeted refresh for 94" followed by
        # "Found 93" reads like a bug otherwise. Discovery drops anything not
        # accepted or under MIN_ACTIVE_DAYS_FOR_TIMELINE.
        if targeted_ids and total_collections != len(targeted_ids):
            reporter.log(
                f"Found {total_collections} collections to process "
                f"({len(targeted_ids) - total_collections} of the "
                f"{len(targeted_ids)} requested are not eligible — not "
                f"accepted, or too few active days).")
        else:
            reporter.log(f"Found {total_collections} collections to process.")

    if not all_sorted:
        reporter.log("No collections to process.")
        return None

    # --- Slice this batch ---
    batch = all_sorted[:batch_size]
    remaining_after = all_sorted[batch_size:]

    reporter.log(f"Processing batch of {len(batch)} collections (chunk {chunk_index})...")

    # --- Preload and slice data ---
    collection_slices = _preload_and_slice(reporter, batch)

    # --- Process the batch ---
    valid_count = _process_batch(
        reporter=reporter,
        collection_ids=batch,
        collection_slices=collection_slices,
        viz_vars=viz_vars,
        collection_first_event=collection_first_event,
        collections_processed=collections_processed,
        total_collections=total_collections,
    )

    new_processed = collections_processed + len(batch)
    skipped_note = (
        " (the rest had no aggregatable data yet — e.g. no annotated plays — and were skipped)"
        if valid_count < len(batch) else ""
    )
    reporter.log(f"Batch complete. {valid_count}/{len(batch)} produced timeline data{skipped_note}. "
                 f"Overall: {new_processed}/{total_collections}.")
    reporter.emit_data({"collections_remaining": len(remaining_after)})

    # --- Check whether to chain ---
    if reporter.check_cancelled():
        reporter.log("Cancellation requested. Stopping after this batch.")
        return None

    if not remaining_after:
        reporter.update_progress(100, "Completed")
        reporter.log(f"Timeline refresh completed. {new_processed}/{total_collections} processed.")
        return None

    # More work remains — request a chain dispatch
    # Serialise first_event map so it survives across chain links
    first_event_subset = {cid: collection_first_event[cid]
                          for cid in remaining_after if cid in collection_first_event}

    next_task_args = {
        "remaining_collections": ",".join(remaining_after),
        "chunk_index": chunk_index + 1,
        "collections_processed": new_processed,
        "total_collections": total_collections,
        "first_event_map_json": json.dumps(first_event_subset),
    }
    # Preserve original targeted collections for stats/logging
    if task_args.get("collections"):
        next_task_args["collections"] = task_args["collections"]

    reporter.log(f"Chaining to next batch (chunk_index={chunk_index + 1}, "
                 f"{len(remaining_after)} remaining)...")

    return {
        "chain": True,
        "next_task_args": next_task_args,
        "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
    }




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Refresh timeline caches")
    parser.add_argument('--collections', type=str, default=None,
                        help='Comma-separated collection_ids to refresh (default: all)')
    args = parser.parse_args()

    task_args: dict = {}
    if args.collections:
        task_args["collections"] = args.collections

    reporter = LocalStatusReporter("timelines_refresh")
    try:
        # Local mode: run all collections in one go (no chaining)
        run_timelines_refresh(reporter=reporter, task_args=task_args, batch_size=999_999)
        reporter.complete()
        print("Timeline refresh completed.")
    except Exception as e:
        reporter.fail(str(e))
        print(f"Timeline refresh failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
