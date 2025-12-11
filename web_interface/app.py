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





app = Flask(__name__)

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT)) # Ensure fyp module is importable
import fyp
# Initialize configuration to access paths
fyp_cf = fyp.init_project(verbose=False)

DOWNLOADER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_downloader.py"
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
    "downloader": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "monitor": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "create_subsets": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "regenerate_datasets": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "create_event_log": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "recode_event_log": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None},
    "calculate_pca": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None}
}

process_stats = {}

# --- Explorer State ---
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
    pkl_path = exports_dir / f"{study}_RECODED.pkl"
    
    if pkl_path.exists():
        print(f"Loading Explorer Study '{study}' from {pkl_path}...")
        explorer_df, explorer_col_types = explorer.load_data(str(pkl_path))
        print(f"Explorer Study '{study}' loaded. Computing total stats...")
        res = explorer.get_current_stats(explorer_df, explorer_col_types)
        explorer_total_stats = res['stats']
        active_explorer_study = study
        print("Total stats computed.")
        return explorer_df, explorer_col_types
    else:
        print(f"Explorer Study pickle not found at {pkl_path}")
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

def enqueue_output(out, queue, progress_state, data_state):
    for line in iter(out.readline, b''):
        line_str = line.decode('utf-8')
        print(line_str, end='') # Mirror to console
        if "::PROGRESS::" in line_str:
            try:
                _, json_str = line_str.split("::PROGRESS::", 1)
                data = json.loads(json_str.strip())
                progress_state.update(data)
            except Exception:
                queue.append(line_str)
        elif "::DATA::" in line_str:
            try:
                _, json_str = line_str.split("::DATA::", 1)
                data = json.loads(json_str.strip())
                data_state.update(data)
            except Exception:
                queue.append(line_str)
        else:
            queue.append(line_str)
    out.close()


def monitor_process_completion(name, proc):
    """Waits for process to finish and updates stats."""
    proc.wait()
    # Process finished
    if proc.returncode == 0:
        # Success
        process_stats[name] = {
            "last_success": datetime.now().isoformat()
        }
        save_process_stats()
    
    # Update global state to stopped
    processes[name]["status"] = "stopped"
    processes[name]["proc"] = None
    processes[name]["start_time"] = None


def start_process(name, script_path, args=[]):
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
        processes[name]["progress"] = {} # Reset progress
        
        # Start logging thread
        t = threading.Thread(target=enqueue_output, args=(proc.stdout, processes[name]["logs"], processes[name]["progress"], processes[name]["data"]))
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
        
    study_name = data.get("study_name") # Get study_name once if needed

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
    
    success, msg = start_process(name, script_map[name], args)
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
            "last_success": process_stats.get(name, {}).get("last_success")
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
    for f in exports_dir.glob("*_RECODED.pkl"):
        # Extract study name: filename is {study_name}_RECODED.pkl
        study_name = f.name.replace("_RECODED.pkl", "")
        studies.append(study_name)
    
    return jsonify(sorted(studies))


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
    metadata['total_stats'] = explorer_total_stats

    # Inject priority list from var_scheme.csv
    try:
        var_scheme_path = PROJECT_ROOT / "config" / "var_scheme.csv"
        if var_scheme_path.exists():
            scheme_df = pd.read_csv(var_scheme_path)
            # Filter rows with numeric web_display_prio
            # Ensure it's numeric, drop NaNs
            scheme_df['web_display_prio'] = pd.to_numeric(scheme_df['web_display_prio'], errors='coerce')
            sorted_vars = scheme_df.dropna(subset=['web_display_prio']).sort_values('web_display_prio')['variable_name'].tolist()
            metadata['priority_list'] = sorted_vars
        else:
            metadata['priority_list'] = []
    except Exception as e:
        print(f"Error loading priority list: {e}")
        metadata['priority_list'] = []

    return jsonify(metadata)


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
    
    filtered_df = explorer.filter_dataframe(df, col_types, filters, search_query)
    result = explorer.get_current_stats(filtered_df, col_types)
    
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
        # Determine sort direction based on type
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
    # Get GCS bucket
    bucket = fyp_cf.get("media_storage", {}).get("bucket")
    if not bucket:
        return "GCS Bucket not available", 503

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


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)
