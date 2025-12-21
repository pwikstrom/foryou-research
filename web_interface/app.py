from flask import Flask, render_template, jsonify, request, Response, stream_with_context
import subprocess
import threading
import time
import os
import signal
import sys
import json
from datetime import datetime
from collections import deque
from pathlib import Path
import numpy as np
import pandas as pd





import logging
# Silence the noisy HTTP request logs from Flask/Werkzeug
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT)) # Ensure fyp module is importable
import fyp
import fyp.data_io as data_io
# Initialize configuration to access paths
fyp_cf = fyp.init_project(verbose=False)

DOWNLOADER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_downloader.py"
INGEST_SCRIPT = PROJECT_ROOT / "web_interface" / "run_ingest_ndjson.py"
ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_annotator.py"
MONITOR_SCRIPT = PROJECT_ROOT / "enrich_tiktok_data" / "monitor_scrape_folder_and_annotate.py"
CREATE_SUBSETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_subsets.py"
REGENERATE_DATASETS_SCRIPT = PROJECT_ROOT / "web_interface" / "run_regenerate_datasets.py"
CREATE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_create_event_log.py"
RECODE_EVENT_LOG_SCRIPT = PROJECT_ROOT / "web_interface" / "run_recode_event_log.py"
CALCULATE_PCA_SCRIPT = PROJECT_ROOT / "web_interface" / "run_calculate_pca.py"
CONFIG_FILE_STUDIES = PROJECT_ROOT / "config" / "studies.toml"
CONFIG_FILE_CORE = PROJECT_ROOT / "config" / "config.toml"
PROCESS_STATS_FILE = PROJECT_ROOT / "web_interface" / "process_stats.json"
PYTHON_EXEC = sys.executable

# --- Global State ---
# Store process handles and logs
processes = {
    "downloader": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "monitor": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "create_subsets": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "regenerate_datasets": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "create_event_log": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "recode_event_log": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "calculate_pca": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
}



process_stats = {}



# --- Explorer State ---
import explorer_backend as explorer
active_explorer_study = None # Store currently loaded study name
explorer_df = None
explorer_col_types = None
explorer_total_stats = None



def get_explorer_data(study):
    global explorer_df, explorer_col_types, explorer_total_stats, active_explorer_study
    
    # If requesting same study and already loaded, return it
    if study == active_explorer_study and explorer_df is not None:
        return explorer_df, explorer_col_types

    # Resolve path
    exports_dir = Path(fyp_cf["paths"]["exports"])
    dataset_path = exports_dir / f"{study}_RECODED{fyp_cf['misc']['file_format']}"
    print(dataset_path)
    if dataset_path.exists():
        #print(f"Loading Explorer Study '{study}' from {dataset_path}...")
        explorer_df, explorer_col_types = explorer.load_data(str(dataset_path))
        #print(f"Explorer Study '{study}' loaded. Computing total stats...")
        res = explorer.get_current_stats(explorer_df, explorer_col_types)
        explorer_total_stats = res['stats']
        active_explorer_study = study
        #print("Total stats computed.")
        return explorer_df, explorer_col_types
    else:
        print(f"Explorer Study dataset not found at {dataset_path}")
        return None, None



def load_process_stats():
    global process_stats
    if PROCESS_STATS_FILE.exists():
        try:
            with open(PROCESS_STATS_FILE, 'r') as f:
                process_stats = json.load(f)
        except Exception as e:
            print(f"Failed to load process stats: {e}")
            process_stats = {}
    else:
        process_stats = {}



def save_process_stats():
    try:
        with open(PROCESS_STATS_FILE, 'w') as f:
            json.dump(process_stats, f)
    except Exception as e:
        print(f"Failed to save process stats: {e}")



# Load stats on startup
load_process_stats()



def enqueue_output(out, queue, process_state):
    for line in iter(out.readline, b''):
        line_str = line.decode('utf-8')
        print(line_str, end='') # Mirror to console
        
        # Update last message for UI
        process_state["last_message"] = line_str.strip()
        
        if "::PROGRESS::" in line_str:
            try:
                _, json_str = line_str.split("::PROGRESS::", 1)
                data = json.loads(json_str.strip())
                process_state["progress"].update(data)
            except Exception:
                queue.append(line_str)
        elif "::DATA::" in line_str:
            try:
                _, json_str = line_str.split("::DATA::", 1)
                data = json.loads(json_str.strip())
                process_state["data"].update(data)
            except Exception:
                queue.append(line_str)
        else:
            queue.append(line_str)
    out.close()




def monitor_process_completion(name, proc):
    """Waits for process to finish and updates stats."""
    proc.wait()
    
    end_time = datetime.now()
    start_time_str = processes[name].get("start_time")
    duration = 0
    if start_time_str:
        start_time = datetime.fromisoformat(start_time_str)
        duration = (end_time - start_time).total_seconds()

    outcome = "Success" if proc.returncode == 0 else "Fail"
    study_name = processes[name].get("study_name")

    # Record stats
    process_stats[name] = {
        "last_success": end_time.isoformat() if outcome == "Success" else process_stats.get(name, {}).get("last_success"),
        "last_run_end_time": end_time.isoformat(),
        "last_run_duration": duration,
        "last_run_outcome": outcome,
        "last_run_study": study_name
    }
    save_process_stats()
    
    # Update global state to stopped
    processes[name]["status"] = "stopped"
    processes[name]["proc"] = None
    processes[name]["start_time"] = None
    # Keep study_name until next run? Or clear it? 
    # Logic in frontend might need it if we are checking active study.
    # But last_run_study in process_stats is the persistent record.
    # We can clear processes[name]["study_name"] here.
    processes[name]["study_name"] = None




def start_process(name, script_path, args=[], study_name=None):
    if processes[name]["proc"] is not None:
        if processes[name]["proc"].poll() is None:
            return False, "Process already running"
    
    env_vars = os.environ.copy()
    env_vars["WEB_INTERFACE"] = "true"
    
    if args and isinstance(args[-1], dict) and args[-1].get("testing"):
        env_vars["FYP_TESTING"] = "true"
        args.pop() # Remove the config dict from args if passed

    cmd = [PYTHON_EXEC, "-u", str(script_path)] + args

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr into stdout
            cwd=str(PROJECT_ROOT), # Run from project root
            env=env_vars
        )
        processes[name]["proc"] = proc
        processes[name]["status"] = "running"
        processes[name]["start_time"] = datetime.now().isoformat()
        processes[name]["study_name"] = study_name
        processes[name]["progress"] = {} # Reset progress
        processes[name]["last_message"] = "" # Reset last message
        
        # Start logging thread
        t = threading.Thread(target=enqueue_output, args=(proc.stdout, processes[name]["logs"], processes[name]))
        t.daemon = True
        t.start()

        # Start monitoring thread
        t_mon = threading.Thread(target=monitor_process_completion, args=(name, proc))
        t_mon.daemon = True
        t_mon.start()
        
        return True, "Started"
    except Exception as e:
        return False, str(e)




def stop_process(name):
    proc = processes[name]["proc"]
    if proc:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        processes[name]["proc"] = None
        processes[name]["status"] = "stopped"
        processes[name]["start_time"] = None
        return True, "Stopped"
    return False, "Not running"




# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html')





@app.route('/api/start/<name>', methods=['POST'])
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

    # Pass testing flag if present
    if data.get("testing"):
        args.append("--testing") # Or however the scripts handle it. 
        # Wait, the other scripts might expect it differently? 
        # downloader uses os.environ usually passed via env var, but previous code 
        # had: if data.get("testing"): args.append({"testing": True}) <- wait, args must be strings for subprocess.
        # Let's see how start_process handles args.
        # Actually start_process does: cmd = [PYTHON_EXEC, script_path] + [str(a) for a in args]
        # So passing a dict {"testing": True} would become string representation.
        # But wait, looking at my previous read of app.py (Step 248? No, 353 and earlier).
        # run_regenerate_datasets.py creates env var from CLI? No.
        # run_downloader.py handles it?
        # Let's look at `start_process` implementation if I can.
        
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
def api_stop(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    success, msg = stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})





@app.route('/api/status', methods=['GET'])
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
            "last_message": p_data.get("last_message", ""),
            "last_success": process_stats.get(name, {}).get("last_success"),
            "last_run_end_time": process_stats.get(name, {}).get("last_run_end_time"),
            "last_run_duration": process_stats.get(name, {}).get("last_run_duration"),
            "last_run_outcome": process_stats.get(name, {}).get("last_run_outcome"),
            "last_run_study": process_stats.get(name, {}).get("last_run_study")
        }
    return jsonify(status_data)




@app.route('/api/logs/clear/<name>', methods=['POST'])
def api_clear_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    processes[name]["logs"].clear()
    return jsonify({"status": "success"})




@app.route('/api/logs/<name>', methods=['GET'])
def api_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    # Return last N lines
    logs = list(processes[name]["logs"])
    return jsonify({"logs": "".join(logs)})




@app.route('/api/config', methods=['GET', 'POST'])
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
def api_explorer_studies():
    exports_dir = Path(fyp_cf["paths"]["exports"])
    if not exports_dir.exists():
        return jsonify([])
    
    studies = []
    for f in exports_dir.glob(f"*_RECODED{fyp_cf['misc']['file_format']}"):
        # Extract study name: filename is {study_name}_RECODED...
        study_name = f.name.replace(f"_RECODED{fyp_cf['misc']['file_format']}", "")
        studies.append(study_name)
    
    return jsonify(sorted(studies))


@app.route('/api/studies/defined', methods=['GET'])
def api_get_study_defs():
    """Return list of study keys defined in fyp_cf['study_defs']"""
    if 'study_defs' in fyp_cf:
        return jsonify(sorted(list(fyp_cf['study_defs'].keys())))
    return jsonify([])




@app.route('/api/explorer/metadata', methods=['GET'])
def api_explorer_metadata():
    study = request.args.get('study')
    if not study:
        return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study)
    
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    metadata = explorer.get_metadata(df, col_types)
    
    # Inject total stats so frontend knows baseline
    # Recompute total_stats dynamically to adhere to current viz_config
    viz_config = get_viz_config()
    res = explorer.get_current_stats(df, col_types, viz_config=viz_config)
    metadata['total_stats'] = res['stats']

    # Inject Source File Info
    try:
        exports_dir = Path(fyp_cf["paths"]["exports"])
        dataset_path = exports_dir / f"{study}_RECODED{fyp_cf['misc']['file_format']}"
        if dataset_path.exists():
            metadata['source_file'] = dataset_path.name
            mtime = datetime.fromtimestamp(dataset_path.stat().st_mtime)
            metadata['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
        else:
             metadata['source_file'] = "Unknown"
             metadata['source_file_modified'] = ""
    except Exception as e:
        print(f"Error getting file info: {e}")
        metadata['source_file'] = "Error"
        metadata['source_file_modified'] = ""

    # Inject priority lists from var_scheme.csv
    try:
        var_scheme_path = PROJECT_ROOT / "config" / "var_scheme.csv"
        if var_scheme_path.exists():
            scheme_df = pd.read_csv(var_scheme_path)
            
            # 1. Display Priority (Viewer Metadata Sort)
            # Filter rows with numeric web_display_prio
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
            # Create a dictionary for section and description
            # Ensure columns exist
            if 'section' not in scheme_df.columns:
                scheme_df['section'] = 'General'
            if 'description' not in scheme_df.columns:
                scheme_df['description'] = ''
            
            # Fill NaNs
            scheme_df['section'] = scheme_df['section'].fillna('General')
            scheme_df['description'] = scheme_df['description'].fillna('')
            
            # Create map: { var_name: { section: "...", description: "..." } }
            # Only for variables present in scheme
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

    return jsonify(metadata)


def get_viz_config():
    """
    Reads var_scheme.csv and returns a dictionary of visualization settings.
    {
        var_name: {
            "log": bool,
            "bins": int or list of edges or None
        }
    }
    """
    config = {}
    try:
        var_scheme_path = PROJECT_ROOT / "config" / "var_scheme.csv"
        if var_scheme_path.exists():
            df = pd.read_csv(var_scheme_path)
            
            # Check if columns exist
            has_log = 'web_viz_log' in df.columns
            has_bins = 'web_viz_bins' in df.columns
            
            if not has_log and not has_bins:
                return {}
                
            for _, row in df.iterrows():
                var = row['variable_name']
                cfg = {}
                
                # Log Setting
                if has_log:
                    val = str(row['web_viz_log']).lower().strip()
                    cfg['log'] = (val == 'yes')
                
                # Bin Setting
                if has_bins:
                    val = row['web_viz_bins']
                    if pd.notna(val):
                        val_str = str(val).strip()
                        if "|" in val_str:
                            # Parse custom edges: "10|30|50"
                            try:
                                edges = [float(x) for x in val_str.split("|")]
                                cfg['bins'] = sorted(edges)
                                
                            except:
                                cfg['bins'] = None
                        elif val_str.isdigit():
                             cfg['bins'] = int(val_str)
                        else:
                             cfg['bins'] = None
                    else:
                        cfg['bins'] = None
                
                if cfg:
                    config[var] = cfg
                    
    except Exception as e:
        print(f"Error reading viz config: {e}")
        
    return config


@app.route('/api/explorer/filter', methods=['POST'])
def api_explorer_filter():
    data = request.json or {}
    study = data.get("study")
    
    if not study:
         return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
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
        
        # If filters2 is empty and no search query, it equals the TOTAL dataset (unfiltered)
        # But we must run it through filter_dataframe to be safe (e.g. if we add global filters later)
        # Actually filter_dataframe handles empty filters by returning copy of df.
        
        filtered_df2 = explorer.filter_dataframe(df, col_types, filters2, search_query2)
        res2 = explorer.get_current_stats(filtered_df2, col_types, viz_config=viz_config)
        
        result['stats2'] = res2['stats']
        result['count2'] = res2['count']
    
    return jsonify(result)



@app.route('/api/viewer/ids', methods=['POST'])
def api_viewer_ids():
    data = request.json or {}
    study = data.get("study")
    
    if not study:
         return jsonify({"error": "No study specified"}), 400

    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    filters = data.get("filters", {})
    search_query = data.get("search_query")
    sort_by = data.get("sort_by")
    
    filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
    
    # Sort if requested
    if sort_by and sort_by in filtered_df.columns:
        # Determine sort direction
        # 1. Explicit request
        sort_order = data.get("sort_order") # 'asc' or 'desc'
        
        if sort_order:
            ascending = (sort_order == 'asc')
        else:
            # 2. Fallback based on type
            # numbers -> descending (highest first)
            # others -> ascending (A-Z)
            dtype = col_types.get(sort_by)
            ascending = True
            if dtype == 'number':
                ascending = False
            
        filtered_df = filtered_df.sort_values(by=sort_by, ascending=ascending)
    
    # Return list of item_ids. Assume column is 'item_id' or 'video_id'
    # Based on csv head: 'item_id'
    id_col = 'item_id'
    if id_col not in filtered_df.columns:
        # Fallback mechanisms?
        if 'video_id' in filtered_df.columns: id_col = 'video_id'
        elif 'G_id' in filtered_df.columns: id_col = 'G_id'
        else: return jsonify({"error": "No ID column found"}), 500
    
    # Convert to string to ensure consistency
    ids = filtered_df[id_col].astype(str).tolist()
    return jsonify({"ids": ids, "count": len(ids)})


# --- PCA Visualization Endpoints ---

# Cache logic for PCA data? Reuse get_explorer_data for efficiency if possible?
# But PCA data is a DIFFERENT file ({study}_PCA..).
# Let's add a separate cache or helper.

pca_df_cache = {}

def get_pca_df(study_name):
    global pca_df_cache
    if study_name in pca_df_cache:
        # Check freshness? Simple version: just return.
        return pca_df_cache[study_name]

    # Load file
    try:
        from os.path import join, exists
        import pandas as pd
        
        # Path logic reusing fyp.cf["paths"]["exports"]
        # But we need access to 'fyp_cf'
        exports_dir = fyp_cf["paths"]["exports"]
        pca_path = join(exports_dir, f"{study_name}_PCA{fyp_cf['misc']['file_format']}")
        
        if not exists(pca_path):
            return None
        
        df = data_io.load_dataset(pca_path)
        pca_df_cache[study_name] = df
        return df
    except Exception as e:
        print(f"Error loading PCA: {e}")
        return None


@app.route('/api/pca/metadata', methods=['POST'])
def api_pca_metadata():
    data = request.json or {}
    study = data.get("study")
    if not study: return jsonify({"error": "No study"}), 400

    df = get_pca_df(study)
    if df is None: return jsonify({"error": "PCA data not found"}), 404

    # Identify metadata
    # Numeric columns (for X/Y): float/int
    # Factors (for Color/Filter): defined in var_scheme where role='factor'/'group_factor'
    # BUT we need to check if they exist in the DF.
    
    # 1. Numeric
    numeric_cols = df.select_dtypes(include=['float', 'int']).columns.tolist()
    # Filter out boring ones? Keep all for flexibility.
    
    # 2. Factors from var_scheme
    # We can use 'fyp_cf' global to access var_scheme
    factors = []
    if "var_scheme" in fyp_cf:
        vs = fyp_cf["var_scheme"]
        # role is 'factor' or 'group_factor'
        target_roles = ['factor', 'group_factor']
        potential_factors = vs[vs['role'].isin(target_roles)]['variable_name'].tolist()
        
        # Intersect with df columns
        factors = [c for c in potential_factors if c in df.columns]
    
    # Fallback if var_scheme not loaded or matching
    if not factors:
        factors = df.select_dtypes(include=['object', 'category']).columns.tolist()

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
        from json import load as json_load
        from os.path import join, exists
        exports_dir = fyp_cf["paths"]["exports"]
        inter_path = join(exports_dir, f"{study}_COMP_INTERPRETATIONS.json")
        if exists(inter_path):
            with open(inter_path, 'r') as f:
                interpretations = json_load(f)
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

    # Prepare response
    # Limit points? 
    MAX_POINTS = 5000
    if len(filtered_df) > MAX_POINTS:
        filtered_df = filtered_df.sample(MAX_POINTS)

    # Need to handle NaN in X/Y
    filtered_df = filtered_df.dropna(subset=[x_col, y_col])
    
    # Construct output list
    # x, y, color, text (metadata tooltip)
    
    # For tooltip, maybe include ID and Color val
    # Assuming 'item_id' exists?
    
    result_data = []
    
    # Pre-fetch columns to numpy for speed?
    # Or just itertuples
    
    # Ensure color column exists, else use default
    has_color = color_col and color_col in filtered_df.columns
    
    for row in filtered_df.itertuples():
        # Get vals safely
        x_val = getattr(row, x_col)
        y_val = getattr(row, y_col)
        
        c_val = "Default"
        if has_color:
            c_val = str(getattr(row, color_col))
        
        # Tooltip text
        # Reuse 'item_id' if possible, else index?
        # But 'itertuples' handles index as Index?
        # Let's just put basic info
        txt = f"{color_col}: {c_val}"
        
        result_data.append({
            "x": x_val,
            "y": y_val,
            "color_val": c_val,
            "text": txt
        })

    return jsonify({"data": result_data})




@app.route('/api/viewer/item/<study>/<item_id>', methods=['GET'])
def api_viewer_item(study, item_id):
    df, col_types = get_explorer_data(study)
    if df is None:
        return jsonify({"error": "Dataset not found"}), 404
    
    # Find row
    # Assume 'item_id' column logic same as above
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
    if fyp_cf["media_storage"]["bucket"] is None:
        #print("Connecting to Google Cloud Storage for video streaming...")
        fyp_cf = fyp.connect_to_google(fyp_cf)

    # Get GCS bucket
    bucket = fyp_cf.get("media_storage", {}).get("bucket")
    if not bucket:
        return "GCS Bucket not available. Check credentials or internet connection.", 503

    # Attempt to find the file
    # Candidates: item_id.mp4, maybe in subfolders?
    # User said "bucket is initialized and ready to go". 
    # Usually files are at root or study/video? 
    # Let's assume root/{item_id}.mp4 based on "video associated with each row".
    
    blob_name = f"{item_id}.mp4"
    blob = bucket.blob(blob_name)
    
    if not blob.exists():
         # Try finding with list_blobs if needed? Too slow.
         # Maybe user meant the `video_uri` column?
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
    
    # Default to firefox downloads if not specified
    if not directory or not directory.strip():
        try:
            directory = fyp_cf["paths"]["firefox_downloads"]
        except KeyError:
            return jsonify({"error": "Default downloads path not configured."}), 500
            
    dir_path = Path(directory)
    if not dir_path.exists():
         return jsonify({"error": f"Directory not found: {directory}"}), 404
         
    # Use fyp.get_recent_files logic or simple glob
    # The user asked to use fyp.fyp_main.get_recent_files
    # get_recent_files(directory, suffix=None, how_recent=10)
    # We probably want ALL files, not just recent 10? 
    # User said "find the ndjson files in that folder. Use fyp.fyp_main.get_recent_files"
    # I will call it with a large how_recent or filter manually if needed. 
    # Actually get_recent_files returns list of dicts: {'filename': ..., 'modified': ...}
    
    try:
        files = fyp.get_recent_files(str(dir_path), suffix=".ndjson", how_recent=525600) # Get files from last year
        
        # Add full path to result
        result_files = []
        for f in files:
            result_files.append({
                "filename": f["filename"], # This is absolute path in get_recent_files return? No let's check.
                # Looking at fyp_main.py, get_recent_files:
                # files_path = os.path.join(directory, file)
                # 'filename': files_path 
                # So it returns absolute path in 'filename' key? 
                # Wait, step 6 output:
                # def get_recent_files(directory, suffix=None, how_recent=10):
                # ...
                # return [{'filename': os.path.join(directory, f), 'modified': ...}]
                # Yes, it returns absolute path.
                
                "path": f["filename"],
                "filename": Path(f["filename"]).name, # Just the name for display
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

    # Run the ingestion script as a subprocess to keep main process clean/safe
    # and reuse the script I wrote.
    
    try:
        # Pass input via stdin
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
             
        # Parse output
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
        # Use AppleScript to open a folder picker dialog
        # 'POSIX path of (choose folder ...)' returns the slash-formatted path
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select Folder containing .ndjson files")'],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            path = result.stdout.strip()
            return jsonify({"path": path})
        else:
            # User likely cancelled
            return jsonify({"error": "Selection cancelled"}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route('/api/upload_ndjson', methods=['POST'])
def api_upload_ndjson():
    try:
        from werkzeug.utils import secure_filename
        import os
        
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
            
        if file and file.filename.endswith('.ndjson'):
            filename = secure_filename(file.filename)
            # Use /tmp or a specific upload folder
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


@app.route('/api/study_files/<study_name>', methods=['GET'])
def api_get_study_files(study_name):
    try:
        files_info = data_io.get_study_export_files(fyp_cf, study_name)
        return jsonify(files_info)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        print(f"Error getting study files: {e}")
        return jsonify({"error": "Failed to retrieve study files"}), 500


@app.route('/api/check_datasets/<study_name>', methods=['GET'])
def api_check_datasets(study_name):
    try:
        details = data_io.get_dataset_details(fyp_cf, study_name)
        return jsonify(details)
    except Exception as e:
        print(f"Error checking datasets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/check_video_counts/<study_name>', methods=['GET'])
def api_check_video_counts(study_name):
    try:
        counts = fyp.generate_and_check_unique_videos_for_scrape_and_annotate(fyp_cf, study_name)
        # Returns dict: {"annotate": (rows, cols), "scrape": (rows, cols)}
        return jsonify(counts)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        print(f"Error checking video counts: {e}")
        return jsonify({"error": str(e)}), 500





if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5003)
