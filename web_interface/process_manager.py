import subprocess
import threading
import json
import os
from datetime import datetime
from collections import deque
from .fyp_config import PROCESS_STATS_FILE, PROJECT_ROOT, PYTHON_EXEC


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
    "calculate_pca": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "queue_scraper": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
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
    processes[name]["study_name"] = None

def start_process(name, script_path, args=[], study_name=None):
    if processes[name]["proc"] is not None:
        if processes[name]["proc"].poll() is None:
            return False, "Process already running"
    
    env_vars = os.environ.copy()
    env_vars["WEB_INTERFACE"] = "true"
    
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
