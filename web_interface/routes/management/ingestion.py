"""Data ingestion endpoints (/api/manage/ingestion*, collection-metadata refresh)."""

import json
import os

from flask import jsonify, request
from flask_login import login_required
from werkzeug.utils import secure_filename

import fyp.data_io as data_io
from fyp.fyp_config import (
    fyp_cf,
)
from fyp.ingest import get_main_collection, parse_donor_timezone
from fyp.organize_datasets import (
    COLLECTIONS_LABEL,
)

from ... import activity_log
from ...data_service import (
    invalidate_collection_tags_cache,
    load_display_id_map,
)
from ...process_manager import (
    start_process,
)
from ...permissions import permission_required



from ...services.worker_status import (
    _actor,
)



from ._blueprint import management_bp


def _aws_credentials_available() -> bool:
    """Return True when boto3 can resolve credentials (no network call).

    Gates the AIO fetch-from-AWS ingestion source: without credentials the
    fetch can only fail, so the card is hidden entirely. Checks the standard
    boto3 chain (env vars, ~/.aws/credentials, instance metadata config).
    """
    try:
        import boto3
        return boto3.session.Session().get_credentials() is not None
    except Exception:
        return False


@management_bp.route('/api/manage/ingestion/sources', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_sources():
    try:
        main_collection = get_main_collection(verbose=False)
        aws_available = _aws_credentials_available()
        sources = []
        total_pending = 0
        for col in main_collection.collections:
            if getattr(col, "ingestion_mode", "upload") == "fetch" and not aws_available:
                continue
            files: list[dict] = []
            manifest_fn = "ingestion_manifest.json"
            if col.raw_path and data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
                manifest = data_io.load_json(
                    storage_location=col.raw_path, filename=manifest_fn, verbose=False
                ) or {}
                for fn, meta in manifest.items():
                    files.append({
                        "filename": fn,
                        "collection_id": (meta or {}).get("collection_id"),
                        "tags": (meta or {}).get("tags") or [],
                        "tz": (meta or {}).get("tz"),
                    })
            files.sort(key=lambda f: f["filename"])
            pending = len(files)
            total_pending += pending
            sources.append({
                "source_platform": col.source_platform,
                "data_source": col.data_source,
                "raw_path": col.raw_path,
                "class_name": col.__class__.__name__,
                "pending_files": pending,
                "files": files,
                "ingestion_mode": getattr(col, "ingestion_mode", "upload"),
                "zip_member_suffixes": col.zip_member_suffixes(),
                "accepted_upload_suffixes": col.accepted_upload_suffixes(),
            })
        return jsonify({"status": "success", "sources": sources, "total_pending": total_pending})
    except Exception as e:
        print(f"Error getting ingestion sources: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/ingestion/fetch_aio', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def fetch_aio_data():
    """Trigger download of recent AIO donations and metadata from AWS."""
    from fyp.fyp_config import AIO_FETCH_SCRIPT

    if not _aws_credentials_available():
        return jsonify({
            "status": "error",
            "message": "No AWS credentials available - the AIO fetch needs the "
                       "standard boto3 credential chain (see docs/installation.md).",
        }), 409

    hours_back = 24
    if request.is_json and request.json:
        hours_back = int(request.json.get('hours_back', 24))

    success, msg = start_process(
        "aio_fetch",
        AIO_FETCH_SCRIPT,
        task_args={"hours_back": hours_back},
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409

@management_bp.route('/api/manage/ingestion/upload', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def upload_ingestion_file():
    """Upload one or more raw files with optional collection_id and tags metadata.

    Accepts form fields:
        files: one or more files (also accepts legacy single 'file' key)
        raw_path: storage location key (e.g. 'ddp_raw')
        collection_id: explicit collection ID (used when collection_id_mode is 'single')
        collection_id_mode: 'single' | 'per_file' (default 'per_file')
        tags: JSON-encoded list of tag strings
        tz: optional donor timezone (IANA name like 'Asia/Kolkata' or a fixed
            offset like '+05:30') — the authoritative source for local-time
            conversion, overriding any ambiguous timezone label in the export.
    """
    # Accept both multi-file ('files') and legacy single-file ('file') keys
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400

    raw_path_key = request.form.get('raw_path')
    if not raw_path_key:
        return jsonify({"error": "raw_path missing"}), 400

    # Reject file types the target platform's parser cannot read — a mismatch
    # accepted here would fail cryptically (and retry forever) at ingest time.
    accepted_suffixes: list[str] = []
    target_col = None
    for col in get_main_collection(verbose=False).collections:
        if col.raw_path == raw_path_key:
            target_col = col
            accepted_suffixes = col.accepted_upload_suffixes()
            break
    if accepted_suffixes:
        for file in files:
            if not file.filename:
                continue
            if not any(file.filename.lower().endswith(s) for s in accepted_suffixes):
                label = f"{target_col.source_platform} {target_col.data_source}"
                msg = (f"'{file.filename}' is not a supported file type for "
                       f"{label} ingestion — expected {' or '.join(accepted_suffixes)}.")
                if file.filename.lower().endswith(".zip") and ".json" in accepted_suffixes:
                    msg += " Unzip the export and upload the extracted .json file."
                return jsonify({"error": msg}), 400

    # Stage uploads in the local temp dir, then hand off to data_io.move()
    # which routes to GCS (production) or the configured local data dir
    # (dev). Writing directly to the resolved local path skipped GCS
    # entirely on Cloud Run, so manifests pointed at files that only ever
    # lived on the request-handling container's ephemeral filesystem.
    temp_dir = fyp_cf['paths']['temp']
    os.makedirs(temp_dir, exist_ok=True)

    collection_id = request.form.get('collection_id', '').strip()
    collection_id_mode = request.form.get('collection_id_mode', 'per_file')
    tags_json = request.form.get('tags', '[]')
    try:
        tags = json.loads(tags_json) if tags_json else []
    except json.JSONDecodeError:
        tags = []

    # Optional donor timezone (IANA name or fixed offset). Validated here so a
    # typo is rejected at upload rather than silently ignored at ingest time.
    donor_tz = request.form.get('tz', '').strip()
    if donor_tz and parse_donor_timezone(donor_tz) is None:
        return jsonify({
            "error": f"Unrecognised timezone '{donor_tz}'. Use an IANA name "
                     f"(e.g. 'Asia/Kolkata') or a fixed offset (e.g. '+05:30').",
        }), 400

    # Load or create the ingestion manifest for this raw_path
    manifest_fn = "ingestion_manifest.json"
    manifest: dict = {}
    if data_io.exists(storage_location=raw_path_key, filename=manifest_fn):
        manifest = data_io.load_json(
            storage_location=raw_path_key, filename=manifest_fn, verbose=False
        ) or {}

    try:
        uploaded = []
        for file in files:
            if file.filename == '':
                continue
            filename = secure_filename(file.filename)
            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)

            data_io.move(
                src_storage_location="temp",
                dst_storage_location=raw_path_key,
                filename=filename,
                verbose=False,
            )
            # data_io.move() swallows GCS upload failures silently, so confirm
            # the file actually landed before we record it in the manifest.
            if not data_io.exists(storage_location=raw_path_key, filename=filename):
                return jsonify({
                    "error": f"Upload of '{filename}' to '{raw_path_key}' did not persist.",
                }), 500

            if collection_id_mode == "single" and collection_id:
                file_collection_id = collection_id
            else:
                file_collection_id = os.path.splitext(filename)[0]

            manifest[filename] = {
                "collection_id": file_collection_id,
                "tags": tags,
            }
            if donor_tz:
                manifest[filename]["tz"] = donor_tz
            uploaded.append(filename)

        # Save updated manifest
        data_io.save_json(
            data=manifest,
            storage_location=raw_path_key,
            filename=manifest_fn,
            verbose=False
        )

        # Pre-populate collection_annotations.json with tags for each unique collection_id
        if tags:
            _prepopulate_annotations(manifest, tags)

        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.upload",
            target=raw_path_key,
            details={
                "files": uploaded,
                "tags": tags,
                "collection_id_mode": collection_id_mode,
                "tz": donor_tz or None,
            },
        )
        return jsonify({
            "status": "success",
            "message": f"{len(uploaded)} file(s) uploaded.",
            "files": uploaded,
        })
    except Exception as e:
        print(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@management_bp.route('/api/manage/refresh-collection-metadata', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_collection_metadata():
    """Regenerate _metadata.parquet from scratch using all events."""
    from fyp.fyp_config import COLLECTION_METADATA_REFRESH_SCRIPT

    success, msg = start_process(
        "collection_metadata_refresh",
        COLLECTION_METADATA_REFRESH_SCRIPT,
    )
    if success:
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409



@management_bp.route('/api/manage/ingestion/refresh', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def refresh_ingestion_collection():
    from fyp.fyp_config import INGEST_REFRESH_SCRIPT

    success, msg = start_process("ingest_refresh", INGEST_REFRESH_SCRIPT)
    if success:
        activity_log.record(
            actor=_actor(),
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="ingestion.refresh",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409




@management_bp.route('/api/manage/ingestion/ledger', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_ledger():
    """The persistent per-file ingestion ledger (newest first).

    This is what lets the UI show the per-file intake report — rows read,
    rows kept, drop-reason breakdown — for PAST runs, not just the live one.
    Optional ``?platform=`` filter. Entries written before the drop-stats
    extension lack ``processed_rows``/``deduped_rows``/``dropped``; the UI
    renders those as em-dashes.
    """
    platform = (request.args.get("platform") or "").strip() or None

    main_collection = get_main_collection(verbose=False)
    files = (main_collection.ledger or {}).get("files", {}) or {}

    entries = []
    for filename, record in files.items():
        if platform and (record or {}).get("platform") != platform:
            continue
        entry = dict(record or {})
        entry["filename"] = filename
        entries.append(entry)
    entries.sort(key=lambda e: str(e.get("ts_last_seen") or ""), reverse=True)

    return jsonify({"files": entries, "count": len(entries)})




@management_bp.route('/api/manage/ingestion/ledger/unskip', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def unskip_ingestion_ledger_entry():
    """Drop a single filename from the ingestion ledger so it will be
    re-scanned on the next ingestion run. The raw file on disk is left
    untouched.
    """
    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    main_collection = get_main_collection(verbose=False)
    removed = main_collection.remove_from_ledger(filename)
    if not removed:
        return jsonify({
            "status": "noop",
            "message": f"'{filename}' was not in the ledger.",
        })

    main_collection.save_ledger()
    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.ledger_unskip",
        target=filename,
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' removed from the ledger. It will be rescanned on the next ingestion run.",
    })




@management_bp.route('/api/manage/ingestion/structure/warnings', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_warnings():
    """List structure-drift verdicts awaiting review (quarantined + warned files)."""
    from fyp import structure_sentinel

    try:
        return jsonify(structure_sentinel.review_queue())
    except Exception as e:
        print(f"Error loading structure warnings: {e}")
        return jsonify({"error": str(e)}), 500




@management_bp.route('/api/manage/ingestion/structure/approve', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_approve():
    """Approve a quarantined file: fold its structure into the learned baseline
    and drop its ledger entry so the next ingestion run ingests it.
    """
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.approve_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

    main_collection = get_main_collection(verbose=False)
    main_collection.remove_from_ledger(filename)
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_approve",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' approved — its structure is now part of the baseline and it will be ingested on the next refresh.",
    })




@management_bp.route('/api/manage/ingestion/structure/reject', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def structure_reject():
    """Reject a quarantined file: mark it manually excluded so it never ingests."""
    from fyp import structure_sentinel

    payload = request.get_json(silent=True) or {}
    filename = (payload.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename missing"}), 400

    try:
        entry = structure_sentinel.reject_file(filename, reviewed_by=_actor())
    except KeyError:
        return jsonify({"error": f"no structure verdict for '{filename}'"}), 404

    main_collection = get_main_collection(verbose=False)
    if not main_collection.set_ledger_outcome(
        filename, "manually_excluded", note="rejected via structure review"
    ):
        # No ledger entry yet (e.g. the refresh that quarantined it failed
        # before saving) — stamp one directly so the file is still excluded.
        main_collection.update_ledger([{
            "filename": filename,
            "outcome": "manually_excluded",
            "raw_rows": (entry.get("raw_stats") or {}).get("raw_rows") or 0,
            "final_rows": 0,
            "canonical_collection_id": None,
            "merged_with_siblings": [],
            "platform": entry.get("platform"),
            "source": entry.get("source"),
            "notes": "rejected via structure review",
        }])
    main_collection.save_ledger()

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.structure_reject",
        target=filename,
        details={"platform": entry.get("platform"), "source": entry.get("source")},
    )
    return jsonify({
        "status": "success",
        "message": f"'{filename}' rejected — it is excluded from future ingestion runs.",
    })


@management_bp.route('/api/manage/ingestion/clear_pending', methods=['POST'])
@permission_required('tab.data_management.ingestion')
@login_required
def clear_pending_uploads():
    """Drop every pending upload across every registered ingester: delete each
    file from its raw_path storage and reset its manifest to an empty dict.
    Lightweight (no parquet I/O), so safe to run inline on the data-hub.
    """
    main_collection = get_main_collection(verbose=False)
    manifest_fn = "ingestion_manifest.json"

    cleared: list[dict] = []
    failures: list[dict] = []
    total_removed = 0

    for col in main_collection.collections:
        if not col.raw_path:
            continue
        if not data_io.exists(storage_location=col.raw_path, filename=manifest_fn):
            continue
        manifest = data_io.load_json(
            storage_location=col.raw_path, filename=manifest_fn, verbose=False
        ) or {}
        if not manifest:
            continue

        removed_here: list[str] = []
        for fn in list(manifest.keys()):
            try:
                if data_io.exists(storage_location=col.raw_path, filename=fn):
                    data_io.remove(storage_location=col.raw_path, filename=fn)
                removed_here.append(fn)
            except Exception as e:
                failures.append({"raw_path": col.raw_path, "filename": fn, "error": str(e)})
                print(f"[clear_pending_uploads] failed to remove {col.raw_path}/{fn}: {e}")

        try:
            data_io.save_json(
                data={},
                storage_location=col.raw_path,
                filename=manifest_fn,
                verbose=False,
            )
        except Exception as e:
            failures.append({"raw_path": col.raw_path, "filename": manifest_fn, "error": str(e)})
            print(f"[clear_pending_uploads] failed to reset manifest for {col.raw_path}: {e}")

        cleared.append({
            "raw_path": col.raw_path,
            "class_name": col.__class__.__name__,
            "removed_files": removed_here,
        })
        total_removed += len(removed_here)

    activity_log.record(
        actor=_actor(),
        category=activity_log.CATEGORY_DATA_MANAGEMENT,
        action="ingestion.clear_pending",
        details={"total_removed": total_removed, "failures": len(failures)},
    )
    return jsonify({
        "status": "success",
        "total_removed": total_removed,
        "cleared": cleared,
        "failures": failures,
    })





def _prepopulate_annotations(manifest: dict, tags: list[str]) -> None:
    """Merge tags into collection_annotations.json for each unique collection_id in the manifest."""
    annotations: dict = {}
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False
        ) or {}

    seen_ids: set = set()
    for _filename, meta in manifest.items():
        cid = meta.get("collection_id")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            existing = annotations.get(cid, {})
            existing_tags = existing.get("annotation_tags", [])
            merged_tags = sorted(set(existing_tags + tags))
            annotations[cid] = {
                "display_collection_id": existing.get("display_collection_id"),
                "annotation_tags": merged_tags,
                "hidden": existing.get("hidden", False),
            }

    data_io.save_json(
        data=annotations,
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_tags.json",
        verbose=False
    )
    invalidate_collection_tags_cache()





@management_bp.route('/api/manage/ingestion/metadata', methods=['GET'])
@permission_required('tab.data_management.ingestion')
@login_required
def get_ingestion_metadata():
    """Return existing collection IDs and all unique tags for the upload modal."""

    from fyp.organize_datasets import COLLECTIONS_LABEL

    collection_ids: list[str] = []
    all_tags: set[str] = set()

    # Get collection IDs from the per-collection metadata parquet — small
    # enough to read in milliseconds, vs. the multi-GB recoded parquet which
    # would block the upload modal for ~5s while the user waits.
    metadata_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
    if data_io.exists(storage_location="recoded", filename=metadata_fn):
        md = data_io.load_parquet(
            storage_location="recoded",
            filename=metadata_fn,
            verbose=False,
        )
        if md is not None and not md.empty:
            if "collection_id" in md.columns:
                collection_ids = sorted(md["collection_id"].dropna().astype(str).unique().tolist())
            else:
                collection_ids = sorted(str(idx) for idx in md.index.dropna().unique().tolist())

    # Get tags from annotations
    if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_tags.json"):
        annotations = data_io.load_json(
            storage_location="recoded",
            filename=f"{COLLECTIONS_LABEL}_tags.json",
            verbose=False,
        ) or {}
        for ann in annotations.values():
            for tag in ann.get("annotation_tags", []):
                all_tags.add(tag)

    display_ids = load_display_id_map()

    return jsonify({
        "status": "success",
        "collection_ids": collection_ids,
        "display_ids": display_ids,
        "tags": sorted(list(all_tags)),
    })
