import ast
import os
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_login import current_user, login_required

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.ingest import TikTokDDPCollection
from fyp.organize_datasets import COLLECTIONS_LABEL
from fyp.recode_variables import get_factors_and_features_from_var_schema

from .. import explorer_backend as explorer
from ..auth import admin_required
from ..data_service import (
    enrich_with_user_tags,
    get_accessible_studies,
    get_collection_tags,
    get_explorer_data,
    get_pca_df,
    get_study_collections,
    get_timeline_data,
    get_viz_config,
    invalidate_collection_tags_cache,
    load_display_id_map,
    load_schema_metadata,
    load_shared_tags,
    make_serializable,
)
from ..security import user_manager

data_bp = Blueprint('data_bp', __name__)

# LOCATION_CACHE_FILE = 'location_timezone_cache.json'
# PERSONA_STATS_CACHE_FILE = 'persona_stats_cache.parquet'

import re

PCA_MIN_VARIANCE_THRESHOLD = 5.0

_PLATFORM_URL_TEMPLATES: dict[str, str] = {
    "tiktok": TikTokDDPCollection.platform_url_template,
}

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


def _enforce_study_collections(metadata, study, verbose=False):
    """
    Ensures that the metadata only contains Donation IDs that are strictly part of the study.
    This prevents any cached artifacts or merging errors from exposing unrelated collection IDs.
    """
    try:
        # Get authoritative list of collections for this study
        collections = get_study_collections(study)
        valid_collection_ids = set()
        #valid_ids = set()
        
        if not collections:
            print(f"    [DATA_ROUTES] Warning: get_study_collections returned empty for {study}. Skipping filter enforcement.")
            return metadata

        for d in collections:
            if d.get('collection_id'): valid_collection_ids.add(str(d['collection_id']).strip())
            
        if not valid_collection_ids:
             print(f"    [DATA_ROUTES] Warning: No valid_collection_ids found for {study}. Skipping filter enforcement.")
             return metadata

        # Filter collection_id
        if 'collection_id' in metadata and 'values' in metadata['collection_id']:
            original = metadata['collection_id']['values']
            # Robust filter with strip
            filtered = [v for v in original if str(v['value']).strip() in valid_collection_ids]
            
            # Debugging mismatch if drastic change
            if len(original) > 0 and len(filtered) == 0:
                print(f"    [DATA_ROUTES] CRITICAL: Filter removed ALL {len(original)} IDs for {study}. Cache is likely stale.")
                print(f"    - Sample Valid IDs: {list(valid_collection_ids)[:5]}")
                print(f"    - Sample Metadata IDs: {[str(v['value']).strip() for v in original[:5]]}")
                return None # Signal to caller that metadata is invalid
            elif len(original) != len(filtered):
                if verbose:
                    print(f"    [DATA_ROUTES] Info: Filtered collection_id for {study}: {len(original)} -> {len(filtered)}")
                
            metadata['collection_id']['values'] = filtered


            
    except Exception as e:
        print(f"    Error enforcing study collections: {e}")
        traceback.print_exc()
    
    return metadata



@data_bp.route('/api/studies/defined', methods=['GET'])
@login_required
def api_get_study_defs():
    detail = request.args.get('detail', 'false').lower() == 'true'

    studies = get_accessible_studies(
        username=current_user.username,
        role=current_user.role,
        is_admin=current_user.is_admin(),
        include_stats=detail,
    )
    return jsonify(studies)





def _inject_collection_tags(metadata: dict, collection_ids: list[str]) -> dict:
    """Inject a virtual 'Collection Tags' filter derived from collection_annotations.json."""
    try:
        annotations = get_collection_tags()
    except Exception:
        return metadata

    # Build tag → set of collection_ids mapping, restricted to IDs in this study
    study_ids = set(str(cid) for cid in collection_ids)
    tag_counter: dict[str, int] = {}
    for cid, anno in annotations.items():
        if str(cid) not in study_ids:
            continue
        for tag in anno.get('annotation_tags', []):
            tag = str(tag).strip()
            if tag:
                tag_counter[tag] = tag_counter.get(tag, 0) + 1

    if not tag_counter:
        return metadata

    # Sort by frequency descending
    sorted_tags = sorted(tag_counter.items(), key=lambda x: -x[1])
    values_list = [{"value": tag, "count": count} for tag, count in sorted_tags[:200]]
    total_unique = len(tag_counter)

    metadata['Collection Tags'] = {
        "type": "list",
        "values": values_list,
        "total_unique": total_unique,
        "null_count": 0,
    }

    # Schema info — same section as collection_id
    if 'schema_map' not in metadata:
        metadata['schema_map'] = {}
    metadata['schema_map']['Collection Tags'] = {
        "section": "Activity details",
        "display_name": "Collection Tags",
        "description": "Filter by tags assigned to collections."
    }

    # Position right after collection_id in filter_priority
    if 'filter_priority' not in metadata:
        metadata['filter_priority'] = []
    fp = metadata['filter_priority']
    if 'Collection Tags' in fp:
        fp.remove('Collection Tags')
    cid_idx = fp.index('collection_id') + 1 if 'collection_id' in fp else len(fp)
    fp.insert(cid_idx, 'Collection Tags')

    return metadata


def _get_shared_simple_map(username, user_settings):
    """
    Returns the merged item_id -> set(tags) map of all other users who opted into
    sharing, or None when the current user has sharing disabled or no sharing peers exist.
    Extracted for reuse between the legacy and overlay endpoints.
    """
    user_settings = user_settings or {}
    if not user_settings.get('share_annotations', True):
        return None
    sharing_users = []
    for u_name, u_obj in user_manager.users.items():
        if u_name == username:
            continue
        if u_obj.settings and u_obj.settings.get('share_annotations', True):
            sharing_users.append(u_name)
    if not sharing_users:
        return None
    shared_simple_map, _ = load_shared_tags(sharing_users)
    return shared_simple_map




def _inject_dynamic_priorities(metadata):
    """
    Inserts User Tags / Has Annotation / Machine Annotations into filter_priority and
    display_priority (at the front), and writes their schema_map entries.
    Idempotent — only inserts columns that exist in `metadata`.
    """
    if 'schema_map' not in metadata:
        metadata['schema_map'] = {}
    if 'filter_priority' not in metadata:
        metadata['filter_priority'] = []
    if 'display_priority' not in metadata:
        metadata['display_priority'] = []

    fp = metadata['filter_priority']
    dp = metadata['display_priority']
    sm = metadata['schema_map']

    if 'User Tags' in metadata:
        sm['User Tags'] = {
            "section": "Annotation Status",
            "display_name": "Tags by Humans",
            "description": "Tags you have assigned to items.",
        }
        if 'User Tags' in fp: fp.remove('User Tags')
        fp.insert(0, 'User Tags')
        if 'User Tags' in dp: dp.remove('User Tags')
        dp.insert(0, 'User Tags')

    if 'Has Annotation' in metadata:
        sm['Has Annotation'] = {
            "section": "Annotation Status",
            "display_name": "Has Human Annotations",
            "description": "Filter items that have notes, tags, or closed tags.",
        }
        if 'Has Annotation' in fp: fp.remove('Has Annotation')
        idx = 1 if 'User Tags' in metadata else 0
        fp.insert(idx, 'Has Annotation')
        if 'Has Annotation' in dp: dp.remove('Has Annotation')
        dp.insert(idx, 'Has Annotation')

    if 'Machine Annotations' in metadata:
        sm['Machine Annotations'] = {
            "section": "Annotation Status",
            "display_name": "Machine Annotations",
            "description": "Filter items by their machine annotation status.",
        }
        if 'Machine Annotations' in fp: fp.remove('Machine Annotations')
        idx = 0
        if 'User Tags' in metadata: idx += 1
        if 'Has Annotation' in metadata: idx += 1
        fp.insert(idx, 'Machine Annotations')
        if 'Machine Annotations' in dp: dp.remove('Machine Annotations')
        dp.insert(idx, 'Machine Annotations')

    return metadata




def _inject_collection_display_ids(metadata):
    """
    Adds `label` to each value in metadata['collection_id']['values'] from the
    project's display-ID map. Returns metadata unchanged when there's nothing to do.
    """
    display_map = load_display_id_map()
    if not display_map:
        return metadata
    for col in ['collection_id']:
        section = metadata.get(col)
        if not section or section.get('type') != 'category':
            continue
        values = section.get('values') or []
        for item in values:
            val = item.get('value')
            if val in display_map:
                item['label'] = display_map[val]
    return metadata




def _finalize_base_metadata(metadata, study):
    """
    Apply the schema/display/collection enrichment that doesn't require the
    DataFrame. Used by /api/explore/metadata/base and the cold-path fallback.
    Returns the finalized metadata, or None when collection enforcement
    invalidates the cache (signals the caller to regenerate).
    """
    metadata = load_schema_metadata(metadata)
    _inject_collection_display_ids(metadata)
    metadata = _enforce_study_collections(metadata, study)
    if metadata is None:
        return None
    collection_ids = metadata.get('collection_ids')
    if collection_ids is None:
        # Older metadata file without baked collection_ids — derive from the
        # collection_id values (a superset that's still safe; will be replaced
        # on next study refresh).
        cid_values = (metadata.get('collection_id') or {}).get('values') or []
        collection_ids = [str(v.get('value')) for v in cid_values if v.get('value') is not None]
    metadata = _inject_collection_tags(metadata, collection_ids)
    return metadata




def _build_full_metadata(df, col_types, study):
    """
    Cold-path metadata builder. Computes the static metadata from the recoded
    DataFrame, bakes collection_ids, and applies all base finalization steps.
    Returns (metadata, full_metadata_for_save) — the same dict, ready to JSON.
    """
    metadata = explorer.get_metadata(df, col_types)

    viz_config = get_viz_config()
    for col, cfg in viz_config.items():
        if col in metadata and metadata[col].get('type') == 'number' and cfg.get('log'):
            metadata[col]['log'] = True
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

    if 'collection_id' in df.columns:
        metadata['collection_ids'] = sorted(
            df['collection_id'].dropna().astype(str).unique().tolist()
        )
    else:
        metadata['collection_ids'] = []

    return metadata




def _compute_dynamic_overlay(df, col_types):
    """
    Compute per-user dynamic columns (User Tags, Has Annotation, Machine
    Annotations) plus their schema_map entries and a User-Tags total_stats
    overlay. Returns a dict shaped for the overlay endpoint.
    """
    dynamic_cols = {}
    if 'User Tags' in col_types:
        dynamic_cols['User Tags'] = 'list'
    if 'Has Annotation' in col_types:
        dynamic_cols['Has Annotation'] = 'category'
    if 'Machine Annotations' in col_types:
        dynamic_cols['Machine Annotations'] = 'category'

    columns = {}
    schema_map = {}
    stats_overlay = {}
    filter_priority_prepend = []
    display_priority_prepend = []

    if dynamic_cols:
        cols_to_get = [c for c in dynamic_cols if c in df.columns]
        if cols_to_get:
            columns = explorer.get_metadata(df[cols_to_get], dynamic_cols)
            if 'User Tags' in df.columns:
                res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'}, viz_config=get_viz_config())
                stats_overlay.update(res_tags.get('stats', {}))

    if 'User Tags' in columns:
        schema_map['User Tags'] = {
            "section": "Annotation Status",
            "display_name": "Tags by Humans",
            "description": "Tags you have assigned to items.",
        }
        filter_priority_prepend.append('User Tags')
        display_priority_prepend.append('User Tags')
    if 'Has Annotation' in columns:
        schema_map['Has Annotation'] = {
            "section": "Annotation Status",
            "display_name": "Has Human Annotations",
            "description": "Filter items that have notes, tags, or closed tags.",
        }
        filter_priority_prepend.append('Has Annotation')
        display_priority_prepend.append('Has Annotation')
    if 'Machine Annotations' in columns:
        schema_map['Machine Annotations'] = {
            "section": "Annotation Status",
            "display_name": "Machine Annotations",
            "description": "Filter items by their machine annotation status.",
        }
        filter_priority_prepend.append('Machine Annotations')
        display_priority_prepend.append('Machine Annotations')

    return {
        "columns": columns,
        "schema_map": schema_map,
        "stats_overlay": stats_overlay,
        "filter_priority_prepend": filter_priority_prepend,
        "display_priority_prepend": display_priority_prepend,
    }




@data_bp.route('/api/explore/metadata/base', methods=['GET'])
@login_required
def api_explorer_metadata_base():
    """
    Fast path: returns the static filter shape (column types, value lists,
    ranges, schema, priorities) without loading the recoded DataFrame, when
    {study}_explorer_metadata.json is on disk.

    Falls back to the cold path (loads the DF, computes metadata, saves under
    the canonical filename) when the JSON is missing or invalidated.
    """
    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    canonical_filename = f"{study}_explorer_metadata.json"

    # Fast path
    if data_io.exists(storage_location="cache", filename=canonical_filename):
        try:
            metadata = data_io.load_json(storage_location="cache", filename=canonical_filename)
            metadata = _finalize_base_metadata(metadata, study)
            if metadata is not None:
                return jsonify(make_serializable(metadata))
            print(f"    [DATA_ROUTES] Cache invalidated for {study}, regenerating...")
        except Exception as e:
            print(f"    Warning: Error loading/processing cached base metadata: {e}")
            traceback.print_exc()

    # Cold path: need the DataFrame to compute metadata from scratch
    df, col_types = get_explorer_data(study, context='explorer')
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    metadata = _build_full_metadata(df, col_types, study)
    data_io.save_json(
        data=make_serializable(metadata),
        storage_location="cache",
        filename=canonical_filename,
        verbose=False,
    )
    metadata = _finalize_base_metadata(metadata, study)
    return jsonify(make_serializable(metadata))




@data_bp.route('/api/explore/metadata/overlay', methods=['GET'])
@login_required
def api_explorer_metadata_overlay():
    """
    Per-user dynamic metadata: User Tags, Has Annotation, Machine Annotations.
    Loads the DataFrame and enriches it with the current user's tags (plus
    shared annotations from peers), then returns just the overlay dict.
    The frontend merges this into the base metadata once it arrives.
    """
    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    context = request.args.get('context', 'explorer')

    df, col_types = get_explorer_data(study, context=context)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    username = current_user.username
    shared_simple_map = _get_shared_simple_map(username, current_user.settings)
    df, col_types = enrich_with_user_tags(df, col_types, username, shared_users_tags=shared_simple_map)

    overlay = _compute_dynamic_overlay(df, col_types)
    return jsonify(make_serializable(overlay))




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
    shared_simple_map = _get_shared_simple_map(username, current_user.settings)
    df, col_types = enrich_with_user_tags(df, col_types, username, shared_users_tags=shared_simple_map)
  

    cached_metadata = None
    if data_io.exists(storage_location="cache", filename=f"{study}_explorer_metadata.json"):
        try:
            potential_metadata = data_io.load_json(storage_location="cache", filename=f"{study}_explorer_metadata.json")
            
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
                 cols_to_get = [c for c in dynamic_cols if c in df.columns]
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
                for col in ['collection_id']:
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
            potential_metadata = _enforce_study_collections(potential_metadata, study)

            # Inject Collection Tags filter
            if 'collection_id' in df.columns:
                potential_metadata = _inject_collection_tags(potential_metadata, df['collection_id'].dropna().unique().tolist())

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
    # Inject log flag into number metadata so frontend sliders can use log scale
    for col, cfg in viz_config.items():
        if col in metadata and metadata[col].get('type') == 'number' and cfg.get('log'):
            metadata[col]['log'] = True
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
        for col in ['collection_id']:
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
    metadata = _enforce_study_collections(metadata, study)

    # Inject Collection Tags filter
    if 'collection_id' in df.columns:
        metadata = _inject_collection_tags(metadata, df['collection_id'].dropna().unique().tolist())

    # Write to the canonical filename (dropping the per-context suffix) so the
    # cached payload is reused on subsequent reads regardless of which tab
    # triggered the cold computation. Both contexts compute identical metadata
    # because get_explorer_data() applies the same filter for explorer and
    # viewer (see data_service.py).
    data_io.save_json(data=make_serializable(metadata), storage_location="cache", filename=f"{study}_explorer_metadata.json", verbose=False)

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
        else: return jsonify({"error": "No ID column found"}), 500
    
    
    
    # Hide Duplicate Videos if requested
    if data.get("hide_duplicates"):
        dedup_col = 'video_id'
        if dedup_col not in filtered_df.columns:
            dedup_col = id_col
            
        filtered_df = filtered_df.drop_duplicates(subset=[dedup_col], keep='first')

    
    # Calculate true total count before slicing
    total_count = len(filtered_df)

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
        "truncated": False
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
            if 'sortable' in row:
                sval = row['sortable']
                if pd.notna(sval):
                    entry['sortable'] = int(sval)
            if entry:
                schema_map[var_name] = entry

    # Map PCA components formatted names (e.g. tiktok_native_C13 -> TikTok Native (C13), or var_entropy -> Var (entropy))
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

    # Load display_ids for collection_id values
    display_ids = {}
    if 'collection_id' in factors:
        display_map = load_display_id_map()
        don_vals = factor_values.get('collection_id', [])
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
    
    # Get display IDs for collection_id
    display_map = {}
    if 'collection_id' in factor_cols_in_df or color_col == 'collection_id':
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
        if col_name == 'collection_id' and str(val) in display_map:
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
            except Exception:
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
        
        # Collect raw factor values for drill-down to Video Analysis
        factors_dict = {}
        if has_color:
            factors_dict[color_col] = str(getattr(row, color_col))
        for fc in factor_cols_in_df:
            fv = getattr(row, fc, None)
            if fv is not None and not pd.isna(fv):
                factors_dict[fc] = str(fv)

        result_data.append({
            "x": x_val,
            "y": y_val,
            "color_val": getattr(row, color_col) if has_color else "Default",
            "text": txt,
            "factors": factors_dict
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
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            mtime = data_io.getmtime(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
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
                attr = current_user.is_admin
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

        # Admins see all accepted collections; non-admins are filtered by study access
        allowed_collection_ids = None  # None means no filtering for admins
        if not is_admin:
            accessible_studies = get_accessible_studies(username, role, is_admin)
            allowed_collection_ids = set()
            for study in accessible_studies:
                study_collections = get_study_collections(study)
                for d in study_collections:
                     if 'collection_id' in d:
                         allowed_collection_ids.add(str(d['collection_id']))

            if not allowed_collection_ids:
                 return jsonify([]) # Return empty list if no access
        # ----------------------

        filename = f"{COLLECTIONS_LABEL}_metadata.parquet"
        if not data_io.exists(storage_location="recoded", filename=filename):
             return jsonify({"error": "Persona metadata file not found."}), 404
        
        stats_df = None
        
        # Load the parquet file
        try:
             stats_df = data_io.load_parquet(
                storage_location="recoded",
                filename=filename
            )
        except Exception as e:
             # Fallback: reconstruction column by column
             print(f"Error loading parquet with default settings: {e}")
             primary, _, _, _ = data_io._resolve_paths(fyp_cf, "recoded", filename)
             try:
                 table = pq.read_table(primary)
                 data = {}
                 for i, col_name in enumerate(table.column_names):
                     data[col_name] = table.column(i).to_pandas()
                 stats_df = pd.DataFrame(data)
             except Exception as e2:
                 print(f"Fallback loading failed: {e2}")
                 return jsonify({"error": f"Failed to load data: {e!s} / {e2!s}"}), 500


        if isinstance(stats_df.index, pd.Index) and stats_df.index.name == 'collection_id':
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
                        else:
                            col_name = name if name else group
                except:
                    pass
            
                
            new_columns.append(col_name)
            
        stats_df.columns = new_columns
        
        # Handle duplicated columns (keep first)
        stats_df = stats_df.loc[:, ~stats_df.columns.duplicated()]

        # --- ACCESS CONTROL: Filter by Allowed Donations ---

        if allowed_collection_ids is not None:
            # Ensure allowed IDs are strings
            allowed_collection_ids = set(str(x) for x in allowed_collection_ids)

            if 'collection_id' in stats_df.columns:
                stats_df = stats_df[stats_df['collection_id'].astype(str).isin(allowed_collection_ids)]
            elif stats_df.index.name == 'collection_id' or 'collection_id' not in stats_df.columns:
                # Try filtering on index if column missing
                stats_df = stats_df[stats_df.index.astype(str).isin(allowed_collection_ids)]

        # ----------------------------------------------------

        # Filter by Accepted
        if 'accepted' in stats_df.columns:
            stats_df = stats_df[stats_df['accepted'] == True].copy()
            # print(f"Filtered to {len(stats_df)} accepted collections")

        # Frontend Compatibility Aliases
        if 'consistency_top_2_hours' in stats_df.columns and 'consistency' not in stats_df.columns:
            stats_df['consistency'] = stats_df['consistency_top_2_hours']

        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        
        # --- MERGE DONATION ANNOTATIONS ---
        da_filename = f"{COLLECTIONS_LABEL}_tags.json"
        try:
            # We load the annotations here
            # We must be careful about concurrency but for now basic load is fine
            if data_io.exists(storage_location="recoded", filename=da_filename):
                collection_annotations = data_io.load_json(storage_location="recoded", filename=da_filename) or {}
                
                for rec in records:
                    d_id = str(rec.get('collection_id', ''))
                    if d_id and d_id in collection_annotations:
                        # Merge the annotation fields
                        # Specifically 'annotation_tags' (list) and 'display_collection_id' (str)
                        rec['annotation_tags'] = collection_annotations[d_id].get('annotation_tags', [])
                        rec['display_collection_id'] = collection_annotations[d_id].get('display_collection_id', "")
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
            mtime = data_io.getmtime(storage_location="recoded", filename=filename)
            # Format as ISO string or similar for frontend parsing

            dt = datetime.fromtimestamp(mtime)
            response.headers['X-Metadata-MTime'] = dt.isoformat()
        except Exception as e:
            print(f"Could not get mtime for {filename}: {e}")

        return response
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/collection/annotate', methods=['POST'])
@login_required 
@admin_required
def api_collection_annotate():
    data = request.json or {}
    collection_id = data.get("collection_id")
    
    if not collection_id:
        return jsonify({"error": "No collection ID provided"}), 400
        
    # Fields to update
    tags = data.get("tags") # list
    display_id = data.get("display_collection_id") # string
    hidden = data.get("hidden") # boolean
    
    # Validation?
    
    da_filename = f"{COLLECTIONS_LABEL}_tags.json"
    
    # Load existing (with lock if we had one, but we rely on atomic write or loose consistency here)
    annotations = {}
    if data_io.exists(storage_location="recoded", filename=da_filename):
        annotations = data_io.load_json(storage_location="recoded", filename=da_filename) or {}
        
    if collection_id not in annotations:
        annotations[collection_id] = {}
        
    # Update fields if provided
    if tags is not None:
        if not isinstance(tags, list): return jsonify({"error": "Tags must be a list"}), 400
        annotations[collection_id]['annotation_tags'] = tags
        
    if display_id is not None:
        annotations[collection_id]['display_collection_id'] = str(display_id).strip()
        
    if hidden is not None:
        annotations[collection_id]['hidden'] = bool(hidden)
        
    # Save
    data_io.save_json(data=annotations, storage_location="recoded", filename=da_filename)
    invalidate_collection_tags_cache()

    return jsonify({"status": "success", "collection_id": collection_id, "data": annotations[collection_id]})


@data_bp.route('/api/video_analysis/item/<study>/<item_id>', methods=['GET', 'POST'])
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


@data_bp.route('/api/timelines/data', methods=['POST'])
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


@data_bp.route('/api/timelines/collections', methods=['POST'])
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
    collection_study_map: dict[str, str] = {}

    # Iterate studies and get collections (using optimized loader)
    for study in studies:
        study_collections = get_study_collections(study) # returns list of dicts
        #print(f"DEBUG TIMELINE: Study {study} returned {len(study_collections)} collections")
        for d in study_collections:
            # d is {'collection_id': ..., }
            if 'collection_id' in d:
                cid = str(d['collection_id'])
                allowed_collection_ids.add(cid)
                if cid not in collection_study_map:
                    collection_study_map[cid] = study
                
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

            # Study that contains this collection (for drill-down to Video Analysis).
            if uid_str in collection_study_map:
                item['study'] = collection_study_map[uid_str]

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


@data_bp.route('/api/video/<study>/<item_id>', methods=['GET'])
def api_video_stream(study, item_id):

    use_gcs = fyp_cf.get('data_io', {}).get('use_gcs_for_media', True)
    chunk_size = 4096 * 16
    range_header = request.headers.get('Range')

    if use_gcs:
        bucket = fyp_cf.get("data_io", {}).get("bucket")
        if not bucket:
            return "GCS Bucket not available. Check credentials or internet connection.", 503

        blob_name = f"{fyp_cf['data_io']['gcs_media_prefix']}/{item_id}.mp4"
        blob = bucket.blob(blob_name)

        if not blob.exists():
             return f"Video {blob_name} not found", 404

        blob.reload()
        total_size = blob.size

        if range_header:
            range_spec = range_header.replace('bytes=', '').strip()
            parts = range_spec.split('-')
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else min(start + chunk_size * 16 - 1, total_size - 1)
            end = min(end, total_size - 1)
            length = end - start + 1

            def generate_range():
                with blob.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        read_size = min(chunk_size, remaining)
                        data = f.read(read_size)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            headers = {
                'Content-Range': f'bytes {start}-{end}/{total_size}',
                'Accept-Ranges': 'bytes',
                'Content-Length': str(length),
                'Content-Type': 'video/mp4',
                'Cache-Control': 'private, max-age=3600',
            }
            return Response(stream_with_context(generate_range()), status=206, headers=headers)

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

    # Local filesystem path
    media_path = os.path.join(fyp_cf['paths']['media'], f"{item_id}.mp4")
    if not os.path.exists(media_path):
        return f"Video {item_id}.mp4 not found", 404

    total_size = os.path.getsize(media_path)

    if range_header:
        range_spec = range_header.replace('bytes=', '').strip()
        parts = range_spec.split('-')
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else min(start + chunk_size * 16 - 1, total_size - 1)
        end = min(end, total_size - 1)
        length = end - start + 1

        def generate_range():
            with open(media_path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            'Content-Range': f'bytes {start}-{end}/{total_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(length),
            'Content-Type': 'video/mp4',
            'Cache-Control': 'private, max-age=3600',
        }
        return Response(stream_with_context(generate_range()), status=206, headers=headers)

    def generate():
        with open(media_path, 'rb') as f:
            while chunk := f.read(chunk_size):
                yield chunk

    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Length': str(total_size),
        'Content-Type': 'video/mp4',
        'Cache-Control': 'private, max-age=3600',
    }
    return Response(stream_with_context(generate()), headers=headers)




@data_bp.route('/api/system-info')
@login_required
def system_info():
    """Return basic system information for the Information panel."""
    import platform

    # Detect Google Cloud Run via its injected environment variables
    k_service = os.environ.get('K_SERVICE')
    is_cloud_run = k_service is not None

    if is_cloud_run:
        environment = f"Google Cloud Run ({k_service})"
        revision = os.environ.get('K_REVISION', 'unknown')
    else:
        environment = "Local"
        revision = None

    # Storage locations: Local or Remote based on the use_gcs_for_* flags
    data_io_cf = fyp_cf.get('data_io', {})
    data_location = "Remote" if data_io_cf.get('use_gcs_for_data') else "Local"
    media_location = "Remote" if data_io_cf.get('use_gcs_for_media') else "Local"
    cache_location = "Remote" if data_io_cf.get('use_gcs_for_cache') else "Local"

    info = {
        'os': f"{platform.system()} {platform.release()}",
        'architecture': platform.machine(),
        'python_version': platform.python_version(),
        'cpu_count': os.cpu_count(),
        'environment': environment,
        'revision': revision,
        'data_location': data_location,
        'media_location': media_location,
        'cache_location': cache_location,
    }

    return jsonify(info)
