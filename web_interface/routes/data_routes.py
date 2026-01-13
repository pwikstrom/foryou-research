from flask import Blueprint, jsonify, request, Response, stream_with_context
from flask_login import login_required
import os
import pandas as pd
import numpy as np
from datetime import datetime
from ..hub_config import fyp_cf, PROJECT_ROOT
from ..data_service import (
    get_explorer_data, get_pca_df, get_viz_config, make_serializable
)
from .. import explorer_backend as explorer
import fyp
import fyp.data_io as data_io
from fyp.calc_donation_stats import calculate_all_donation_stats, enrich_stats_with_metadata

data_bp = Blueprint('data_bp', __name__)

LOCATION_CACHE_FILE = 'location_timezone_cache.json'
PERSONA_STATS_CACHE_FILE = 'persona_stats_cache.parquet'

@data_bp.route('/api/explorer/studies', methods=['GET'])
@login_required
def api_explorer_studies():
    studies = []
    if data_io.exists(
        cf=fyp_cf,
        storage_location="cache",
        filename="_recoded.parquet",
        ):
        recoded_files = [fn for fn in data_io.listdir(cf=fyp_cf, storage_location="cache") if fn.endswith("_recoded.parquet")]
        for fn in recoded_files:
            study_name = fn.replace("_recoded.parquet", "")
            studies.append(study_name)
    
    return jsonify(sorted(studies))


@data_bp.route('/api/studies/defined', methods=['GET'])
@login_required
def api_get_study_defs():
    if 'study_defs' in fyp_cf:
        return jsonify(sorted(list(fyp_cf['study_defs'].keys())))
    return jsonify([])


@data_bp.route('/api/explorer/metadata', methods=['GET'])
@login_required
def api_explorer_metadata():
    #from os.path import exists as os_exists, getmtime as os_getmtime, join as os_join
    from datetime import datetime

    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    print("awesome")

    df, col_types = get_explorer_data(study)

 
    context = request.args.get('context', 'explorer')

 
    if context == 'viewer':
         df = df[df.scraped_ok].copy()
         print(f"    Filtered to {len(df):,} scraped events")
    else:
         df = df[df.annotated_ok].copy()
         print(f"    Filtered to {len(df):,} annotated events")

 
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
 
    if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study}_explorer_metadata.json"):
        metadata = data_io.load_json(cf=fyp_cf, storage_location="cache", filename=f"{study}_explorer_metadata.json")
        print(f"    Using cached metadata for {study}")
        return jsonify(make_serializable(metadata))

    print(f"    No cached metadata for {study}, calculating...")
    metadata = explorer.get_metadata(df, col_types)
    
    viz_config = get_viz_config()
    res = explorer.get_current_stats(df, col_types, viz_config=viz_config)
    metadata['total_stats'] = res['stats']

    try:
        the_recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(cf=fyp_cf, storage_location="cache", filename=the_recoded_file):
            metadata['source_file'] = the_recoded_file
            mtime = datetime.fromtimestamp(data_io.getmtime(cf=fyp_cf, storage_location="cache", filename=the_recoded_file))
            metadata['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
        else:
             metadata['source_file'] = "Unknown"
             metadata['source_file_modified'] = ""
    except Exception as e:
        print(f"Error getting file info: {e}")
        metadata['source_file'] = "Error"
        metadata['source_file_modified'] = ""

    try:
        var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if var_schema_path.exists():
            scheme_df = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
            scheme_df['web_display_prio'] = pd.to_numeric(scheme_df['web_display_prio'], errors='coerce')
            display_df = scheme_df.dropna(subset=['web_display_prio']).sort_values('web_display_prio')
            metadata['display_priority'] = display_df['variable_name'].tolist()

            if 'web_viz_prio' in scheme_df.columns:
                scheme_df['web_viz_prio'] = pd.to_numeric(scheme_df['web_viz_prio'], errors='coerce')
                viz_df = scheme_df.dropna(subset=['web_viz_prio']).sort_values('web_viz_prio')
                metadata['viz_priority'] = viz_df['variable_name'].tolist()
            else:
                 metadata['viz_priority'] = []
            
            if 'web_filter_prio' in scheme_df.columns:  
                scheme_df['web_filter_prio'] = pd.to_numeric(scheme_df['web_filter_prio'], errors='coerce')
                filter_df = scheme_df.dropna(subset=['web_filter_prio']).sort_values('web_filter_prio')
                metadata['filter_priority'] = filter_df['variable_name'].tolist()
            else:
                metadata['filter_priority'] = []

            if 'section' not in scheme_df.columns:
                scheme_df['section'] = 'General'
            if 'description' not in scheme_df.columns:
                scheme_df['description'] = ''
            
            scheme_df['section'] = scheme_df['section'].fillna('General')
            scheme_df['description'] = scheme_df['description'].fillna('')
            
            schema_map = {}
            for _, row in scheme_df.iterrows():
                var_name = row['variable_name']
                schema_map[var_name] = {
                    "section": str(row['section']),
                    "description": str(row['description'])
                }
            metadata['schema_map'] = schema_map
                
        else:
            metadata['display_priority'] = []
            metadata['filter_priority'] = []
            metadata['schema_map'] = {}
    except Exception as e:
        print(f"Error loading priority list: {e}")
        metadata['display_priority'] = []
        metadata['filter_priority'] = []
        metadata['schema_map'] = {}

    data_io.save_json(cf=fyp_cf, data=make_serializable(metadata), storage_location="cache", filename=f"{study}_explorer_metadata.json", verbose=True)

    return jsonify(make_serializable(metadata))


@data_bp.route('/api/explorer/filter', methods=['POST'])
@login_required
def api_explorer_filter():
    data = request.json or {}
    study = data.get("study")
    
    if not study:
         return jsonify({"error": "No study specified"}), 400
    print("tjolahopp")
    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    df = df[df.annotated_ok].copy()
    print(f"    Filtered to {len(df):,} annotated events")

    filters = data.get("filters", {})
    search_query = data.get("search_query")
    
    
    # Selective Calculation Logic
    trigger_slice = data.get("trigger_slice") # 1, 2, or None (both)

    # Optimization: Load cached metadata to potentially reuse total_stats
    cached_metadata = {}
    try:
        if data_io.exists(cf=fyp_cf, storage_location="cache", filename=f"{study}_explorer_metadata.json"):
            cached_metadata = data_io.load_json(cf=fyp_cf, storage_location="cache", filename=f"{study}_explorer_metadata.json")
    except Exception as e:
        print(f"    Warning: Could not load cached metadata: {e}")
    
    result = {}

    # --- SLICE 1 ---
    if trigger_slice is None or trigger_slice == 1:
        # Check if filters are empty and we have cached stats
        is_empty_filters = (not filters) and (not search_query)
        
        if is_empty_filters and 'total_stats' in cached_metadata:
            print("    Optimization: Using cached total_stats for S1")
            result['stats'] = cached_metadata['total_stats']
            result['count'] = len(df)
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
        
        # Optimization: If filters are identical to S1 and S1 was just calculated, reuse result
        # This handles the initial load case where both are empty/default
        is_identical = (filters == filters2) and (search_query == search_query2)
        s1_available = (trigger_slice is None or trigger_slice == 1) and 'stats' in result
        
        if is_identical and s1_available:
            print("    Optimization: S2 identical to S1, reusing stats")
            result['stats2'] = result['stats']
            result['count2'] = result['count']
        else:
            # Check if filters are empty (for S2 specific case if not identical/S1 not avail)
            is_empty_filters2 = (not filters2) and (not search_query2)
            
            if is_empty_filters2 and 'total_stats' in cached_metadata:
                 print("    Optimization: Using cached total_stats for S2")
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

    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    df = df[df.scraped_ok].copy()
    print(f"    Filtered to {len(df):,} scraped events")

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
    
    ids = filtered_df[id_col].astype(str).tolist()
    return jsonify({"ids": ids, "count": len(ids)})


@data_bp.route('/api/pca/metadata', methods=['POST'])
def api_pca_metadata():
    from fyp.recode_variables import get_factors_and_features_from_var_schema
    
    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    factors, _ = get_factors_and_features_from_var_schema(cf = fyp_cf, some_events_df = df, verbose = False)
    
    if not factors:
        raise Exception("No factors found in var_schema")

    factor_values = {}
    for f in factors:
        vals = df[f].dropna().unique().tolist()
        if len(vals) < 500: 
            factor_values[f] = sorted([str(v) for v in vals])

    interpretations = {}
    try:
        inter_path = f"{study}_COMP_INTERPRETATIONS.json"
        interpretations = data_io.load_json(fyp_cf, "exports", inter_path, verbose=False)
    except Exception as e:
        print(f"Error loading interpretations: {e}")

    return jsonify({
        "numeric_cols": sorted(numeric_cols),
        "factor_cols": sorted(factors),
        "factor_values": factor_values,
        "interpretations": interpretations
    })


@data_bp.route('/api/pca/data', methods=['POST'])
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
        if data_io.exists(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE):
            mtime = data_io.getmtime(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE)
            timestamp = datetime.fromtimestamp(mtime).strftime('%d %b %Y %H:%M')
            return jsonify({"exists": True, "timestamp": timestamp})
        return jsonify({"exists": False, "timestamp": None})


@data_bp.route('/api/persona_stats_cached', methods=['GET'])
def api_persona_stats_cached():
    try:
        if not data_io.exists(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE):
            return jsonify({"error": "No cached stats found. Click 'Recalculate Stats' to generate."}), 404
        
        print(f"Loading cached persona stats from {PERSONA_STATS_CACHE_FILE}...")
        stats_df = data_io.load_parquet(
            cf=fyp_cf,
            storage_location="ddp_main",
            filename=PERSONA_STATS_CACHE_FILE)
        
        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        for rec in records:
            for key, val in rec.items():
                rec[key] = make_serializable(val)
        
        return jsonify(records)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/persona_stats', methods=['POST'])
def api_persona_stats():
    try:
        print(f"Loading global DDP dataset...")
        events_df = data_io.load_parquet(
            cf=fyp_cf,
            storage_location="ddp_main",
            filename="all_participant_events.parquet"
        )
        
        if events_df is None or events_df.empty:
            return jsonify({"error": "No DDP events found"}), 404
            
        print(f"Calculating persona stats for {len(events_df)} events...")
        stats_df = calculate_all_donation_stats(events_df)
        
        try:
            if data_io.exists(fyp_cf, "ddp_main", "all_participant_metadata.parquet"):
                metadata_df = data_io.load_parquet(fyp_cf, "ddp_main", "all_participant_metadata.parquet")
                print(f"Loaded {len(metadata_df)} metadata records")
                stats_df = enrich_stats_with_metadata(fyp_cf, stats_df, metadata_df, tz_location_cache_filename=LOCATION_CACHE_FILE)
            else:
                print("Metadata file not found, skipping enrichment.")
        except Exception as e:
            print(f"Could not load metadata or enrich stats: {e}")
            import traceback
            traceback.print_exc()
        
        stats_df = stats_df.reset_index(drop=True)
        data_io.save_parquet(
            cf=fyp_cf,
            df=stats_df,
            storage_location="ddp_main",
            filename=PERSONA_STATS_CACHE_FILE
        )
        print(f"Saved persona stats cache")
        
        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        for rec in records:
            for key, val in rec.items():
                rec[key] = make_serializable(val)
        
        return jsonify(records)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@data_bp.route('/api/viewer/item/<study>/<item_id>', methods=['GET'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    df = df[df.scraped_ok].copy()
    print(f"    Filtered to {len(df):,} scraped events")

    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        else: return jsonify({"error": "ID column missing"}), 500

    row = df[df[id_col].astype(str) == str(item_id)]
    if row.empty:
        return jsonify({"error": "Item not found"}), 404
    
    record = row.iloc[0].replace({np.nan: None}).to_dict()
    return jsonify(record)


@data_bp.route('/api/video/<study>/<item_id>', methods=['GET'])
def api_video_stream(study, item_id):
    global fyp_cf
    if fyp_cf["data_io"]["bucket"] is None:
        fyp_cf = fyp.connect_to_google(fyp_cf)

    bucket = fyp_cf.get("data_io", {}).get("bucket")
    if not bucket:
        return "GCS Bucket not available. Check credentials or internet connection.", 503

    blob_name = f"{fyp_cf['paths']['gcs_media_prefix']}/{item_id}.mp4"
    blob = bucket.blob(blob_name)
    
    if not blob.exists():
         return f"Video {blob_name} not found", 404

    def generate():
        with blob.open("rb") as f:
            while chunk := f.read(4096 * 16): 
                yield chunk

    return Response(stream_with_context(generate()), mimetype="video/mp4")
