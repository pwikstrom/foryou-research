from flask import Blueprint, jsonify, request
from flask_login import login_required
import web_interface.auth as auth
from fyp.fyp_config import (
    DOWNLOADER_SCRIPT, INGEST_SCRIPT, ANNOTATOR_SCRIPT, MONITOR_SCRIPT, 
    CREATE_EVENT_LOG_SCRIPT, #REGENERATE_DATASETS_SCRIPT, CREATE_SUBSETS_SCRIPT, 
    RECODE_EVENT_LOG_SCRIPT, CALCULATE_PCA_SCRIPT,
    QUEUE_SCRAPER_SCRIPT, META_REFRESH_VIEWER_SCRIPT, 
    META_REFRESH_GROUPS_SCRIPT, TIMELINES_REFRESH_SCRIPT
)
from ..process_manager import (
    processes, process_stats, start_process, stop_process
)

process_bp = Blueprint('process_bp', __name__)

@process_bp.route('/api/start/<name>', methods=['POST'])
@auth.admin_required
def api_start(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    data = request.json or {}
    args = []
    
    if "study_name" in data:
        args.append(data["study_name"])

    if name in ["downloader", "annotator", "queue_scraper"]:
        if data.get("batch_size") and str(data["batch_size"]).strip():
             args.extend(["--batch-size", str(data["batch_size"])])
        if data.get("max_batches") and str(data["max_batches"]).strip():
             args.extend(["--max-batches", str(data["max_batches"])])

    study_name = data.get("study_name") 

    script_map = {
        "downloader": DOWNLOADER_SCRIPT,
        "monitor": MONITOR_SCRIPT,
        "annotator": ANNOTATOR_SCRIPT,
        #"create_subsets": CREATE_SUBSETS_SCRIPT,
        #"regenerate_datasets": REGENERATE_DATASETS_SCRIPT,
        "create_event_log": CREATE_EVENT_LOG_SCRIPT,
        "recode_event_log": RECODE_EVENT_LOG_SCRIPT,
        "calculate_pca": CALCULATE_PCA_SCRIPT,
        "queue_scraper": QUEUE_SCRAPER_SCRIPT,
        "meta_refresh_viewer": META_REFRESH_VIEWER_SCRIPT,
        "meta_refresh_groups": META_REFRESH_GROUPS_SCRIPT,
        "timelines_refresh": TIMELINES_REFRESH_SCRIPT
    }
    
    success, msg = start_process(name, script_map[name], args, study_name=study_name)
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 409


@process_bp.route('/api/stop/<name>', methods=['POST'])
@auth.admin_required
def api_stop(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    success, msg = stop_process(name)
    return jsonify({"status": "success" if success else "error", "message": msg})


@process_bp.route('/api/status', methods=['GET'])
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


@process_bp.route('/api/logs/clear/<name>', methods=['POST'])
@auth.admin_required
def api_clear_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    processes[name]["logs"].clear()
    return jsonify({"status": "success"})


@process_bp.route('/api/logs/<name>', methods=['GET'])
@login_required
def api_logs(name):
    if name not in processes:
        return jsonify({"error": "Unknown process"}), 400
    
    # Return last N lines
    logs = list(processes[name]["logs"])
    return jsonify({"logs": "".join(logs)})



