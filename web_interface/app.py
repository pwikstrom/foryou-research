from flask import Flask, render_template, jsonify, request
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

app = Flask(__name__)

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
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




if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)
