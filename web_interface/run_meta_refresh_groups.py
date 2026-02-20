import sys
from pathlib import Path
from datetime import datetime

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        from web_interface.explorer_backend import load_data, get_metadata, get_current_stats, make_serializable
        from web_interface.data_service import load_schema_metadata, get_viz_config
        from fyp.studies import init_study_defs
        import fyp.data_io as data_io
        
        print("Starting Group Comparisons (Explorer) Metadata Refresh...")
        
        # Init studies
        init_study_defs()
        studies = fyp_cf.get('study_defs', {})
        
        total = len(studies)
        for i, (study_name, config) in enumerate(studies.items()):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Processing {study_name}\" }}")
            print(f"Processing study: {study_name}")
            
            try:
                # Load Data
                df, col_types = load_data(study_name, verbose=False)
                
                if df is None:
                    print(f"Skipping {study_name}: No data found.")
                    continue
                
                # Context = Explorer (Annotated OK)
                if 'annotated_ok' in df.columns:
                    df_explorer = df[df.annotated_ok].copy()
                else:
                    df_explorer = df.copy()
                    
                print(f"  Generating metadata for {len(df_explorer)} items...")
                meta = get_metadata(df_explorer, col_types)
                
                # Calculate Stats
                viz_config = get_viz_config()
                stats_res = get_current_stats(df_explorer, col_types, viz_config=viz_config)
                meta['total_stats'] = stats_res['stats']
                
                # Source Info Injection
                try:
                    the_recoded_file = f"{study_name}_recoded.parquet"
                    if data_io.exists(storage_location="cache", filename=the_recoded_file):
                        meta['source_file'] = the_recoded_file
                        mtime = datetime.fromtimestamp(data_io.getmtime(storage_location="cache", filename=the_recoded_file))
                        meta['source_file_modified'] = mtime.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        meta['source_file'] = "Unknown"
                        meta['source_file_modified'] = ""
                except Exception as e:
                    meta['source_file'] = "Error"
                    meta['source_file_modified'] = ""
                
                meta = load_schema_metadata(meta)
                
                filename = f"{study_name}_explorer_metadata.json"
                data_io.save_json(data=make_serializable(meta), storage_location="cache", filename=filename, verbose=False)
                print(f"  Updated metadata for {study_name}")
                
            except Exception as e:
                print(f"Error processing {study_name}: {e}")
                import traceback
                traceback.print_exc()

        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print("Group Comparisons Metadata refresh completed.")

    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
