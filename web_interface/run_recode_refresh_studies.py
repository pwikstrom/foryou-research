import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

if __name__ == "__main__":
    try:
        from fyp.fyp_config import fyp_cf
        from fyp.studies import init_study_defs
        from fyp.organize_datasets import create_study_recoded_dataset
        
        print("Starting Study Definitions (Recoded Data) Refresh...")
        
        # Init studies
        init_study_defs()
        studies = fyp_cf.get('study_defs', {})
        
        total = len(studies)
        if total == 0:
            print("No studies found to refresh.")
        
        for i, (study_name, config) in enumerate(studies.items()):
            print(f"::PROGRESS:: {{ \"percent\": {int((i/total)*100)}, \"message\": \"Processing {study_name}\" }}")
            print(f"Processing study: {study_name}")
            
            try:
                # Force generation of the recoded dataset for the study
                df_study = create_study_recoded_dataset(study_name=study_name, save_to_cache=True, verbose=False)
                
                if df_study is None:
                    print(f"Skipping {study_name}: No data generated.")
                else:
                    print(f"  Successfully refreshed data for {study_name} ({len(df_study)} rows)")
            except Exception as e:
                print(f"Error processing {study_name}: {e}")
                
        print(f"::PROGRESS:: {{ \"percent\": 100, \"message\": \"Completed\" }}")
        print("Study Definitions (Recoded Data) refresh completed.")

    except Exception as e:
        print(f"Process failed: {e}")
        sys.exit(1)
