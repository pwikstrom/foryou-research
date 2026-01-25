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



def get_timeline_data(study, donation_id, interval='day'):
    """
    Returns timeline data for plotting.
    - Numeric: Daily Mean (Raw values, invalid/missing ignored). 
      Includes metadata if log scale is requested.
    - Categorical: Daily Counts per category + Daily Total Count (for % calc).
    """
    import fyp.data_io as data_io
    from fyp import fyp_main

    cf = fyp_main.initialize()
    if 'var_schema' not in cf:
        print("ERROR: var_schema missing")
        return {}

    schema = cf.get('var_schema', {})
    
    # Use load_schema_metadata helper to get priority list reliably
    meta = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])
    
    print(f"DEBUG TIMELINE: load_schema_metadata found {len(viz_vars)} viz_vars (timeline_prio): {viz_vars}")
    
    # Also load schema map for log config
    schema_map = meta.get('schema_map', {})

    # Load Data (using our cache logic)
    filename = f"{study}_recoded.parquet"
    if not data_io.exists(cf, "cache", filename):
        print("Recoded data not found for timeline")
        return {}

    df = data_io.load_parquet(cf, "cache", filename)
    
    # Filter for Donation
    # Use D_donation_id
    if 'D_donation_id' not in df.columns:
        print("DEBUG TIMELINE: D_donation_id column missing in df keys:", df.columns.tolist())
        print("D_donation_id column missing")
        return {}
        
    subset = df[df['D_donation_id'] == donation_id].copy()
    
    # Filter for Watch events only
    if 'D_feature_name' in subset.columns:
        subset = subset[subset['D_feature_name'] == 'watch']
        
    print(f"DEBUG TIMELINE: Filtering {study} for donation {donation_id} (interval={interval}). Rows found: {len(subset)}")
    
    if subset.empty:
        return {"dates": [], "variables": {}, "counts": {"day": 0, "week": 0, "month": 0}}

    # Date Handling
    date_col = 'T_local_date'
    if date_col not in subset.columns:
        print("T_local_date missing")
        return {}
    
    # Ensure correct datetime conversion
    # Force to standard numpy datetime to avoid ArrowTemporalProperties issues (no to_period)
    subset[date_col] = pd.to_datetime(subset[date_col]).astype('datetime64[ns]')

    # Calculate Counts for Metadata (available periods)
    # Using 'period' logic ensures consistency
    n_days = subset[date_col].dt.date.nunique()
    n_weeks = subset[date_col].dt.to_period('W').nunique()
    n_months = subset[date_col].dt.to_period('M').nunique()
    
    period_counts = {
        "day": n_days,
        "week": n_weeks,
        "month": n_months
    }

    # Generate Grouping Column based on interval
    if interval == 'week':
        # Start of week
        subset['period'] = subset[date_col].dt.to_period('W').apply(lambda r: r.start_time).dt.date.astype(str)
    elif interval == 'month':
        # Start of month
        subset['period'] = subset[date_col].dt.to_period('M').apply(lambda r: r.start_time).dt.date.astype(str)
    else: # day
        subset['period'] = subset[date_col].dt.date.astype(str)
        
    group_col = 'period'

    # Get Date Range

    # Get Date Range
    dates = sorted(subset[group_col].unique())
    print(f"DEBUG TIMELINE: Found {len(dates)} {interval}s.")
    
    # Generate Formatted Labels
    date_labels = []
    for d_str in dates:
        try:
            dt = pd.to_datetime(d_str)
            if interval == 'day':
                # dd/mm/yy
                lbl = dt.strftime('%d/%m/%y')
            elif interval == 'week':
                # yyyy-ww
                # Use %V for ISO week number
                lbl = dt.strftime('%Y-%V')
            elif interval == 'month':
                # mmm-yy
                lbl = dt.strftime('%b-%y')
            else:
                lbl = d_str
            date_labels.append(lbl)
        except:
            date_labels.append(d_str)

    variables = {}
    
    # Deduplicate columns in subset to avoid weirdness
    subset = subset.loc[:, ~subset.columns.duplicated()]

    # Global Column Types Helper
    def get_col_type(c):
        dt = subset[c].dtype
        if pd.api.types.is_numeric_dtype(dt) and not pd.api.types.is_bool_dtype(dt):
            return 'number'
        if isinstance(dt, pd.ArrowDtype) and 'list' in str(dt):
            return 'list'
        
        # Check explicit list check like explorer
        first_val = subset[c].dropna().iloc[0] if not subset[c].dropna().empty else None
        if isinstance(first_val, list):
            return 'list'
            
        return 'category'

    col_types = {v: get_col_type(v) for v in viz_vars if v in subset.columns}

    for var in viz_vars:
        if var not in subset.columns:
            continue
            
        try:
            # print(f"DEBUG TIMELINE: Processing Var {var}")
            is_numeric = (col_types.get(var) == 'number')
            
            if is_numeric:
                s_nums = pd.to_numeric(subset[var], errors='coerce')
                
                # Strict DataFrame construction for groupby
                temp_df = pd.DataFrame({
                    'date': subset[group_col].values,
                    'val': s_nums.values
                })
                
                # Mean per period
                daily_means = temp_df.groupby('date')['val'].mean()
                daily_means = daily_means.reindex(dates) # Keep NaNs for gaps
                
                # Check for Log Scale preference
                use_log = False
                if schema_map.get(var, {}).get('web_viz_log') == 'yes':
                    use_log = True
                elif isinstance(schema, dict) and schema.get(var, {}).get('web_viz_log') == 'yes':
                    use_log = True

                variables[var] = {
                    "type": "numeric",
                    "values": daily_means.where(pd.notnull(daily_means), None).tolist(),
                    "log": use_log
                }
            
            else:
                # Categorical: Counts + Period Totals (for shares)
                is_list = (col_types.get(var) == 'list')
                
                # Period Total Calculation (Number of items per period)
                daily_video_counts = subset[group_col].value_counts().reindex(dates, fill_value=0).sort_index()

                if is_list:
                    # Explode! safely
                    temp_pre_explode = pd.DataFrame({
                        'date': subset[group_col],
                        'var': subset[var]
                    })
                    exploded_df = temp_pre_explode.explode('var')
                    
                    s_for_counts = exploded_df['var']
                    date_for_counts = exploded_df['date']
                    
                    # Total items (tags) per period
                    daily_total_items = date_for_counts.value_counts().reindex(dates, fill_value=0).sort_index()
                    
                else:
                    s_for_counts = subset[var]
                    date_for_counts = subset[group_col]
                    temp_df = pd.DataFrame({'date': date_for_counts, 'var': s_for_counts})
                    daily_total_items = daily_video_counts # Same for single cat
                
                # Calculate VALID Counts (non-NA rows) for Denominator
                if is_list:
                     valid_mask = subset[var].apply(lambda x: isinstance(x, list) and len(x) > 0) 
                     valid_mask = subset[var].notna() # Using same simplified logic as before
                else:
                     valid_mask = subset[var].notna()
                
                daily_valid_counts = subset.loc[valid_mask, group_col].value_counts().reindex(dates, fill_value=0).sort_index()
                
                # Top categories logic
                top_counts = s_for_counts.value_counts(dropna=True).head(50).index.tolist()
                
                # Filter to top 50
                if is_list:
                    sub_cat = exploded_df[exploded_df['var'].isin(top_counts)]
                else:
                    sub_cat = temp_df[temp_df['var'].isin(top_counts)]
                
                if sub_cat.empty:
                     daily_counts_dict = []
                else:
                    daily_counts = pd.crosstab(sub_cat['date'], sub_cat['var'])
                    daily_counts = daily_counts.reindex(dates, fill_value=0)
                    daily_counts_dict = daily_counts.to_dict(orient='records')

                variables[var] = {
                    "type": "categorical",
                    "counts": daily_counts_dict,
                    "daily_video_counts": daily_video_counts.to_list(),
                    "daily_valid_counts": daily_valid_counts.to_list(),
                    "daily_item_counts": daily_total_items.to_list(),
                    "top_categories": top_counts[:3]
                }
        except Exception as e:
            print(f"DEBUG TIMELINE: Error processing var {var}: {e}")
            import traceback
            traceback.print_exc()
            continue

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
    #from os.path import exists as os_exists, join as os_join
    #from pandas import read_parquet as pd_read_parquet
    import fyp.data_io as data_io

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
