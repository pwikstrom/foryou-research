import ast
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL

from ..auth import admin_required
from ..data_service import (
    get_accessible_studies,
    get_study_collections,
    invalidate_collection_tags_cache,
    make_serializable,
)

collections_bp = Blueprint('collections_bp', __name__)


@collections_bp.route('/api/collections/info', methods=['GET'])
@login_required
def api_persona_stats_info():
    if True:
        if data_io.exists(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet"):
            mtime = data_io.getmtime(storage_location="recoded", filename=f"{COLLECTIONS_LABEL}_metadata.parquet")
            timestamp = datetime.fromtimestamp(mtime).strftime('%d %b %Y %H:%M')
            return jsonify({"exists": True, "timestamp": timestamp})
        return jsonify({"exists": False, "timestamp": None})


@collections_bp.route('/api/collections/cached', methods=['GET'])
@login_required
def api_persona_stats_cached():
    # Alias to the main stats endpoint since we no longer distinguish between cached and calculated
    return api_persona_stats()


@collections_bp.route('/api/collections/stats', methods=['POST', 'GET']) # Allow GET for convenience
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


@collections_bp.route('/api/collection/annotate', methods=['POST'])
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
