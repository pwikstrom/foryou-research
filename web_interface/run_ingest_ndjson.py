
import sys
#import os
#import shutil 
import json
import traceback
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

import fyp.fyp_main as fyp
from fyp.zeeschuimer import read_ndjson_file, refine_zeeschuimer_log, get_baseline_info_as_string
import fyp.data_io as data_io
#import pandas as pd




def ingest_files(filenames, label):
    log_output = []
    summary_output = []
    
    def log(msg):
        print(msg, file=sys.stderr)
        log_output.append(msg)

    fyp_cf = fyp.initialize(verbose=False)
    
    
    processed_count = 0
    
    for filename in filenames:
        try:
            if not data_io.exists(storage_location="firefox_downloads", filename=filename):
                log(f"Error: File not found: {filename}")
                continue
                
            
            data_io.move(src_storage_location="firefox_downloads", dst_storage_location="zeeschuimer_raw", filename=filename)
            
            # 3. Read and Refine
            
            raw_data = read_ndjson_file(storage_location="zeeschuimer_raw", filename=filename)
            if not raw_data:
                log("  Warning: Empty data found.")
                continue
                
            df = zeeschuimer.refine_zeeschuimer_log(raw_data)
            
            if df.empty:
                log("  Warning: DataFrame empty after refinement.")
                continue

            # 4. Overwrite Label
            df['label'] = label
            
            # 5. Save File
            processed_fn = filename.replace(".ndjson", '.parquet')
            

            
            #save_path = refined_dir / processed_fn
            df = fyp.convert_dtypes_to_pyarrow(df, verbose=False)
            data_io.save_parquet(df=df, storage_location="zeeschuimer_refined", filename=processed_fn, verbose=False)
            log(f"  Saved refined DataFrame to {processed_fn}")
            
            # Generate summary
            summary = get_baseline_info_as_string(df)
            summary_output.append(f"Summary for {filename}:\n{summary}")
            
            processed_count += 1
            
        except Exception as e:
            log(f"  Error processing {filename}: {e}")
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
