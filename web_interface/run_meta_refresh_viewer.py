import sys
from pathlib import Path
import datetime

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        from web_interface.explorer_backend import load_data, get_metadata, make_serializable
        from web_interface.data_service import load_schema_metadata
        from fyp.studies import init_study_defs
        import fyp.data_io as data_io
        
        print("Starting Video Analysis (Viewer) Metadata Refresh...")
        
        # Init studies
        init_study_defs()
        studies = fyp_cf.get('study_defs', {})
        
        total = len(studies)
        for i, (study_name, config) in enumerate(studies.items()):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Study {i+1}/{total}\" }}")
            print(f"Processing study: {study_name}")
            
            try:
                # Load Data
                # This might take time
                df, col_types = load_data(study_name, verbose=False)
                
                if df is None: 
                    print(f"Skipping {study_name}: No data found.")
                    continue
                
                # Context = Viewer (Annotated OK + Activity Filter)
                if 'annotated_ok' in df.columns:
                    df_viewer = df[df.annotated_ok].copy()
                else:
                    df_viewer = df.copy() # Fallback
                df_viewer = df_viewer[df_viewer['activity_type'].isin(['play', 'observe'])]
                df_viewer = df_viewer[df_viewer['item_id'].notna()]
                
                print(f"  Generating metadata for {len(df_viewer)} items...")
                meta = get_metadata(df_viewer, col_types)
                meta = load_schema_metadata(meta)
                
                filename = f"{study_name}_viewer_metadata.json"
                
                # Remove old if exists (save_json overwrites, but good to be clean)
                # data_io.remove(storage_location="cache", filename=filename) 
                
                data_io.save_json(data=make_serializable(meta), storage_location="cache", filename=filename, verbose=False)
                print(f"  Updated metadata for {study_name}")
                
            except Exception as e:
                print(f"Error processing {study_name}: {e}")
        
        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print("Video Analysis Metadata refresh completed.")

    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
