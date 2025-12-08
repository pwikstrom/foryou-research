from flask import Flask, render_template, jsonify, request
import subprocess
import threading
import time
import os
import signal
import sys
from collections import deque
from pathlib import Path

app = Flask(__name__)

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADER_SCRIPT = PROJECT_ROOT / "web_interface" / "run_downloader.py"
ANNOTATOR_SCRIPT = PROJECT_ROOT / "web_interface" / "run_annotator.py"
MONITOR_SCRIPT = PROJECT_ROOT / "enrich_tiktok_data" / "monitor_scrape_folder_and_annotate.py"
CONFIG_FILE_STUDIES = PROJECT_ROOT / "config" / "studies.toml"
CONFIG_FILE_CORE = PROJECT_ROOT / "config" / "config.toml"
PYTHON_EXEC = sys.executable

# --- Global State ---
# Store process handles and logs
processes = {
    "downloader": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}},
    "monitor": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}},
    "annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}}
}

import json

def enqueue_output(out, queue, progress_state):
    for line in iter(out.readline, b''):
        line_str = line.decode('utf-8')
        if "::PROGRESS::" in line_str:
            try:
                _, json_str = line_str.split("::PROGRESS::", 1)
                data = json.loads(json_str.strip())
                progress_state.update(data)
            except Exception:
                queue.append(line_str)
        else:
            queue.append(line_str)
    out.close()

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
        
        # Start logging thread
        t = threading.Thread(target=enqueue_output, args=(proc.stdout, processes[name]["logs"], processes[name]["progress"]))
        t.daemon = True
        t.start()
        
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
    
    if name == "downloader" and "study_name" in data:
        args.append(data["study_name"])
    
    if name == "annotator" and "study_name" in data:
        args.append(data["study_name"])

    # Pass testing configuration as a dict to start_process
    if data.get("testing"):
        args.append({"testing": True})
        
    script_map = {
        "downloader": DOWNLOADER_SCRIPT,
        "monitor": MONITOR_SCRIPT,
        "annotator": ANNOTATOR_SCRIPT
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
        state = "stopped"
        if p_data["proc"]:
            if p_data["proc"].poll() is None:
                state = "running"
            else:
                state = "stopped" # terminated recently
                # Clean up if it died on its own
                if p_data["status"] == "running": 
                     p_data["status"] = "stopped"
                     p_data["proc"] = None
        
        status_data[name] = {
            "state": state,
            "progress": p_data["progress"]
        }
    return jsonify(status_data)

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
