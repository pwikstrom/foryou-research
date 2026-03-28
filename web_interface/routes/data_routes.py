from flask import Blueprint, jsonify, request, Response, stream_with_context
from flask_login import login_required, current_user
import pandas as pd
import numpy as np
from datetime import datetime
from fyp.fyp_config import fyp_cf, PROJECT_ROOT
from ..data_service import (
    get_explorer_data, get_pca_df, get_viz_config, make_serializable, enrich_with_user_tags,
    load_schema_metadata, get_timeline_data, get_study_donations, load_shared_tags,
    load_display_id_map, get_accessible_studies
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

import re

PCA_MIN_VARIANCE_THRESHOLD = 5.0

def _filter_pca_components_by_variance(numeric_cols, interpretations):
    """
    Filters a list of PCA component names based on their explained variance.
    Always keeps non-PCA components and the PCA component with the highest variance, 
    then any other PCA components >= threshold.
    """
    if not interpretations or not numeric_cols:
        return numeric_cols

    pca_cols_with_var = []
    non_pca_cols = []
    
    # Match PCA components (e.g., ends with _C and a number like _C1)
    pca_pattern = re.compile(r'_C\d+$')

    # Extract variances for the columns that have them
    for col in numeric_cols:
        if pca_pattern.search(col):
            var_val = 0.0
            if col in interpretations and 'explained_variance_pct' in interpretations[col]:
                try:
                    var_val = float(interpretations[col]['explained_variance_pct'])
                except (ValueError, TypeError):
                    pass
            pca_cols_with_var.append((col, var_val))
        else:
            # Not a PCA component, always keep it
            non_pca_cols.append(col)

    if not pca_cols_with_var:
        return numeric_cols

    # Sort descending by variance
    pca_cols_with_var.sort(key=lambda x: x[1], reverse=True)
    
    # Always keep the top one
    top_col = pca_cols_with_var[0][0]
    filtered_cols = [top_col]

    # Keep others that meet the threshold
    for col, var_val in pca_cols_with_var[1:]:
        if var_val >= PCA_MIN_VARIANCE_THRESHOLD:
            filtered_cols.append(col)

    # Combine and return sorted
    return sorted(non_pca_cols + filtered_cols)


def _enforce_study_donations(metadata, study, verbose=False):
    """
    Ensures that the metadata only contains Donation IDs that are strictly part of the study.
    This prevents any cached artifacts or merging errors from exposing unrelated donation IDs.
    """
    try:
        # Get authoritative list of donations for this study
        donations = get_study_donations(study)
        valid_donation_ids = set()
        #valid_ids = set()
        
        if not donations:
            print(f"    [DATA_ROUTES] Warning: get_study_donations returned empty for {study}. Skipping filter enforcement.")
            return metadata

        for d in donations:
            if d.get('D_donation_id'): valid_donation_ids.add(str(d['D_donation_id']).strip())
            #if d.get('D_id'): valid_ids.add(str(d['D_id']).strip())
            
        if not valid_donation_ids:
             print(f"    [DATA_ROUTES] Warning: No valid_donation_ids found for {study}. Skipping filter enforcement.")
             return metadata

        # Filter D_donation_id
        if 'D_donation_id' in metadata and 'values' in metadata['D_donation_id']:
            original = metadata['D_donation_id']['values']
            # Robust filter with strip
            filtered = [v for v in original if str(v['value']).strip() in valid_donation_ids]
            
            # Debugging mismatch if drastic change
            if len(original) > 0 and len(filtered) == 0:
                print(f"    [DATA_ROUTES] CRITICAL: Filter removed ALL {len(original)} IDs for {study}. Cache is likely stale.")
                print(f"    - Sample Valid IDs: {list(valid_donation_ids)[:5]}")
                print(f"    - Sample Metadata IDs: {[str(v['value']).strip() for v in original[:5]]}")
                return None # Signal to caller that metadata is invalid
            elif len(original) != len(filtered):
                if verbose:
                    print(f"    [DATA_ROUTES] Info: Filtered D_donation_id for {study}: {len(original)} -> {len(filtered)}")
                
            metadata['D_donation_id']['values'] = filtered


            
    except Exception as e:
        print(f"    Error enforcing study donations: {e}")
        traceback.print_exc()
    
    return metadata



@data_bp.route('/api/studies/defined', methods=['GET'])
@login_required
def api_get_study_defs():

    studies = get_accessible_studies(
        username=current_user.username,
        role=current_user.role,
        is_admin=current_user.is_admin()
    )
    return jsonify(studies)





@data_bp.route('/api/explore/metadata', methods=['GET'])
@login_required
def api_explorer_metadata():

    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    context = request.args.get('context', 'explorer')

    df, col_types = get_explorer_data(study, context=context)
 
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

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
  

    cached_metadata = None
    if data_io.exists(storage_location="cache", filename=f"{study}_{context}_metadata.json"):
        try:
            potential_metadata = data_io.load_json(storage_location="cache", filename=f"{study}_{context}_metadata.json")
            
            # ... (Dynamic columns logic omitted for brevity as it modifies potential_metadata in place) ...
            # To avoid complexity in replacement, I will assume the dynamic logic is robust or harmless if metadata is discarded later.
            # Actually, I need to keep the existing logic structure but wrap the return.
            
            # Force refresh of dynamic metadata (User Tags & Has Annotation)
            # We must re-calculate these every time because the cache might be stale w.r.t user actions
            dynamic_cols = {}
            if 'User Tags' in col_types: dynamic_cols['User Tags'] = 'list'
            if 'Has Annotation' in col_types: dynamic_cols['Has Annotation'] = 'category'
            if 'Machine Annotations' in col_types: dynamic_cols['Machine Annotations'] = 'category'
            
            if dynamic_cols:
                 cols_to_get = [c for c in dynamic_cols.keys() if c in df.columns]
                 if cols_to_get:
                      dynamic_meta = explorer.get_metadata(df[cols_to_get], dynamic_cols)
                      potential_metadata.update(dynamic_meta)
                      
                      # Force update of User Tags stats specifically if it's a list (to capture merged shared tags)
                      if 'User Tags' in df.columns:
                          res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'}, viz_config=get_viz_config())
                          if 'stats' in res_tags:
                              if 'total_stats' not in potential_metadata: potential_metadata['total_stats'] = {}
                              potential_metadata['total_stats'].update(res_tags['stats'])
    
            # Ensure User Tags is in filter_priority if it exists
            if 'User Tags' in potential_metadata and 'filter_priority' in potential_metadata:
                if 'User Tags' in potential_metadata['filter_priority']:
                    potential_metadata['filter_priority'].remove('User Tags')
                potential_metadata['filter_priority'].insert(0, 'User Tags')
            
            # Always refresh schema metadata (accepted_labels, priorities) from CSV
            potential_metadata = load_schema_metadata(potential_metadata)
    
            # Inject User Annotation Schema Info (User Tags & Has Annotation) - POST SCHEMA LOAD
            if 'schema_map' not in potential_metadata: potential_metadata['schema_map'] = {}
            
            # 1. User Tags -> Tags by Humans
            if 'User Tags' in potential_metadata:
                potential_metadata['schema_map']['User Tags'] = {
                    "section": "Annotation Status",
                    "display_name": "Tags by Humans",
                    "description": "Tags you have assigned to items."
                }
                # Re-insert into priorities
                if 'filter_priority' not in potential_metadata: potential_metadata['filter_priority'] = []
                if 'User Tags' in potential_metadata['filter_priority']: potential_metadata['filter_priority'].remove('User Tags')
                potential_metadata['filter_priority'].insert(0, 'User Tags')
    
                if 'display_priority' not in potential_metadata: potential_metadata['display_priority'] = []
                if 'User Tags' in potential_metadata['display_priority']: potential_metadata['display_priority'].remove('User Tags')
                potential_metadata['display_priority'].insert(0, 'User Tags')
                
            # 2. Has Annotation -> Has Human Annotations
            if 'Has Annotation' in potential_metadata:
                potential_metadata['schema_map']['Has Annotation'] = {
                    "section": "Annotation Status",
                    "display_name": "Has Human Annotations",
                    "description": "Filter items that have notes, tags, or closed tags."
                }
                # Re-insert into priorities (After User Tags)
                if 'filter_priority' not in potential_metadata: potential_metadata['filter_priority'] = []
                if 'Has Annotation' in potential_metadata['filter_priority']: potential_metadata['filter_priority'].remove('Has Annotation')
                # Insert at 1 if User Tags exists, else 0
                idx = 1 if 'User Tags' in potential_metadata else 0
                potential_metadata['filter_priority'].insert(idx, 'Has Annotation')
    
                if 'display_priority' not in potential_metadata: potential_metadata['display_priority'] = []
                if 'Has Annotation' in potential_metadata['display_priority']: potential_metadata['display_priority'].remove('Has Annotation')
                idx = 1 if 'User Tags' in potential_metadata else 0
                potential_metadata['display_priority'].insert(idx, 'Has Annotation')
    
            if 'Machine Annotations' in potential_metadata:
                potential_metadata['schema_map']['Machine Annotations'] = {
                    "section": "Annotation Status",
                    "display_name": "Machine Annotations",
                    "description": "Filter items by their machine annotation status."
                }
                # Priority
                if 'filter_priority' not in potential_metadata: potential_metadata['filter_priority'] = []
                if 'Machine Annotations' in potential_metadata['filter_priority']: potential_metadata['filter_priority'].remove('Machine Annotations')
                # Insert after Has Annotation
                idx = 0
                if 'User Tags' in potential_metadata: idx += 1
                if 'Has Annotation' in potential_metadata: idx += 1
                potential_metadata['filter_priority'].insert(idx, 'Machine Annotations')
                
                if 'display_priority' not in potential_metadata: potential_metadata['display_priority'] = []
                if 'Machine Annotations' in potential_metadata['display_priority']: potential_metadata['display_priority'].remove('Machine Annotations')
                potential_metadata['display_priority'].insert(idx, 'Machine Annotations')
    
            # Inject Display IDs (Cached Path)
            display_map = load_display_id_map()
            if display_map:
                for col in ['D_donation_id']:#, 'D_id']:
                    if col in potential_metadata and potential_metadata[col].get('type') == 'category': 
                        if 'values' in potential_metadata[col]:
                            new_values = []
                            for item in potential_metadata[col]['values']:
                                val = item['value']
                                if val in display_map:
                                    item['label'] = display_map[val]
                                new_values.append(item)
                            potential_metadata[col]['values'] = new_values
    
            # Enforce strict study membership for Donation IDs
            potential_metadata = _enforce_study_donations(potential_metadata, study)
            
            if potential_metadata:
                #print(f"    [DATA_ROUTES] Returning cached metadata for {study}")
                return jsonify(make_serializable(potential_metadata))
            else:
                print(f"    [DATA_ROUTES] Cache invalidated for {study}, regenerating...")

        except Exception as e:
            print(f"    Warning: Error loading/processing cached metadata: {e}")
            traceback.print_exc()
            # Fall through to regeneration



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

    # Inject Display IDs for ID Columns
    display_map = load_display_id_map()
    if display_map:
        for col in ['D_donation_id']:#, 'D_id']:
            if col in metadata and metadata[col].get('type') == 'category': # IDs are often category/list in metadata
                # Check values list
                if 'values' in metadata[col]:
                    new_values = []
                    for item in metadata[col]['values']:
                        # item is {value: "...", count: ...}
                        val = item['value']
                        # Look up display ID
                        if val in display_map:
                            item['label'] = display_map[val] # Add label
                        else:
                            # Fallback? No label needed, frontend defaults to value
                            pass
                        new_values.append(item)
                    metadata[col]['values'] = new_values

    # Enforce strict study membership for Donation IDs (before saving to cache)
    metadata = _enforce_study_donations(metadata, study)

    data_io.save_json(data=make_serializable(metadata), storage_location="cache", filename=f"{study}_{context}_metadata.json", verbose=False)

    return jsonify(make_serializable(metadata))





@data_bp.route('/api/explore/filter', methods=['POST'])
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
            #print("    Using cached total_stats for Slice 1")
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
    if "filters2" in data and (trigger_slice is None or trigger_slice == 2):
        filters2 = data.get("filters2", {})
        search_query2 = data.get("search_query2")
        
        # If filters are identical to S1 and S1 was just calculated, reuse result
        # This handles the initial load case where both are empty/default
        is_identical = (filters == filters2) and (search_query == search_query2)
        s1_available = (trigger_slice is None or trigger_slice == 1) and 'stats' in result
        
        if is_identical and s1_available:
            #print("    Slice 2 identical to Slice 1, reusing stats")
            result['stats2'] = result['stats']
            result['count2'] = result['count']
        else:
            # Check if filters are empty (for S2 specific case if not identical/S1 not avail)
            is_empty_filters2 = (not filters2) and (not search_query2)
            
            if is_empty_filters2 and 'total_stats' in cached_metadata:
                 #print("    Using cached total_stats for Slice 2")
                 result['stats2'] = cached_metadata['total_stats']
                 result['count2'] = len(df)
            else:
                filtered_df2 = explorer.filter_dataframe(df, col_types, filters2, search_query2)
                viz_config = get_viz_config()
                res2 = explorer.get_current_stats(filtered_df2, col_types, viz_config=viz_config)
                
                result['stats2'] = res2['stats']
                result['count2'] = res2['count']
    
    return jsonify(make_serializable(result))





@data_bp.route('/api/video_analysis/ids', methods=['POST'])
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
    
    # Pagination Optional Params
    offset = data.get("offset", 0)
    limit = data.get("limit", 1000)
    
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

    
    # Calculate true total count before slicing
    total_count = len(filtered_df)
    
    # Slice the series according to pagination
    chunked_ids = filtered_df[id_col].iloc[offset : offset + limit].astype(str).tolist()
    
    # Return display IDs map ONLY for the returned filtered IDs to save bandwidth
    display_map = load_display_id_map()
    relevant_display_ids = {}
    for i in chunked_ids:
        if i in display_map:
            relevant_display_ids[i] = display_map[i]

    return jsonify({
        "ids": chunked_ids, 
        "count": total_count, # True total count for the frontend to compute UI bounds
        "offset": offset,
        "display_ids": relevant_display_ids,
        "truncated": False # Deprecated flag, kept for backward compat while frontend transitions
    })


@data_bp.route('/api/video_analysis/tags', methods=['GET'])
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


@data_bp.route('/api/video_analysis/tags/save', methods=['POST'])
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


@data_bp.route('/api/video_analysis/tags/<path:tag_name>', methods=['DELETE'])
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
        return jsonify({"status": "success", "message": "Tag not found in any item"}), 200


@data_bp.route('/api/video_analysis/votes', methods=['GET'])
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


@data_bp.route('/api/video_analysis/vote', methods=['POST'])
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
        
    return jsonify({"status": "success", "votes": votes})

@data_bp.route('/api/timelines/vote_annotation', methods=['POST'])
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



@data_bp.route('/api/correlations/metadata', methods=['POST'])
@login_required
def api_pca_metadata():
    
    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    # Get numeric columns and exclude any that have 1 or fewer unique non-null values
    # Also explicitly exclude the unscaled '_raw' tooltip columns from appearing in the UI dropdowns
    all_numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    numeric_cols = [col for col in all_numeric_cols if df[col].nunique(dropna=True) > 1 and not str(col).endswith('_raw')]
    
    factors, _ = get_factors_and_features_from_var_schema(some_events_df = df, verbose = False)
    
    if not factors:
        import traceback
        traceback.print_exc()
        raise Exception("No factors found in var_schema")

    # Exclude session_id from factors — not useful for filtering
    factors = [f for f in factors if f.lower() != 'session_id']

    # Build schema_map with display_name from var_schema
    schema_map = {}
    if 'var_schema' in fyp_cf and isinstance(fyp_cf['var_schema'], pd.DataFrame):
        vs = fyp_cf['var_schema']
        for _, row in vs.iterrows():
            var_name = str(row.get('variable_name', ''))
            entry = {}
            if 'display_name' in row:
                dname = str(row['display_name'])
                if dname and dname.lower() != 'nan' and dname.strip():
                    entry['display_name'] = dname.strip()
            if entry:
                schema_map[var_name] = entry

    # Map PCA components formatted names (e.g. G_tiktok_native_C13 -> TikTok Native (C13), or G_var_entropy -> G Var (entropy))
    # Check if unrecognized numeric columns begin with a known schema variable base name
    sorted_base_names = sorted(schema_map.keys(), key=len, reverse=True)
    for col in numeric_cols:
        if col in schema_map:
            continue
            
        for base_name in sorted_base_names:
            if col.startswith(base_name + '_'):
                raw_suffix = col[len(base_name) + 1:]
                
                # Format suffix: replace underscores with spaces
                formatted_suffix = raw_suffix.replace('_', ' ')
                
                if 'display_name' in schema_map[base_name]:
                    display_name = f"{schema_map[base_name]['display_name']} ({formatted_suffix})"
                else:
                    display_name = f"{base_name} ({formatted_suffix})"
                    
                schema_map[col] = {'display_name': display_name}
                break

    import re
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

    # Build factor_values with date handling
    factor_values = {}
    for f in factors:
        is_dt = pd.api.types.is_datetime64_any_dtype(df[f])
        if is_dt or "date" in f.lower():
            vals = df[f].dropna().astype(str).str[:10].unique().tolist()
        else:
            vals = df[f].dropna().unique().tolist()
            
        if len(vals) < 500: 
            formatted_vals = []
            for v in vals:
                v_str = str(v)
                if "week" in f.lower():
                    parts = v_str.split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        v_str = f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            v_str = f"{parts[0]}-{int(week_num):02d}"
                formatted_vals.append(v_str)
                
            factor_values[f] = sorted(formatted_vals, key=natural_sort_key)

    # Load display_ids for D_donation_id values
    display_ids = {}
    if 'D_donation_id' in factors:
        display_map = load_display_id_map()
        don_vals = factor_values.get('D_donation_id', [])
        for v in don_vals:
            if v in display_map:
                display_ids[v] = display_map[v]

    interpretations = {}
    try:
        inter_path = f"{study}_comp_interpretations.json"
        if data_io.exists(storage_location="cache", filename=inter_path):
            loaded_interps = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
            if loaded_interps:
                interpretations = loaded_interps
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error loading interpretations: {e}")
        
    filtered_numeric_cols = _filter_pca_components_by_variance(numeric_cols, interpretations)

    return jsonify({
        "numeric_cols": filtered_numeric_cols,
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "interpretations": interpretations,
        "schema_map": schema_map,
        "display_ids": display_ids
    })


@data_bp.route('/api/correlations/data', methods=['POST'])
@login_required
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
            is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
            if is_dt or "date" in col.lower():
                mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
            elif "week" in col.lower():
                def format_week(v_str):
                    parts = str(v_str).split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        return f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            return f"{parts[0]}-{int(week_num):02d}"
                    return str(v_str)
                formatted_col = df[col].apply(format_week)
                mask &= formatted_col.astype(str).isin(vals)
            else:
                mask &= df[col].astype(str).isin(vals)
    
    filtered_df = df[mask].copy()

    filtered_df = filtered_df.dropna(subset=[x_col, y_col])

    total_count = len(filtered_df)

    MAX_POINTS = 5000
    if len(filtered_df) > MAX_POINTS:
        filtered_df = filtered_df.sample(MAX_POINTS)
    
    # Get factor columns for richer hover tooltips
    factors, _ = get_factors_and_features_from_var_schema(some_events_df=df, verbose=False)
    
    result_data = []
    has_color = color_col and color_col in filtered_df.columns
    
    # Build schema_map for friendly display names in tooltips
    schema_map = {}
    if 'var_schema' in fyp_cf and isinstance(fyp_cf['var_schema'], pd.DataFrame):
        vs = fyp_cf['var_schema']
        for _, row in vs.iterrows():
            var_name = str(row.get('variable_name', ''))
            dname = str(row.get('display_name', ''))
            if dname and dname.lower() != 'nan' and dname.strip():
                schema_map[var_name] = dname.strip()

    # Build richer hover text with grouping factors
    factor_cols_in_df = [f for f in factors if f in filtered_df.columns and f != color_col]
    
    # Get display IDs for D_donation_id
    display_map = {}
    if 'D_donation_id' in factor_cols_in_df or color_col == 'D_donation_id':
        display_map = load_display_id_map()

    # Helper function to format specific values
    def format_value(col_name, val):
        if pd.isna(val) or val is None:
            return "N/A"
        # Truncate dates to just YYYY-MM-DD
        if "date" in col_name.lower() or isinstance(val, (pd.Timestamp, np.datetime64)):
            return str(val)[:10]
        if "week" in col_name.lower():
            v_str = str(val)
            parts = v_str.split('-')
            if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                return f"{parts[0]}-{int(parts[1]):02d}"
            elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                week_num = parts[1][1:]
                if week_num.isdigit():
                    return f"{parts[0]}-{int(week_num):02d}"
        # Resolve display IDs
        if col_name == 'D_donation_id' and str(val) in display_map:
            return display_map[str(val)]
        # Format numeric values (comma for thousands, up to 4 precision/significant digits)
        if isinstance(val, (int, float, np.integer, np.floating)):
            if val == 0:
                return "0"
            import math
            try:
                # Calculate required decimals for 4 significant digits
                decimals = 4 - int(math.floor(math.log10(abs(val)))) - 1
                if decimals <= 0:
                    rounded_val = int(round(val, decimals))
                    formatted = f"{rounded_val:,}"
                else:
                    formatted = f"{round(val, decimals):,}"
                    if '.' in formatted:
                        formatted = formatted.rstrip('0').rstrip('.')
                return formatted if formatted else "0"
            except Exception as e:
                return str(val)
            
        return str(val)

    # Identify unscaled absolute numeric features
    raw_numeric_cols = [c for c in filtered_df.columns if str(c).endswith('_raw')]

    # Prepare sorted bases for suffix extraction on PCA components
    sorted_base_names = sorted(schema_map.keys(), key=len, reverse=True)

    for row in filtered_df.itertuples():
        x_val = getattr(row, x_col)
        y_val = getattr(row, y_col)
        
        c_val = "Default"
        if has_color:
            c_val = format_value(color_col, getattr(row, color_col))
        
        # Build hover text with all grouping factors
        color_col_display = schema_map.get(color_col, color_col)
        hover_parts = [f"{color_col_display}: {c_val}"]
        
        for fc in factor_cols_in_df:
            fv = getattr(row, fc, None)
            if fv is not None:
                fc_display = schema_map.get(fc, fc)
                fv_formatted = format_value(fc, fv)
                hover_parts.append(f"{fc_display}: {fv_formatted}")
                
        # Inject absolute unscaled values
        for r_col in raw_numeric_cols:
            r_val = getattr(row, r_col, None)
            if r_val is not None and not pd.isna(r_val):
                base_col_name = str(r_col)[:-4] # strip _raw
                
                # Try base name, then parse for PCA suffixes natively
                r_display = schema_map.get(base_col_name)
                if not r_display:
                    r_display = base_col_name
                    for b_name in sorted_base_names:
                        if base_col_name.startswith(b_name + '_'):
                            formatted_suf = base_col_name[len(b_name) + 1:].replace('_', ' ')
                            r_display = f"{schema_map[b_name]} ({formatted_suf})"
                            break
                
                r_val_formatted = format_value(base_col_name, r_val)
                    
                hover_parts.append(f"{r_display} (Abs): {r_val_formatted}")
                
        txt = "<br>".join(hover_parts)
        
        result_data.append({
            "x": x_val,
            "y": y_val,
            "color_val": getattr(row, color_col) if has_color else "Default", # preserve raw for frontend mapping
            "text": txt
        })

    return jsonify({"data": result_data, "total_count": total_count})


@data_bp.route('/api/correlations/correlation_matrix', methods=['POST'])
@login_required
def api_pca_correlation_matrix():
    data = request.json or {}
    study = data.get("study")
    filters = data.get("filters", {})

    if not study:
        return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None:
        return jsonify({"error": "PCA data not found"}), 404

    # Apply filters
    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col in df.columns:
            is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
            if is_dt or "date" in col.lower():
                mask &= df[col].astype(str).str[:10].isin([str(v)[:10] for v in vals])
            elif "week" in col.lower():
                def format_week(v_str):
                    parts = str(v_str).split('-')
                    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].isdigit():
                        return f"{parts[0]}-{int(parts[1]):02d}"
                    elif len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4 and parts[1].lower().startswith('w'):
                        week_num = parts[1][1:]
                        if week_num.isdigit():
                            return f"{parts[0]}-{int(week_num):02d}"
                    return str(v_str)
                formatted_col = df[col].apply(format_week)
                mask &= formatted_col.astype(str).isin(vals)
            else:
                mask &= df[col].astype(str).isin(vals)
    filtered_df = df[mask].copy()

    # Select only numeric columns for correlation (exclude unscaled '_raw' columns)
    numeric_df = filtered_df.select_dtypes(include=['number'])
    numeric_cols_to_keep = [col for col in numeric_df.columns if not str(col).endswith('_raw')]
    numeric_df = numeric_df[numeric_cols_to_keep]
    
    # Filter out any columns that are constant within this filtered subset
    numeric_df = numeric_df.loc[:, numeric_df.nunique(dropna=True) > 1]
    
    if numeric_df.shape[1] < 2:
        return jsonify({"error": "Not enough numeric columns for correlation"}), 400
        
    # Apply variance threshold filtering
    interpretations = {}
    try:
        inter_path = f"{study}_comp_interpretations.json"
        if data_io.exists(storage_location="cache", filename=inter_path):
            loaded_interps = data_io.load_json(storage_location="cache", filename=inter_path, verbose=False)
            if loaded_interps:
                interpretations = loaded_interps
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error loading interpretations for heatmap: {e}")
        
    filtered_cols = _filter_pca_components_by_variance(numeric_df.columns.tolist(), interpretations)
    numeric_df = numeric_df[filtered_cols]
    
    if numeric_df.shape[1] < 2:
         return jsonify({"error": "Not enough numeric columns after variance filtering"}), 400

    # Compute Pearson correlations
    corr = numeric_df.corr()

    # Replace NaN with 0 for serialization
    corr = corr.fillna(0.0)

    return jsonify({
        "columns": corr.columns.tolist(),
        "matrix": corr.values.tolist(),
        "count": len(filtered_df)
    })


@data_bp.route('/api/collections/info', methods=['GET'])
@login_required
def api_persona_stats_info():
    if True:
        if data_io.exists(storage_location="processed_activities", filename="ddp_metadata.parquet"):
            mtime = data_io.getmtime(storage_location="processed_activities", filename="ddp_metadata.parquet")
            timestamp = datetime.fromtimestamp(mtime).strftime('%d %b %Y %H:%M')
            return jsonify({"exists": True, "timestamp": timestamp})
        return jsonify({"exists": False, "timestamp": None})


@data_bp.route('/api/collections/cached', methods=['GET'])
@login_required
def api_persona_stats_cached():
    # Alias to the main stats endpoint since we no longer distinguish between cached and calculated
    return api_persona_stats()


@data_bp.route('/api/collections/stats', methods=['POST', 'GET']) # Allow GET for convenience
@login_required
def api_persona_stats():
    try:
        # --- ACCESS CONTROL ---
        if current_user.is_authenticated:
            # Use username consistently like other routes
            username = getattr(current_user, 'username', current_user.id)
            
            # Handle role attribute
            role = 'viewer' # Default
            if hasattr(current_user, 'role'):
                role = current_user.role
            elif hasattr(current_user, 'user_role'):
                role = current_user.user_role
                
            # Correctly determine admin status (is_admin is a METHOD, must be called)
            is_admin = False
            if hasattr(current_user, 'is_admin'):
                attr = getattr(current_user, 'is_admin')
                if callable(attr):
                    is_admin = attr()
                else:
                    is_admin = bool(attr)
            
            # Fallback check against role string directly
            if role == 'admin':
                is_admin = True
                
        else:
             # If not authenticated but route allows (?), default to public
             username = 'anonymous'
             role = 'viewer'
             is_admin = False

        accessible_studies = get_accessible_studies(username, role, is_admin)
        # print(f"DEBUG PERSONA: Accessible studies for {username}: {accessible_studies}")

        # Collect allowed donation IDs
        allowed_donation_ids = set()
        for study in accessible_studies:
            study_donations = get_study_donations(study)
            for d in study_donations:
                 if 'D_donation_id' in d:
                     allowed_donation_ids.add(str(d['D_donation_id']))
        
        # print(f"DEBUG PERSONA: Found {len(allowed_donation_ids)} allowed donations")
        
        if not allowed_donation_ids:
             return jsonify([]) # Return empty list if no access
        # ----------------------

        filename = "ddp_metadata.parquet"
        if not data_io.exists(storage_location="processed_activities", filename=filename):
             return jsonify({"error": "Persona metadata file not found."}), 404
        
        stats_df = None
        
        # Load the parquet file
        try:
             stats_df = data_io.load_parquet(
                storage_location="processed_activities",
                filename=filename
            )
        except Exception as e:
             # Fallback: reconstruction column by column
             print(f"Error loading parquet with default settings: {e}")
             primary, _, _, _ = data_io._resolve_paths(fyp_cf, "processed_activities", filename)
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
                #elif group == 'other' and name == 'D_id':
                #     col_name = 'D_id'
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
                        #elif group == 'other' and name == 'D_id':
                        #    col_name = 'D_id'
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

        # --- ACCESS CONTROL: Filter by Allowed Donations ---
        
        # Ensure allowed IDs are strings
        allowed_donation_ids = set(str(x) for x in allowed_donation_ids)

        if 'D_donation_id' in stats_df.columns:
            stats_df = stats_df[stats_df['D_donation_id'].astype(str).isin(allowed_donation_ids)]
        elif stats_df.index.name == 'D_donation_id' or 'D_donation_id' not in stats_df.columns:
            # Try filtering on index if column missing
            stats_df = stats_df[stats_df.index.astype(str).isin(allowed_donation_ids)]
            
        # ----------------------------------------------------

        # Filter by Accepted
        if 'accepted' in stats_df.columns:
            stats_df = stats_df[stats_df['accepted'] == True].copy()
            # print(f"Filtered to {len(stats_df)} accepted donations")

        # Frontend Compatibility Aliases
        if 'consistency_top_2_hours' in stats_df.columns and 'consistency' not in stats_df.columns:
            stats_df['consistency'] = stats_df['consistency_top_2_hours']

        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        
        # --- MERGE DONATION ANNOTATIONS ---
        da_filename = "donation_annotations.json"
        try:
            # We load the annotations here
            # We must be careful about concurrency but for now basic load is fine
            if data_io.exists(storage_location="processed_activities", filename=da_filename):
                donation_annotations = data_io.load_json(storage_location="processed_activities", filename=da_filename) or {}
                
                for rec in records:
                    d_id = str(rec.get('D_donation_id', ''))
                    if d_id and d_id in donation_annotations:
                        # Merge the annotation fields
                        # Specifically 'annotation_tags' (list) and 'display_donation_id' (str)
                        rec['annotation_tags'] = donation_annotations[d_id].get('annotation_tags', [])
                        rec['display_donation_id'] = donation_annotations[d_id].get('display_donation_id', "")
            else:
                 pass
        except Exception as e:
            print(f"Error merging annotations: {e}")
            
        
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
            mtime = data_io.getmtime(storage_location="processed_activities", filename=filename)
            # Format as ISO string or similar for frontend parsing

            dt = datetime.fromtimestamp(mtime)
            response.headers['X-Metadata-MTime'] = dt.isoformat()
        except Exception as e:
            print(f"Could not get mtime for {filename}: {e}")

        return response
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/donation/annotate', methods=['POST'])
@login_required 
@admin_required
def api_donation_annotate():
    data = request.json or {}
    donation_id = data.get("donation_id")
    
    if not donation_id:
        return jsonify({"error": "No donation ID provided"}), 400
        
    # Fields to update
    tags = data.get("tags") # list
    display_id = data.get("display_donation_id") # string
    hidden = data.get("hidden") # boolean
    
    # Validation?
    
    da_filename = "donation_annotations.json"
    
    # Load existing (with lock if we had one, but we rely on atomic write or loose consistency here)
    annotations = {}
    if data_io.exists(storage_location="processed_activities", filename=da_filename):
        annotations = data_io.load_json(storage_location="processed_activities", filename=da_filename) or {}
        
    if donation_id not in annotations:
        annotations[donation_id] = {}
        
    # Update fields if provided
    if tags is not None:
        if not isinstance(tags, list): return jsonify({"error": "Tags must be a list"}), 400
        annotations[donation_id]['annotation_tags'] = tags
        
    if display_id is not None:
        annotations[donation_id]['display_donation_id'] = str(display_id).strip()
        
    if hidden is not None:
        annotations[donation_id]['hidden'] = bool(hidden)
        
    # Save
    data_io.save_json(data=annotations, storage_location="processed_activities", filename=da_filename)
    
    return jsonify({"status": "success", "donation_id": donation_id, "data": annotations[donation_id]})


@data_bp.route('/api/video_analysis/item/<study>/<item_id>', methods=['GET', 'POST'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study, context="viewer")
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
        
    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        else: return jsonify({"error": "ID column missing"}), 500

    # OPTIMIZATION: Filter down to the item FIRST before running huge global filters and tag enrichment!
    # This turns an O(N) operation heavily bottlenecked by dataset size into an O(1) instantaneous fetch
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

    # Apply Context Filters if provided (POST)
    # We still need this in case duplicate rows exist for the same item_id, 
    # to find the specific duplicate row that matched the global filters.
    if request.method == 'POST':
        data = request.json or {}
        filters = data.get("filters", {})
        search_query = data.get("search_query")
        
        if filters or search_query:
            filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
            # If the filter dropped all duplicates, we fallback to the first unfiltered one
            # to avoid returning a 404 when clicking next/prev immediately after a filter change.
            if not filtered_df.empty:
                 df = filtered_df
    # Since we sliced df to the specific item at the completely top, 
    # df now only contains exactly the matching duplicate row(s).
    record = df.iloc[0].replace({np.nan: None}).to_dict()
    # Inject Shared Annotations for this item
    if shared_detailed_map:
        str_id = str(item_id)
        if str_id in shared_detailed_map:
            record['shared_annotations'] = shared_detailed_map[str_id]

    # Inject Display ID
    display_map = load_display_id_map()
    # Check D_donation_id, D_id, or item_id itself
    # Usually display_id is mapped from D_donation_id
    did = record.get('D_donation_id')
    if did:
        did_str = str(did)
        if did_str in display_map:
            record['display_donation_id'] = display_map[did_str]
    
    # Also check D_id if different?
    # Usually D_id is same as D_donation_id or very related. 
    # The map is keyed by D_donation_id typically.

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
        
    # --- ACCESS CONTROL ---
    # Verify user has access to this donation via at least one study
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
    # Optimization: iterate studies, check if donation is in it. Stop at first match.
    
    for study in studies:
        study_donations = get_study_donations(study) 
        # Convert to set of strings for fast lookup
        # (study_donations is cached if we used lru_cache, but we didn't add it yet.
        # explorer_backend.get_explorer_data IS cached. 
        # And get_study_donations uses simple load_parquet which hits disk or OS buffer.)
        
        # Let's just check the ids.
        for d in study_donations:
            if str(d.get('D_donation_id')) == str(donation_id):
                has_access = True
                break
        if has_access:
            break
            
    if not has_access:
        return jsonify({"error": "Access denied to this donation"}), 403
    # ----------------------

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
    """
    Returns list of donations ({D_donation_id, D_id, ...}) that the current user 
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
        
    # 2. Collect allowed donation IDs from these studies
    allowed_donation_ids = set()
    
    # Iterate studies and get donations (using optimized loader)
    for study in studies:
        study_donations = get_study_donations(study) # returns list of dicts
        #print(f"DEBUG TIMELINE: Study {study} returned {len(study_donations)} donations")
        for d in study_donations:
            # d is {'D_donation_id': ..., 'D_id': ...}
            if 'D_donation_id' in d:
                allowed_donation_ids.add(str(d['D_donation_id']))
                
    #print(f"DEBUG TIMELINE: Total allowed donation IDs: {len(allowed_donation_ids)}")
    if not allowed_donation_ids:
        return jsonify([])

    # 3. Load Metadata to get details (D_id match etc)
    # We still load the full metadata because we need to return the same structure as before
    # matching the logic. Or we can just build it from the study info?
    # The previous logic loaded `ddp_metadata.parquet` (all donations ever).
    # We should filter THAT by allowed_donation_ids.
    
    meta_df = data_io.load_parquet(storage_location="processed_activities", filename="ddp_metadata.parquet")
    
    if meta_df is None or meta_df.empty:
        return jsonify([])
        
    # Reset index if needed
    df_reset = meta_df.reset_index()
    
    # Handle MultiIndex columns (same logic as before)
    accepted_col = None
    if isinstance(meta_df.columns, pd.MultiIndex):
        if ('other', 'accepted') in meta_df.columns:
            accepted_col = ('other', 'accepted')
    else:
        if 'accepted' in meta_df.columns:
            accepted_col = 'accepted'
            
    filtered = df_reset
    if accepted_col:
        try:
             filtered = df_reset[df_reset[accepted_col] == True]
        except:
             pass
    
    target_id_col = 'D_donation_id'
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
        pass # handled above
        
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
        #print(f"DEBUG TIMELINE: Total unique donations in metadata: {len(unique_ids)}")
        
        # Filter against allowed set
        # Only include if in allowed_donation_ids
        final_valid_ids = [uid for uid in unique_ids if str(uid) in allowed_donation_ids]
        
        # Load annotations
        da_filename = "donation_annotations.json"
        annotations = {}
        try:
            if data_io.exists(storage_location="processed_activities", filename=da_filename):
                annotations = data_io.load_json(storage_location="processed_activities", filename=da_filename) or {}
        except:
            pass

        final_list = []
        for uid in final_valid_ids:
            if pd.isna(uid): continue
            uid_str = str(uid)
            item = {'D_donation_id': uid_str}
            
            # Inject display ID and tags
            if uid_str in annotations:
                 annot_data = annotations[uid_str]
                 disp = annot_data.get('display_donation_id')
                 tags = annot_data.get('annotation_tags')
                 hidden = annot_data.get('hidden')
                 
                 if disp: item['display_donation_id'] = disp
                 if tags: item['annotation_tags'] = tags
                 if hidden is not None: item['hidden'] = bool(hidden)
            
            final_list.append(item)
        
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
