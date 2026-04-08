import subprocess
import threading
import json
import os
from pathlib import Path
from datetime import datetime
from collections import deque
from fyp.fyp_config import PROCESS_STATS_FILE, PROJECT_ROOT, PYTHON_EXEC


GRACEFUL_STOP_DIR = PROJECT_ROOT / "tmp" / "graceful_stop"


# --- Global State ---
# Store process handles and logs
processes = {
    "queue_scraper": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "queue_annotator": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "meta_refresh_viewer": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "meta_refresh_groups": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "timelines_refresh": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None},
    "recode_refresh_studies": {"proc": None, "logs": deque(maxlen=1000), "status": "stopped", "progress": {}, "data": {}, "start_time": None, "last_message": "", "study_name": None}
}

process_stats = {}



#TODO: change this to use data_io. It won't work on Cloud Run
def load_process_stats():
    global process_stats
    if PROCESS_STATS_FILE.exists():
        try:
            with open(PROCESS_STATS_FILE, 'r') as f:
                loaded = json.load(f)
            process_stats.clear()
            process_stats.update(loaded)
        except Exception as e:
            print(f"Failed to load process stats: {e}")
            process_stats.clear()
    else:
        process_stats.clear()




#TODO: change this to use data_io. It won't work on Cloud Run
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
    _clear_graceful_stop(name)
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
        _clear_graceful_stop(name)
        return True, "Stopped"
    # Process handle already cleared (e.g. by monitor thread) — clean up state
    if processes[name]["status"] in ("running", "stopping"):
        processes[name]["status"] = "stopped"
        processes[name]["start_time"] = None
        _clear_graceful_stop(name)
        return True, "Stopped"
    return False, "Not running"


def graceful_stop_process(name: str) -> tuple[bool, str]:
    """Signal a process to stop after finishing its current batch."""
    proc = processes[name]["proc"]
    if proc and proc.poll() is None:
        GRACEFUL_STOP_DIR.mkdir(parents=True, exist_ok=True)
        sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
        sentinel.touch()
        processes[name]["status"] = "stopping"
        return True, "Graceful stop requested"
    return False, "Not running"


def _clear_graceful_stop(name: str) -> None:
    """Remove the graceful stop sentinel file for a process."""
    sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
    if sentinel.exists():
        sentinel.unlink(missing_ok=True)


def check_graceful_stop(name: str) -> bool:
    """Check if a graceful stop has been requested for a process. Called by worker scripts."""
    sentinel = GRACEFUL_STOP_DIR / f"{name}.stop"
    return sentinel.exists()
