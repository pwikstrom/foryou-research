
import sys
import os
import shutil
import json
import traceback
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

import fyp.fyp_main as fyp
from fyp.get_baseline_log import read_ndjson_file, refine_zeeschuimer_log, get_baseline_info_as_string
import pandas as pd

def ingest_files(file_paths, label):
    log_output = []
    summary_output = []
    
    def log(msg):
        print(msg, file=sys.stderr)
        log_output.append(msg)

    fyp_cf = fyp.init_project(verbose=False)
    
    raw_dir = Path(fyp_cf["paths"]["zeeschuimer_raw"])
    refined_dir = Path(fyp_cf["paths"]["zeeschuimer_refined"])
    
    processed_count = 0
    
    for file_path in file_paths:
        try:
            original_path = Path(file_path)
            if not original_path.exists():
                log(f"Error: File not found: {file_path}")
                continue
                
            #log(f"Processing: {original_path.name}")
            
            # 1. Prepare new filename (Label + original name)
            # Sanitize label a bit
            safe_label = "".join(c for c in label if c.isalnum() or c in (' ', '_', '-')).strip().replace(" ", "_")
            
            # Construct new filename: Label_OriginalName
            # existing naming convention in get_baseline_log uses script name as prefix.
            # We will use the label as "the_script" essentially.
            
            new_filename = f"{safe_label}_{original_path.name}"
            dest_path = raw_dir / new_filename
            
            # 2. Move File
            if dest_path.exists():
                # Handle collision? For now overwriting or maybe appending timestamp?
                # User asked to just move. I'll append timestamp if exists to be safe.
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                new_filename = f"{safe_label}_{timestamp}_{original_path.name}"
                dest_path = raw_dir / new_filename
                
            shutil.move(str(original_path), str(dest_path))
            #log(f"  Moved to {dest_path}")
            
            # 3. Read and Refine
            # The read_ndjson_file function expects the file to be in zeeschuimer_raw usually 
            # or we can pass absolute path.
            # It uses fyp.cf["misc"]["label"] which is hardcoded in config... 
            # We need to overwrite the label column anyway.
            
            raw_data = read_ndjson_file(str(dest_path))
            if not raw_data:
                log("  Warning: Empty data found.")
                continue
                
            df = refine_zeeschuimer_log(raw_data)
            
            if df.empty:
                log("  Warning: DataFrame empty after refinement.")
                continue

            # 4. Overwrite Label
            df['label'] = label
            #log(f"  Assigned label: {label}")
            
            # 5. Save Pickle
            # Naming logic from move_and_refine_recent_file
            # It creates names like {script}{original_name_wo_zeeschuimer}.pkl
            # simplified:
            pickle_fn = new_filename.replace(".ndjson", ".pkl")
            
            # Ensure unique
            r = 0
            while (refined_dir / pickle_fn).exists():
                r += 1
                stem = new_filename.replace(".ndjson", "")
                pickle_fn = f"{stem}_{r:04}.pkl"
            
            save_path = refined_dir / pickle_fn
            df.to_pickle(str(save_path))
            save_path = refined_dir / pickle_fn
            df.to_pickle(str(save_path))
            log(f"  Saved refined DataFrame to {save_path.name}")
            
            # Generate summary
            summary = get_baseline_info_as_string(df)
            summary_output.append(f"Summary for {original_path.name}:\n{summary}")
            
            processed_count += 1
            
        except Exception as e:
            log(f"  Error processing {file_path}: {e}")
            log(traceback.format_exc())

    return log_output, processed_count, "\n".join(summary_output)

if __name__ == "__main__":
    # Expects JSON input from stdin with "files" and "label"
    try:
        input_data = json.load(sys.stdin)
        files = input_data.get("files", [])
        label = input_data.get("label", "")
        
        if not files or not label:
            print(json.dumps({"status": "error", "message": "Missing arguments"}))
            sys.exit(1)
            
        logs, count, summary = ingest_files(files, label)
        
        print(json.dumps({
            "status": "success", 
            "message": f"Processed {count} files.", 
            "log": "\n".join(logs),
            "summary": summary
        }))
        
    except Exception as e:
        print(json.dumps({
            "status": "error", 
            "message": str(e),
            "log": traceback.format_exc()
        }))
