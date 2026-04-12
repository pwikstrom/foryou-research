import sys
from pathlib import Path
import pandas as pd
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))





def _init_worker() -> None:
    """Initialise fyp_config and study definitions in a worker process."""
    from fyp.studies import init_study_defs
    init_study_defs()


def process_one_collection(
    collection_id: str,
    preloaded_slice: pd.DataFrame | None,
    viz_vars: list[str],
    first_activity_date: str | None,
) -> bool:
    """Process a single collection: aggregate timeline cache + run analysis.

    This function is designed to run in a worker process. It initialises
    configuration on first call (per-process) and then performs all timeline
    work for one collection.

    Returns:
        True if the collection was processed successfully.
    """
    from fyp.fyp_config import fyp_cf
    from fyp.studies import init_study_defs
    import fyp.data_io as data_io
    from web_interface.data_service import check_and_update_timeline_cache, get_timeline_data
    from fyp.timeline_analysis import analyse_timeline

    # Ensure config is initialised in this process
    if 'studies' not in fyp_cf:
        init_study_defs()

    # Remove existing cache to force recalculation
    for interval in ['day']:#, 'week', 'month']:
        filename = f"timeline_{collection_id}_{interval}.parquet"
        if data_io.exists(storage_location="cache", filename=filename):
            data_io.remove(storage_location="cache", filename=filename)

    # Aggregate
    if not check_and_update_timeline_cache(collection_id, viz_vars, preloaded_df=preloaded_slice):
        return False

    # Analyse for each interval
    for a_interval in ['day']:#, 'week', 'month']:
        try:
            tdata = get_timeline_data(collection_id, interval=a_interval, skip_cache_check=True)
            if tdata and tdata.get("dates"):
                analysis = analyse_timeline(tdata, interval=a_interval, first_activity_date=first_activity_date)
                if analysis:
                    analysis_fname = f"timeline_analysis_{collection_id}_{a_interval}.json"
                    data_io.save_json(analysis, storage_location="cache", filename=analysis_fname)
        except Exception as ae:
            print(f"  Warning: Analysis failed for {collection_id}/{a_interval}: {ae}")

    return True


if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        from web_interface.data_service import check_and_update_timeline_cache, load_schema_metadata
        from fyp.studies import init_study_defs
        from fyp.organize_datasets import COLLECTIONS_LABEL, SCRAPES_LABEL, MACHINE_ANNOTATIONS_LABEL
        import fyp.data_io as data_io
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument('--collections', type=str, default=None,
                            help='Comma-separated collection_ids to refresh (default: all)')
        args = parser.parse_args()

        print("Starting Timeline Refresh Process...")

        # Init configuration
        init_study_defs()

        # Load schema metadata to get viz_vars
        meta = {}
        load_schema_metadata(meta)
        viz_vars = meta.get('timeline_priority', [])

        if 'machine_state' not in viz_vars:
            viz_vars = ['machine_state'] + viz_vars

        if not viz_vars:
            print("Warning: No timeline variables defined in schema (timeline_priority).")

        # Identify accepted collections
        all_collections = set()
        collection_first_event = {}

        # If targeted collections specified, use those directly
        targeted_collections = None
        if args.collections:
            targeted_collections = {s.strip() for s in args.collections.split(',') if s.strip()}
            print(f"Targeted refresh for {len(targeted_collections)} collection(s).")

        # Load from {COLLECTIONS_LABEL}_metadata.parquet
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            try:
                print(f"Loading {COLLECTIONS_LABEL}_metadata.parquet to identify accepted collections...")
                df = data_io.load_parquet(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet", verbose=False)

                if df is not None and not df.empty:
                    # Filter for accepted collections
                    # The user specified the column is ('other', 'accepted')
                    # Check if it exists as a tuple (MultiIndex) or flattened

                    found_col = False
                    if ('other', 'accepted') in df.columns:
                        print("Filtering for ('other', 'accepted') == True")
                        # Ensure boolean comparison, handling potential string/other types safely
                        accepted_mask = df[('other', 'accepted')] == True
                        all_collections = set(df[accepted_mask].index.astype(str))
                        found_col = True
                    elif 'other_accepted' in df.columns: # flatten fallback
                         print("Filtering for 'other_accepted' == True")
                         accepted_mask = df['other_accepted'] == True
                         all_collections = set(df[accepted_mask].index.astype(str))
                         found_col = True

                    if not found_col:
                        print("Warning: Could not find 'accepted' column. Processing ALL collections.")
                        # Fallback to all index
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

                    collection_first_event = {}
                    if first_event_col is not None:
                        for did in all_collections:
                            if did in df.index:
                                ts = df.loc[did, first_event_col]
                                if pd.notna(ts):
                                    collection_first_event[did] = str(ts)[:10]
                        print(f"Loaded first_event_ts for {len(collection_first_event)} collections.")

            except Exception as e:
                print(f"Error loading ddp_metadata: {e}")

        # If still empty, maybe iterate studies?
        if not all_collections:
            print("No collections found in ddp_metadata. Checking studies...")
            # Fallback logic could go here if needed.

        # Filter to targeted collections if specified
        if targeted_collections:
            all_collections = all_collections & targeted_collections if all_collections else targeted_collections

        print(f"Found {len(all_collections)} collections to process.")

        # --- PRELOAD ALL DATA FOR EFFICIENCY ---
        giant_df = None
        if all_collections:
            print("Preloading core datasets to optimize timeline compilation...")
            from fyp.organize_datasets import new_merge
            all_datasets = {}
            for k, f in [(COLLECTIONS_LABEL, f"{COLLECTIONS_LABEL}_recoded.parquet"),
                         (SCRAPES_LABEL, "{SCRAPES_LABEL}_recoded.parquet"),
                         (MACHINE_ANNOTATIONS_LABEL, f"{MACHINE_ANNOTATIONS_LABEL}_recoded.parquet")]:
                 if data_io.exists(storage_location="recoded", filename=f):
                     all_datasets[k] = data_io.load_parquet(storage_location="recoded", filename=f, verbose=False)
                 else:
                     all_datasets[k] = pd.DataFrame()
            try:
                giant_df = new_merge(study_name=None, all_datasets=all_datasets, save_to_cache=False, verbose=False)
                if giant_df is not None and not giant_df.empty and 'collection_id' in giant_df.columns:
                    giant_df['collection_id'] = giant_df['collection_id'].astype(str)
                else:
                     print("Warning: giant_df is empty or missing 'collection_id'.")
            except Exception as e:
                print(f"Error merging core datasets: {e}")
                giant_df = None
        # ---------------------------------------

        total = len(all_collections)
        sorted_collections = sorted(list(all_collections))

        # Pre-slice DataFrames to avoid sending giant_df to each worker
        collection_slices: dict[str, pd.DataFrame | None] = {}
        for cid in sorted_collections:
            if giant_df is not None and not giant_df.empty and 'collection_id' in giant_df.columns:
                collection_slices[cid] = giant_df[giant_df['collection_id'] == str(cid)].copy()
            else:
                collection_slices[cid] = None

        # Free giant_df memory now that slices are extracted
        del giant_df

        # Determine worker count: use up to 4 workers, or fewer if few collections
        max_workers = min(4, total, os.cpu_count() or 1)

        if max_workers <= 1:
            # Sequential fallback for single collection or single CPU
            valid_count = 0
            for i, cid in enumerate(sorted_collections):
                print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Collection {i+1}/{total}\" }}")
                try:
                    if process_one_collection(cid, collection_slices[cid], viz_vars, collection_first_event.get(cid)):
                        valid_count += 1
                except Exception as e:
                    print(f"Error processing {cid}: {e}")
        else:
            print(f"Using {max_workers} parallel workers.")
            valid_count = 0
            completed = 0

            with ProcessPoolExecutor(max_workers=max_workers, initializer=_init_worker) as pool:
                futures = {}
                for cid in sorted_collections:
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
                    print(f"::PROGRESS:: {{ \"percent\": {int((completed/total)*100)}, \"message\": \"Collection {completed}/{total}\" }}")
                    try:
                        if future.result():
                            valid_count += 1
                    except Exception as e:
                        print(f"Error processing {cid}: {e}")

        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print(f"Timeline refresh completed. {valid_count}/{total} updated successfully.")

    except ImportError as e:
        print(f"Import Error: {e}")
        # Fallback if web_interface module import fails due to path issues
        print("Ensure running from project root or correct python path.")
        sys.exit(1)
    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
