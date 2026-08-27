import os
import threading
import time

import numpy as np
import pandas as pd
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from flask_login import current_user, login_required

import fyp.data_io as data_io
import fyp.media_paths as media_paths
from fyp.annotation import human_eval
from fyp.fyp_config import fyp_cf
from fyp.ingest import platform_url_templates

from .. import explorer_backend as explorer
from ..data_service import (
    enrich_with_user_tags,
    get_explorer_data,
    get_explorer_rows,
    get_study_col_types,
    invalidate_user_json_cache,
    load_display_id_map,
    load_shared_tags,
)
from ..permissions import permission_required, user_has_permission
from ..security import user_manager
from ._access import study_access_error

viewer_bp = Blueprint('viewer_bp', __name__)


# Built from the collection registry — the same map the frontend gets as
# window.PLATFORM_URL_TEMPLATES, so a server-rendered link and a JS-built one
# can never diverge. Adding a platform = adding its collection subclass.
_PLATFORM_URL_TEMPLATES: dict[str, str] = platform_url_templates()


# Which timezone the viewer's activity span is expressed in, per the timestamp
# column it was computed from. The span is shown verbatim rather than converted,
# so the header has to say which clock it belongs to.
TS_COL_BASIS: dict[str, str] = {
    "utc_timestamp": "UTC",
    "local_timestamp": "participant local time",
    "create_time": "UTC",
}


# Columns this endpoint reads no matter what the request asks for: the id and
# dedup keys, the timestamp candidates it sorts and spans on, the two columns
# behind extra_data_indices, and what enrich_with_user_tags derives its dynamic
# columns from.
_IDS_BASE_COLUMNS = (
    "item_id", "video_id",
    "utc_timestamp", "local_timestamp", "create_time",
    "extra_data", "play_duration",
    "annotated_ok", "annotation_version",
)


# How many index -> timestamp samples the slider's scrub chip gets for the whole
# filtered set. Sending one per row would be tens of thousands of strings; the
# ladder is enough to name the point in time the thumb is over, and every index
# is sampled exactly whenever the result set is this small or smaller.
_SLIDER_TIME_MARKS = 500


def _iso_timestamps(series) -> list:
    """ISO-8601 strings for a timestamp series, ``None`` where unparseable.

    Accepts either a real datetime column or the ISO strings the recoded
    parquets store, and normalizes both to the same wire form so the client
    formats every activity timestamp through one path.
    """
    parsed = pd.to_datetime(series, errors="coerce")
    return [None if pd.isna(v) else pd.Timestamp(v).isoformat() for v in parsed]


def _ids_columns(filters, search_query, full_col_types=None):
    """Columns ``api_viewer_ids`` needs for this request, or None for all.

    The set is fully determined by the active filters, and — when a free-text
    search is active — the columns the search sweeps (every
    category/long_text/list column, plus numbers for numeric terms; mirrors
    ``filter_dataframe``'s Global Search block via ``explorer.search_columns``).
    Projecting keeps the per-request copy of a multi-million-row study small.
    Falls back to the full width only when a search is active but the study's
    column types are unavailable.
    """
    wanted = list(_IDS_BASE_COLUMNS)
    for col in (filters or {}):
        # 'Collection Tags' is a virtual filter resolved against collection_id.
        wanted.append("collection_id" if col == "Collection Tags" else col)
    if search_query:
        if not full_col_types:
            return None
        wanted.extend(explorer.search_columns(full_col_types, search_query))
    return tuple(dict.fromkeys(wanted))


@viewer_bp.route('/api/video_analysis/ids', methods=['POST'])
@permission_required('tab.video_analysis')
def api_viewer_ids():
    data = request.json or {}
    study = data.get("study")

    if not study:
         return jsonify({"error": "No study specified"}), 400

    denied = study_access_error(study)
    if denied is not None:
        return denied

    filters = data.get("filters", {})
    search_query = data.get("search_query")

    # The searchable-column set needs the study's full column types; the frame
    # is warmed by the same call, so a subsequent projected fetch is free.
    full_col_types = get_study_col_types(study) if search_query else None
    df, col_types = get_explorer_data(
        study, context="viewer",
        columns=_ids_columns(filters, search_query,
                             full_col_types=full_col_types),
    )
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    # Enrich with User Tags
    username = current_user.username
    df, col_types = enrich_with_user_tags(df, col_types, username, study=study)

    """df = df[df.scraped_ok].copy()
    print(f"    Filtered to {len(df):,} scraped events")"""

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

    # The header span is a separate choice from the sort key. Sorting is on UTC
    # so rows from different participants interleave in true chronological
    # order, but the span is shown on the participant's own clock to match the
    # detail panel's "Activity timestamp" and the participant-local dates used
    # by Explore and Timelines.
    span_col = next((c for c in ("local_timestamp", "utc_timestamp", "create_time")
                     if c in filtered_df.columns), None)

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

    # Optional "land on this video" lookup, used by the Semantic Space drill-down.
    # The client cannot do this itself: it only ever holds one 1000-row chunk, and
    # item_id is an identifier column so it cannot be passed as a filter. Resolved
    # here against the fully filtered + sorted frame, so the answer is a real
    # position the client can page to. None means the item is not in this study,
    # or the current filters exclude it — the caller says so rather than silently
    # landing on row 0.
    focus_item_id = data.get("focus_item_id")
    focus_index = None
    if focus_item_id and total_count > 0:
        matches = np.flatnonzero(
            filtered_df[id_col].astype(str).to_numpy() == str(focus_item_id)
        )
        if matches.size:
            focus_index = int(matches[0])

    # First / last activity timestamp across the whole filtered set (the
    # chronological span shown in the viewer header). Only needed on the initial
    # chunk; pagination requests reuse the value already on the client.
    time_span = None
    if offset == 0 and span_col is not None and total_count > 0:
        try:
            first_ts = filtered_df[span_col].min()
            last_ts = filtered_df[span_col].max()
            if pd.notna(first_ts) and pd.notna(last_ts):
                # Sent as bare ISO so the client formats it like every other
                # timestamp. ``basis`` names the timezone the span is expressed
                # in, which depends on the column that was available.
                time_span = {
                    "first": pd.Timestamp(first_ts).isoformat(),
                    "last": pd.Timestamp(last_ts).isoformat(),
                    "basis": TS_COL_BASIS.get(span_col, span_col),
                }
        except (ValueError, TypeError):
            time_span = None

    # Coarse index -> timestamp ladder across the WHOLE filtered set, on the same
    # clock as the header span. It labels the slider's scrub chip at positions
    # outside the downloaded chunk, which is most of them once a study runs to
    # tens of thousands of videos. First chunk only; the client keeps it.
    time_marks = None
    if offset == 0 and span_col is not None and total_count > 0:
        positions = np.unique(
            np.linspace(0, total_count - 1, min(total_count, _SLIDER_TIME_MARKS))
            .round().astype(int)
        )
        time_marks = {
            "idx": positions.tolist(),
            "ts": _iso_timestamps(filtered_df[span_col].iloc[positions]),
        }

    # Build global list of indices (0-based) where extra_data is present.
    # Only computed on the first chunk request (offset 0) to avoid repeat work.
    extra_data_indices = None
    if offset == 0 and 'extra_data' in filtered_df.columns:
        mask = filtered_df['extra_data'].notna()
        if 'play_duration' in filtered_df.columns:
            mask = mask & filtered_df['play_duration'].notna() & (filtered_df['play_duration'] != 0)
        # Resolved in one numpy pass. Reading the mask a row at a time through
        # ``.iloc`` cost 2.4s on a 1.5M-row study, on every unpaginated request.
        extra_data_indices = np.flatnonzero(
            mask.fillna(False).to_numpy(dtype=bool)).tolist()

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
        # Exact per-row timestamps for the rows actually returned, so scrubbing
        # inside the loaded chunk names the real activity time rather than the
        # nearest ladder sample.
        "timestamps": _iso_timestamps(chunk[span_col]) if span_col is not None else [],
    }

    if focus_item_id:
        result["focus_index"] = focus_index

    if time_marks is not None:
        result["time_marks"] = time_marks

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
@permission_required('tab.my_stuff.video_tags')
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
@permission_required('tab.my_stuff.video_tags')
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
@permission_required('feature.annotation_votes')
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
@permission_required('tab.video_analysis')
def api_viewer_item(study, item_id):
    denied = study_access_error(study)
    if denied is not None:
        return denied

    # The exact row index resolves duplicate item_id ambiguity (the same video
    # watched twice is two legitimate rows).
    data = {}
    row_idx = None
    if request.method == 'POST':
        data = request.json or {}
        row_idx = data.get("row_idx")

    # Row selection happens against the cached frame, so this never materialises
    # the whole study to return one row.
    df, col_types = get_explorer_rows(study, item_id=item_id, row_index=row_idx)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

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
        for u_name, u_obj in user_manager.get_all_users().items():
            if u_name == username: continue
            if u_obj.settings and u_obj.settings.get('share_annotations'):
                sharing_users.append(u_name)

        if sharing_users:
            shared_simple_map, shared_detailed_map = load_shared_tags(sharing_users)

    df, col_types = enrich_with_user_tags(df, col_types, username,
                                          shared_users_tags=shared_simple_map,
                                          study=study)

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




# Membership check for the media stream: (study, mtime) -> frozenset of item
# ids. The study DataFrame itself is RAM-cached, but an astype(str) scan per
# HTTP Range request would still be O(rows) — the id set makes each chunk
# request a set lookup. Keyed on the recoded parquet's cache mtime so a study
# refresh invalidates the set together with the DataFrame cache.
_STUDY_ID_SET_CACHE: dict[str, tuple[object, frozenset]] = {}
_study_id_set_lock = threading.Lock()






def _study_item_ids(study: str) -> frozenset | None:
    """Return the set of item ids visible in ``study``, or None if unknown."""
    from ..services.study_data import _get_recoded_mtime

    mtime = _get_recoded_mtime(study)
    with _study_id_set_lock:
        entry = _STUDY_ID_SET_CACHE.get(study)
        if entry is not None and entry[0] == mtime:
            return entry[1]

    df, _ = get_explorer_data(
        study, context="viewer", columns=("item_id", "video_id"),
    )
    if df is None:
        return None
    id_col = 'item_id' if 'item_id' in df.columns else (
        'video_id' if 'video_id' in df.columns else None)
    if id_col is None:
        return None
    ids = frozenset(df[id_col].astype(str))
    with _study_id_set_lock:
        _STUDY_ID_SET_CACHE[study] = (mtime, ids)
    return ids






# Eval-stream access: username -> (expiry_monotonic, frozenset of item ids the
# user may stream via the "eval" pseudo-study). Short TTL — task invitations
# change rarely, but Range requests arrive in bursts.
_EVAL_ACCESS_CACHE: dict[str, tuple[float, frozenset]] = {}
_EVAL_ACCESS_TTL_S = 60.0






def _eval_stream_allowed(item_id: str) -> bool:
    """May the current user stream ``item_id`` via the ``eval`` pseudo-study?

    Admin/ab-eval/human-eval permission holders may stream any eval item;
    invited coders may stream items belonging to their coding tasks.
    """
    if (user_has_permission(current_user, 'tab.admin.ab_eval')
            or user_has_permission(current_user, 'tab.admin.human_eval')):
        return True

    username = current_user.username
    with _study_id_set_lock:
        entry = _EVAL_ACCESS_CACHE.get(username)
        if entry is not None and entry[0] > time.monotonic():
            return str(item_id) in entry[1]

    allowed: set[str] = set()
    for index_entry in human_eval.tasks_for_user(username):
        task = human_eval.load_task(index_entry.get("run_id"),
                                    index_entry.get("task_type"))
        if task:
            allowed.update(str(i) for i in task.get("item_ids", []))
    ids = frozenset(allowed)
    with _study_id_set_lock:
        _EVAL_ACCESS_CACHE[username] = (time.monotonic() + _EVAL_ACCESS_TTL_S, ids)
    return str(item_id) in ids






# Cloud Run drops any non-chunked HTTP/1 response over 32 MiB and logs it as a
# 500 (the app never sees an error). Both stream paths below stay under it: a
# ranged reply is capped at MAX_RANGE_CHUNK regardless of what the client asked
# for, and a whole-object reply goes out chunked once it would cross the line.
RESPONSE_SIZE_CAP = 32 * 1024 * 1024
MAX_RANGE_CHUNK = 4096 * 16 * 16  # ~1 MiB


def _parse_byte_range(range_header, total_size, max_chunk=MAX_RANGE_CHUNK):
    """Resolve a Range header into the inclusive ``(start, end)`` to serve.

    Handles the three legal single-range forms — ``N-M``, ``N-`` (open-ended)
    and ``-N`` (the last N bytes, which mp4 players use to fetch a trailing
    moov atom) — and takes the first range of a multi-range list.

    ``end`` is always clamped to ``start + max_chunk - 1``: answering with less
    than was asked for is legal (the player simply requests the rest), and it
    is what keeps an oversized file from being buffered whole and then rejected
    by the platform.

    Args:
        range_header: The raw ``Range`` request header.
        total_size: Size of the object being served, in bytes.
        max_chunk: Largest body to return for one request.

    Returns:
        ``(start, end)``, or ``None`` when the header is malformed or asks for
        an offset at/past the end — RFC 9110 lets a server ignore those, and
        the caller then serves the whole object.
    """
    spec = str(range_header or "").strip()
    if not spec.lower().startswith("bytes="):
        return None
    spec = spec[len("bytes="):].split(",")[0].strip()
    first, sep, last = spec.partition("-")
    if not sep:
        return None
    try:
        if not first:
            # Suffix form: the final `last` bytes of the object.
            suffix = int(last)
            if suffix <= 0:
                return None
            start, end = max(0, total_size - suffix), total_size - 1
        else:
            start = int(first)
            end = int(last) if last else total_size - 1
    except ValueError:
        return None
    if start < 0 or start >= total_size or end < start:
        return None
    return start, min(end, start + max_chunk - 1, total_size - 1)


@viewer_bp.route('/api/video/<study>/<item_id>', methods=['GET'])
@login_required
def api_video_stream(study, item_id):
    # Two callers share this endpoint: the Video Analysis tab streams items of
    # a real study, and the annotation-testing / human-coding pages stream
    # eval-set items under the "eval" pseudo-study (coders typically hold no
    # analysis-tab permissions, so the eval branch has its own gate).
    if study == "eval":
        if not _eval_stream_allowed(item_id):
            return jsonify({"error": "Access denied"}), 403
    else:
        # The Sessions tab embeds the same per-item stream in its episode
        # cards, so either tab permission grants playback (the study-access +
        # item-membership checks below still apply unchanged).
        if not (user_has_permission(current_user, 'tab.video_analysis')
                or user_has_permission(current_user, 'tab.sessions')):
            return jsonify({"error": "Access denied"}), 403

        denied = study_access_error(study)
        if denied is not None:
            return denied

        # The study segment used to be decorative; it now scopes the stream —
        # the item must actually appear in the (accessible) study being viewed.
        known_ids = _study_item_ids(study)
        if known_ids is None:
            return jsonify({"error": "Dataset not found"}), 404
        if str(item_id) not in known_ids:
            return jsonify({"error": "Item not found in this study"}), 404

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

        byte_range = _parse_byte_range(range_header, total_size) if range_header else None
        if byte_range is not None:
            start, end = byte_range

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
            'Content-Type': 'video/mp4',
            'Cache-Control': 'private, max-age=3600',
        }
        # Declaring Content-Length forfeits chunked transfer-encoding, and with
        # it the exemption from the platform's response cap. Only declare it
        # while the body is safely under that cap.
        if total_size < RESPONSE_SIZE_CAP:
            headers['Content-Length'] = str(total_size)
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
