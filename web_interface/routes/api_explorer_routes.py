import os
import platform
import traceback
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
import web_interface.auth as auth
from fyp.fyp_config import fyp_cf
from fyp.scrape import scraper_alerts
from web_interface import task_failures

from .. import explorer_backend as explorer
from ..data_service import (
    _get_recoded_mtime,
    enrich_with_user_tags,
    get_accessible_studies,
    get_collection_tags,
    get_explorer_data,
    get_study_collections,
    load_display_id_map,
    load_schema_metadata,
    load_shared_tags,
    make_serializable,
)
from ..permissions import permission_required
from ..security import user_manager
from ..services import system_health
from ..services.user_variables import compose_effective_variables
from ._access import study_access_error

explorer_bp = Blueprint('explorer_bp', __name__)


@explorer_bp.route('/api/studies/defined', methods=['GET'])
@login_required
@permission_required('tab.explore', 'tab.timelines', 'tab.video_analysis',
                     'tab.correlations', 'tab.semantic_space')
def api_get_study_defs():
    detail = request.args.get('detail', 'false').lower() == 'true'

    studies = get_accessible_studies(
        username=current_user.username,
        role=current_user.role,
        is_admin=current_user.is_admin(),
        include_stats=detail,
    )
    return jsonify(studies)


@explorer_bp.route('/api/studies/<study>/methods', methods=['GET'])
@permission_required('tab.explore', 'tab.video_analysis', 'tab.my_stuff.my_studies')
def api_study_methods(study):
    """The study's methods/provenance note (written at refresh time).

    Informational: describes how the dataset was built — filters, counts,
    annotation/scrape/activity versions, embedding model, refresh dates.

    Surfaced from the My Studies table (any-of permissions above); the per-study
    ``study_access_error`` gate below is what actually scopes the response.
    """
    from ..services import methods_note as methods_note_service

    denied = study_access_error(study)
    if denied is not None:
        return denied

    note = methods_note_service.read_methods_note(study)
    if note is None:
        return jsonify({
            "error": "No methods note exists for this study yet",
            "hint": "Refresh the study (or run the data pipeline) to generate it.",
        }), 404

    payload = dict(note)
    payload["staleness"] = methods_note_service.note_staleness(study, note)
    return jsonify(payload)


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
        "section": "Activity",
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
    # Sharing is opt-in: an unset value reads as off (matches the viewer route).
    if not user_settings.get('share_annotations'):
        return None
    sharing_users = []
    for u_name, u_obj in user_manager.get_all_users().items():
        if u_name == username:
            continue
        if u_obj.settings and u_obj.settings.get('share_annotations'):
            sharing_users.append(u_name)
    if not sharing_users:
        return None
    shared_simple_map, _ = load_shared_tags(sharing_users)
    return shared_simple_map


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


def _stamp_source_file_modified(metadata: dict, study: str) -> None:
    """Refresh ``source_file_modified`` from the parquet on every read.

    The cached ``{study}_explorer_metadata.json`` can predate the switch to
    offset-aware instants, and those older entries are zone-less strings written
    in whatever timezone the machine that produced them ran in. Recomputing here
    — it is a single stat call — means the browser only ever sees an unambiguous
    instant, without waiting for a study refresh to rewrite the cache.
    """
    mtime = _get_recoded_mtime(study)
    if mtime is None:
        return
    metadata['source_file_modified'] = datetime.fromtimestamp(
        mtime, tz=UTC,
    ).isoformat(timespec='seconds')






def _finalize_base_metadata(metadata, study):
    """
    Apply the schema/display/collection enrichment that doesn't require the
    DataFrame. Used by /api/explore/metadata/base and the cold-path fallback.
    Returns the finalized metadata, or None when collection enforcement
    invalidates the cache (signals the caller to regenerate).
    """
    metadata = load_schema_metadata(metadata)
    _stamp_source_file_modified(metadata, study)
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

    res = explorer.get_current_stats(df, col_types, number_meta=metadata)
    metadata['total_stats'] = res['stats']

    try:
        the_recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(storage_location="cache", filename=the_recoded_file):
            metadata['source_file'] = the_recoded_file
            mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file), tz=UTC)
            metadata['source_file_modified'] = mtime.isoformat(timespec='seconds')
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


# The overlay is built entirely from the three dynamic columns
# enrich_with_user_tags adds, and those are derived from just these source
# columns. Projecting the study frame to them keeps this endpoint's per-request
# copy in the tens of MB instead of several GB on a multi-million-row study.
OVERLAY_SOURCE_COLUMNS = ("item_id", "annotated_ok", "annotation_version")


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
                res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'})
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
            "description": "Filter items by the model that machine-annotated them (or their annotation status).",
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


@explorer_bp.route('/api/explore/metadata/base', methods=['GET'])
@permission_required('tab.explore', 'tab.video_analysis')
def api_explorer_metadata_base():
    """
    Fast path: returns the static filter shape (column types, value lists,
    ranges, schema, priorities) without loading the recoded DataFrame, when
    {study}_explorer_metadata.json is on disk.

    Falls back to the cold path (loads the DF, computes metadata, saves under
    the canonical filename) when the JSON is missing or invalidated.

    Serves both the Explore and Video Analysis tabs (either permission grants
    access).
    """
    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    denied = study_access_error(study)
    if denied is not None:
        return denied

    canonical_filename = f"{study}_explorer_metadata.json"

    # Fast path
    if data_io.exists(storage_location="cache", filename=canonical_filename):
        # Staleness check: if the recoded parquet was rewritten after this
        # JSON was saved, the cached filter counts no longer match the data.
        # Fall through to the cold path so metadata is regenerated.
        cache_is_fresh = True
        parquet_mtime = _get_recoded_mtime(study)
        if parquet_mtime is not None:
            try:
                json_mtime = data_io.getmtime(storage_location="cache", filename=canonical_filename)
                if json_mtime < parquet_mtime:
                    cache_is_fresh = False
                    print(f"    [DATA_ROUTES] Base metadata for {study} is older than recoded parquet, regenerating...")
            except Exception as e:
                print(f"    Warning: Could not read base metadata mtime for {study}: {e}")
                cache_is_fresh = False

        if cache_is_fresh:
            try:
                metadata = data_io.load_json(storage_location="cache", filename=canonical_filename)
                metadata = _finalize_base_metadata(metadata, study)
                if metadata is not None:
                    return jsonify(make_serializable(metadata))
                print(f"    [DATA_ROUTES] Cache invalidated for {study}, regenerating...")
            except Exception as e:
                print(f"    Warning: Error loading/processing cached base metadata: {e}")
                traceback.print_exc()

    # Cold path: need the DataFrame to compute metadata from scratch. This runs
    # whenever the cached JSON is stale relative to the recoded parquet — most
    # commonly right after a study has been (re)recoded, when the parquet may
    # still be mid-write or the in-process schema/version caches lag the new
    # columns. Any failure here must surface as a clean, retryable message
    # rather than a raw 500 (which the frontend can only render as the opaque
    # "Failed to load metadata").
    try:
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
    except Exception as e:
        print(f"    [DATA_ROUTES] Cold-path base metadata build failed for {study}: {e}")
        traceback.print_exc()
        return jsonify({
            "error": "This study's data is still being prepared (it may be "
                     "mid-refresh). Please retry in a moment."
        }), 503


@explorer_bp.route('/api/explore/metadata/overlay', methods=['GET'])
@permission_required('tab.explore', 'tab.video_analysis')
def api_explorer_metadata_overlay():
    """
    Per-user dynamic metadata: User Tags, Has Annotation, Machine Annotations.
    Loads the DataFrame and enriches it with the current user's tags (plus
    shared annotations from peers), then returns just the overlay dict.
    The frontend merges this into the base metadata once it arrives.

    Serves both the Explore and Video Analysis tabs (either permission grants
    access).
    """
    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    denied = study_access_error(study)
    if denied is not None:
        return denied

    context = request.args.get('context', 'explorer')

    # The overlay is per-user enrichment (tags, annotation status) that the
    # frontend merges on top of the base metadata. A failure here is non-fatal:
    # the page still renders from the base call, so degrade gracefully to an
    # empty overlay rather than 500-ing.
    try:
        df, col_types = get_explorer_data(
            study, context=context, columns=OVERLAY_SOURCE_COLUMNS,
        )
        if df is None:
            return jsonify({"error": "Dataset not found"}), 404

        username = current_user.username
        shared_simple_map = _get_shared_simple_map(username, current_user.settings)
        df, col_types = enrich_with_user_tags(df, col_types, username, shared_users_tags=shared_simple_map)

        overlay = _compute_dynamic_overlay(df, col_types)
        return jsonify(make_serializable(overlay))
    except Exception as e:
        print(f"    [DATA_ROUTES] Overlay metadata build failed for {study}: {e}")
        traceback.print_exc()
        return jsonify({
            "columns": {}, "schema_map": {}, "stats_overlay": {},
            "filter_priority_prepend": [], "display_priority_prepend": [],
        })


@explorer_bp.route('/api/explore/metadata', methods=['GET'])
@permission_required('tab.explore', 'tab.video_analysis')
def api_explorer_metadata():

    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    denied = study_access_error(study)
    if denied is not None:
        return denied

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
                          res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'})
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
                    "description": "Filter items by the model that machine-annotated them (or their annotation status)."
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

    res = explorer.get_current_stats(df, col_types, number_meta=metadata)
    metadata['total_stats'] = res['stats']

    try:
        the_recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(storage_location="cache", filename=the_recoded_file):
            metadata['source_file'] = the_recoded_file
            mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file), tz=UTC)
            metadata['source_file_modified'] = mtime.isoformat(timespec='seconds')
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
            "description": "Filter items by the model that machine-annotated them (or their annotation status)."
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


# Dynamic per-user columns are kept in the stats payload whenever present:
# they are not schema variables, so they can never appear in the viz prefs,
# and the frontend renders them through their own prepend lists.
_DYNAMIC_STATS_COLUMNS = ("User Tags", "Has Annotation", "Machine Annotations")


def _viz_stats_col_types(col_types, user_settings):
    """Narrow ``col_types`` to the columns whose stats the user will see.

    ``get_current_stats`` is the whole cost of a filter change — dominated by
    columns the frontend then never renders. The rendered set is the user's
    effective viz list (global ``viz_priority`` composed with their stored
    ``variable_prefs.viz`` deltas), which lives server-side in the user
    settings, so it is composed here the same way Timelines composes its
    variable list. Falls back to the full mapping when the schema yields no
    viz list (fresh install / missing var_schema) — in that case the frontend
    renders every stats key, so every key must be computed.

    Args:
        col_types: Full column -> classified-type mapping for the frame.
        user_settings: The current user's settings dict (may be None).

    Returns:
        Filtered col_types mapping, in the original iteration order.
    """
    schema_meta = load_schema_metadata({})
    global_viz = schema_meta.get('viz_priority') or []
    all_order = schema_meta.get('all_variables_order') or []
    if not global_viz:
        return col_types

    prefs = ((user_settings or {}).get('variable_prefs') or {}).get('viz') or {}
    effective = compose_effective_variables(global_viz, prefs, all_order)
    keep = set(effective) | set(_DYNAMIC_STATS_COLUMNS)
    return {k: v for k, v in col_types.items() if k in keep}


@explorer_bp.route('/api/explore/filter', methods=['POST'])
@permission_required('tab.explore')
def api_explorer_filter():
    data = request.json or {}
    study = data.get("study")

    if not study:
         return jsonify({"error": "No study specified"}), 400

    denied = study_access_error(study)
    if denied is not None:
        return denied

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

    # Stats are only computed for the columns this user's Explore actually
    # renders (their effective viz set + dynamic columns) — the dominant cost
    # of this endpoint used to be stats for ~90 columns the frontend dropped.
    stats_col_types = _viz_stats_col_types(col_types, current_user.settings)

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
                     res_tags = explorer.get_current_stats(df[['User Tags']], {'User Tags': 'list'})
                     result['stats'].update(res_tags['stats'])

        else:
            filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
            res1 = explorer.get_current_stats(
                filtered_df, stats_col_types, number_meta=cached_metadata)
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
                res2 = explorer.get_current_stats(
                    filtered_df2, stats_col_types, number_meta=cached_metadata)

                result['stats2'] = res2['stats']
                result['count2'] = res2['count']

    return jsonify(make_serializable(result))


@explorer_bp.route('/api/system-info')
@permission_required('tab.admin.system_info')
def system_info():
    """Return basic system information for the Information panel."""

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


@explorer_bp.route('/api/system-health')
@permission_required('tab.admin.system_info')
def get_system_health():
    """Return the current system-health document for the Information panel.

    Includes the active per-platform scraper alerts (raised by the scrape
    worker on systematic failures such as a permanent-failure storm) so the
    panel can flag "scraper needs revision" conditions alongside the checks,
    plus the recent background-task failure ledger (the dead-letter record for
    the Cloud Tasks queue, which has no native dead-letter topic).
    """
    doc = system_health.get_health()
    doc["scraper_alerts"] = scraper_alerts.load_alerts()
    doc["task_failures"] = task_failures.unacknowledged_dead()
    return jsonify(doc)


@explorer_bp.route('/api/system-health/task-failures/ack', methods=['POST'])
@auth.admin_required
def ack_task_failures():
    """Acknowledge one ledger entry (``{"id": ...}``) or all of them."""
    data = request.json or {}
    changed = task_failures.acknowledge(str(data.get("id") or ""))
    return jsonify({"status": "success", "acknowledged": changed})


@explorer_bp.route('/api/system-health/run', methods=['POST'])
@permission_required('tab.admin.system_info')
def run_system_health():
    """Kick off a manual health-check run; 409 when one is already running."""
    if not system_health.start_health_check(trigger="manual"):
        return jsonify({"started": False, "reason": "already_running"}), 409
    return jsonify({"started": True})
