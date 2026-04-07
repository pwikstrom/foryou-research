import threading
import pandas as pd
import json
import numpy as np
from sklearn.metrics import cohen_kappa_score
from cachetools import LRUCache
import fyp.data_io as data_io
from fyp.pca import calculate_scaled_pca_scores
from fyp.fyp_config import fyp_cf, PROJECT_ROOT
from . import explorer_backend as explorer
from fyp.organize_datasets import create_collection_unified_dataset
from fyp.studies import init_study_defs

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




def get_explorer_data(study, context=None, verbose=False):
    # Check cache (First Check)
    cached = study_cache.get(study)
    
    # Store raw data in cache, filter on retrieval
    raw_df = None
    raw_col_types = None
    
    if cached:
        if verbose:
            print(f"    Study {study} found in RAM cache. Accessing {len(cached['df']):,} rows")
        raw_df = cached['df']
        raw_col_types = cached['col_types']
    else:
        # Double-Checked Locking
        if not hasattr(study_cache, 'loading_lock'):
             study_cache.loading_lock = threading.Lock()
             
        with study_cache.loading_lock:
            # Check cache again (Second Check)
            cached = study_cache.get(study)
            if cached:
                if verbose:
                    print(f"    Study {study} found in RAM cache (after lock). Accessing {len(cached['df']):,} rows")
                raw_df = cached['df']
                raw_col_types = cached['col_types']
            else:
                if verbose:
                    print(f"    Loading study {study} from disk (with lock)...")
                # Resolve path
                raw_df, raw_col_types = explorer.load_data(study, verbose=False)

                if raw_df is None:
                    if verbose:
                        print(f"The requested recoded study dataset was not found")
                    return None, None

                # Store in cache (RAW DATA)
                cache_item = {
                    "df": raw_df, 
                    "col_types": raw_col_types,
                }
                study_cache.put(study, cache_item)
    
    # Apply Context Filtering on a COPY
    if raw_df is not None:
        if context == "viewer":
            filtered_df = raw_df[
                (raw_df.annotated_ok)
                & (raw_df['activity_type'].isin(['play', 'observe']))
                & (raw_df['item_id'].notna())
            ].copy()
        elif context == "explorer":
            filtered_df = raw_df[
                (raw_df.annotated_ok)
                & (raw_df['activity_type'].isin(['play', 'observe']))
                & (raw_df['item_id'].notna())
            ].copy()
        else:
            # return raw copy to be safe. this should never happen though...
            filtered_df = raw_df.copy()


        return filtered_df, raw_col_types.copy()

    return None, None





def enrich_with_user_tags(df, col_types, username, shared_users_tags=None):
    """
    Injects a 'User Tags' column into the DataFrame based on the user's tag file.
    Returns (enriched_df, enriched_col_types).
    If no tags found, returns original.
    """
    tag_filename = f"{username}_tags.json"
    filename = f"{username}.json"
    user_data = {}
    user_tags = {}
    
    # Try loading exact match first
    if data_io.exists(storage_location="users", filename=filename):
        user_data = data_io.load_json(storage_location="users", filename=filename) or {}
    else:
        # Fallback to lowercase
        filename_lower = f"{username.lower()}.json"
        if data_io.exists(storage_location="users", filename=filename_lower):
             user_data = data_io.load_json(storage_location="users", filename=filename_lower) or {}

    if user_data:
        user_tags = user_data.get('annotations', {})
        
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
            
    # Merge Shared Tags
    if shared_users_tags:
        for iid, tags in shared_users_tags.items():
            str_id = str(iid)
            annotated_ids.add(str_id) # Ensure ID is marked as annotated
            
            # Update id_to_tags
            if str_id in id_to_tags:
                existing = set(id_to_tags[str_id])
                existing.update(tags)
                id_to_tags[str_id] = list(existing)
            else:
                id_to_tags[str_id] = list(tags)

    # Was: if not annotated_ids: return df, col_types
    # We continue now to ensure "Has Annotation" and "Machine Annotations" are added even if empty.
        

    # Copy to avoid modifying cache
    df = df.copy()
    col_types = col_types.copy()
    
    str_ids = df['item_id'].astype(str) # just to be safe. item_id should always be a string
    
    # 1. User Tags (List)
    if id_to_tags:
        df['User Tags'] = str_ids.map(id_to_tags)
        
        # Fill NaNs with empty lists (crucial for type safety in filters)
        df['User Tags'] = df['User Tags'].apply(lambda x: x if isinstance(x, list) else [])
        
        if df['User Tags'].apply(len).sum() > 0: # Check if any tags exist
             col_types['User Tags'] = 'list'
        else:
             df.drop(columns=['User Tags'], inplace=True, errors='ignore')
    
    # 2. Has Annotation (Boolean/Category)
    if shared_users_tags:
        annotated_ids.update(str(k) for k in shared_users_tags.keys())

    
    df['Has Annotation'] = str_ids.isin(annotated_ids)
    
    # Only keep if there are any true values? Or always keep if explicit user request?
    # If no annotations exist at all, we returned early above.
    # So we have annotations.
    col_types['Has Annotation'] = 'category' # Treat as category to trigger checkbox UI
    
    # 3. Machine Annotations (New Request)
    # Check if annotated_ok exists
    if 'annotated_ok' in df.columns:
        # Map boolean to cleaner labels
        df['Machine Annotations'] = 'Not Attempted'
        df.loc[df['annotated_ok'] == True, 'Machine Annotations'] = 'Machine Annotated'
        df.loc[df['annotated_ok'] == False, 'Machine Annotations'] = 'Cannot Machine Annotate'
        
        col_types['Machine Annotations'] = 'category'
    
    return df, col_types


def load_shared_tags(allowed_usernames):
    """
    Loads tags from a list of usernames.
    Returns:
        simple_map: { item_id: set(tags) } (For DF Enrichment)
        detailed_map: { item_id: { variable: { user: { tags: [], notes: ... } } } } (For Viewer Details)
    """
    simple_map = {}
    detailed_map = {}
    
    if not allowed_usernames:
        return simple_map, detailed_map
        
    #print(f"DEBUG: load_shared_tags called for: {allowed_usernames}")
        
    for user in allowed_usernames:
        try:
            filename = f"{user}.json"
            
            # Check exist
            user_blob = None
            if data_io.exists(storage_location="users", filename=filename):
                user_blob = data_io.load_json(storage_location="users", filename=filename)
            else:
                 # Check lowercase
                 filename_lower = f"{user.lower()}.json"
                 if data_io.exists(storage_location="users", filename=filename_lower):
                     user_blob = data_io.load_json(storage_location="users", filename=filename_lower)
            
            if not user_blob:
                #print(f"DEBUG: No user file for {user}")
                continue
                
            user_data = user_blob.get('annotations', {})
            
            if not user_data: 
                #print(f"DEBUG: Empty tag data for {user}")
                continue
            
            #print(f"DEBUG: Loaded {len(user_data)} items for {user}")
            
            for item_id, item_vars in user_data.items():
                str_id = str(item_id)
                
                # --- Simple Map (All tags flattened) ---
                if str_id not in simple_map: simple_map[str_id] = set()
                
                # --- Detailed Map ---
                if str_id not in detailed_map: detailed_map[str_id] = {}
                
                for var, val in item_vars.items():
                    # Parse Special Keys
                    real_var = var
                    type_ = 'tags'
                    
                    if var.endswith('__NOTES'):
                        real_var = var[:-7]
                        type_ = 'notes'
                    elif var.endswith('__CLOSED_TAGGING'):
                        real_var = var[:-16]
                        type_ = 'closed'
                    
                    # Ensure struct
                    if real_var not in detailed_map[str_id]: detailed_map[str_id][real_var] = {}
                    if user not in detailed_map[str_id][real_var]: 
                        detailed_map[str_id][real_var][user] = {'tags': [], 'notes': None, 'closed': None}
                    
                    entry = detailed_map[str_id][real_var][user]
                    
                    if type_ == 'tags':
                        if isinstance(val, list):
                            simple_map[str_id].update(val)
                            entry['tags'] = val
                    elif type_ == 'notes':
                        entry['notes'] = val
                    elif type_ == 'closed':
                        entry['closed'] = val
                        
        except Exception as e:
            print(f"Error loading tokens for {user}: {e}")
            
    return simple_map, detailed_map




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
        #var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if True:#var_schema_path.exists():
            df = fyp_cf["var_schema"].copy() #pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
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



def check_and_update_timeline_cache(collection_id, viz_vars, verbose=False, preloaded_df=None):
    """
    Ensures that timeline aggregation for day exists in cache.
    If not, calculates it from the unified collection dataset.
    """

    intervals = ['day']
    missing = []
    
    # Check if files exist and have the required viz_vars
    for interval in intervals:
        filename = f"timeline_{collection_id}_{interval}.parquet"
        if not data_io.exists(storage_location="cache", filename=filename):
            missing.append(interval)
        else:
            try:
                # Basic schema check: make sure machine_state is actually in the cached datasets
                existing_df = data_io.load_parquet(storage_location="cache", filename=filename, columns=['period'])
                # If we could load the whole thing to check columns, it's slow. We can just check the schema safely:
                schema = data_io.get_parquet_schema(storage_location="cache", filename=filename) # Assuming get_parquet_schema exists, or we just load 1 row
            except Exception:
                pass # Just let missing logic handle it or below
            
            try:
                # Cheaper check: load 1 row to get columns
                sample_df = data_io.load_parquet(storage_location="cache", filename=filename) # Should ideally be rows=1 but this works
                if 'machine_state_counts' not in sample_df.columns:
                     if interval not in missing: missing.append(interval)
            except Exception as e:
                if interval not in missing: missing.append(interval)

    if not missing:
        if verbose:
            print(f"    [TIMELINE] Using cached timeline data for {collection_id}")
        return True # All good
            
    # Generate Data
    # 1. Load Unified Dataset
    if preloaded_df is not None:
        if verbose:
            print(f"    [TIMELINE] Using locally provided dataframe for {collection_id} (shape: {preloaded_df.shape})")
        df = preloaded_df.copy()
    else:
        df = create_collection_unified_dataset(collection_id=collection_id, verbose=False)
        
    if df is None or df.empty:
        print("ERROR: Could not load unified dataset for collection", collection_id)
        return False
        
    # Ensure date column
    date_col = 'local_date'
    if date_col not in df.columns:
         print(f"ERROR: {date_col} missing in unified dataset")
         return False
         
    df[date_col] = pd.to_datetime(df[date_col]).astype('datetime64[ns]')
    
    # Filter for Watch events only removed - now processing all events
    # Construct 'machine_state'
    if 'scraped_ok' in df.columns and 'scraped_fail' in df.columns and 'annotated_ok' in df.columns:
        df['machine_state'] = '1: Activity data only'
        df.loc[df['scraped_fail'] == True, 'machine_state'] = '2: Scrape failed'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'].isna()), 'machine_state'] = '3: Scrape ok, not tried MA'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'] == False), 'machine_state'] = '4: Scrape ok, MA failed'
        df.loc[(df['scraped_ok'] == True) & (df['annotated_ok'] == True), 'machine_state'] = '5: Scrape ok, MA ok'

    # ---------------------------------------------------------
    # 2. Iterate and Aggregate
    # We allow regenerating ALL intervals if any is missing to keep them in sync, 
    # or just missing. Let's do all to be safe if one is missing (might be stale).
    # Actually user said "check ... and if they are not there", implying lazy load.
    # But loading unified is expensive, so efficiently do all 3 if we loaded it.
    
    for interval in intervals:
        
        # Grouping
        temp_df = df.copy()
        temp_df['period'] = temp_df[date_col].dt.date.astype(str)
             
        group_col = 'period'
        periods = sorted(temp_df[group_col].unique())
        
        # Build Result DataFrame
        # We need a row per period.
        # Columns: period, count (videos), valid_count_{var}, {var}_val (numeric), {var}_counts (json)
        
        agg_data = [] # List of dicts
        
        # Pre-calculate video counts per period
        video_counts = temp_df[group_col].value_counts().to_dict()
        
        # Pre-calculate extra_data counts per period (engagement activity like comment, fave, share)
        has_extra_data_col = 'extra_data' in temp_df.columns
        extra_data_counts: dict[str, int] = {}
        if has_extra_data_col:
            ed_mask = temp_df['extra_data'].notna()
            if 'play_duration' in temp_df.columns:
                ed_mask = ed_mask & temp_df['play_duration'].notna() & (temp_df['play_duration'] != 0)
            extra_data_counts = temp_df[ed_mask].groupby(group_col).size().to_dict()

        for p in periods:
            row = {'period': p, 'video_count': video_counts.get(p, 0)}
            if has_extra_data_col:
                row['extra_data_count'] = extra_data_counts.get(p, 0)
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
                # if var in ['desc_hashtags', 'symbol_and_brands', 'content_categories']:
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
        filename = f"timeline_{collection_id}_{interval}.parquet"
        data_io.save_parquet(df=agg_df, storage_location="cache", filename=filename)

    return True




def get_timeline_data(collection_id, interval='day'):
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

    if 'machine_state' not in viz_vars:
        viz_vars = ['machine_state'] + viz_vars

    # Ensure Cache Exists
    try:
        if not check_and_update_timeline_cache(collection_id, viz_vars):
            print("ERROR: Failed to update timeline cache.")
            return {}
    except Exception as e:
        print(f"ERROR: Failed to update timeline cache: {e}")
        return {}
        
    # Get Counts Metadata (Load all 3 aggs to get lengths)

    period_counts = {}
    
    # Helper to load specific interval
    def load_interval_df(u_interval):
        fname = f"timeline_{collection_id}_{u_interval}.parquet"
        if data_io.exists(storage_location="cache", filename=fname):
            return data_io.load_parquet(storage_location="cache", filename=fname)
        return None

    # Load all to get counts
    aggs = {}
    for inv in ['day']:
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
            dt = pd.to_datetime(d_str)
            lbl = dt.strftime('%d/%m/%y')
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
             
        # Get Display Name
        display_name = schema_map.get(var, {}).get('display_name', var)
        if var == 'machine_state':
            display_name = 'Scrape and Annotation States'
             
        # Metadata common props
        valid_counts = df.get(f"{var}_valid", pd.Series([0]*len(df))).tolist()
        video_counts = df['video_count'].tolist() # Total videos per period
        
        if has_val:
            # Numeric
            # Using list comprehension to avoid PyArrow Result bug with .where(..., None)
            vals = [None if pd.isna(x) else float(x) for x in df[f"{var}_val"]]
            variables[var] = {
                "type": "numeric",
                "values": vals,
                "log": use_log,
                "daily_valid_counts": valid_counts,
                "daily_video_counts": video_counts,
                "display_name": display_name
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
            
            ignore_cats = {
                fyp_cf.get('labels', {}).get('OTHER_THINGS', 'Other things'),
                fyp_cf.get('labels', {}).get('UNABLE_TO_DETECT', 'Unable to detect'),
                fyp_cf.get('labels', {}).get('NOT_CODED', 'Not coded')
            }
            
            for json_str in df[f"{var}_counts"]:
                try:
                    if json_str and isinstance(json_str, str):
                        c_dict = json.loads(json_str)
                        for igc in ignore_cats:
                            c_dict.pop(igc, None)
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
                "top_categories": top_cats if var == 'machine_state' else top_cats[:3],
                "default_all": True if var == 'machine_state' else False,
                "display_name": display_name
            }

    # Extra-data (engagement activity) counts per period
    extra_data_counts = df['extra_data_count'].tolist() if 'extra_data_count' in df.columns else None

    result = {"dates": dates, "date_labels": date_labels, "variables": variables, "counts": period_counts, "variables_order": viz_vars}

    if extra_data_counts is not None:
        result["extra_data_counts"] = extra_data_counts

    # Attach pre-computed analysis data if available, or generate if missing
    analysis_fname = f"timeline_analysis_{collection_id}_{interval}.json"
    try:
        if data_io.exists(storage_location="cache", filename=analysis_fname):
            analysis = data_io.load_json(storage_location="cache", filename=analysis_fname)
            if analysis:
                result["analysis"] = analysis
        else:
            # Analysis is missing, generate it on the fly
            from fyp.timeline_analysis import analyse_timeline
            
            # Try to fetch first_activity_date from ddp_metadata.parquet
            first_date = None
            try:
                if data_io.exists(storage_location="recoded", filename="ddp_metadata.parquet"):
                    ddp_meta = data_io.load_parquet(storage_location="recoded", filename="ddp_metadata.parquet", verbose=False)
                    if ddp_meta is not None:
                        # Check index or column for collection_id
                        if ddp_meta.index.name == 'collection_id' or ddp_meta.index.name is None:
                            mask = ddp_meta.index.astype(str) == str(collection_id)
                        elif 'collection_id' in ddp_meta.columns:
                            mask = ddp_meta['collection_id'].astype(str) == str(collection_id)
                        else:
                            mask = ddp_meta.index.astype(str) == str(collection_id)
                            
                        row = ddp_meta[mask]
                        if not row.empty:
                            if ('personas', 'first_event_ts') in row.columns:
                                ts = row[('personas', 'first_event_ts')].iloc[0]
                                if pd.notna(ts):
                                    first_date = str(ts)[:10]
                            elif 'first_event_ts' in row.columns:
                                ts = row['first_event_ts'].iloc[0]
                                if pd.notna(ts):
                                    first_date = str(ts)[:10]
            except Exception as e:
                print(f"Warning: Could not get first_event_ts for analysis generation: {e}")

            analysis = analyse_timeline(result, interval=interval, first_activity_date=first_date)
            if analysis:
                data_io.save_json(analysis, storage_location="cache", filename=analysis_fname)
                result["analysis"] = analysis

    except Exception as e:
        print(f"Warning: Could not load or generate analysis for {collection_id}/{interval}: {e}")

    return result


def load_display_id_map():
    """
    Loads collection_annotations.json and returns a map of { raw_id: display_id }.
    """
    mapping = {}
    da_filename = "collection_annotations.json"
    try:
        if data_io.exists(storage_location="recoded", filename=da_filename):
            annotations = data_io.load_json(storage_location="recoded", filename=da_filename) or {}
            for raw_id, data in annotations.items():
                disp = data.get('display_collection_id')
                if disp and str(disp).strip():
                    mapping[str(raw_id)] = str(disp).strip()
    except Exception as e:
        print(f"Error loading display id map: {e}")
    return mapping



def get_study_collections(study):
    """
    Returns a list of unique collections present in the study dataset.
    Returns: [{ 'collection_id': '...', }, ...]
    """

    if not "study_defs" in fyp_cf:
        init_study_defs()

    if not study in fyp_cf["study_defs"]:
        return []

    selected_collections = fyp_cf["study_defs"][study].get("SELECTED_DONATIONS", [])

    selected_collections = [{"collection_id": str(d).strip()} for d in selected_collections]

    return selected_collections



    """try:
        
        recoded_file = f"{study}_recoded.parquet"
        if data_io.exists(storage_location="cache", filename=recoded_file):
             df = data_io.load_parquet(
                 storage_location="cache", 
                 filename=recoded_file, 
             )
             #print(f"DEBUG DONATIONS: Loaded fast columns for {study}: shape={df.shape}")
        else:
             # Fallback to full load if cache missing (triggering creation)
             df, _ = get_explorer_data(study, context="explorer")
             
             
        if df is None:
            #print(f"DEBUG DONATIONS: df is None for {study}")
            return []
        
        if 'collection_id' not in df.columns:
            print(f"ERROR: collection_id not found in df for {study}")
            return []

        # Unique collections
        collections = df[['collection_id']].drop_duplicates()
        
        # Format for frontend
        result = []
        for _, row in collections.iterrows():
            item = {'collection_id': row['collection_id']}
            result.append(item)
            
        return sorted(result, key=lambda x: str(x.get('collection_id', '')))
        
    except Exception as e:
        print(f"Error getting study collections: {e}")
        return []"""



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
        comp_inter_filename = f"{study_name}_comp_interpretations.json"

        if data_io.exists(storage_location="cache", filename=pca_filename) and data_io.exists(storage_location="cache", filename=comp_inter_filename):         
            print("Loading PCA scores for study from cache: ", study_name)
            events_pca_scores_scaled = data_io.load_parquet(
                storage_location="cache",
                filename=pca_filename,
                )

        else:
            print("Calculating PCA scores for study: ", study_name)
            events_pca_scores_scaled, _ = calculate_scaled_pca_scores(
                study_name = study_name,
                study_recoded_dataset = None,
                minimum_group_size = 10,
                target_explained_variance = 0.8,
                drop_rare_globally_below = 0.01,
            )
            if events_pca_scores_scaled is None:
                return None
            data_io.save_parquet(
                df=events_pca_scores_scaled,
                storage_location="cache",
                filename=pca_filename,
            )

        pca_df_cache[study_name] = events_pca_scores_scaled
        return events_pca_scores_scaled

    if False: #except Exception as e:
        print(f"Error loading PCA for {study_name}: {e}")
        return None



def get_accessible_studies(username, role, is_admin):
    """
    Returns a list of study names that the user has access to.
    """
    from fyp.studies import init_study_defs
    
    if not 'study_defs' in fyp_cf:
        init_study_defs()

    #print(f"DEBUG ACCESS: Checking access for user={username}, role={role}, admin={is_admin}")
    accessible_studies = []
    
    if 'study_defs' in fyp_cf:
        for study_name, study_config in fyp_cf['study_defs'].items():
            # 1. Admin Override
            if is_admin:
                has_access = True
            else:
                user_access = study_config.get('USER_ACCESS')

                # 2. Missing or Empty => Default Allow
                if not user_access:
                    has_access = True

                # Ensure it is a list
                elif not isinstance(user_access, list):
                    has_access = True

                # 3. 'all' keyword
                elif 'all' in user_access:
                    has_access = True

                # 4. Role Match
                elif role in user_access:
                    has_access = True

                # 5. Username Match
                elif username in user_access:
                    has_access = True
                else:
                    has_access = False

            if has_access:
                # Data Integrity Checks
                if not data_io.exists(storage_location="cache", filename=f"{study_name}_recoded.parquet"):
                    continue
                
                stats = study_config.get('stats', {})
                #print("DEBUG: stats = ", stats)
                #print("DEBUG: unique_videos = ", stats.get('unique_videos', 0))
                if stats.get('unique_videos', 0) <= 0:
                    continue

                accessible_studies.append(study_name)
    
    #print(f"DEBUG ACCESS: Accessible studies found: {accessible_studies}")
    return sorted(accessible_studies)




def load_schema_metadata(metadata):
    """Helper to load and inject schema metadata (priorities, descriptions, accepted_labels) from CSV."""
    try:
        #var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if True: #var_schema_path.exists():
            schema_df = fyp_cf["var_schema"].copy() #= pd.read_csv(var_schema_path, dtype_backend="pyarrow")
            
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

                # Add Sortable (for sort dropdown in viewer)
                if 'sortable' in row:
                    sval = row['sortable']
                    if pd.notna(sval):
                        schema_map[var_name]['sortable'] = int(sval)

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


def calculate_inter_coder_reliability():
    """
    Calculates inter-coder reliability (Agreement % and Cohen's Kappa) for closed tags.
    Returns a dictionary of stats.
    """
    
    # 1. Load Schema to identify accepted labels and closed variables
    meta = {}
    load_schema_metadata(meta)
    schema_map = meta.get('schema_map', {})
    
    # Identify Variables with accepted_labels (Closed Tags)
    closed_vars = {}
    for var, details in schema_map.items():
        if 'accepted_labels' in details and details['accepted_labels']:
            closed_vars[var] = details['accepted_labels']

    if not closed_vars:
        return {"error": "No closed tagging variables found in schema."}

    # 2. Load All User Annotations
    user_files = []
    try:
        # We assume 'users' storage location is set up in data_io
        # Listing files in users directory
        all_files = data_io.listdir(storage_location='users')
        user_files = [f for f in all_files if f.endswith('.json') and not f.endswith('_tags.json')]
    except Exception as e:
        print(f"Error listing users: {e}")
        return {"error": f"Error listing users: {str(e)}"}

    if not user_files:
        return {"error": "No user files found."}

    # 3. Aggregate Data
    all_data = []

    for uf in user_files:
        username = uf.replace('.json', '')
        try:
            user_blob = data_io.load_json(storage_location='users', filename=uf)
            if not user_blob: continue
            
            annotations = user_blob.get('annotations', {})
            
            for item_id, item_vars in annotations.items():
                for var_key, val in item_vars.items():
                    # Handle variable naming conventions (e.g. VarName__CLOSED_TAGGING)
                    real_var = var_key
                    if var_key.endswith('__CLOSED_TAGGING'):
                         real_var = var_key[:-16]
                    
                    if real_var in closed_vars:
                         cleaned_val = None
                         if isinstance(val, list):
                             # Multi-label handling: For Kappa, we ideally need single labels.
                             # If specific requirement isn't set, we treat single-element lists as the value,
                             # and multi-element lists as a combined string to allow exact match agreement check.
                             if len(val) == 1:
                                 cleaned_val = val[0]
                             elif len(val) > 1:
                                 cleaned_val = ",".join(sorted(val))
                         else:
                             cleaned_val = str(val)
                             
                         if cleaned_val:
                             all_data.append({
                                 "item_id": str(item_id),
                                 "variable": real_var,
                                 "user": username,
                                 "value": cleaned_val
                             })
                             
        except Exception as e:
            print(f"Error loading {uf}: {e}")
            continue

    if not all_data:
         return {"error": "No closed tags found in user files."}

    df = pd.DataFrame(all_data)

    # 4. Compute Statistics Per Variable
    results = []
    
    # We define Consensus as the Mode (Most Common) tag for each item.
    
    unique_vars = sorted(df['variable'].unique())
    
    for var in unique_vars:
        var_df = df[df['variable'] == var]
        
        # Calculate Consensus (Mode) per Item
        item_groups = var_df.groupby('item_id')['value']
        consensus_map = {}
        
        for item, group in item_groups:
            modes = group.mode()
            if not modes.empty:
                consensus_val = sorted(modes.tolist())[0]
                consensus_map[item] = consensus_val
                
        # Calculate Stats Per User
        users = sorted(var_df['user'].unique())
        
        user_agreements = []
        user_kappas = []
        user_n_items = []
        
        for u in users:
            user_subset = var_df[var_df['user'] == u]
            
            y_true = [] # Consensus
            y_pred = [] # User
            
            common_items = 0
            
            for _, row in user_subset.iterrows():
                iid = row['item_id']
                val = row['value']
                
                if iid in consensus_map:
                    cons_val = consensus_map[iid]
                    y_true.append(cons_val)
                    y_pred.append(val)
                    common_items += 1
            
            if common_items == 0:
                continue
            
            user_n_items.append(common_items)
            
            # Percent Agreement
            agreement = np.mean(np.array(y_true) == np.array(y_pred))
            user_agreements.append(agreement)
            
            # Cohen's Kappa - simplified
            kappa = 0.0
            if common_items > 1 and len(set(y_true)) > 1: # Need variation for Kappa
                try:
                     kappa = cohen_kappa_score(y_true, y_pred)
                     if pd.isna(kappa): kappa = 0.0
                except:
                    kappa = 0.0
            elif common_items > 0 and y_true == y_pred:
                 # Perfect agreement on single item or constant values
                 # Technically Kappa is undefined or 0, but Agreement is 1.0. 
                 # We'll treat Kappa as 1.0 for perfect match to not punish consistency?
                 # No, standard is 0 if expected==observed.
                 # Let's keep it 0.0 but rely on Agreement for interpretation.
                 kappa = 0.0 
                 # Wait, if I have 1 item and I match, Agreement is 100%. Kappa is undefined.
                 pass

            user_kappas.append(kappa)
            
        if user_agreements:
            avg_agreement = np.mean(user_agreements)
            avg_kappa = np.mean(user_kappas)
            avg_n = np.mean(user_n_items)
            
            results.append({
                "variable": var,
                "avg_agreement": round(avg_agreement * 100, 1),
                "avg_kappa": round(avg_kappa, 3),
                "n_raters": len(users),
                "avg_items": round(avg_n, 1)
            })
            
    # Sort results
    results.sort(key=lambda x: x['variable'])

    return {"results": results}
