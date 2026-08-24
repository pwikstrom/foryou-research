"""API routes for the "My Collections" My-stuff page.

Access is by ownership (``user_id`` links in collections_tags.json), not study
membership — see :func:`._access.owned_collection_access_error`. Everything
served here is computed from donated activity data only (no scrape, no
annotation): the participant sees their own data the moment it is ingested.
"""

import os

from flask import Blueprint, jsonify, request
from flask_login import current_user
from werkzeug.utils import secure_filename

from ..permissions import permission_required
from ._access import current_user_ctx, owned_collection_access_error

my_collections_bp = Blueprint('my_collections_bp', __name__)


def _pending_owner_error(raw_path: str, filename: str):
    """403 unless the manifest entry for this pending upload belongs to the
    current user (admins and Edit Collections holders pass — same policy as
    ``owned_collection_access_error``). Returns (error_response, entry)."""
    import fyp.data_io as data_io
    from ..services.my_collections_service import MANIFEST_FILENAME, donation_upload_sources

    if raw_path not in {s["raw_path"] for s in donation_upload_sources()}:
        return (jsonify({"error": "Unknown donation platform"}), 400), None
    manifest = {}
    if data_io.exists(storage_location=raw_path, filename=MANIFEST_FILENAME):
        manifest = data_io.load_json(
            storage_location=raw_path, filename=MANIFEST_FILENAME, verbose=False) or {}
    entry = manifest.get(filename)
    if not isinstance(entry, dict):
        return (jsonify({"error": "No pending upload with that name"}), 404), None
    username, _role, is_admin = current_user_ctx()
    can_access = getattr(current_user, "can_access", None)
    privileged = is_admin or (callable(can_access) and can_access("tab.data_management.edit_collections"))
    if not privileged and entry.get("user_id") != username:
        return (jsonify({"error": "This upload is not linked to your account"}), 403), None
    return None, entry


@my_collections_bp.route('/api/my/collections')
@permission_required('tab.my_stuff.my_collections')
def api_my_collections():
    """List the current user's own collections with light picker metadata.

    ``?fresh=1`` bypasses the sidecar RAM cache and the service caches — used
    right after an ingest run so pending cards flip to ready immediately.
    """
    from ..services import my_collections_service as svc
    if request.args.get('fresh'):
        svc.invalidate_cache()
        from ..collection_accounts import collections_for_user
        collections_for_user(current_user.username, fresh=True)
        from ..services.study_data import get_collection_tags
        get_collection_tags(force_reload=True)
    return jsonify({"collections": svc.list_owned_collections(current_user.username)})


@my_collections_bp.route('/api/my/collections/upload/sources')
@permission_required('tab.my_stuff.my_collections')
def api_my_upload_sources():
    """The donation platforms a participant can upload to (registry-driven)."""
    from ..services.my_collections_service import donation_upload_sources
    return jsonify({"sources": donation_upload_sources()})


@my_collections_bp.route('/api/my/collections/upload', methods=['POST'])
@permission_required('tab.my_stuff.my_collections')
def api_my_upload():
    """Self-serve donation upload: simplified clone of the admin ingestion
    upload. No tags, no account choice (always the logged-in user), filename
    becomes the collection id (auto-suffixed on collision), browser-detected
    timezone accepted silently.
    """
    import fyp.data_io as data_io
    from fyp.fyp_config import fyp_cf
    from fyp.ingest import parse_donor_timezone
    from .. import activity_log
    from ..collection_accounts import set_collection_owner
    from ..services.my_collections_service import (
        MANIFEST_FILENAME,
        _load_metadata_personas,
        donation_upload_sources,
        invalidate_cache,
    )
    from ..services.study_data import get_collection_tags

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400

    raw_path_key = request.form.get('raw_path', '')
    source = next((s for s in donation_upload_sources() if s["raw_path"] == raw_path_key), None)
    if source is None:
        return jsonify({"error": "Unknown donation platform"}), 400

    accepted = source["accepted_upload_suffixes"]
    for file in files:
        if not file.filename:
            continue
        if accepted and not any(file.filename.lower().endswith(s) for s in accepted):
            msg = (f"'{file.filename}' is not a supported file type for a "
                   f"{source['source_platform']} donation — expected {' or '.join(accepted)}.")
            if file.filename.lower().endswith(".zip") and ".json" in accepted:
                msg += " Unzip the export and upload the extracted .json file."
            return jsonify({"error": msg}), 400

    # Browser-detected timezone: silently dropped if unparseable (it was never
    # typed by the user, so an error message would only confuse).
    donor_tz = request.form.get('tz', '').strip()
    if donor_tz and parse_donor_timezone(donor_tz) is None:
        donor_tz = ''

    username = current_user.username

    manifest = {}
    if data_io.exists(storage_location=raw_path_key, filename=MANIFEST_FILENAME):
        manifest = data_io.load_json(
            storage_location=raw_path_key, filename=MANIFEST_FILENAME, verbose=False) or {}
    meta = _load_metadata_personas(None)
    dataset_ids = set(meta.index.map(str)) if meta is not None else set()
    tags_sidecar = get_collection_tags() or {}

    def _collides(fn: str, cid: str) -> bool:
        m_entry = manifest.get(fn)
        if isinstance(m_entry, dict) and m_entry.get("user_id") != username:
            return True
        if cid in dataset_ids:
            return True
        t_entry = tags_sidecar.get(cid)
        if isinstance(t_entry, dict) and t_entry.get("user_id") not in (None, username):
            return True
        return False

    temp_dir = fyp_cf['paths']['temp']
    os.makedirs(temp_dir, exist_ok=True)

    try:
        uploaded = []
        for file in files:
            if file.filename == '':
                continue
            filename = secure_filename(file.filename)
            base, ext = os.path.splitext(filename)
            cid = base
            # Auto-suffix on collision: someone else's file/collection may
            # already carry this name (filename = collection id). The same
            # user re-uploading the same pending filename replaces it.
            n = 2
            while _collides(filename, cid):
                filename = f"{base}-{n}{ext}"
                cid = f"{base}-{n}"
                n += 1

            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)
            data_io.move(
                src_storage_location="temp",
                dst_storage_location=raw_path_key,
                filename=filename,
                verbose=False,
            )
            if not data_io.exists(storage_location=raw_path_key, filename=filename):
                return jsonify({
                    "error": f"Upload of '{filename}' did not persist. Please try again.",
                }), 500

            manifest[filename] = {"collection_id": cid, "tags": [], "user_id": username}
            if donor_tz:
                manifest[filename]["tz"] = donor_tz
            set_collection_owner(cid, username)
            uploaded.append({"collection_id": cid, "raw_path": raw_path_key,
                             "filename": filename})

        data_io.save_json(data=manifest, storage_location=raw_path_key,
                          filename=MANIFEST_FILENAME, verbose=False)

        activity_log.record(
            actor=username,
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="my_collections.upload",
            target=raw_path_key,
            details={"files": [u["filename"] for u in uploaded], "tz": donor_tz or None},
        )
        invalidate_cache()
        return jsonify({"status": "success", "collections": uploaded})
    except Exception as e:
        print(f"Error in self-serve upload: {e}")
        return jsonify({"error": "The upload failed. Please try again."}), 500


@my_collections_bp.route('/api/my/collections/pending/personality')
@permission_required('tab.my_stuff.my_collections')
def api_my_pending_personality():
    """Instant personality preview of an uploaded-but-unprocessed donation.

    Computed from the raw file with the pipeline's own parser — a parse
    failure is a QA rejection: the file, manifest entry and account link are
    removed and the participant gets a friendly explanation (422).
    """
    from ..services.my_collections_service import (
        PendingPreviewError,
        build_pending_personality,
        discard_pending_upload,
    )
    raw_path = request.args.get('raw_path', '')
    filename = request.args.get('filename', '')
    err, _entry = _pending_owner_error(raw_path, filename)
    if err:
        return err
    try:
        return jsonify(build_pending_personality(raw_path, filename))
    except PendingPreviewError as exc:
        discard_pending_upload(raw_path, filename)
        return jsonify({"error": str(exc), "rejected": True}), 422


@my_collections_bp.route('/api/my/collections/process', methods=['POST'])
@permission_required('tab.my_stuff.my_collections')
def api_my_process():
    """Run the ingest worker over all pending uploads (corpus-wide, same
    process the Data Management page starts). 409 = already running."""
    from fyp.fyp_config import INGEST_REFRESH_SCRIPT
    from .. import activity_log
    from ..process_manager import start_process

    success, msg = start_process("ingest_refresh", INGEST_REFRESH_SCRIPT,
                                 started_by=current_user.username)
    if success:
        activity_log.record(
            actor=current_user.username,
            category=activity_log.CATEGORY_DATA_MANAGEMENT,
            action="my_collections.process",
        )
        return jsonify({"status": "started", "message": msg})
    return jsonify({"status": "error", "message": msg}), 409


@my_collections_bp.route('/api/my/collections/combined/personality')
@permission_required('tab.my_stuff.my_collections')
def api_my_combined_personality():
    """The cross-platform personality bundle over ALL the user's collections.

    Registered before the ``<collection_id>`` route matters not at all to
    Flask's routing (static segments win over converters), but keep the name
    ``combined`` reserved — a collection id can never claim it.
    """
    from ..collection_accounts import collections_for_user
    from ..services.my_collections_service import build_personality
    cids = collections_for_user(current_user.username)
    if not cids:
        return jsonify({"error": "No collections are linked to your account"}), 404
    bundle = build_personality(cids)
    if bundle is None:
        return jsonify({"error": "No donated activity data found for your collections"}), 404
    return jsonify(bundle)


@my_collections_bp.route('/api/my/collections/<collection_id>/personality')
@permission_required('tab.my_stuff.my_collections', 'tab.data_management.edit_collections')
def api_my_collection_personality(collection_id):
    """The personality bundle for one of the user's own collections.

    Also serves the Edit Collections modal (OR-gated on the pipeline
    permission), where the ownership check is waived — see
    ``owned_collection_access_error``.
    """
    err = owned_collection_access_error(collection_id)
    if err:
        return err
    from ..services.my_collections_service import build_personality
    bundle = build_personality([collection_id])
    if bundle is None:
        return jsonify({"error": "No donated activity data found for this collection"}), 404
    return jsonify(bundle)
