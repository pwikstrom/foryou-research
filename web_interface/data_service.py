import threading
import pandas as pd
import json
import numpy as np
from cachetools import LRUCache
from datetime import datetime
import fyp.data_io as data_io
from fyp.pca import calculate_scaled_pca_scores
from .hub_config import fyp_cf, PROJECT_ROOT
from . import explorer_backend as explorer
from fyp import fyp_main
from fyp.organize_datasets import create_donation_unified_dataset

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
            print(f"    Filtering for scraped_ok and watch-only events. Reducing rows from {len(explorer_df):,} to ", end="")
            explorer_df = explorer_df[(explorer_df.scraped_ok) & (explorer_df.D_feature_name=="watch")].copy()
            print(f"{len(explorer_df):,}")
        elif context == "explorer":
            print(f"    Filtering for annotated_ok and watch-only events. Reducing rows from {len(explorer_df):,} to ", end="")
            explorer_df = explorer_df[(explorer_df.annotated_ok) & (explorer_df.D_feature_name=="watch")].copy()
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


def enrich_with_user_tags(df, col_types, username):
    """
    Injects a 'User Tags' column into the DataFrame based on the user's tag file.
    Returns (enriched_df, enriched_col_types).
    If no tags found, returns original.
    """
    tag_filename = f"{username}_tags.json"
    if not data_io.exists(fyp_cf, "users", tag_filename):
        return df, col_types

    user_tags = data_io.load_json(fyp_cf, "users", tag_filename)
    if not user_tags:
        return df, col_types

    # user_tags: { item_id: { var: [tags...] } }
    # We want a map: item_id -> unique list of tags (flattened across variables)
    
    # Pre-calculate map for Tags
    id_to_tags = {}
    
    # Set of IDs with ANY annotation (tags, notes, closed tags)
    annotated_ids = set()
    
    for item_id, var_map in user_tags.items():
        # If item is in user_tags, it has SOME annotation (due to cleanup logic on save)
        annotated_ids.add(str(item_id))
        
        # Collect explicit tags for the list column
        all_tags = set()
        for key, val in var_map.items():
            if isinstance(val, list): # It's a tag list
                all_tags.update(val)
                
        if all_tags:
            id_to_tags[str(item_id)] = list(all_tags)
            
    if not annotated_ids:
        return df, col_types
        
    # Create the column
    # Ensure ID matching
    id_col = 'item_id'
    if id_col not in df.columns:
        if 'video_id' in df.columns: id_col = 'video_id'
        elif 'G_id' in df.columns: id_col = 'G_id'
        else: return df, col_types

    # Copy to avoid modifying cache
    df = df.copy()
    col_types = col_types.copy()
    
    # Vectorized mapping
    str_ids = df[id_col].astype(str)
    
    # 1. User Tags (List)
    if id_to_tags:
        df['User Tags'] = str_ids.map(id_to_tags)
        if df['User Tags'].count() > 0:
             col_types['User Tags'] = 'list'
        else:
             df.drop(columns=['User Tags'], inplace=True, errors='ignore')
    
    # 2. Has Annotation (Boolean/Category)
    # We map to "Yes" / "No" or boolean? 
    # Boolean is cleaner but 'category' type in explorer often expects strings. 
    # Let's use boolean, pandas handles it. Explorer backend might convert boolean to "True"/"False" string representations.
    # Let's check explorer backend? 'values' in metadata for boolean are [True, False].
    # Frontend checkboxes: True, False. 
    # To make it user friendly ("Yes"), maybe I should map to "Yes"/NaN?
    # If I map to boolean True/False, I get checkboxes "True" and "False".
    # User calls it "Has Annotation". Checkbox "True" is O.K.
    # Maybe map to "Annotated" / "Not Annotated"?
    # "Has Annotation": [x] Annotated. 
    # Let's try Boolean first.
    
    df['Has Annotation'] = str_ids.isin(annotated_ids)
    
    # Only keep if there are any true values? Or always keep if explicit user request?
    # If no annotations exist at all, we returned early above.
    # So we have annotations.
    col_types['Has Annotation'] = 'category' # Treat as category to trigger checkbox UI
    
    return df, col_types




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



def check_and_update_timeline_cache(donation_id, viz_vars):
    """
    Ensures that timeline aggregations for day, week, and month exist in cache.
    If not, calculates them from the unified donation dataset.
    """
    
    intervals = ['day', 'week', 'month']
    missing = []
    
    # Check if files exist
    # DEBUG: Force regeneration to fix cached bad data
    # for interval in intervals:
    #     filename = f"timeline_{donation_id}_{interval}.parquet"
    #     if not data_io.exists(fyp_cf, "cache", filename):
    #         missing.append(interval)
            
    # Force missing to trigger generation
    missing = intervals 
            
    # if not missing:
    #     return True # All good
            
    # Generate Data
    # 1. Load Unified Dataset
    df = create_donation_unified_dataset(fyp_cf, donation_id=donation_id, verbose=True)
    if df is None or df.empty:
        print("ERROR: Could not load unified dataset for donation", donation_id)
        return False
        
    # Ensure date column
    date_col = 'T_local_date'
    if date_col not in df.columns:
         print(f"ERROR: {date_col} missing in unified dataset")
         return False
         
    df[date_col] = pd.to_datetime(df[date_col]).astype('datetime64[ns]')
    
    # Filter for Watch events only (as per previous requirement)
    if 'D_feature_name' in df.columns:
        df = df[df['D_feature_name'] == 'watch']



    # ---------------------------------------------------------
    # Custom Variables Calculation
    # 1. Completion Rate
    wd_col = 'D_watch_duration'
    vd_col = 'S_video_duration'

    # DEBUG: Print all columns to find the right ones
    print(f"DEBUG TIMELINE: Available columns: {sorted(df.columns.tolist())}")

    if True:# wd_col in df.columns and vd_col:
        #print(f"DEBUG TIMELINE: Calculating completion_rate using {wd_col} / {vd_col}")
        #wd = pd.to_numeric(df[wd_col], errors='coerce')
        #vd = pd.to_numeric(df[vd_col], errors='coerce')
        
        # Avoid zero division
        rate = df['D_watch_duration'] / df['S_video_duration']
        rate = rate.replace([np.inf, -np.inf], np.nan)
        
        # Clip sensible range? 0 to ~1 (or >1 if rewatched). usage says "split by" -> divide
        df['completion_rate'] = rate.map(lambda x: min(max(x, 0), 1))
        
        if 'completion_rate' not in viz_vars:
            viz_vars.append('completion_rate')
    else:
        print(f"DEBUG TIMELINE: Columns for completion_rate missing. WD: {wd_col in df.columns}, VD: {vd_col}")


    # ---------------------------------------------------------
    # 2. Iterate and Aggregate
    # We allow regenerating ALL intervals if any is missing to keep them in sync, 
    # or just missing. Let's do all to be safe if one is missing (might be stale).
    # Actually user said "check ... and if they are not there", implying lazy load.
    # But loading unified is expensive, so efficiently do all 3 if we loaded it.
    
    for interval in intervals:
        
        # Grouping
        temp_df = df.copy()
        if interval == 'week':
            temp_df['period'] = temp_df[date_col].dt.to_period('W').apply(lambda r: r.start_time).dt.date.astype(str)
        elif interval == 'month':
             temp_df['period'] = temp_df[date_col].dt.to_period('M').apply(lambda r: r.start_time).dt.date.astype(str)
        else: # day
             temp_df['period'] = temp_df[date_col].dt.date.astype(str)
             
        group_col = 'period'
        periods = sorted(temp_df[group_col].unique())
        
        # Build Result DataFrame
        # We need a row per period.
        # Columns: period, count (videos), valid_count_{var}, {var}_val (numeric), {var}_counts (json)
        
        agg_data = [] # List of dicts
        
        # Pre-calculate video counts per period
        video_counts = temp_df[group_col].value_counts().to_dict()
        
        for p in periods:
            row = {'period': p, 'video_count': video_counts.get(p, 0)}
            period_subset = temp_df[temp_df[group_col] == p]
            
            for var in viz_vars:
                if var not in period_subset.columns:
                    continue
                
                # Check type in this subset? or globally? 
                # Better globally or inferred.
                # Logic:
                s_subset = period_subset[var]
                
                # Check for list
                # Robust Arrow check
                dt = s_subset.dtype
                is_arrow_list = isinstance(dt, pd.ArrowDtype) and 'list' in str(dt)
                
                first_valid = s_subset.dropna().iloc[0] if not s_subset.dropna().empty else None
                is_py_list = isinstance(first_valid, list)
                is_np_array = isinstance(first_valid, np.ndarray)
                
                is_list = is_arrow_list or is_py_list or is_np_array
                
                # Ensure it's not numeric if we think it's a list (unless it's a list of numbers, which we treat as categorical/list anyway for now)
                # But is_numeric check below relies on dtype.
                # If object dtype containing lists, is_numeric_dtype represents 'object' -> False. Correct.
                
                is_numeric = pd.api.types.is_numeric_dtype(s_subset.dtype) and not is_list
                
                # Verify numeric isn't just low cardinality numbers masquerading 
                # (Actually user config 'web_viz_log' helps, but relying on dtype is standard)

                # DEBUG
                # if var in ['S_desc_hashtags', 'G_symbol_and_brands', 'G_content_categories']:
                #    print(f"DEBUG TIMELINE: Var {var} is_list={is_list} (Arrow={is_arrow_list}, PyList={is_py_list}, NpArray={is_np_array}). Sample: {first_valid} Type: {type(first_valid)}")
                
                # Valid Count
                if is_list:
                    # Safely handle potential numpy arrays
                    # Convert to list to ensure explode works cleanly
                    def safe_to_list(x):
                        if isinstance(x, np.ndarray):
                            return x.tolist()
                        if isinstance(x, list):
                            return x
                        return x # pass through for None/NaN fallback
                        
                    # Apply conversion
                    s_subset = s_subset.apply(safe_to_list)

                    valid_files = s_subset[s_subset.apply(lambda x: isinstance(x, list) and len(x) > 0)]
                    valid_c = len(valid_files)
                else:
                    valid_files = None
                    valid_c = s_subset.count() # non-NA
                
                row[f"{var}_valid"] = valid_c
                
                if is_numeric:
                    # Mean
                    row[f"{var}_val"] = s_subset.mean()
                else:
                    # Counts
                    if is_list:
                         # Explode
                         exploded = s_subset.explode()
                         cnts = exploded.value_counts().to_dict()
                    else:
                         cnts = s_subset.value_counts().to_dict()
                    
                    # Store as JSON string
                    row[f"{var}_counts"] = json.dumps(cnts)
            
            agg_data.append(row)
            
        agg_df = pd.DataFrame(agg_data)
        
        # Save
        filename = f"timeline_{donation_id}_{interval}.parquet"
        data_io.save_parquet(fyp_cf, agg_df, "cache", filename)

    return True




def get_timeline_data(donation_id, interval='day'):
    """
    Returns timeline data for plotting.
    - Numeric: Daily Mean (Raw values, invalid/missing ignored). 
      Includes metadata if log scale is requested.
    - Categorical: Daily Counts per category + Daily Total Count (for % calc).
    """

    if 'var_schema' not in fyp_cf:
        print("ERROR: var_schema missing")
        return {}

    schema = fyp_cf.get('var_schema', {})
    
    # Load Schema Metadata
    meta = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])
    schema_map = meta.get('schema_map', {})


    # Ensure Cache Exists
    if not check_and_update_timeline_cache(donation_id, viz_vars):
        print("ERROR: Failed to update timeline cache.")
        return {}
        
    # Get Counts Metadata (Load all 3 aggs to get lengths)

    period_counts = {}
    
    # Helper to load specific interval
    def load_interval_df(u_interval):
        fname = f"timeline_{donation_id}_{u_interval}.parquet"
        if data_io.exists(fyp_cf, "cache", fname):
            return data_io.load_parquet(fyp_cf, "cache", fname)
        return None

    # Load all to get counts
    aggs = {}
    for inv in ['day', 'week', 'month']:
        df_agg = load_interval_df(inv)
        if df_agg is not None:
             period_counts[inv] = len(df_agg)
             aggs[inv] = df_agg
        else:
             period_counts[inv] = 0
             
    # Use requested interval data
    df = aggs.get(interval)
    if df is None or df.empty:
         return {"dates": [], "variables": {}, "counts": period_counts}
         
    # Prepare Result
    # Dates
    # Sort by period just in case
    df = df.sort_values(by='period')
    dates = df['period'].tolist()
    
    # Formatted Labels
    date_labels = []
    for d_str in dates:
        try:
            # We assume d_str is YYYY-MM-DD or similar from the generation step
            # Note: generation step produces:
            # Day: YYYY-MM-DD
            # Week: YYYY-MM-DD (start date)
            # Month: YYYY-MM-DD (start date)
            dt = pd.to_datetime(d_str)
            if interval == 'day':
                lbl = dt.strftime('%d/%m/%y')
            elif interval == 'week':
                lbl = dt.strftime('%Y-%V')
            elif interval == 'month':
                lbl = dt.strftime('%b-%y')
            else:
                lbl = d_str
            date_labels.append(lbl)
        except:
            date_labels.append(str(d_str))
            
    variables = {}
    
    for var in viz_vars:
        # Check if we have columns for this var
        # Expected: {var}_val (numeric) OR {var}_counts (categorical)
        # And {var}_valid
        
        # Determine type based on cols existence
        has_val = f"{var}_val" in df.columns
        has_counts = f"{var}_counts" in df.columns
        
        if not has_val and not has_counts:
            continue
            
        # Log Scale Config
        use_log = False
        if schema_map.get(var, {}).get('web_viz_log') == 'yes':
             use_log = True
        elif isinstance(schema, dict) and schema.get(var, {}).get('web_viz_log') == 'yes':
             use_log = True
             
        # Metadata common props
        valid_counts = df.get(f"{var}_valid", pd.Series([0]*len(df))).tolist()
        video_counts = df['video_count'].tolist() # Total videos per period
        
        if has_val:
            # Numeric
            vals = df[f"{var}_val"].where(pd.notnull(df[f"{var}_val"]), None).tolist()
            variables[var] = {
                "type": "numeric",
                "values": vals,
                "log": use_log,
                "daily_valid_counts": valid_counts,
                "daily_video_counts": video_counts
            }
        elif has_counts:
            # Categorical
            # Parse JSON counts
            counts_list = []
            
            # Need to get top categories globally to match frontend logic?
            # Or just return all? Frontend sorts them. 
            # Frontend expects "counts" as list of dicts.
            # And "top_categories" for initial selection.
            
            global_cat_counts = {}
            
            for json_str in df[f"{var}_counts"]:
                try:
                    if json_str and isinstance(json_str, str):
                        c_dict = json.loads(json_str)
                    else:
                        c_dict = {}
                except:
                    c_dict = {}
                
                counts_list.append(c_dict)
                
                # Aggregate for top_categories
                for k, v in c_dict.items():
                    global_cat_counts[k] = global_cat_counts.get(k, 0) + v
            
            # Top categories
            top_cats = sorted(global_cat_counts.keys(), key=lambda x: global_cat_counts[x], reverse=True)
            
            variables[var] = {
                "type": "categorical",
                "counts": counts_list,
                "daily_video_counts": video_counts,
                "daily_valid_counts": valid_counts,
                "top_categories": top_cats[:3]
            }

    return {"dates": dates, "date_labels": date_labels, "variables": variables, "counts": period_counts}

def get_study_donations(study):
    """
    Returns a list of unique donations present in the study dataset.
    Returns: [{ 'D_donation_id': '...', 'D_id': '...' }, ...]
    """
    try:
        df, col_types = get_explorer_data(study, context="explorer")
        if df is None:
            return []
        
        if 'D_donation_id' not in df.columns:
            # Fallback if D_donation_id is missing? 
            # It should be there for any valid study.
            return []

        # Unique donations
        # We need D_donation_id and D_id (if available)
        cols_to_use = ['D_donation_id']
        if 'D_id' in df.columns:
            cols_to_use.append('D_id')
            
        donations = df[cols_to_use].drop_duplicates()
        
        # Format for frontend
        result = []
        for _, row in donations.iterrows():
            item = {'D_donation_id': row['D_donation_id']}
            if 'D_id' in row:
                item['D_id'] = row['D_id']
            else:
                item['D_id'] = row['D_donation_id'] # Fallback
            result.append(item)
            
        return sorted(result, key=lambda x: str(x.get('D_id', '')))
        
    except Exception as e:
        print(f"Error getting study donations: {e}")
        return []



# Alias from explorer to handle serialization issues
make_serializable = explorer.make_serializable


# --- PCA Visualization Endpoints ---

pca_df_cache = {}

def get_pca_df(study_name):


    global pca_df_cache
    if study_name in pca_df_cache:
        # Check freshness? Simple version: just return.
        return pca_df_cache[study_name]

    print("Loading PCA scores for study: ", study_name)

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




def load_schema_metadata(metadata):
    """Helper to load and inject schema metadata (priorities, descriptions, accepted_labels) from CSV."""
    try:
        var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if var_schema_path.exists():
            schema_df = pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
            schema_df['web_display_prio'] = pd.to_numeric(schema_df['web_display_prio'], errors='coerce')
            display_df = schema_df.dropna(subset=['web_display_prio']).sort_values('web_display_prio')
            metadata['display_priority'] = display_df['variable_name'].tolist()

            if 'web_viz_prio' in schema_df.columns:
                schema_df['web_viz_prio'] = pd.to_numeric(schema_df['web_viz_prio'], errors='coerce')
                viz_df = schema_df.dropna(subset=['web_viz_prio']).sort_values('web_viz_prio')
                metadata['viz_priority'] = viz_df['variable_name'].tolist()
            else:
                 metadata['viz_priority'] = []
            
            if 'web_timeline_prio' in schema_df.columns:
                schema_df['web_timeline_prio'] = pd.to_numeric(schema_df['web_timeline_prio'], errors='coerce')
                timeline_df = schema_df.dropna(subset=['web_timeline_prio']).sort_values('web_timeline_prio')
                metadata['timeline_priority'] = timeline_df['variable_name'].tolist()
            else:
                 metadata['timeline_priority'] = []

            if 'web_filter_prio' in schema_df.columns:  
                schema_df['web_filter_prio'] = pd.to_numeric(schema_df['web_filter_prio'], errors='coerce')
                filter_df = schema_df.dropna(subset=['web_filter_prio']).sort_values('web_filter_prio')
                metadata['filter_priority'] = filter_df['variable_name'].tolist()
            else:
                metadata['filter_priority'] = []

            if 'section' not in schema_df.columns:
                schema_df['section'] = 'General'
            if 'description' not in schema_df.columns:
                schema_df['description'] = ''
            
            schema_df['section'] = schema_df['section'].fillna('General')
            schema_df['description'] = schema_df['description'].fillna('')
            
            schema_map = {}
            for _, row in schema_df.iterrows():
                var_name = row['variable_name']
                schema_map[var_name] = {
                    "section": str(row['section']),
                    "description": str(row['description'])
                }
                
                # Parse Accepted Labels for Closed Tags
                if 'accepted_labels' in row:
                    accepted = str(row['accepted_labels'])
                    if accepted and accepted.lower() != 'nan' and accepted.startswith('[') and accepted.endswith(']'):
                        content = accepted[1:-1]
                        if content.strip():
                            labels = [x.strip() for x in content.split(',')]
                            schema_map[var_name]['accepted_labels'] = labels
                
                # Add Display Name
                if 'display_name' in row:
                    dname = str(row['display_name'])
                    if dname and dname.lower() != 'nan' and dname.strip():
                        schema_map[var_name]['display_name'] = dname.strip()

                # Add Display Priority (for filtering in viewer)
                if 'web_display_prio' in row:
                    prio = row['web_display_prio']
                    if pd.notna(prio):
                         schema_map[var_name]['web_display_prio'] = float(prio)
            
            metadata['schema_map'] = schema_map
                
        else:
            # Only reset if keys missing? Or always reset? 
            # If CSV missing, we might want to keep existing if available?
            # But here we assume CSV is source of truth.
            metadata['display_priority'] = []
            metadata['filter_priority'] = []
            metadata['schema_map'] = {}
    except Exception as e:
        print(f"Error loading priority list: {e}")
        # Don't overwrite with empty if error?
        pass
    return metadata
