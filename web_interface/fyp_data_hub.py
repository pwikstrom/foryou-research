from flask import Flask, render_template, jsonify, request, Response, stream_with_context, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import os
import sys
import json
from datetime import datetime
import numpy as np
import pandas as pd
import logging

# Silence the noisy HTTP request logs from Flask/Werkzeug
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- Script Execution Support ---
# Allow running via `python web_interface/fyp_data_hub.py` by setting package context
import sys
from pathlib import Path
if __name__ == "__main__" and __package__ is None:
    file_path = Path(__file__).resolve()
    project_root = file_path.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    __package__ = "web_interface"


# --- Imports from new modules ---
from .hub_config import (
    PROJECT_ROOT, 
    PYTHON_EXEC, 
    fyp_cf,
    DOWNLOADER_SCRIPT, 
    INGEST_SCRIPT, 
    ANNOTATOR_SCRIPT, 
    MONITOR_SCRIPT, 
    CREATE_SUBSETS_SCRIPT, 
    REGENERATE_DATASETS_SCRIPT, 
    CREATE_EVENT_LOG_SCRIPT, 
    RECODE_EVENT_LOG_SCRIPT, 
    CALCULATE_PCA_SCRIPT,
    CONFIG_FILE_STUDIES,
    CONFIG_FILE_CORE
)

from .process_manager import (
    processes, 
    process_stats, 
    load_process_stats, 
    save_process_stats, 
    start_process, 
    stop_process
)

from .data_service import (
    study_cache, 
    get_explorer_data, 
    get_pca_df, 
    get_viz_config, 
    make_serializable as _make_serializable
)

# Initialize stats
load_process_stats()

import fyp
import web_interface.auth as auth
from . import explorer_backend as explorer
import fyp.data_io as data_io
from fyp.calc_donation_stats import calculate_all_donation_stats, enrich_stats_with_metadata

# --- Custom JSON Provider for Numpy/Pandas ---
from flask.json.provider import DefaultJSONProvider

class CustomJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, (np.integer, int)):
            return int(obj)
        if isinstance(obj, (np.floating, float)):
            # Check for NaN/Inf
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        if pd.isna(obj): # Handles pd.NA, np.nan, pd.NaT
            return None
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
        
        return super().default(obj)



app.json = CustomJSONProvider(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")




# --- Auth Setup ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'



# Initialize User Manager
USERS_FILE = PROJECT_ROOT / "config" / "users.json"
user_manager = auth.UserManager(USERS_FILE)



@login_manager.user_loader
def load_user(user_id):
    return user_manager.get_user(user_id)



LOCATION_CACHE_FILE = 'location_timezone_cache.json' # sits in 'ddp_main'
PERSONA_STATS_CACHE_FILE = 'persona_stats_cache.parquet' # sits in 'ddp_main'



# --- Auth Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Check if user exists first to distinguish between "Wrong password" and "Not approved"
        user_obj = user_manager.get_user(username)
        
        if user_obj:
            if not user_obj.approved:
                flash('Your account is pending approval from an administrator.')
            elif auth.verify_password(user_obj.password_hash, password):
                login_user(user_obj)
                next_page = request.args.get('next')
                return redirect(next_page or url_for('index'))
            else:
                flash('Invalid username or password')
        else:
            flash('Invalid username or password')
    
    return render_template('login.html')





@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
         return redirect(url_for('index'))
         
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash("Passwords do not match")
            return render_template('signup.html')
            
        success, msg = user_manager.add_user(username, password, auth.ROLE_VIEWER, approved=False)
        if success:
            flash("Account created! Please wait for an administrator to approve your account.")
            return redirect(url_for('login'))
        else:
            flash(msg)
            
    return render_template('signup.html')





@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))





@app.route('/api/admin/users', methods=['GET', 'POST', 'PUT', 'DELETE'])
@auth.admin_required
def api_admin_users():
    if request.method == 'GET':
        # Return list of users (excluding password hashes)
        users_list = []
        for u in user_manager.users.values():
            ud = u.to_dict()
            del ud['password_hash']
            users_list.append(ud)
        return jsonify(users_list)
        
    elif request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        role = data.get('role', auth.ROLE_VIEWER)
        
        if not username or not password:
            return jsonify({"error": "Missing username or password"}), 400
            
        success, msg = user_manager.add_user(username, password, role, approved=True)
        if success:
            return jsonify({"status": "success", "message": msg})
        else:
            return jsonify({"error": msg}), 400

    elif request.method == 'PUT':
        data = request.json
        action = data.get('action')
        username = data.get('username')
        
        if not username:
             return jsonify({"error": "Missing username"}), 400
             
        if action == 'approve':
             success, msg = user_manager.approve_user(username)
             if success: return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400
             
        elif action == 'reset_password':
             new_password = data.get('new_password')
             if not new_password: return jsonify({"error": "Missing new password"}), 400
             
             success, msg = user_manager.update_password(username, new_password)
             if success: return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400
             
        elif action == 'change_role':
             new_role = data.get('role')
             success, msg = user_manager.update_user_role(username, new_role)
             if success: return jsonify({"status": "success", "message": msg})
             else: return jsonify({"error": msg}), 400

        return jsonify({"error": "Invalid action"}), 400

    elif request.method == 'DELETE':
        username = request.args.get('username')
        if not username:
             return jsonify({"error": "Missing username"}), 400
             
        success, msg = user_manager.delete_user(username)
        if success:
             return jsonify({"status": "success", "message": msg})
        else:
             return jsonify({"error": msg}), 400


# --- Routes ---

@app.route('/')
@login_required
def index():
    return render_template('index.html', user=current_user)


@app.route('/api/start/<name>', methods=['POST'])
@auth.researcher_required
def api_start(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    data = request.json or {}
    args = []
    
    if "study_name" in data:
        args.append(data["study_name"])

    # Pass batch controls for downloader and annotator
    if name in ["downloader", "annotator"]:
        if data.get("batch_size") and data["batch_size"].strip():
             args.extend(["--batch-size", str(data["batch_size"])])
        if data.get("max_batches") and data["max_batches"].strip():
             args.extend(["--max-batches", str(data["max_batches"])])

    study_name = data.get("study_name") 

    script_map = {
        "downloader": DOWNLOADER_SCRIPT,
        "monitor": MONITOR_SCRIPT,
        "annotator": ANNOTATOR_SCRIPT,
        "create_subsets": CREATE_SUBSETS_SCRIPT,
        "regenerate_datasets": REGENERATE_DATASETS_SCRIPT,
        "create_event_log": CREATE_EVENT_LOG_SCRIPT,
        "recode_event_log": RECODE_EVENT_LOG_SCRIPT,
        "calculate_pca": CALCULATE_PCA_SCRIPT
    }
    
    success, msg = start_process(name, script_map[name], args, study_name=study_name)
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409


@app.route('/api/stop/<name>', methods=['POST'])
@auth.researcher_required
def api_stop(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    success, msg = stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})


@app.route('/api/status', methods=['GET'])
@login_required
def api_status():
    status_data = {}
    for name, p_data in processes.items():
        state = p_data["status"]
        if p_data["proc"]:
            if p_data["proc"].poll() is not None:
                # This should be handled by monitor_process_completion, but just in case
                if state == "running":
                    state = "stopped"
        
        status_data[name] = {
            "state": state,
            "progress": p_data["progress"],
            "data": p_data["data"],
            "start_time": p_data["start_time"],
            "last_message": p_data.get("last_message", ""),
            "last_success": process_stats.get(name, {}).get("last_success"),
            "last_run_end_time": process_stats.get(name, {}).get("last_run_end_time"),
            "last_run_duration": process_stats.get(name, {}).get("last_run_duration"),
            "last_run_outcome": process_stats.get(name, {}).get("last_run_outcome"),
            "last_run_study": process_stats.get(name, {}).get("last_run_study")
        }
    return jsonify(status_data)


@app.route('/api/logs/clear/<name>', methods=['POST'])
@auth.researcher_required
def api_clear_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    processes[name]["logs"].clear()
    return jsonify({"status": "success"})


@app.route('/api/logs/<name>', methods=['GET'])
@login_required
def api_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    # Return last N lines
    logs = list(processes[name]["logs"])
    return jsonify({"logs": "".join(logs)})


@app.route('/api/config', methods=['GET', 'POST'])
@auth.admin_required
def api_config():
    filename = request.args.get('file', 'studies.toml')
    target_file = CONFIG_FILE_STUDIES if filename == 'studies.toml' else CONFIG_FILE_CORE
    
    if request.method == 'GET':
        if target_file.exists():
            with open(target_file, 'r') as f:
                content = f.read()
            return jsonify({"content": content})
        return jsonify({"content": ""})
    
    elif request.method == 'POST':
        content = request.json.get('content')
        if content is None:
            return jsonify({"error": "No content provided"}), 400
        
        try:
            with open(target_file, 'w') as f:
                f.write(content)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/explorer/studies', methods=['GET'])
@login_required
def api_explorer_studies():
    from os import listdir as os_listdir
    """
    Look for precomputed recoded files in temp folder and extract list of study names.
    """
    studies = []
    # Using fyp_cf from hub_config
    if os.path.exists(fyp_cf['paths']['temp']):
        recoded_files = [fn for fn in os_listdir(fyp_cf['paths']['temp']) if fn.endswith("_recoded.parquet")]
        for fn in recoded_files:
            # Extract study name: filename is {study_name}_recoded...
            study_name = fn.replace("_recoded.parquet", "")
            studies.append(study_name)
    
    return jsonify(sorted(studies))


@app.route('/api/studies/defined', methods=['GET'])
@login_required
def api_get_study_defs():
    """Return list of study keys defined in fyp_cf['study_defs']"""
    if 'study_defs' in fyp_cf:
        return jsonify(sorted(list(fyp_cf['study_defs'].keys())))
    return jsonify([])


@app.route('/api/explorer/metadata', methods=['GET'])
@login_required
def api_explorer_metadata():
    from os.path import exists as os_exists, getmtime as os_getmtime, join as os_join

    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study)
    
    # I only want the events where the has been downloaded and annotated
    # Otherwise there is no data to explore!
    
    context = request.args.get('context', 'explorer')
    
    if context == 'viewer':
         df = df[df.scraped_ok].copy()
         print(f"    Filtered to {len(df):,} scraped events")
    else:
         df = df[df.annotated_ok].copy()
         print(f"    Filtered to {len(df):,} annotated events")

    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    metadata = explorer.get_metadata(df, col_types)
    
    # Inject total stats so frontend knows baseline
    viz_config = get_viz_config()
    res = explorer.get_current_stats(df, col_types, viz_config=viz_config)
    metadata['total_stats'] = res['stats']

    # Inject Source File Info
    try:
        the_recoded_file = f"CACHE_{study}_recoded.parquet"
        if os_exists(os_join(fyp_cf['paths']['temp'], the_recoded_file)):
            metadata['source_file'] = the_recoded_file
            mtime = datetime.fromtimestamp(os_getmtime(os_join(fyp_cf['paths']['temp'], the_recoded_file)))
            metadata['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
        else:
             metadata['source_file'] = "Unknown"
             metadata['source_file_modified'] = ""
    except Exception as e:
        print(f"Error getting file info: {e}")
        metadata['source_file'] = "Error"
        metadata['source_file_modified'] = ""

    # Inject priority lists from var_schema.csv
    try:
        var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if var_schema_path.exists():
            scheme_df = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
            # 1. Display Priority (Viewer Metadata Sort)
            scheme_df['web_display_prio'] = pd.to_numeric(scheme_df['web_display_prio'], errors='coerce')
            display_df = scheme_df.dropna(subset=['web_display_prio']).sort_values('web_display_prio')
            metadata['display_priority'] = display_df['variable_name'].tolist()

            # 1b. Visualization Priority (Explorer Plots)
            if 'web_viz_prio' in scheme_df.columns:
                scheme_df['web_viz_prio'] = pd.to_numeric(scheme_df['web_viz_prio'], errors='coerce')
                viz_df = scheme_df.dropna(subset=['web_viz_prio']).sort_values('web_viz_prio')
                metadata['viz_priority'] = viz_df['variable_name'].tolist()
            else:
                 metadata['viz_priority'] = []
            
            # 2. Filter Priority (Explorer & Viewer Filters)
            if 'web_filter_prio' in scheme_df.columns:  
                scheme_df['web_filter_prio'] = pd.to_numeric(scheme_df['web_filter_prio'], errors='coerce')
                filter_df = scheme_df.dropna(subset=['web_filter_prio']).sort_values('web_filter_prio')
                metadata['filter_priority'] = filter_df['variable_name'].tolist()
            else:
                metadata['filter_priority'] = []

            # 3. Schema Map (Section & Description)
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

    return jsonify(_make_serializable(metadata))


@app.route('/api/explorer/filter', methods=['POST'])
@login_required
def api_explorer_filter():
    data = request.json or {}
    study = data.get("study")
    
    if not study:
         return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404

    df = df[df.annotated_ok].copy()
    print(f"    Filtered to {len(df):,} annotated events")

    filters = data.get("filters", {})
    search_query = data.get("search_query")
    
    # Slice 1 Processing
    filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
    
    # Load Viz Config
    viz_config = get_viz_config()
    
    result = explorer.get_current_stats(filtered_df, col_types, viz_config=viz_config)
    
    # Slice 2 Processing (Optional)
    if "filters2" in data:
        filters2 = data.get("filters2", {})
        search_query2 = data.get("search_query2")
        
        filtered_df2 = explorer.filter_dataframe(df, col_types, filters2, search_query2)
        res2 = explorer.get_current_stats(filtered_df2, col_types, viz_config=viz_config)
        
        result['stats2'] = res2['stats']
        result['count2'] = res2['count']
    
    return jsonify(_make_serializable(result))


@app.route('/api/viewer/ids', methods=['POST'])
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
    
    # Sort if requested
    if sort_by and sort_by in filtered_df.columns:
        sort_order = data.get("sort_order") # 'asc' or 'desc'
        
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


@app.route('/api/pca/metadata', methods=['POST'])
def api_pca_metadata():
    from fyp.recode_variables import get_factors_and_features_from_var_schema
    
    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    # 1. Numeric
    numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
    
    # 2. Factors from var_schema
    factors, _ = get_factors_and_features_from_var_schema(cf = fyp_cf, some_events_df = df, verbose = False)
    
    if not factors:
        raise Exception("No factors found in var_schema")

    # Get unique values for factors (for filters)
    factor_values = {}
    for f in factors:
        # Cap at certain number to avoid huge lists
        vals = df[f].dropna().unique().tolist()
        if len(vals) < 500: # Reasonable limit?
            factor_values[f] = sorted([str(v) for v in vals])

    # Load Interpretations
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


@app.route('/api/pca/data', methods=['POST'])
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

    # Filter
    mask = pd.Series(True, index=df.index)
    for col, vals in filters.items():
        if col in df.columns:
            # vals is list of allowed strings
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


@app.route('/api/persona_stats_info', methods=['GET'])
def api_persona_stats_info():
    """Get info about cached stats file (existence and timestamp)."""
    if True:#try:
        if data_io.exists(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE):
            mtime = data_io.getmtime(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE)
            timestamp = datetime.fromtimestamp(mtime).strftime('%d %b %Y %H:%M')
            return jsonify({"exists": True, "timestamp": timestamp})
        else:
            return jsonify({"exists": False, "timestamp": None})
    if False:#except Exception as e:
        return jsonify({"exists": False, "timestamp": None, "error": str(e)})


@app.route('/api/persona_stats_cached', methods=['GET'])
def api_persona_stats_cached():
    """Load pre-calculated stats from cache file."""
    try:
        if not data_io.exists(fyp_cf, "ddp_main", PERSONA_STATS_CACHE_FILE):
            return jsonify({"error": "No cached stats found. Click 'Recalculate Stats' to generate."}), 404
        
        print(f"Loading cached persona stats from {PERSONA_STATS_CACHE_FILE}...")
        stats_df = data_io.load_parquet(
            cf=fyp_cf,
            storage_location="ddp_main",
            filename=PERSONA_STATS_CACHE_FILE)
        
        # Convert to JSON-safe records
        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        for rec in records:
            for key, val in rec.items():
                rec[key] = _make_serializable(val)
        
        return jsonify(records)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/persona_stats', methods=['POST'])
def api_persona_stats():
    """Recalculate all persona stats and save to cache file."""
    
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
        
        # Convert to JSON-safe records
        records = stats_df.replace({np.nan: None}).to_dict(orient='records')
        for rec in records:
            for key, val in rec.items():
                rec[key] = _make_serializable(val)
        
        return jsonify(records)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/viewer/item/<study>/<item_id>', methods=['GET'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    df = df[df.scraped_ok].copy()
    print(f"    Filtered to {len(df):,} scraped events")

    # Find row
    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        else: return jsonify({"error": "ID column missing"}), 500

    row = df[df[id_col].astype(str) == str(item_id)]
    if row.empty:
        return jsonify({"error": "Item not found"}), 404
    
    # Convert row to dict. Handle NaNs
    record = row.iloc[0].replace({np.nan: None}).to_dict()
    return jsonify(record)


@app.route('/api/video/<study>/<item_id>', methods=['GET'])
def api_video_stream(study, item_id):
    global fyp_cf
    
    # Lazy init of GCS bucket if not already connected
    if fyp_cf["data_io"]["bucket"] is None:
        #print("Connecting to Google Cloud Storage for video streaming...")
        fyp_cf = fyp.connect_to_google(fyp_cf)

    # Get GCS bucket
    bucket = fyp_cf.get("data_io", {}).get("bucket")
    if not bucket:
        return "GCS Bucket not available. Check credentials or internet connection.", 503

    blob_name = f"{fyp_cf['paths']['gcs_media_prefix']}/{item_id}.mp4"
    blob = bucket.blob(blob_name)
    
    if not blob.exists():
         return f"Video {blob_name} not found", 404

    # Stream the blob
    def generate():
        with blob.open("rb") as f:
            while chunk := f.read(4096 * 16): # 64KB chunks
                yield chunk

    return Response(stream_with_context(generate()), mimetype="video/mp4")


@app.route('/api/find_ndjson', methods=['POST'])
def api_find_ndjson():
    data = request.json or {}
    directory = data.get('directory')
    
    if not directory or not directory.strip():
        try:
            directory = fyp_cf["paths"]["firefox_downloads"]
        except KeyError:
            return jsonify({"error": "Default downloads path not configured."}), 500
            
    dir_path = Path(directory)
    if not dir_path.exists():
         return jsonify({"error": f"Directory not found: {directory}"}), 404
         
    try:
        files = fyp.get_recent_files(fyp_cf, directory, suffix=".ndjson", how_recent=525600) 
        
        result_files = []
        for f in files:
            result_files.append({
                "filename": f["filename"], 
                "path": f["filename"],
                "filename": Path(f["filename"]).name, 
                "modified": f["mtime"].strftime('%Y-%m-%d %H:%M:%S')
            })
            
        return jsonify({"directory": str(dir_path), "files": result_files})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ingest_ndjson', methods=['POST'])
def api_ingest_ndjson():
    data = request.json or {}
    files = data.get('files', [])
    label = data.get('label')
    
    if not files:
        return jsonify({"error": "No files specified"}), 400
    if not label:
        return jsonify({"error": "No label provided"}), 400

    try:
        cmd = [PYTHON_EXEC, str(INGEST_SCRIPT)]
        
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT)
        )
        
        input_str = json.dumps({"files": files, "label": label})
        stdout, stderr = proc.communicate(input=input_str.encode('utf-8'))
        
        if proc.returncode != 0:
             return jsonify({
                 "status": "error", 
                 "message": "Script failed", 
                 "log": stderr.decode('utf-8') + "\n" + stdout.decode('utf-8')
             })
             
        try:
            output_json = json.loads(stdout.decode('utf-8'))
            return jsonify(output_json)
        except json.JSONDecodeError:
             return jsonify({
                 "status": "error", 
                 "message": "Invalid script output", 
                 "log": stdout.decode('utf-8')
             })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/browse_folder', methods=['POST'])
def api_browse_folder():
    try:
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select Folder containing .ndjson files")'],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            path = result.stdout.strip()
            return jsonify({"path": path})
        else:
            return jsonify({"error": "Selection cancelled"}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/upload_ndjson', methods=['POST'])
def api_upload_ndjson():
    try:
        from werkzeug.utils import secure_filename
        
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
            
        if file and file.filename.endswith('.ndjson'):
            filename = secure_filename(file.filename)
            upload_dir = Path("/tmp/fyp_uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            
            save_path = upload_dir / filename
            file.save(str(save_path))
            
            return jsonify({
                "status": "success",
                "path": str(save_path),
                "filename": filename,
                "modified": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })
            
        else:
            return jsonify({"error": "Invalid file type. Only .ndjson allowed."}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Initialize process stats on start (already happening at module level now)
    app.run(host='0.0.0.0', port=5002, debug=True)
