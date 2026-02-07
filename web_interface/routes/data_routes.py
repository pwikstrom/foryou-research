from flask import Blueprint, jsonify, request, Response, stream_with_context
from flask_login import login_required, current_user
import pandas as pd
import numpy as np
from datetime import datetime
from fyp.fyp_config import fyp_cf, PROJECT_ROOT
from ..data_service import (
    get_explorer_data, get_pca_df, get_viz_config, make_serializable, enrich_with_user_tags,
    load_schema_metadata, get_timeline_data, get_study_donations, load_shared_tags
)
from ..security import user_manager
from ..auth import admin_required
from .. import explorer_backend as explorer
from fyp.recode_variables import get_factors_and_features_from_var_schema
from fyp.studies import init_study_defs, save_study_defs
import fyp.data_io as data_io
import pyarrow.parquet as pq
import ast
import traceback

data_bp = Blueprint('data_bp', __name__)

# LOCATION_CACHE_FILE = 'location_timezone_cache.json'
# PERSONA_STATS_CACHE_FILE = 'persona_stats_cache.parquet'



"""@data_bp.route('/api/explorer/studies', methods=['GET'])
@login_required
def api_explorer_studies():
    studies = []
    if data_io.exists(
        storage_location="cache",
        filename="_recoded.parquet",
        ):
        print("%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%does this ever happen?")
        recoded_files = [fn for fn in data_io.listdir(storage_location="cache") if fn.endswith("_recoded.parquet")]
        for fn in recoded_files:
            study_name = fn.replace("_recoded.parquet", "")
            studies.append(study_name)
    
    return jsonify(sorted(studies))"""


@data_bp.route('/api/studies/defined', methods=['GET'])
@login_required
def api_get_study_defs():

    if not 'study_defs' in fyp_cf:
        init_study_defs()
        
    if 'study_defs' in fyp_cf:
        studies = []
        for study_name, study_config in fyp_cf['study_defs'].items():
            # 1. Admin Override: Admins see everything
            if current_user.is_admin():
                studies.append(study_name)
                continue

            user_access = study_config.get('USER_ACCESS')

            # 2. Missing or Empty => Default Allow (Backward Compatibility)
            if not user_access:
                studies.append(study_name)
                continue

            # Ensure it is a list for subsequent checks
            if not isinstance(user_access, list):
                # Should not happen given TOML encoding but safe fallback
                studies.append(study_name)
                continue

            # 3. 'all' keyword
            if 'all' in user_access:
                studies.append(study_name)
                continue

            # 4. Role Match
            if current_user.role in user_access:
                studies.append(study_name)
                continue

            # 5. Username Match
            if current_user.username in user_access:
                studies.append(study_name)
                continue
                
        return jsonify(sorted(studies))
    return jsonify([])





@data_bp.route('/api/explorer/metadata', methods=['GET'])
@login_required
def api_explorer_metadata():

    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    context = request.args.get('context', 'explorer')

    df, col_types = get_explorer_data(study, context=context)
    
    # Enrich with User Tags
    username = current_user.username
    
    # Check for Shared Annotations
    shared_simple_map = None
    user_settings = current_user.settings or {}
    if user_settings.get('share_annotations', True):
        sharing_users = []
        for u_name, u_obj in user_manager.users.items():
            if u_name == username: continue
            if u_obj.settings and u_obj.settings.get('share_annotations', True):
                sharing_users.append(u_name)
        
        if sharing_users:
            shared_simple_map, _ = load_shared_tags(sharing_users)

    df, col_types = enrich_with_user_tags(df, col_types, username, shared_users_tags=shared_simple_map)
  
    """if context == 'viewer':
         df = df[df.scraped_ok].copy()
         print(f"    Filtered to {len(df):,} scraped events")
    else:
         df = df[df.annotated_ok].copy()
         print(f"    Filtered to {len(df):,} annotated events")"""

 
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
 
    if data_io.exists(storage_location="cache", filename=f"{study}_{context}_metadata.json"):
        metadata = data_io.load_json(storage_location="cache", filename=f"{study}_{context}_metadata.json")
        print(f"    Using cached metadata for {study}")
        
        # Force refresh of dynamic metadata (User Tags & Has Annotation)
        # We must re-calculate these every time because the cache might be stale w.r.t user actions
        dynamic_cols = {}
        if 'User Tags' in col_types: dynamic_cols['User Tags'] = 'list'
        if 'User Tags' in col_types: dynamic_cols['User Tags'] = 'list'
        if 'Has Annotation' in col_types: dynamic_cols['Has Annotation'] = 'category'
        if 'Machine Annotations' in col_types: dynamic_cols['Machine Annotations'] = 'category'
        
        if dynamic_cols:
             cols_to_get = [c for c in dynamic_cols.keys() if c in df.columns]
             if cols_to_get:
                  dynamic_meta = explorer.get_metadata(df[cols_to_get], dynamic_cols)
                  metadata.update(dynamic_meta)
                  
                  # Force update of User Tags stats specifically if it's a list (to capture merged shared tags)
                  if 'User Tags' in df.columns:
                      res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'}, viz_config=get_viz_config())
                      if 'stats' in res_tags:
                          if 'total_stats' not in metadata: metadata['total_stats'] = {}
                          metadata['total_stats'].update(res_tags['stats'])
                          # metadata['User Tags'] = res_tags['stats']['User Tags'] # REMOVED: This overwrites filter config with stats

        # Ensure User Tags is in filter_priority if it exists
        if 'User Tags' in metadata and 'filter_priority' in metadata:
            if 'User Tags' in metadata['filter_priority']:
                metadata['filter_priority'].remove('User Tags')
            metadata['filter_priority'].insert(0, 'User Tags')
        
        # Always refresh schema metadata (accepted_labels, priorities) from CSV
        metadata = load_schema_metadata(metadata)

        # Inject User Annotation Schema Info (User Tags & Has Annotation) - POST SCHEMA LOAD
        if 'schema_map' not in metadata: metadata['schema_map'] = {}
        
        # 1. User Tags -> Tags by Humans
        if 'User Tags' in metadata:
            metadata['schema_map']['User Tags'] = {
                "section": "Annotation Status",
                "display_name": "Tags by Humans",
                "description": "Tags you have assigned to items."
            }
            # Re-insert into priorities
            if 'filter_priority' not in metadata: metadata['filter_priority'] = []
            if 'User Tags' in metadata['filter_priority']: metadata['filter_priority'].remove('User Tags')
            metadata['filter_priority'].insert(0, 'User Tags')

            if 'display_priority' not in metadata: metadata['display_priority'] = []
            if 'User Tags' in metadata['display_priority']: metadata['display_priority'].remove('User Tags')
            metadata['display_priority'].insert(0, 'User Tags')
            
        # 2. Has Annotation -> Has Human Annotations
        if 'Has Annotation' in metadata:
            metadata['schema_map']['Has Annotation'] = {
                "section": "Annotation Status",
                "display_name": "Has Human Annotations",
                "description": "Filter items that have notes, tags, or closed tags."
            }
            # Re-insert into priorities (After User Tags)
            if 'filter_priority' not in metadata: metadata['filter_priority'] = []
            if 'Has Annotation' in metadata['filter_priority']: metadata['filter_priority'].remove('Has Annotation')
            # Insert at 1 if User Tags exists, else 0
            idx = 1 if 'User Tags' in metadata else 0
            metadata['filter_priority'].insert(idx, 'Has Annotation')

            if 'display_priority' not in metadata: metadata['display_priority'] = []
            if 'Has Annotation' in metadata['display_priority']: metadata['display_priority'].remove('Has Annotation')
            idx = 1 if 'User Tags' in metadata else 0
            metadata['display_priority'].insert(idx, 'Has Annotation')

        # 3. Machine Annotations
        if 'Machine Annotations' in metadata:
            metadata['schema_map']['Machine Annotations'] = {
                "section": "Annotation Status",
                "display_name": "Machine Annotations",
                "description": "Filter items by their machine annotation status."
            }
            # Priority
            if 'filter_priority' not in metadata: metadata['filter_priority'] = []
            if 'Machine Annotations' in metadata['filter_priority']: metadata['filter_priority'].remove('Machine Annotations')
            # Insert after Has Annotation
            idx = 0
            if 'User Tags' in metadata: idx += 1
            if 'Has Annotation' in metadata: idx += 1
            metadata['filter_priority'].insert(idx, 'Machine Annotations')
            
            if 'display_priority' not in metadata: metadata['display_priority'] = []
            if 'Machine Annotations' in metadata['display_priority']: metadata['display_priority'].remove('Machine Annotations')
            metadata['display_priority'].insert(idx, 'Machine Annotations')

        return jsonify(make_serializable(metadata))



    print(f"    No cached explorer metadata for '{study}', calculating...")
    metadata = explorer.get_metadata(df, col_types)
    
    # Ensure User Tags is in filter_priority if it exists (for non-cached path)
    if 'User Tags' in metadata and 'filter_priority' in metadata:
        if 'User Tags' in metadata['filter_priority']:
            metadata['filter_priority'].remove('User Tags')
        metadata['filter_priority'].insert(0, 'User Tags')
    
    viz_config = get_viz_config()
    res = explorer.get_current_stats(df, col_types, viz_config=viz_config)
    metadata['total_stats'] = res['stats']

    try:
        the_recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(storage_location="cache", filename=the_recoded_file):
            metadata['source_file'] = the_recoded_file
            mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file))
            metadata['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
        else:
             metadata['source_file'] = "Unknown"
             metadata['source_file_modified'] = ""
    except Exception as e:
        print(f"Error getting file info: {e}")
        metadata['source_file'] = "Error"
        metadata['source_file_modified'] = ""

    metadata = load_schema_metadata(metadata)

    # Inject User Annotation Schema Info (User Tags & Has Annotation)
    if 'schema_map' not in metadata: metadata['schema_map'] = {}

    # 1. User Tags -> Tags by Humans
    if 'User Tags' in metadata:
        metadata['schema_map']['User Tags'] = {
            "section": "Annotation Status",
            "display_name": "Tags by Humans",
            "description": "Tags you have assigned to items."
        }           
        # Re-insert into priorities
        if 'filter_priority' not in metadata: metadata['filter_priority'] = []
        if 'User Tags' in metadata['filter_priority']: metadata['filter_priority'].remove('User Tags')
        metadata['filter_priority'].insert(0, 'User Tags')

        if 'display_priority' not in metadata: metadata['display_priority'] = []
        if 'User Tags' in metadata['display_priority']: metadata['display_priority'].remove('User Tags')
        metadata['display_priority'].insert(0, 'User Tags')

    # 2. Has Annotation -> Has Human Annotations
    if 'Has Annotation' in metadata:
        metadata['schema_map']['Has Annotation'] = {
            "section": "Annotation Status",
            "display_name": "Has Human Annotations",
            "description": "Filter items that have notes, tags, or closed tags."
        }
        # Re-insert into priorities (After User Tags)
        if 'filter_priority' not in metadata: metadata['filter_priority'] = []
        if 'Has Annotation' in metadata['filter_priority']: metadata['filter_priority'].remove('Has Annotation')
        idx = 1 if 'User Tags' in metadata else 0
        metadata['filter_priority'].insert(idx, 'Has Annotation')

        if 'display_priority' not in metadata: metadata['display_priority'] = []
        if 'Has Annotation' in metadata['display_priority']: metadata['display_priority'].remove('Has Annotation')
        idx = 1 if 'User Tags' in metadata else 0
        metadata['display_priority'].insert(idx, 'Has Annotation')

    # 3. Machine Annotations
    if 'Machine Annotations' in metadata:
        metadata['schema_map']['Machine Annotations'] = {
            "section": "Annotation Status",
            "display_name": "Machine Annotations",
            "description": "Filter items by their machine annotation status."
        }
        # Priority
        if 'filter_priority' not in metadata: metadata['filter_priority'] = []
        if 'Machine Annotations' in metadata['filter_priority']: metadata['filter_priority'].remove('Machine Annotations')
        # Insert after Has Annotation
        idx = 0
        if 'User Tags' in metadata: idx += 1
        if 'Has Annotation' in metadata: idx += 1
        metadata['filter_priority'].insert(idx, 'Machine Annotations')
        
        if 'display_priority' not in metadata: metadata['display_priority'] = []
        if 'Machine Annotations' in metadata['display_priority']: metadata['display_priority'].remove('Machine Annotations')
        metadata['display_priority'].insert(idx, 'Machine Annotations')

    data_io.save_json(data=make_serializable(metadata), storage_location="cache", filename=f"{study}_{context}_metadata.json", verbose=True)

    return jsonify(make_serializable(metadata))





@data_bp.route('/api/explorer/filter', methods=['POST'])
@login_required
def api_explorer_filter():
    data = request.json or {}
    study = data.get("study")
    
    if not study:
         return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study, context="explorer")
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    # Enrich with User Tags
    username = current_user.username
    df, col_types = enrich_with_user_tags(df, col_types, username)
    
    """df = df[df.annotated_ok].copy()
    print(f"    Filtered to {len(df):,} annotated events")"""

    filters = data.get("filters", {})
    search_query = data.get("search_query")
    
    
    # Selective Calculation Logic
    trigger_slice = data.get("trigger_slice") # 1, 2, or None (both)

    # Load cached metadata to potentially reuse total_stats
    cached_metadata = {}
    try:
        if data_io.exists(storage_location="cache", filename=f"{study}_explorer_metadata.json"):
            cached_metadata = data_io.load_json(storage_location="cache", filename=f"{study}_explorer_metadata.json")
    except Exception as e:
        print(f"    Warning: Could not load cached metadata: {e}")
    
    result = {}

    # --- SLICE 1 ---
    if trigger_slice is None or trigger_slice == 1:
        # Check if filters are empty and we have cached stats
        is_empty_filters = (not filters) and (not search_query)
        
        if is_empty_filters and 'total_stats' in cached_metadata:
            print("    Using cached total_stats for Slice 1")
            result['stats'] = cached_metadata['total_stats']
            result['count'] = len(df)
            
            # Inject User Tags stats if missing
            if 'User Tags' in col_types and 'User Tags' not in result['stats']:
                 if 'User Tags' in df.columns:
                     res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'}, viz_config=get_viz_config())
                     result['stats'].update(res_tags['stats'])

        else:
            filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
            viz_config = get_viz_config()
            res1 = explorer.get_current_stats(filtered_df, col_types, viz_config=viz_config)
            result['stats'] = res1['stats']
            result['count'] = res1['count']
    
    # --- SLICE 2 ---
    # --- SLICE 2 ---
    if "filters2" in data and (trigger_slice is None or trigger_slice == 2):
        filters2 = data.get("filters2", {})
        search_query2 = data.get("search_query2")
        
        # If filters are identical to S1 and S1 was just calculated, reuse result
        # This handles the initial load case where both are empty/default
        is_identical = (filters == filters2) and (search_query == search_query2)
        s1_available = (trigger_slice is None or trigger_slice == 1) and 'stats' in result
        
        if is_identical and s1_available:
            print("    Slice 2 identical to Slice 1, reusing stats")
            result['stats2'] = result['stats']
            result['count2'] = result['count']
        else:
            # Check if filters are empty (for S2 specific case if not identical/S1 not avail)
            is_empty_filters2 = (not filters2) and (not search_query2)
            
            if is_empty_filters2 and 'total_stats' in cached_metadata:
                 print("    Using cached total_stats for Slice 2")
                 result['stats2'] = cached_metadata['total_stats']
                 result['count2'] = len(df)
            else:
                filtered_df2 = explorer.filter_dataframe(df, col_types, filters2, search_query2)
                viz_config = get_viz_config()
                res2 = explorer.get_current_stats(filtered_df2, col_types, viz_config=viz_config)
                
                result['stats2'] = res2['stats']
                result['count2'] = res2['count']
    
    return jsonify(make_serializable(result))





@data_bp.route('/api/viewer/ids', methods=['POST'])
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
    sort_by = data.get("sort_by")
    
    filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
    
    if sort_by and sort_by in filtered_df.columns:
        sort_order = data.get("sort_order") 
        if sort_order:
            ascending = (sort_order == 'asc')
        else:
            dtype = col_types.get(sort_by)
            ascending = True
            if dtype == 'number':
                ascending = False
            
        filtered_df = filtered_df.sort_values(by=sort_by, ascending=ascending)
    
    id_col = 'item_id'
    if id_col not in filtered_df.columns:
        if 'video_id' in filtered_df.columns: id_col = 'video_id'
        elif 'G_id' in filtered_df.columns: id_col = 'G_id'
        else: return jsonify({"error": "No ID column found"}), 500
    
    
    
    # Hide Duplicate Videos if requested
    if data.get("hide_duplicates"):
        dedup_col = 'video_id'
        if dedup_col not in filtered_df.columns:
            if 'G_id' in filtered_df.columns: dedup_col = 'G_id'
            else: dedup_col = id_col
            
        filtered_df = filtered_df.drop_duplicates(subset=[dedup_col], keep='first')

    
    ids = filtered_df[id_col].astype(str).tolist()

    return jsonify({"ids": ids, "count": len(ids)})


@data_bp.route('/api/viewer/tags', methods=['GET'])
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


@data_bp.route('/api/viewer/tags/save', methods=['POST'])
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
    
    return jsonify({"status": "success", "tags": tags, "notes": notes, "closed_tagging": closed_tagging})


@data_bp.route('/api/viewer/tags/<path:tag_name>', methods=['DELETE'])
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
        return jsonify({"status": "success", "message": f"Tag '{tag_name}' deleted"})
    else:
        return jsonify({"status": "success", "message": "Tag not found in any item"}), 200






@data_bp.route('/api/pca/metadata', methods=['POST'])
@admin_required
def api_pca_metadata():
    
    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    factors, _ = get_factors_and_features_from_var_schema(some_events_df = df, verbose = False)
    
    if not factors:
        raise Exception("No factors found in var_schema")

    factor_values = {}
    for f in factors:
        vals = df[f].dropna().unique().tolist()
        if len(vals) < 500: 
            factor_values[f] = sorted([str(v) for v in vals])

    interpretations = {}
    try:
        inter_path = f"{study}_comp_interpretations.json"
        interpretations = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
    except Exception as e:
        print(f"Error loading interpretations: {e}")

    return jsonify({
        "numeric_cols": sorted(numeric_cols),
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "interpretations": interpretations
    })


@data_bp.route('/api/pca/data', methods=['POST'])
@admin_required
def api_pca_data():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})
    x_col = data.get("x_col")
    y_col = data.get("y_col")
    color_col = data.get("color_col")

    if not study or not x_col or not y_col: 
        return jsonify({"error": "Missing params"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col in df.columns:
            mask &= df[col].astype(str).isin(vals)
    
    filtered_df = df[mask].copy()

    filtered_df = filtered_df.dropna(subset=[x_col, y_col])

    MAX_POINTS = 5000
    if len(filtered_df) > MAX_POINTS:
        filtered_df = filtered_df.sample(MAX_POINTS)
    
    result_data = []
    has_color = color_col and color_col in filtered_df.columns
    
    for row in filtered_df.itertuples():
        x_val = getattr(row, x_col)
        y_val = getattr(row, y_col)
        
        c_val = "Default"
        if has_color:
            c_val = str(getattr(row, color_col))
        
        txt = f"{color_col}: {c_val}"
        
        result_data.append({
            "x": x_val,
            "y": y_val,
            "color_val": c_val,
            "text": txt
        })

    return jsonify({"data": result_data})


@data_bp.route('/api/persona_stats_info', methods=['GET'])
def api_persona_stats_info():
    if True:
        if data_io.exists(storage_location="ddp_main", filename="ddp_metadata.parquet"):
            mtime = data_io.getmtime(storage_location="ddp_main", filename="ddp_metadata.parquet")
            timestamp = datetime.fromtimestamp(mtime).strftime('%d %b %Y %H:%M')
            return jsonify({"exists": True, "timestamp": timestamp})
        return jsonify({"exists": False, "timestamp": None})


@data_bp.route('/api/persona_stats_cached', methods=['GET'])
def api_persona_stats_cached():
    # Alias to the main stats endpoint since we no longer distinguish between cached and calculated
    return api_persona_stats()


@data_bp.route('/api/persona_stats', methods=['POST', 'GET']) # Allow GET for convenience
def api_persona_stats():
    try:
        filename = "ddp_metadata.parquet"
        if not data_io.exists(storage_location="ddp_main", filename=filename):
             return jsonify({"error": "Persona metadata file not found."}), 404
        
        stats_df = None
        
        # Load the parquet file
        try:
             stats_df = data_io.load_parquet(
                storage_location="ddp_main",
                filename=filename
            )
        except Exception as e:
             # Fallback: reconstruction column by column
             print(f"Error loading parquet with default settings: {e}")
             primary, _, _, _ = data_io._resolve_paths(fyp_cf, "ddp_main", filename)
             try:
                 table = pq.read_table(primary)
                 data = {}
                 for i, col_name in enumerate(table.column_names):
                     data[col_name] = table.column(i).to_pandas()
                 stats_df = pd.DataFrame(data)
             except Exception as e2:
                 print(f"Fallback loading failed: {e2}")
                 return jsonify({"error": f"Failed to load data: {str(e)} / {str(e2)}"}), 500


        if isinstance(stats_df.index, pd.Index) and stats_df.index.name == 'D_donation_id':
             stats_df.reset_index(inplace=True)

        # Flatten MultiIndex columns (handling both Tuples and String-Tuples)
        new_columns = []
        
        for col in stats_df.columns:
            col_name = str(col)
            
            # Case 1: Real Tuple (from pandas load)
            if isinstance(col, tuple):
                group, name = col
                if group == 'other' and name == 'accepted':
                    col_name = 'accepted'
                elif group == 'other' and name == 'D_id':
                     col_name = 'D_id'
                else:
                    col_name = name if name else group

            # Case 2: String representation of Tuple (from pyarrow fallback)
            elif isinstance(col, str) and col.startswith("(") and col.endswith(")"):
                try:
                    val = ast.literal_eval(col)
                    if isinstance(val, tuple):
                        group, name = val
                        if group == 'other' and name == 'accepted':
                            col_name = 'accepted'
                        elif group == 'other' and name == 'D_id':
                             col_name = 'D_id'
                        else:
                            col_name = name if name else group
                except:
                    pass
            
            # Renaming for consistency
            # if col_name == 'D_donation_id':
            #     col_name = 'donation_id'
                
            new_columns.append(col_name)
            
        stats_df.columns = new_columns
        
        # Handle duplicated columns (keep first)
        stats_df = stats_df.loc[:, ~stats_df.columns.duplicated()]

        # Filter by Accepted
        if 'accepted' in stats_df.columns:
            stats_df = stats_df[stats_df['accepted'] == True].copy()
            # print(f"Filtered to {len(stats_df)} accepted donations")

        # Frontend Compatibility Aliases
        if 'consistency_top_2_hours' in stats_df.columns and 'consistency' not in stats_df.columns:
            stats_df['consistency'] = stats_df['consistency_top_2_hours']

        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        
        # Access Control: Redact PII for Viewers
        if current_user.is_authenticated and current_user.role == 'viewer':
            redact_fields = ['name', 'email', 'tiktokHandle']
            for rec in records:
                for field in redact_fields:
                    if field in rec:
                        rec[field] = "hidden"
        
        # Serialize
        for rec in records:
            for key, val in rec.items():
                rec[key] = make_serializable(val)
        response = jsonify(records)
        
        try:
            mtime = data_io.getmtime(storage_location="ddp_main", filename=filename)
            # Format as ISO string or similar for frontend parsing

            dt = datetime.fromtimestamp(mtime)
            response.headers['X-Metadata-MTime'] = dt.isoformat()
        except Exception as e:
            print(f"Could not get mtime for {filename}: {e}")

        return response
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/viewer/item/<study>/<item_id>', methods=['GET', 'POST'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study, context="viewer")
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    # Enrich with User Tags
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

    # Apply Context Filters if provided (POST)
    if request.method == 'POST':
        data = request.json or {}
        filters = data.get("filters", {})
        search_query = data.get("search_query")
        
        if filters or search_query:
            df = explorer.filter_dataframe(df, col_types, filters, search_query)

    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        else: return jsonify({"error": "ID column missing"}), 500

    row = df[df[id_col].astype(str) == str(item_id)]
    
    if row.empty:
        return jsonify({"error": "Item not found in current context"}), 404
    
    record = row.iloc[0].replace({np.nan: None}).to_dict()
    
    # Inject Shared Annotations for this item
    if shared_detailed_map:
        str_id = str(item_id)
        if str_id in shared_detailed_map:
            record['shared_annotations'] = shared_detailed_map[str_id]

    return jsonify(record)


@data_bp.route('/api/timelines/data', methods=['POST'])
@login_required
def api_timeline_data():
    data = request.json or {}
    #study = data.get("study")
    donation_id = data.get("donation_id")
    interval = data.get("interval", "day")
    
    if not donation_id:
        return jsonify({"error": "Missing donation_id"}), 400
        
    try:
        result = get_timeline_data(donation_id, interval=interval)
        if result is None:
             return jsonify({"error": "No data found"}), 404
        if "error" in result:
             return jsonify(result), 400
             
        return jsonify(make_serializable(result))
    except Exception as e:

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/timelines/donations', methods=['POST'])
@login_required
def api_timeline_donations():
    # Load from ddp_metadata.parquet in cache
    # Return list of { D_donation_id, D_id (optional) }
    
    
    
    meta_df = data_io.load_parquet(storage_location="ddp_main", filename="ddp_metadata.parquet")
    
    if meta_df is None:
        #print("DEBUG TIMELINE: ddp_metadata.parquet is None")
        return jsonify([])
        
    if meta_df.empty:
        #print("DEBUG TIMELINE: ddp_metadata.parquet is Empty")
        return jsonify([])
        
    
    # Reset index to access D_donation_id if it's in index
    df_reset = meta_df.reset_index()
    
    # Handle MultiIndex columns
    # We look for ('other', 'accepted')
    accepted_col = None
    if isinstance(meta_df.columns, pd.MultiIndex):
        if ('other', 'accepted') in meta_df.columns:
            accepted_col = ('other', 'accepted')
    else:
        if 'accepted' in meta_df.columns:
            accepted_col = 'accepted'
            
    filtered = df_reset
    if accepted_col:
        # Ensure boolean
        try:
             filtered = df_reset[df_reset[accepted_col] == True]
        except Exception as e:
             # maybe string 'True'?
             pass

    
    target_id_col = 'D_donation_id'
    if target_id_col not in filtered.columns:
        # Try to find it
        if 'index' in filtered.columns:
             # Assume index is the ID
             target_id_col = 'index'
        else:
             return jsonify([])

    # Unique donations
    # Vectorized optimized extraction
    
    # We want specific column(s)
    # If target_id_col returns a DataFrame (duplicate columns), take first
    try:
        don_ids_series = filtered[target_id_col]
        if isinstance(don_ids_series, pd.DataFrame):
            # Duplicate column names, take first
            don_ids_series = don_ids_series.iloc[:, 0]
            
        unique_ids = don_ids_series.unique().tolist()
        
        # Build simple dict list
        final_list = [{'D_donation_id': str(uid)} for uid in unique_ids if pd.notna(uid)]
        
        return jsonify(final_list)
        
    except Exception as e:

        traceback.print_exc()
        return jsonify([])


@data_bp.route('/api/video/<study>/<item_id>', methods=['GET'])
def api_video_stream(study, item_id):

    bucket = fyp_cf.get("data_io", {}).get("bucket")
    if not bucket:
        return "GCS Bucket not available. Check credentials or internet connection.", 503

    blob_name = f"{fyp_cf['data_io']['gcs_media_prefix']}/{item_id}.mp4"
    blob = bucket.blob(blob_name)
    
    if not blob.exists():
         return f"Video {blob_name} not found", 404

    def generate():
        with blob.open("rb") as f:
            while chunk := f.read(4096 * 16): 
                yield chunk

    return Response(stream_with_context(generate()), mimetype="video/mp4")
