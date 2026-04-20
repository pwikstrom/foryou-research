import json
import sys
import time
import traceback
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_collection_delete(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Delete a collection: drop its rows from the recoded/metadata parquets,
    remove it from collections_tags.json and every study's SELECTED_COLLECTIONS,
    archive its raw upload files, and invalidate study caches.

    Heavy enough to OOM the data-hub if run inline (loads + rewrites the
    1+ GB collections_recoded.parquet), so it lives on the task-runner.

    After the delete itself, dispatches a study_refresh Cloud Task for each
    affected study so their cached files get rebuilt without the deleted rows.
    """
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.organize_datasets import COLLECTIONS_LABEL
    from fyp.studies import init_study_defs, save_study_defs
    from web_interface.data_service import (
        invalidate_collection_tags_cache,
        study_cache,
    )
    from web_interface.process_manager import start_process
    from web_interface.routes.management_routes import (
        _affected_studies_for_collection,
        _find_raw_file_locations,
    )

    task_args = task_args or {}
    collection_id = (task_args.get("collection_id") or "").strip()
    if not collection_id:
        reporter.fail("Missing collection_id in task_args")
        return None

    init_study_defs()

    recoded_fn = f"{COLLECTIONS_LABEL}_recoded.parquet"
    metadata_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
    tags_fn = f"{COLLECTIONS_LABEL}_tags.json"

    _t_start = time.perf_counter()

    # 1. Load events parquet and discover raw files referenced by this
    # collection. Done first so a load failure leaves all state intact.
    reporter.update_progress(0, f"Loading activity events to plan delete of '{collection_id}'...")
    raw_files: list[str] = []
    events_df = None
    if data_io.exists(storage_location="recoded", filename=recoded_fn):
        events_df = data_io.load_parquet(storage_location="recoded", filename=recoded_fn)
        if events_df is not None and 'collection_id' in events_df.columns:
            mask = events_df['collection_id'].astype(str) == str(collection_id)
            if mask.any() and 'raw_file' in events_df.columns:
                raw_files = (
                    events_df.loc[mask, 'raw_file']
                    .dropna()
                    .astype(str)
                    .unique()
                    .tolist()
                )
    raw_locations = _find_raw_file_locations(raw_files)
    rows_total = len(events_df) if events_df is not None else 0
    rows_to_drop = int(mask.sum()) if events_df is not None and 'collection_id' in (events_df.columns if events_df is not None else []) else 0
    reporter.log(
        f"Loaded {rows_total:,} events; {rows_to_drop:,} belong to '{collection_id}'; "
        f"{len(raw_files)} raw file(s) referenced."
    )

    # 2. Snapshot study_defs and tags so we can roll back the JSON edits if a
    # subsequent parquet rewrite raises.
    reporter.update_progress(15, "Snapshotting study definitions and tags for rollback...")
    study_defs_snapshot = json.loads(json.dumps(fyp_cf.get('study_defs') or {}))
    tags_snapshot: dict | None = None
    if data_io.exists(storage_location="recoded", filename=tags_fn):
        tags_snapshot = data_io.load_json(storage_location="recoded", filename=tags_fn) or {}

    affected_studies = _affected_studies_for_collection(collection_id)
    reporter.log(f"{len(affected_studies)} affected study/studies: {affected_studies or '[]'}")

    try:
        # 3. Update studies.json: drop collection_id from each affected study.
        if affected_studies:
            reporter.update_progress(20, f"Removing '{collection_id}' from {len(affected_studies)} study definition(s)...")
            for sname in affected_studies:
                sel = fyp_cf['study_defs'][sname].get('SELECTED_COLLECTIONS') or []
                fyp_cf['study_defs'][sname]['SELECTED_COLLECTIONS'] = [
                    c for c in sel if c != collection_id
                ]
            save_study_defs()

        # 4. Update collections_tags.json: drop the key.
        if tags_snapshot is not None and collection_id in tags_snapshot:
            reporter.update_progress(25, "Updating collection tags...")
            updated_tags = {k: v for k, v in tags_snapshot.items() if k != collection_id}
            data_io.save_json(
                data=updated_tags, storage_location="recoded", filename=tags_fn
            )

        # 5. Rewrite collections_metadata.parquet without the collection's row.
        # Handles both layouts: collection_id as a column or as the index.
        if data_io.exists(storage_location="recoded", filename=metadata_fn):
            reporter.update_progress(30, "Rewriting collection metadata parquet...")
            md = data_io.load_parquet(storage_location="recoded", filename=metadata_fn)
            if md is not None and not md.empty:
                if 'collection_id' in md.columns:
                    md = md[md['collection_id'].astype(str) != str(collection_id)]
                else:
                    if md.index.name != 'collection_id':
                        md.index.name = 'collection_id'
                    md = md.drop(index=collection_id, errors='ignore')
                data_io.save_parquet(
                    df=md, storage_location="recoded", filename=metadata_fn
                )

        # 6. Rewrite collections_recoded.parquet without the collection's rows.
        if events_df is not None and 'collection_id' in events_df.columns:
            reporter.update_progress(45, f"Rewriting activity events parquet (dropping {rows_to_drop:,} rows)...")
            kept = events_df[events_df['collection_id'].astype(str) != str(collection_id)]
            data_io.save_parquet(
                df=kept, storage_location="recoded", filename=recoded_fn
            )

    except Exception as e:
        # Best-effort rollback of the JSON edits. Parquet rewrites that
        # succeeded before the exception are not rolled back — at this point
        # the caller can re-run delete to converge.
        try:
            fyp_cf['study_defs'] = study_defs_snapshot
            save_study_defs()
        except Exception:
            pass
        try:
            if tags_snapshot is not None:
                data_io.save_json(
                    data=tags_snapshot, storage_location="recoded", filename=tags_fn
                )
        except Exception:
            pass
        reporter.log(traceback.format_exc())
        reporter.fail(f"Delete failed during parquet rewrite: {e}")
        return None

    # 7. Move raw upload files to archive and prune them from each source
    # manifest. Failures here are non-fatal — the collection is already gone
    # from every reference; raw files remaining at source just become orphaned
    # uploads the admin can clean up manually.
    reporter.update_progress(70, f"Archiving {len(raw_locations)} raw upload file(s)...")
    archived: list[str] = []
    archive_failures: list[str] = []
    manifests_to_save: dict[str, dict] = {}
    for src_loc, fn in raw_locations:
        try:
            data_io.move(
                src_storage_location=src_loc,
                dst_storage_location="archive",
                filename=fn,
            )
            archived.append(fn)
            if src_loc not in manifests_to_save:
                if data_io.exists(storage_location=src_loc, filename="ingestion_manifest.json"):
                    manifests_to_save[src_loc] = data_io.load_json(
                        storage_location=src_loc,
                        filename="ingestion_manifest.json",
                        verbose=False,
                    ) or {}
                else:
                    manifests_to_save[src_loc] = {}
            manifests_to_save[src_loc].pop(fn, None)
        except Exception as e:
            reporter.log(f"Failed to archive {src_loc}/{fn}: {e}")
            archive_failures.append(fn)

    for src_loc, manifest in manifests_to_save.items():
        try:
            data_io.save_json(
                data=manifest,
                storage_location=src_loc,
                filename="ingestion_manifest.json",
                verbose=False,
            )
        except Exception as e:
            reporter.log(f"Failed to update manifest for {src_loc}: {e}")

    # 8. Invalidate caches: drop the per-study cache files and the in-memory
    # entries so the next access rebuilds without the deleted collection.
    reporter.update_progress(85, "Invalidating study caches...")
    for sname in affected_studies:
        for cached_file in [
            f"{sname}_recoded.parquet",
            f"{sname}_explorer_metadata.json",
            f"{sname}_comp_interpretations.json",
            f"{sname}_PCA.parquet",
        ]:
            try:
                data_io.remove(storage_location="cache", filename=cached_file)
            except Exception as e:
                reporter.log(f"Failed to remove cache {cached_file}: {e}")
        study_cache.invalidate(sname)

    invalidate_collection_tags_cache()

    # 9. Dispatch a study_refresh for each affected study so the cache rebuilds
    # without the deleted collection. Done from inside this worker so we get
    # the same dispatch path the delete route used to use.
    reporter.update_progress(
        95,
        f"Dispatching study_refresh for {len(affected_studies)} affected study/studies...",
    )
    refresh_dispatched: list[str] = []
    refresh_failed: list[dict] = []
    for sname in affected_studies:
        sub_args = {
            "study_name": sname,
            "refresh_pca": True,
            "refresh_metadata": True,
        }
        success, msg = start_process("study_refresh", None, task_args=sub_args)
        if success:
            refresh_dispatched.append(sname)
        else:
            refresh_failed.append({"study": sname, "error": msg})
            reporter.log(f"study_refresh dispatch for {sname} failed: {msg}")

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "collection_id": collection_id,
        "rows_dropped": rows_to_drop,
        "affected_studies": affected_studies,
        "archived_files": archived,
        "archive_failures": archive_failures,
        "refresh_dispatched": refresh_dispatched,
        "refresh_failed": refresh_failed,
    })
    reporter.update_progress(
        100,
        f"Deleted '{collection_id}': dropped {rows_to_drop:,} rows, archived {len(archived)} raw file(s), "
        f"queued {len(refresh_dispatched)} study refresh(es) ({_t_total:.0f}s).",
    )
    reporter.log(
        f"[TIMING] collection_delete total={_t_total:.1f}s "
        f"rows_dropped={rows_to_drop} archived={len(archived)} "
        f"affected={len(affected_studies)} refresh_dispatched={len(refresh_dispatched)} "
        f"refresh_failed={len(refresh_failed)}"
    )

    return None




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Delete a collection")
    parser.add_argument('--collection-id', required=True,
                        help='Collection ID to delete.')
    args = parser.parse_args()

    reporter = LocalStatusReporter("collection_delete")
    try:
        run_collection_delete(
            reporter=reporter,
            task_args={"collection_id": args.collection_id},
        )
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
