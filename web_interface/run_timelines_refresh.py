"""
Timeline refresh: recompute timeline caches and analysis for collections.

On Cloud Run this runs as a self-chaining Cloud Task — each link processes
a batch of collections, then dispatches the next batch until all collections
are done or the user cancels.

Locally it runs all collections in a single subprocess (same as before).
"""

import sys
from pathlib import Path
import pandas as pd
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


# How many collections to process per Cloud Task before chaining.
COLLECTIONS_PER_BATCH = 30

# Cloud Tasks dispatch_deadline — conservative; each batch finishes in ~5 min.
_DISPATCH_DEADLINE = 1800




def process_one_collection(
    collection_id: str,
    preloaded_slice: pd.DataFrame | None,
    viz_vars: list[str],
    first_activity_date: str | None,
) -> bool:
    """Process a single collection: aggregate timeline cache + run analysis.

    Returns:
        True if the collection was processed successfully.
    """
    import fyp.data_io as data_io
    from web_interface.data_service import check_and_update_timeline_cache, get_timeline_data
    from fyp.timeline_analysis import analyse_timeline

    # Remove existing cache to force recalculation
    for interval in ['day']:#, 'week', 'month']:
        filename = f"timeline_{collection_id}_{interval}.parquet"
        if data_io.exists(storage_location="cache", filename=filename):
            data_io.remove(storage_location="cache", filename=filename)

    # Aggregate — returns {interval: agg_df} or None on failure
    agg_result = check_and_update_timeline_cache(collection_id, viz_vars, preloaded_df=preloaded_slice)
    if not agg_result:
        return False

    # Analyse for each interval, passing preloaded agg_df to avoid re-reading cache
    for a_interval in ['day']:#, 'week', 'month']:
        try:
            agg_df = agg_result.get(a_interval)
            tdata = get_timeline_data(collection_id, interval=a_interval,
                                      skip_cache_check=True, preloaded_agg_df=agg_df)
            if tdata and tdata.get("dates"):
                analysis = analyse_timeline(tdata, interval=a_interval, first_activity_date=first_activity_date)
                if analysis:
                    analysis_fname = f"timeline_analysis_{collection_id}_{a_interval}.json"
                    data_io.save_json(analysis, storage_location="cache", filename=analysis_fname)
        except Exception as ae:
            print(f"  Warning: Analysis failed for {collection_id}/{a_interval}: {ae}")

    return True




def _discover_collections(reporter: TaskStatusReporter,
                          targeted_ids: set[str] | None,
                          ) -> tuple[list[str], dict[str, str]]:
    """Discover accepted collections and their first_event_ts.

    Returns:
        (sorted_collection_ids, {collection_id: first_event_date_str})
    """
    from fyp.organize_datasets import COLLECTIONS_LABEL
    import fyp.data_io as data_io

    all_collections: set[str] = set()
    collection_first_event: dict[str, str] = {}

    meta_file = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if data_io.exists(storage_location="recoded", filename=meta_file):
        try:
            reporter.log(f"Loading {meta_file} to identify accepted collections...")
            df = data_io.load_parquet(storage_location="recoded", filename=meta_file, verbose=False)

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

                # Extract first_event_ts map for analysis filtering
                first_event_col = None
                if ('personas', 'first_event_ts') in df.columns:
                    first_event_col = ('personas', 'first_event_ts')
                elif 'first_event_ts' in df.columns:
                    first_event_col = 'first_event_ts'

                if first_event_col is not None:
                    for did in all_collections:
                        if did in df.index:
                            ts = df.loc[did, first_event_col]
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
    from fyp.organize_datasets import (
        COLLECTIONS_LABEL, SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL, new_merge,
    )
    import fyp.data_io as data_io

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
    item_filter = [("item_id", "in", item_ids)] if item_ids else None

    reporter.log(f"Filtered collections to {len(coll_df)} rows, {len(item_ids)} unique items.")

    for k, f in [(SCRAPES_LABEL, f"{SCRAPES_LABEL}_recoded.parquet"),
                 (MACHINE_ANNOTATIONS_LABEL, f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet")]:
        if data_io.exists(storage_location="recoded", filename=f):
            all_datasets[k] = data_io.load_parquet(
                storage_location="recoded", filename=f,
                filters=item_filter, verbose=False)
        else:
            all_datasets[k] = pd.DataFrame()

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
    max_workers = min(4, batch_total, os.cpu_count() or 1)
    valid_count = 0

    if max_workers <= 1:
        for i, cid in enumerate(collection_ids):
            if reporter.check_cancelled():
                reporter.log("Cancellation requested. Stopping.")
                break
            overall_done = collections_processed + i
            pct = int((overall_done / total_collections) * 100) if total_collections else 0
            reporter.update_progress(pct, f"Collection {overall_done + 1}/{total_collections}")
            try:
                if process_one_collection(cid, collection_slices[cid], viz_vars, collection_first_event.get(cid)):
                    valid_count += 1
            except Exception as e:
                reporter.log(f"Error processing {cid}: {e}")
    else:
        reporter.log(f"Using {max_workers} parallel workers.")
        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for cid in collection_ids:
                f = pool.submit(
                    process_one_collection,
                    cid,
                    collection_slices[cid],
                    viz_vars,
                    collection_first_event.get(cid),
                )
                futures[f] = cid

            for future in as_completed(futures):
                cid = futures[future]
                completed += 1
                overall_done = collections_processed + completed
                pct = int((overall_done / total_collections) * 100) if total_collections else 0
                reporter.update_progress(pct, f"Collection {overall_done}/{total_collections}")
                try:
                    if future.result():
                        valid_count += 1
                except Exception as e:
                    reporter.log(f"Error processing {cid}: {e}")

                if reporter.check_cancelled():
                    reporter.log("Cancellation requested. Shutting down workers...")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

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
    reporter.log(f"Batch complete. {valid_count}/{len(batch)} succeeded. "
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
