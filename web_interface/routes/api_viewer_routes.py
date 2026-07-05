import os

import numpy as np
import pandas as pd
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from flask_login import current_user, login_required

import fyp.data_io as data_io
import fyp.media_paths as media_paths
from fyp.fyp_config import fyp_cf
from fyp.ingest import ForYouBaseCollection

from .. import explorer_backend as explorer
from ..data_service import (
    enrich_with_user_tags,
    get_explorer_data,
    invalidate_user_json_cache,
    load_display_id_map,
    load_shared_tags,
)
from ..security import user_manager

viewer_bp = Blueprint('viewer_bp', __name__)


# Built from the collection registry: every registered collection class that
# declares both a source_platform and a platform_url_template contributes an
# "open on platform" link template. Adding a platform = adding its collection
# subclass; no edit here.
_PLATFORM_URL_TEMPLATES: dict[str, str] = {
    cls.source_platform: cls.platform_url_template
    for cls in ForYouBaseCollection._registry
    if getattr(cls, "source_platform", None) and getattr(cls, "platform_url_template", None)
}


@viewer_bp.route('/api/video_analysis/ids', methods=['POST'])
@login_required
def api_viewer_ids():
    data = request.json or {}
    study = data.get("study")

    if not study:
         return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study, context="viewer")
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    # Enrich with User Tags
    username = current_user.username
    df, col_types = enrich_with_user_tags(df, col_types, username)

    """df = df[df.scraped_ok].copy()
    print(f"    Filtered to {len(df):,} scraped events")"""

    filters = data.get("filters", {})
    search_query = data.get("search_query")

    # Pagination Optional Params
    offset = data.get("offset", 0)
    limit = data.get("limit", 1000)

    filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)

    # Videos are always presented in chronological order. Sort ascending (oldest
    # first) on the first available activity-timestamp column.
    ts_col = next((c for c in ("utc_timestamp", "local_timestamp", "create_time")
                   if c in filtered_df.columns), None)
    if ts_col is not None:
        filtered_df = filtered_df.sort_values(by=ts_col, ascending=True)

    id_col = 'item_id'
    if id_col not in filtered_df.columns:
        if 'video_id' in filtered_df.columns: id_col = 'video_id'
        else: return jsonify({"error": "No ID column found"}), 500



    # Hide Duplicate Videos if requested
    if data.get("hide_duplicates"):
        dedup_col = 'video_id'
        if dedup_col not in filtered_df.columns:
            dedup_col = id_col

        filtered_df = filtered_df.drop_duplicates(subset=[dedup_col], keep='first')


    # Calculate true total count before slicing
    total_count = len(filtered_df)

    # First / last activity timestamp across the whole filtered set (the
    # chronological span shown in the viewer header). Only needed on the initial
    # chunk; pagination requests reuse the value already on the client.
    time_span = None
    if offset == 0 and ts_col is not None and total_count > 0:
        try:
            first_ts = filtered_df[ts_col].min()
            last_ts = filtered_df[ts_col].max()
            if pd.notna(first_ts) and pd.notna(last_ts):
                time_span = {
                    "first": pd.Timestamp(first_ts).strftime("%Y-%m-%d %H:%M"),
                    "last": pd.Timestamp(last_ts).strftime("%Y-%m-%d %H:%M"),
                }
        except (ValueError, TypeError):
            time_span = None

    # Build global list of indices (0-based) where extra_data is present.
    # Only computed on the first chunk request (offset 0) to avoid repeat work.
    extra_data_indices = None
    if offset == 0 and 'extra_data' in filtered_df.columns:
        mask = filtered_df['extra_data'].notna()
        if 'play_duration' in filtered_df.columns:
            mask = mask & filtered_df['play_duration'].notna() & (filtered_df['play_duration'] != 0)
        extra_data_indices = [int(i) for i in range(total_count) if mask.iloc[i]]

    # Slice the series according to pagination
    chunk = filtered_df.iloc[offset : offset + limit]
    chunked_ids = chunk[id_col].astype(str).tolist()
    chunked_row_idxs = chunk.index.tolist()

    # Return display IDs map ONLY for the returned filtered IDs to save bandwidth
    display_map = load_display_id_map()
    relevant_display_ids = {}
    for i in chunked_ids:
        if i in display_map:
            relevant_display_ids[i] = display_map[i]

    result = {
        "ids": chunked_ids,
        "row_idxs": chunked_row_idxs,
        "count": total_count,
        "offset": offset,
        "display_ids": relevant_display_ids,
        "truncated": False,
        "time_span": time_span,
    }

    if extra_data_indices is not None:
        result["extra_data_indices"] = extra_data_indices

    # When the empty result is caused by the recoded parquet missing the
    # enrichment column the current viz config requires (e.g. scraped_ok
    # before the study has been re-recoded), pass the explanation through so
    # the UI can prompt the user to refresh instead of just saying "0 items".
    status = filtered_df.attrs.get('fyp_dataset_status')
    if status and not status.get('ok'):
        result["dataset_status"] = status

    return jsonify(result)


@viewer_bp.route('/api/video_analysis/tags', methods=['GET'])
@login_required
def api_get_tags():
    username = current_user.username
    filename = f"{username}.json"

    if data_io.exists(storage_location = "users", filename = filename):
        user_data = data_io.load_json(storage_location = "users", filename = filename) or {}
        tags = user_data.get('annotations', {})
        return jsonify(tags)
    else:
        return jsonify({})


@viewer_bp.route('/api/video_analysis/tags/save', methods=['POST'])
@login_required
def api_save_tags():
    data = request.json or {}
    # study = data.get("study") # Deprecated for storage
    item_id = str(data.get("item_id")) # Ensure string for consistency
    variable = data.get("variable")
    tags = data.get("tags") # List of tags
    notes = data.get("notes") # Optional free text notes
    closed_tagging = data.get("closed_tagging") # Optional closed tagging value

    username = current_user.username
    username = current_user.username
    print(f"[TAGS] Saving tags for {username}: {item_id} / {variable} -> {tags} (Notes: {len(notes) if notes else 0} chars, CC: {closed_tagging})")

    if not item_id or not variable:
        return jsonify({"error": "Missing required fields"}), 400

    filename = f"{username}.json"

    # Load existing
    user_file_data = {}
    if data_io.exists(storage_location="users", filename=filename):
        user_file_data = data_io.load_json(storage_location="users", filename=filename) or {}

    # Get Annotations Section
    user_data = user_file_data.get('annotations', {})

    # Update structure (Global Item ID centric)
    if item_id not in user_data: user_data[item_id] = {}

    # Save Tags
    user_data[item_id][variable] = tags

    # Save Notes (using suffix convention)
    notes_key = f"{variable}__NOTES"
    if notes and str(notes).strip():
        user_data[item_id][notes_key] = str(notes).strip()
    else:
        # Remove if empty / deleted
        if notes_key in user_data[item_id]:
            del user_data[item_id][notes_key]

    # Save Closed Tags (using suffix convention)
    cc_key = f"{variable}__CLOSED_TAGGING"
    if closed_tagging and str(closed_tagging).strip():
        user_data[item_id][cc_key] = str(closed_tagging).strip()
    else:
        # Remove if empty / deleted
        if cc_key in user_data[item_id]:
             del user_data[item_id][cc_key]

    # Prune empty
    if not tags:
        # If variable exists, delete it
        if variable in user_data[item_id]:
            del user_data[item_id][variable]

    # Check if item_id is now completely empty (no tags AND no notes)
    # We need to check if there are ANY keys left in user_data[item_id]
    if not user_data[item_id]:
        del user_data[item_id]

    # Save
    # Save back to file structure
    user_file_data['annotations'] = user_data

    # print(f"[TAGS] User data after update: {user_data}")
    data_io.save_json(data=user_file_data, storage_location="users", filename=filename)
    invalidate_user_json_cache(username)

    return jsonify({"status": "success", "tags": tags, "notes": notes, "closed_tagging": closed_tagging})


@viewer_bp.route('/api/video_analysis/tags/<path:tag_name>', methods=['DELETE'])
@login_required
def api_delete_tag(tag_name):
    # Decode tag name (it might contain slashes or spaces, though path parameter handles slashes)
    # If tag name has slashes, flask might interpret it as path segments. <path:tag_name> handles this.

    username = current_user.username
    filename = f"{username}.json"

    print(f"[TAGS] Deleting tag '{tag_name}' for user {username}")

    if not data_io.exists(storage_location="users", filename=filename):
        return jsonify({"status": "success", "message": "No tags found"}), 200

    user_file_data = data_io.load_json(storage_location="users", filename=filename) or {}
    user_data = user_file_data.get('annotations', {})
    modified = False

    # Iterate and remove
    # user_data structure: { item_id: { variable: [tags...] } }

    # We need to collect keys to delete to avoid modifying dict while iterating if we were deleting keys,
    # but here we are modifying lists inside.

    items_to_prune = []

    for item_id, item_vars in user_data.items():
        vars_to_prune = []
        for var, tags in item_vars.items():
            # SKIP NOTES AND CLOSED TAGGING
            if var.endswith("__NOTES") or var.endswith("__CLOSED_TAGGING"):
                continue

            if tag_name in tags:
                tags.remove(tag_name)
                modified = True
                if not tags:
                    vars_to_prune.append(var)

        for var in vars_to_prune:
            del item_vars[var]

        # Only prune item if it's completely empty (no tags AND no notes)
        if not item_vars:
            items_to_prune.append(item_id)

    for item_id in items_to_prune:
        del user_data[item_id]

    if modified:
        user_file_data['annotations'] = user_data # Update annotations block
        data_io.save_json(data=user_file_data, storage_location="users", filename=filename)
        invalidate_user_json_cache(username)
        return jsonify({"status": "success", "message": f"Tag '{tag_name}' deleted"})
        return jsonify({"status": "success", "message": "Tag not found in any item"}), 200


@viewer_bp.route('/api/video_analysis/votes', methods=['GET'])
@login_required
def api_get_votes():
    username = current_user.username
    filename = f"{username}.json"

    if data_io.exists(storage_location="users", filename=filename):
        user_data = data_io.load_json(storage_location="users", filename=filename) or {}
        votes = user_data.get('votes', [])
        return jsonify(votes)
    else:
        return jsonify([])


@viewer_bp.route('/api/video_analysis/vote', methods=['POST'])
@login_required
def api_save_vote():
    data = request.json or {}
    item_id = str(data.get("item_id"))

    if not item_id or item_id == "None":
        return jsonify({"error": "Missing required item_id"}), 400

    username = current_user.username
    print(f"[VOTES] Saving vote for {username} on item {item_id}")
    filename = f"{username}.json"

    user_file_data = {}
    if data_io.exists(storage_location="users", filename=filename):
        user_file_data = data_io.load_json(storage_location="users", filename=filename) or {}

    votes = user_file_data.get('votes', [])
    if item_id not in votes:
        votes.append(item_id)
        user_file_data['votes'] = votes
        data_io.save_json(data=user_file_data, storage_location="users", filename=filename)
        invalidate_user_json_cache(username)

    return jsonify({"status": "success", "votes": votes})


@viewer_bp.route('/api/video_analysis/item/<study>/<item_id>', methods=['GET', 'POST'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study, context="viewer")
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        else: return jsonify({"error": "ID column missing"}), 500

    # Try to use the exact row index first (resolves duplicate item_id ambiguity)
    row_idx = None
    if request.method == 'POST':
        data = request.json or {}
        row_idx = data.get("row_idx")

    if row_idx is not None and row_idx in df.index:
        df = df.loc[[row_idx]]
    else:
        # Fallback: filter by item_id (may return multiple rows for duplicate events)
        df = df[df[id_col].astype(str) == str(item_id)]

    if df.empty:
        return jsonify({"error": "Item not found in current context"}), 404

    # Enrich with User Tags (now extremely fast since df is tiny)
    username = current_user.username

    # Check for Shared Annotations
    shared_simple_map = None
    shared_detailed_map = None

    user_settings = current_user.settings or {}
    if user_settings.get('share_annotations'):
        sharing_users = []
        for u_name, u_obj in user_manager.users.items():
            if u_name == username: continue
            if u_obj.settings and u_obj.settings.get('share_annotations'):
                sharing_users.append(u_name)

        if sharing_users:
            shared_simple_map, shared_detailed_map = load_shared_tags(sharing_users)

    df, col_types = enrich_with_user_tags(df, col_types, username, shared_users_tags=shared_simple_map)

    # Apply Context Filters as fallback disambiguation when row_idx was not available
    if row_idx is None and request.method == 'POST':
        filters = data.get("filters", {})
        search_query = data.get("search_query")

        if filters or search_query:
            filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
            if not filtered_df.empty:
                 df = filtered_df

    record = df.iloc[0].replace({np.nan: None}).to_dict()
    # Inject Shared Annotations for this item
    if shared_detailed_map:
        str_id = str(item_id)
        if str_id in shared_detailed_map:
            record['shared_annotations'] = shared_detailed_map[str_id]

    # Inject Display ID
    display_map = load_display_id_map()
    # Check collection_id or item_id itself
    # Usually display_id is mapped from collection_id
    did = record.get('collection_id')
    if did:
        did_str = str(did)
        if did_str in display_map:
            record['display_collection_id'] = display_map[did_str]

    # Inject Platform URL
    src = record.get('source_platform')
    iid = record.get('item_id')
    if src and iid and src in _PLATFORM_URL_TEMPLATES:
        record['platform_url'] = _PLATFORM_URL_TEMPLATES[src].format(item_id=iid)

    return jsonify(record)




@viewer_bp.route('/api/video/<study>/<item_id>', methods=['GET'])
def api_video_stream(study, item_id):

    use_gcs = fyp_cf.get('data_io', {}).get('use_gcs_for_media', True)
    chunk_size = 4096 * 16
    range_header = request.headers.get('Range')

    # Media may live at the per-platform subpath or the legacy flat path;
    # resolve_media owns the fallback order (and caches, so the viewer's
    # repeated Range requests don't re-probe GCS per chunk).
    platform = request.args.get('platform') or None
    resolved = media_paths.resolve_media(item_id, platform=platform)
    if resolved is None:
        return f"Video {item_id} not found", 404

    if use_gcs:
        bucket = fyp_cf.get("data_io", {}).get("bucket")
        if not bucket:
            return "GCS Bucket not available. Check credentials or internet connection.", 503

        blob_name = resolved["blob_name"]
        blob = bucket.blob(blob_name)

        # resolve_media already verified existence and captured the size (and
        # caches both), so no per-request exists()/reload() round-trips.
        total_size = resolved.get("size")
        if total_size is None:
            blob.reload()
            total_size = blob.size

        if range_header:
            range_spec = range_header.replace('bytes=', '').strip()
            parts = range_spec.split('-')
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else min(start + chunk_size * 16 - 1, total_size - 1)
            end = min(end, total_size - 1)

            # Read only the requested range into memory (a single GCS ranged GET) and
            # return it — no long-lived streaming generator holding the GCS connection
            # open. A <video> tag navigated away mid-stream abandons the generator,
            # leaking its connection/fd until GC ("Too many open files" under rapid
            # navigation). Range chunks are small (~1 MB) so buffering is cheap.
            data = blob.download_as_bytes(start=start, end=end)
            headers = {
                'Content-Range': f'bytes {start}-{end}/{total_size}',
                'Accept-Ranges': 'bytes',
                'Content-Length': str(len(data)),
                'Content-Type': 'video/mp4',
                'Cache-Control': 'private, max-age=3600',
            }
            return Response(data, status=206, headers=headers)

        def generate():
            with blob.open("rb") as f:
                while chunk := f.read(chunk_size):
                    yield chunk

        headers = {
            'Accept-Ranges': 'bytes',
            'Content-Length': str(total_size),
            'Content-Type': 'video/mp4',
            'Cache-Control': 'private, max-age=3600',
        }
        return Response(stream_with_context(generate()), headers=headers)

    # Local filesystem path. send_file(conditional=True) serves HTTP Range requests
    # natively AND manages the file handle via the response lifecycle (closed even on
    # client disconnect), avoiding the fd leak a manual streaming generator has.
    media_path = resolved["path"]
    if not os.path.exists(media_path):
        return f"Video {item_id}.mp4 not found", 404
    return send_file(
        media_path,
        mimetype='video/mp4',
        conditional=True,
        max_age=3600,
    )
