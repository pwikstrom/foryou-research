import traceback

import pandas as pd
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
from fyp.organize_datasets import COLLECTIONS_LABEL

from ..data_service import (
    get_accessible_studies,
    get_study_collections,
    get_timeline_data,
    make_serializable,
)
from ..security import user_manager

timelines_bp = Blueprint('timelines_bp', __name__)


@timelines_bp.route('/api/timelines/vote_annotation', methods=['POST'])
@login_required
def api_save_annotation_vote():
    data = request.json or {}
    collection_id = data.get("collection_id")
    period = data.get("period")

    if not collection_id or not period:
        return jsonify({"error": "Missing required collection_id or period"}), 400

    username = current_user.username
    print(f"[VOTES] Saving machine annotation vote for {username} on collection {collection_id} for period {period}")

    # Use user_manager directly
    success, msg = user_manager.register_annotation_vote(username, collection_id, period)

    if success:
         return jsonify({"status": "success", "message": msg})
    else:
         return jsonify({"error": msg}), 400


@timelines_bp.route('/api/timelines/data', methods=['POST'])
@login_required
def api_timeline_data():
    data = request.json or {}
    #study = data.get("study")
    collection_id = data.get("collection_id")
    interval = data.get("interval", "day")

    if not collection_id:
        return jsonify({"error": "Missing collection_id"}), 400

    # --- ACCESS CONTROL ---
    # Verify user has access to this collection via at least one study
    studies = get_accessible_studies(
        username=current_user.username,
        role=current_user.role,
        is_admin=current_user.is_admin()
    )

    has_access = False
    # This might be slow if we check every time.
    # But for security it's needed.
    # To optimize: we can trust the frontend IF we assume obscure IDs are secret enough?
    # No, strict requirement "user should only see...". Backend must enforce.
    # Optimization: iterate studies, check if collection is in it. Stop at first match.

    for study in studies:
        study_collections = get_study_collections(study)
        # Convert to set of strings for fast lookup
        # (study_collections is cached if we used lru_cache, but we didn't add it yet.
        # explorer_backend.get_explorer_data IS cached.
        # And get_study_collections uses simple load_parquet which hits disk or OS buffer.)

        # Let's just check the ids.
        for d in study_collections:
            if str(d.get('collection_id')) == str(collection_id):
                has_access = True
                break
        if has_access:
            break

    if not has_access:
        return jsonify({"error": "Access denied to this collection"}), 403
    # ----------------------

    try:
        result = get_timeline_data(collection_id, interval=interval)
        if result is None:
             return jsonify({"error": "No data found"}), 404
        if "error" in result:
             return jsonify(result), 400

        return jsonify(make_serializable(result))
    except Exception as e:

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@timelines_bp.route('/api/timelines/collections', methods=['POST'])
@login_required
def api_timeline_collections():
    """
    Returns list of collections ({collection_id, ...}) that the current user
    has access to via their allowed studies.
    """

    # 1. Get Accessible Studies
    studies = get_accessible_studies(
        username=current_user.username,
        role=current_user.role,
        is_admin=current_user.is_admin()
    )

    if not studies:
        return jsonify([])

    # 2. Collect allowed collection IDs from these studies
    allowed_collection_ids = set()
    collection_studies_map: dict[str, list[str]] = {}

    # Iterate studies and get collections (using optimized loader)
    for study in studies:
        study_collections = get_study_collections(study) # returns list of dicts
        #print(f"DEBUG TIMELINE: Study {study} returned {len(study_collections)} collections")
        for d in study_collections:
            # d is {'collection_id': ..., }
            if 'collection_id' in d:
                cid = str(d['collection_id'])
                allowed_collection_ids.add(cid)
                collection_studies_map.setdefault(cid, []).append(study)

    #print(f"DEBUG TIMELINE: Total allowed collection IDs: {len(allowed_collection_ids)}")
    if not allowed_collection_ids:
        return jsonify([])

    # 3. Load Metadata to get details

    # Project to only the columns this handler uses: the `accepted` flag (under
    # the MultiIndex `('other', 'accepted')` form on disk, or the flat
    # 'accepted' name as a fallback), `active_days` so the dropdown can
    # surface timeline-length context and disable sub-threshold collections,
    # and `collection_id` as the index.
    meta_df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
        columns=["('other', 'accepted')", "accepted",
                 "('personas', 'active_days')", "active_days"],
        set_index='collection_id',
    )

    if meta_df is None or meta_df.empty:
        return jsonify([])

    # Reset index if needed
    df_reset = meta_df.reset_index()

    # Resolve column names. `load_parquet_selective` returns tuple column
    # names directly (not wrapped in a MultiIndex), so check for tuples in
    # the plain Index first, then fall back to flat string names.
    accepted_col = None
    active_days_col = None
    cols_set = set(meta_df.columns)
    if ('other', 'accepted') in cols_set:
        accepted_col = ('other', 'accepted')
    elif 'accepted' in cols_set:
        accepted_col = 'accepted'
    if ('personas', 'active_days') in cols_set:
        active_days_col = ('personas', 'active_days')
    elif 'active_days' in cols_set:
        active_days_col = 'active_days'

    filtered = df_reset
    if accepted_col:
        try:
             filtered = df_reset[df_reset[accepted_col] == True]
        except:
             pass

    target_id_col = 'collection_id'
    if target_id_col not in filtered.columns:
        if 'index' in filtered.columns:
             target_id_col = 'index'
        else:
             return jsonify([])

    # FILTER BY ALLOWED IDS
    # Ensure target column is string for comparison
    try:
        # Check if target_id_col is in columns (it might be index moved to col)
        # If duplicated, take first
        s_ids = filtered[target_id_col]
        if isinstance(s_ids, pd.DataFrame):
             s_ids = s_ids.iloc[:, 0]

        # Create mask
        # We need to ensure we align with the filtered DataFrame
        # Easier: Filter the DataFrame

        # We need to handle the case where columns are duplicated (DataFrame result)
        # So let's extract the series specifically
        # handled above

        # We can't use .isin on a DataFrame property if it's duplicated easily without care.
        # But let's assume standard case or handle unique.

        # Let's rebuild the flow slightly to be robust:
        # 1. Get all accepted as before
        # 2. Extract unique tuples of (id, ...)
        # 3. Filter list

    except Exception as e:
        print(f"Error filtering allowed IDs: {e}")
        return jsonify([])

    # Vectorized optimized extraction (re-using previous logic but adding filter)
    try:
        don_ids_series = filtered[target_id_col]
        if isinstance(don_ids_series, pd.DataFrame):
            don_ids_series = don_ids_series.iloc[:, 0]

        unique_ids = don_ids_series.unique().tolist()
        #print(f"DEBUG TIMELINE: Total unique collections in metadata: {len(unique_ids)}")

        # Filter against allowed set
        # Only include if in allowed_collection_ids
        final_valid_ids = [uid for uid in unique_ids if str(uid) in allowed_collection_ids]

        # Build a {collection_id -> active_days} lookup for the dropdown.
        active_days_map: dict[str, int | None] = {}
        if active_days_col is not None:
            try:
                ad_df = filtered[[target_id_col, active_days_col]].dropna(subset=[target_id_col])
                for _, row in ad_df.iterrows():
                    cid = str(row[target_id_col])
                    val = row[active_days_col]
                    if pd.isna(val):
                        active_days_map[cid] = None
                    else:
                        active_days_map[cid] = int(val)
            except Exception:
                pass

        # Load annotations
        da_filename = f"{COLLECTIONS_LABEL}_tags.json"
        annotations = {}
        try:
            if data_io.exists(storage_location="recoded", filename=da_filename):
                annotations = data_io.load_json(storage_location="recoded", filename=da_filename) or {}
        except:
            pass

        final_list = []
        for uid in final_valid_ids:
            if pd.isna(uid): continue
            uid_str = str(uid)
            item = {'collection_id': uid_str}

            # All studies that include this collection. Used by the client to
            # filter the dropdown under the active study — a collection can
            # legitimately belong to multiple studies.
            if uid_str in collection_studies_map:
                item['studies'] = collection_studies_map[uid_str]
                # `study` retained for backward-compat consumers; set to the
                # first enclosing study.
                item['study'] = collection_studies_map[uid_str][0]

            # Active days (timeline-length context for the dropdown).
            if uid_str in active_days_map:
                item['active_days'] = active_days_map[uid_str]

            # Inject display ID and tags
            if uid_str in annotations:
                 annot_data = annotations[uid_str]
                 disp = annot_data.get('display_collection_id')
                 tags = annot_data.get('annotation_tags')
                 hidden = annot_data.get('hidden')

                 if disp: item['display_collection_id'] = disp
                 if tags: item['annotation_tags'] = tags
                 if hidden is not None: item['hidden'] = bool(hidden)

            final_list.append(item)

        return jsonify(final_list)

    except Exception:
        traceback.print_exc()
        return jsonify([])
