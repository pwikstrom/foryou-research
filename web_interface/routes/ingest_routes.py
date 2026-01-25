from flask import Blueprint, jsonify, request
from pathlib import Path
import subprocess
import json
from datetime import datetime
from ..fyp_config import fyp_cf, PROJECT_ROOT, PYTHON_EXEC, INGEST_SCRIPT
import fyp.data_io as data_io

ingest_bp = Blueprint('ingest_bp', __name__)

@ingest_bp.route('/api/find_ndjson', methods=['POST'])
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
        files = data_io.get_recent_files(fyp_cf, directory, suffix=".ndjson", how_recent=525600) 
        
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


@ingest_bp.route('/api/ingest_ndjson', methods=['POST'])
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


@ingest_bp.route('/api/browse_folder', methods=['POST'])
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


@ingest_bp.route('/api/upload_ndjson', methods=['POST'])
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
