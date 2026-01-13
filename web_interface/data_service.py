import threading
import pandas as pd
import numpy as np
from cachetools import LRUCache
from datetime import datetime
import fyp.data_io as data_io
from fyp.pca import calculate_scaled_pca_scores
from .hub_config import fyp_cf, PROJECT_ROOT
from . import explorer_backend as explorer

# --- Explorer State ---

class StudyCache:
    def __init__(self, maxsize=2):
        self.cache = LRUCache(maxsize=maxsize)
        self.lock = threading.Lock()

    def get(self, study_name):
        with self.lock:
            return self.cache.get(study_name)

    def put(self, study_name, data):
        with self.lock:
            self.cache[study_name] = data




study_cache = StudyCache(maxsize=2)




def get_explorer_data(study, context = None):
    from datetime import datetime
    # Check cache (First Check)
    cached = study_cache.get(study)
    if cached:
        print(f"    Study {study} found in RAM cache. Accessing {len(cached['df']):,} rows")
        return cached['df'], cached['col_types']

    # Double-Checked Locking
    # We will use a dedicated lock for the critical section of *checking and loading*
    if not hasattr(study_cache, 'loading_lock'):
         study_cache.loading_lock = threading.Lock()
         
    with study_cache.loading_lock:
        # Check cache again (Second Check)
        cached = study_cache.get(study)
        if cached:
            print(f"    Study {study} found in RAM cache (after lock). Accessing {len(cached['df']):,} rows")
            return cached['df'], cached['col_types']
            
        print(f"    Loading study {study} from disk (with lock)...")
        # Resolve path
        explorer_df, explorer_col_types = explorer.load_data(fyp_cf, study, verbose=True)

        if context == "viewer":
            print(f"    Filtering for scraped_ok. Reducing rows from {len(explorer_df):,} to ", end="")
            explorer_df = explorer_df[explorer_df.scraped_ok].copy()
            print(f"{len(explorer_df):,}")
        elif context == "explorer":
            print(f"    Filtering for annotated_ok. Reducing rows from {len(explorer_df):,} to ", end="")
            explorer_df = explorer_df[explorer_df.annotated_ok].copy()
            print(f"{len(explorer_df):,}")


        if explorer_df is None:
            print(f"The requested recoded study dataset was not found")
            return None, None

        # Store in cache
        cache_item = {
            "df": explorer_df, 
            "col_types": explorer_col_types,
        }
        study_cache.put(study, cache_item)
    
    return explorer_df, explorer_col_types




def get_viz_config():
    """
    Reads var_schema.csv and returns a dictionary of visualization settings.
    {
        var_name: {
            "log": bool,
            "bins": int or list of edges or None
        }
    }
    """
    config = {}
    try:
        var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if var_schema_path.exists():
            df = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
            # Check if columns exist
            has_log = 'web_viz_log' in df.columns
            has_bins = 'web_viz_bins' in df.columns
            
            if not has_log and not has_bins:
                return {}
                
            for _, row in df.iterrows():
                var = row['variable_name']
                cfg = {}
                
                # Log Setting
                if has_log:
                    val = str(row['web_viz_log']).lower().strip()
                    cfg['log'] = (val == 'yes')
                
                # Bin Setting
                if has_bins:
                    val = row['web_viz_bins']
                    if pd.notna(val):
                        val_str = str(val).strip()
                        if "|" in val_str:
                            # Parse custom edges: "10|30|50"
                            try:
                                edges = [float(x) for x in val_str.split("|")]
                                cfg['bins'] = sorted(edges)
                                
                            except:
                                cfg['bins'] = None
                        elif val_str.isdigit():
                             cfg['bins'] = int(val_str)
                        else:
                             cfg['bins'] = None
                    else:
                        cfg['bins'] = None
                
                if cfg:
                    config[var] = cfg
                    
    except Exception as e:
        print(f"Error reading viz config: {e}")
        
    return config

def make_serializable(obj):
    """Helper to convert non-JSON-serializable types."""
    if obj is None:
        return None
        
    # Check for containers FIRST to avoid pd.isna() returning an array
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}

    if isinstance(obj, np.ndarray):
        return [make_serializable(x) for x in obj.tolist()]
        
    if isinstance(obj, (list, tuple)):
        return [make_serializable(x) for x in obj]
    
    # Check for scalar NAs (NaN, NaT, None)
    # This is safe now because we've handled most containers
    try:
        if pd.isna(obj):
            return None
    except:
        pass

    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
        
    if hasattr(obj, 'tolist'):  # generic numpy scalar fallback
        return obj.tolist()
        
    return obj

# --- PCA Visualization Endpoints ---

pca_df_cache = {}

def get_pca_df(study_name):
    #from os.path import exists as os_exists, join as os_join
    #from pandas import read_parquet as pd_read_parquet
    import fyp.data_io as data_io

    global pca_df_cache
    if study_name in pca_df_cache:
        # Check freshness? Simple version: just return.
        return pca_df_cache[study_name]

    # Load file
    if True:# try:
        
        pca_filename = f"{study_name}_PCA.parquet"
        
        if data_io.exists(
            cf=fyp_cf,
            storage_location="cache",
            filename=pca_filename,
            ):
            
            events_pca_scores_scaled = data_io.load_parquet(
                cf=fyp_cf,
                storage_location="cache",
                filename=pca_filename,
                )

        else:
            print("Calculating PCA scores for study: ", study_name)
            events_pca_scores_scaled, _ = calculate_scaled_pca_scores(
                cf = fyp_cf,
                study_name = study_name,
                study_recoded_dataset = None,
                minimum_group_size = 10,
                target_explained_variance = 0.8,
                drop_rare_globally_below = 0.01,
                scale_it = True,
                down_sample = 1,
                min_sample_size = 20000,
                load_from_cache = True,
                save_to_cache = True,
                verbose = False,
                )
        
        pca_df_cache[study_name] = events_pca_scores_scaled
        return events_pca_scores_scaled
    if False:#except Exception as e:
        print(f"Error loading PCA: {e}")
        return None
