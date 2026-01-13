import pandas as pd
import ast
import numpy as np
import fyp.data_io as data_io





def load_data_old(fyp_cf, study, verbose=False):
    from numpy import ndarray as np_ndarray
    #from pandas import read_parquet as pd_read_parquet
    #from os.path import join as os_join, exists as os_exists
    from fyp.organize_datasets import create_study_recoded_dataset

    df = None
    #recoded_cache_path = os_join(fyp_cf['paths']['cache'], f"{study}_recoded.parquet")
    if data_io.exists(
        cf=fyp_cf,
        storage_location = "cache",
        filename = f"{study}_recoded.parquet",
        verbose=verbose
        ):
        df = data_io.load_parquet(
            cf=fyp_cf,
            storage_location="cache",
            filename=f"{study}_recoded.parquet",
            verbose=verbose,
            )
    else:
        print("@@ No cached recoded study dataset found. I must run the recoding process to create it. Please wait a moment...")
        df = create_study_recoded_dataset(
            cf = fyp_cf,
            study_name = study,
            load_from_cache = True,
            save_to_cache = True,
            verbose = verbose
        )
        print("@@ Back after finalising the recoding process. I will now resume loading the data.")

    if df is None:
        print("    ERROR: This process cannot run without a study dataset. Process failed.")
        return None, {}

    column_types = {}

    for col in df.columns:
        # Check first non-null value
        first_valid_index = df[col].first_valid_index()
        if first_valid_index is None:
            column_types[col] = "category"
            continue
        
        val = df[col].loc[first_valid_index]

        # 1. Check for List (actual list or stringified)
        if isinstance(val, (list, np_ndarray)):
            column_types[col] = "list"
        elif isinstance(val, str) and val.strip().startswith('[') and val.strip().endswith(']'):
            try:
                # Attempt to parse entire column as list
                df[col] = df[col].apply(lambda x: ast.literal_eval(x) if isinstance(x, str) and x.strip().startswith('[') else (x if isinstance(x, (list, np_ndarray)) else []))
                column_types[col] = "list"
            except (ValueError, SyntaxError):
                column_types[col] = "category"
        
        # 2. Check for Number
        elif pd.api.types.is_numeric_dtype(df[col]):
            # Sanity Check: If numbers are huge (e.g. IDs), do not treat as continuous number for plotting
            # 1e15 is a safe upper bound for "counts" or "durations". 
            # Snowflake IDs are ~1e18.
            # Timestamps (ms) are ~1.7e12 (now) to 1e13.
            try:
                # Use max of non-nulls
                max_val = df[col].max()
                if max_val > 1e15:
                    column_types[col] = "identifier" # Or category
                else:
                    column_types[col] = "number"
            except:
                 column_types[col] = "number"
        
        # 3. Check for Long Text (if category/string)
        else:
            # Check average length of non-null values
            sample = df[col].dropna() # don't count null values
            sample = sample[sample!="oThEr tHiNgS-+-"]
            sample = sample.map(lambda x: len(str(x)))
            sample = sample[sample > 0] # don't count empty strings
            sample = sample.head(1000)
            if not sample.empty:
                mean_len = sample.mean()
                if mean_len > 60: 
                    column_types[col] = "long_text"
                else:
                    # Check for High Cardinality (Identifier)
                    # If unique count is very high (>90% of non-null rows) and count > 100
                    n_unique = df[col].nunique()
                    n_rows = len(df[col].dropna())
                    if n_rows > 100 and n_unique > 0.9 * n_rows:
                        column_types[col] = "identifier"
                    else:
                        column_types[col] = "category"
            else:
                column_types[col] = "category"


    return df, column_types





def get_robust_bounds(series):
    """
    Calculates 1st and 99th percentiles to exclude extreme outliers.
    """
    if series.empty:
        return 0, 0
    
    # Drop NaNs
    s = series.dropna()
    if s.empty:
        return 0, 0

    # Calculate min/max (No outlier clipping)
    low = s.min()
    high = s.max()
    
    return float(low), float(high)





def get_metadata(df, column_types):
    """
    Returns metadata for frontend:
    - columns: { name: type }
    - stats: min/max for numbers, unique values for categories, null_counts
    """
    from datetime import datetime
    t1 = datetime.now()
    print("    Calculating things for viewer and explorer...")
    from numpy import ndarray as np_ndarray
    metadata = {}
    for col, dtype in column_types.items():
        # Calculate Null Count
        null_count = int(df[col].isna().sum())
        
        base_meta = {
            "null_count": null_count
        }

        if dtype == "number":
            min_val, max_val = get_robust_bounds(df[col])
            base_meta.update({
                "type": "number",
                "min": min_val,
                "max": max_val
            })
            metadata[col] = base_meta
        elif dtype == "category":
            # Strict limit for UI filters
            # Only send top 50 most frequent values for filtering to save DOM
            vc = df[col].value_counts().head(50)
            
            # Sort alphabetically for consistency
            top_50_keys = sorted(vc.index.tolist(), key=lambda x: str(x))
            
            unique_vals = [{"value": str(x), "count": int(vc[x])} for x in top_50_keys]
            
            base_meta.update({
                "type": "category",
                "values": unique_vals
            })
            metadata[col] = base_meta

        elif dtype == "list":
            # Extract all unique items from lists
            # Flatten
            all_items = []
            for row in df[col].dropna():
                if isinstance(row, (list, np_ndarray)):
                    all_items.extend(row)
            
            # Use Counter to find top 50 tags
            from collections import Counter
            c = Counter(all_items)
            top_50 = c.most_common(50)
            
            # Sort alphabetically
            top_50.sort(key=lambda x: str(x[0]))
            
            items_list = [{"value": str(k), "count": v} for k, v in top_50]

            base_meta.update({
                "type": "list",
                "values": items_list
            })
            metadata[col] = base_meta
        
        # Explicitly ignore long_text and identifier
        elif dtype in ["long_text", "identifier"]:
            continue
    
    print(f"    ...done calculating things for viewer and explorer. Time: {datetime.now()-t1}")
    return metadata





def filter_dataframe(df, column_types, filters, search_query=None):
    from numpy import ndarray as np_ndarray

    filtered_df = df.copy()

    for col, criteria in filters.items():
        if col not in df.columns:
            continue
        
        val = criteria.get("value")
        include_na = criteria.get("na", False)
        
        # If neither value criteria nor NA inclusion is active, skip
        if (val is None or val == "") and not include_na:
            continue

        dtype = column_types.get(col)
        
        # We need to construct a mask for this column that represents:
        # (Matches Values) OR (Is NA if include_na)
        
        value_mask = pd.Series(False, index=filtered_df.index)
        has_value_criteria = False

        if dtype == "number":
            min_val = val.get("min") if val else None
            max_val = val.get("max") if val else None
            if min_val is not None or max_val is not None:
                # Start with True (all valid for now)
                temp_mask = pd.Series(True, index=filtered_df.index)
                if min_val is not None:
                     temp_mask &= (filtered_df[col] >= float(min_val))
                if max_val is not None:
                     temp_mask &= (filtered_df[col] <= float(max_val))
                
                value_mask = temp_mask
                has_value_criteria = True

        elif dtype == "category":
            if isinstance(val, (list, np_ndarray)) and len(val) > 0:
                value_mask = filtered_df[col].astype(str).isin(val)
                has_value_criteria = True
        
        elif dtype == "list":
            if isinstance(val, (list, np_ndarray)) and len(val) > 0:
                search_set = set(val)
                value_mask = filtered_df[col].apply(lambda x: bool(set(x) & search_set) if isinstance(x, (list, np_ndarray)) else False)
                has_value_criteria = True

        # Combine with NA logic
        final_col_mask = pd.Series(False, index=filtered_df.index)
        
        if include_na:
             final_col_mask |= filtered_df[col].isna()
        
        if has_value_criteria:
             final_col_mask |= value_mask

        # Apply mask
        filtered_df = filtered_df[final_col_mask]

    # Global Search Logic
    if search_query and isinstance(search_query, str):
        terms = [t.strip().lower() for t in search_query.split(",") if t.strip()]
        if terms:
            # We want rows where ALL terms appear ANYWHERE in the row
            original_indices = filtered_df.index
            final_mask = pd.Series(True, index=original_indices)
            
            for term in terms:
                term_mask = pd.Series(False, index=original_indices)
                for col in filtered_df.columns:
                    try:
                        mask = filtered_df[col].astype(str).str.contains(term, case=False, na=False)
                        term_mask |= mask
                    except:
                        continue
                final_mask &= term_mask
            
            filtered_df = filtered_df[final_mask]

    return filtered_df





def calculate_adaptive_histogram(data, min_val, max_val, bins=50, max_empty_ratio=0.1):
    """
    Recursively reduces bin count if too many bins are empty.
    """
    counts, bin_edges = np.histogram(data, bins=bins, range=(min_val, max_val), density=True)
    
    # Check emptiness
    # Count bins with 0 data
    empty_bins = np.sum(counts == 0)
    empty_ratio = empty_bins / bins
    
    if empty_ratio > max_empty_ratio and bins > 5:
        # Reduce bins by ~50% (Agilent approach)
        new_bins = int(bins * 0.5)
        # Ensure we don't get stuck if bins * 0.5 rounds to same int (unlikely with >5)
        if new_bins == bins: new_bins -= 1
        return calculate_adaptive_histogram(data, min_val, max_val, bins=new_bins, max_empty_ratio=max_empty_ratio)
    
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    return counts, bin_centers






def get_current_stats_old(df, column_types, viz_config=None):
    from pandas import set_option
    from numpy import ndarray as np_ndarray
    set_option('future.no_silent_downcasting', True)
    """
    Returns robust stats for the (filtered) dataframe.
    viz_config: { var: { 'log': bool, 'bins': int/list/None } }
    """
    count = len(df)
    stats = {}
    if viz_config is None: viz_config = {}

    if count == 0:
        return {"count": 0, "stats": {}}

    for col, dtype in column_types.items():
        if dtype == "number":
             # Check for Discrete Integer
             # If integer type and distinct count is low
             is_integer = pd.api.types.is_integer_dtype(df[col])
             n_unique = df[col].nunique()
             
             if is_integer and n_unique < 20:
                 # Treat as category for plotting (Bar Chart)
                 vc = df[col].value_counts().sort_index().to_dict()
                 # Convert keys to str for JSON consistency
                 stats[col] = {str(k): v for k, v in vc.items()}
                 continue

             # Continuous Variable - Density Plot
             # Use robust bounds to exclude outliers
             try:
                 series = df[col][df[col]>=0].dropna()
             except Exception as e:
                 print(f"Error filtering {col}: {e}")
                 stats[col] = {}
                 continue

             if series.empty:
                 stats[col] = {"type": "density", "x": [], "y": []}
                 continue
             
             # Calculate Mean, Std, Count (Safe for JSON)
             try:
                 import math
                 val = float(series.mean())
                 mean_val = val if math.isfinite(val) else None
                 
                 std_val = float(series.std())
                 std_val = std_val if math.isfinite(std_val) else None
                 
                 count_val = int(len(series))
             except:
                 mean_val = None
                 std_val = None
                 count_val = 0

             min_val, max_val = get_robust_bounds(series)
                 
             # Check Skewness & Transform
             transform = "linear"
             
             # Config Override for LOG
             use_log = False
             if col in viz_config and viz_config[col].get('log'):
                 use_log = True
             else:
                 # Default Logic (if not strictly specified to NO? User said "if yes -> log, otherwise not")
                 # This implies we ONLY log if yes. So disable auto-skew check?
                 # "web_viz_log: if this column is 'yes' then the variable should be logged, otherwise not."
                 # This implies strict override. 
                 use_log = False
                 
             if use_log:# and min_val >= 0:
                 transform = "log10"
             
             # Clamp data (original domain)
             clamped_series = series.clip(lower=min_val, upper=max_val)
             
             
             # Calculate Histogram
             try:
                if min_val == max_val:
                     # Constant value
                     # For log10, we'd plot at log10(val+1)
                     x_val = np.log10(min_val + 1) if transform == "log10" else min_val
                     
                     stats[col] = {
                        "type": "density",
                        "x": [float(x_val)],
                        "y": [float(len(clamped_series))],
                        "transform": transform,
                        "min": min_val,
                        "max": max_val,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                    }
                     continue
                
                # Bin Configuration
                # Default: 10 bins (User request)
                # Config: int or list
                bins_arg = 10 
                adaptive = True # Default to adaptive? User said "defaults to 10". 
                # "If a numerical plot doesn't have a value ... default to 10"
                # If explicit bins are given, likely we shouldn't adaptively reduce them?
                
                if col in viz_config and viz_config[col].get('bins') is not None:
                     bins_arg = viz_config[col]['bins']
                     adaptive = False # explicit bins -> disable adaptive
                elif col not in viz_config: 
                     # Should we keep adaptive for default 10? 
                     # Start with 10. If empty, reducing to 5 is fine.
                     adaptive = True
                
                if transform == "log10":
                    # Transform data: log10(x + 1)
                    log_data = np.log10(clamped_series + 1)
                    log_min = np.log10(min_val + 1)
                    log_max = np.log10(max_val + 1)
                    
                    # Histogram in log domain (linear bins in log space)
                    if adaptive and isinstance(bins_arg, int):
                         counts, bin_centers = calculate_adaptive_histogram(log_data, log_min, log_max, bins=bins_arg)
                    else:
                         # Explicit bins (int or list)
                         # If list (edges), we need to ensure they are in log domain? 
                         # User config is likely in ORIGINAL domain.
                         # "10|30|50"
                         # If log, edges need transform. 
                         chosen_bins = bins_arg
                         if isinstance(chosen_bins, (list, np_ndarray)):
                             # Transform edges to log
                             chosen_bins = [np.log10(b + 1) for b in chosen_bins]
                             # Add min/max to edges if not covering range? 
                             # np.histogram handles range if bins is int. If bins is sequence, it defines edges.
                             
                         counts, bin_edges = np.histogram(log_data, bins=chosen_bins, range=(log_min, log_max) if isinstance(chosen_bins, int) else None, density=True)
                         bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

                    
                    # Generate Custom Ticks for Log Axis (Original Values)
                    # We want ticks at 0, 1, 10, 100, 1000...
                    tick_vals = []
                    tick_text = []
                    
                    # 0
                    if 0 >= min_val and 0 <= max_val:
                        tick_vals.append(np.log10(1))
                        tick_text.append("0")
                    
                    # Powers of 10
                    p = 0
                    while True:
                        v = 10**p
                        if v > max_val:
                            break
                        if v >= min_val:
                            tick_vals.append(np.log10(v + 1))
                            tick_text.append(f"{v:,}") # Add comma separator
                        p += 1
                        
                    stats[col] = {
                        "type": "density",
                        "x": bin_centers.tolist(), # Transformed if log
                        "y": counts.tolist(),
                        "transform": transform,
                        "min": min_val, # Original units
                        "max": max_val,  # Original units
                        "tick_vals": tick_vals,
                        "tick_text": tick_text,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                    }
                    continue # Use continue to skip the default stats assignment below
                else:
                    # Linear Bins in original domain
                    if adaptive and isinstance(bins_arg, int):
                        counts, bin_centers = calculate_adaptive_histogram(clamped_series, min_val, max_val, bins=bins_arg)
                    else:
                        counts, bin_edges = np.histogram(clamped_series, bins=bins_arg, range=(min_val, max_val) if isinstance(bins_arg, int) else None, density=True)
                        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
                
                stats[col] = {
                    "type": "density",
                    "x": bin_centers.tolist(), # Transformed if log
                    "y": counts.tolist(),
                    "transform": transform,
                    "min": min_val, # Original units
                    "max": max_val,  # Original units
                    "mean": mean_val,
                    "std": std_val,
                    "count": count_val
                }
             except Exception as e:
                 print(f"Error calculating histogram for {col}: {e}")
                 stats[col] = {}
        
        elif dtype == "category":
            # Cap value counts for charts to Top 20
            # Sending thousands of bars crashes frontend
            vc = df[col].value_counts().head(20).to_dict()
            stats[col] = vc

        elif dtype == "list":
            # Flatten and count
            all_items = []
            for row in df[col].dropna():
                    if isinstance(row, (list, np_ndarray)):
                        all_items.extend(row)
            
            from collections import Counter
            # Cap list items to Top 20
            stats[col] = dict(Counter(all_items).most_common(20))
             
    # DEBUG LOGGING
    """try:
        with open("debug_explorer_stats.txt", "w") as f:
            f.write(f"Count: {count}\n")
            f.write(f"Columns in stats: {list(stats.keys())}\n")
            for k, v in stats.items():
                f.write(f"{k}: Type={v.get('type', 'Category')}, Mean={v.get('mean')}, Error={v == {}}\n")
    except:
        pass"""

    return {"count": count, "stats": stats}





def load_data(fyp_cf, study, verbose=False):
    from numpy import ndarray as np_ndarray
    from fyp.organize_datasets import create_study_recoded_dataset

    print("    Loading study data for viewer and explorer...")
    df = None

    if data_io.exists(
        cf=fyp_cf,
        storage_location = "cache",
        filename = f"{study}_recoded.parquet",
        verbose=verbose
        ):
        df = data_io.load_parquet(
            cf=fyp_cf,
            storage_location="cache",
            filename=f"{study}_recoded.parquet",
            verbose=verbose,
            )
    else:
        print("@@ No cached recoded study dataset found. I must run the recoding process to create it. Please wait a moment...")
        df = create_study_recoded_dataset(
            cf = fyp_cf,
            study_name = study,
            load_from_cache = True,
            save_to_cache = True,
            verbose = verbose
        )
        print("@@ Back after finalising the recoding process. I will now resume loading the data.")

    if df is None:
        print("    ERROR: This process cannot run without a study dataset. Process failed.")
        return None, {}
    
    
    column_types = {}
    
    # Bulk type inspection
    for col in df.columns:
        dtype = df[col].dtype
        is_list = False
        is_numeric = pd.api.types.is_numeric_dtype(dtype)
        
        if not is_numeric:
            # Check for actual lists
            if isinstance(dtype, pd.ArrowDtype) and 'list' in str(dtype):
                 is_list = True
            
            if not is_list:
                # Heuristic: Check first valid value
                first_idx = df[col].first_valid_index()
                if first_idx is not None:
                    first_val = df[col].loc[first_idx]
                    if isinstance(first_val, (list, np_ndarray)):
                        is_list = True
                    elif isinstance(first_val, str):
                        s_val = first_val.strip()
                        if s_val.startswith('[') and s_val.endswith(']'):
                           try: 
                               # Convert to object to use python apply? or string accessor?
                               # Only apply if it actually looks like a list
                               df[col] = df[col].apply(lambda x: ast.literal_eval(x) if isinstance(x, str) and x.strip().startswith('[') else (x if isinstance(x, (list, np_ndarray)) else []))
                               is_list = True
                           except:
                               pass

        if is_list:
            column_types[col] = "list"
            continue

        if is_numeric:
             try:
                 max_val = df[col].max()
                 if pd.isna(max_val): 
                     column_types[col] = "number" 
                 elif max_val > 1e15:
                     column_types[col] = "identifier"
                 else:
                     column_types[col] = "number"
             except:
                 column_types[col] = "number"
             continue
        
        # Check for Long Text / Category / Identifier
        series_sample = df[col].dropna()
        if len(series_sample) > 1000:
            series_sample = series_sample.head(1000)
            
        series_sample = series_sample[series_sample != "oThEr tHiNgS-+-"]
        
        if series_sample.empty:
            column_types[col] = "category"
            continue
            
        lengths = series_sample.astype(str).str.len()
        lengths = lengths[lengths > 0]
        
        if not lengths.empty and lengths.mean() > 60:
             column_types[col] = "long_text"
        else:
             n_rows = len(df[col].dropna()) 
             if n_rows > 100:
                 n_unique = df[col].nunique()
                 if n_unique > 0.9 * n_rows:
                     column_types[col] = "identifier"
                 else:
                     column_types[col] = "category"
             else:
                 column_types[col] = "category"
    
    return df, column_types





def get_current_stats(df, column_types, viz_config=None):
    from pandas import set_option
    from numpy import ndarray as np_ndarray
    from datetime import datetime
    set_option('future.no_silent_downcasting', True)
    t1 = datetime.now()
    print("    Calculating stats for viewer and explorer...")

    count = len(df)
    stats = {}
    if viz_config is None: viz_config = {}

    if count == 0:
        return {"count": 0, "stats": {}}

    for col, dtype in column_types.items():
        if dtype == "number":
             col_data = df[col]
             
             if pd.api.types.is_integer_dtype(col_data):
                  if col_data.nunique() < 20: 
                      vc = col_data.value_counts().sort_index().to_dict()
                      stats[col] = {str(k): v for k, v in vc.items()}
                      continue

             series = col_data.dropna()
             series = series[series >= 0]
             
             if series.empty:
                 stats[col] = {"type": "density", "x": [], "y": []}
                 continue
             
             count_val = len(series)
             mean_val = float(series.mean())
             std_val = float(series.std())
             min_val = float(series.min())
             max_val = float(series.max())
             
             transform = "linear"
             use_log = False
             if col in viz_config and viz_config[col].get('log'):
                 use_log = True
             if use_log: transform = "log10"
             
             clamped_series = series

             try:
                 if min_val == max_val:
                     x_val = np.log10(min_val + 1) if transform == "log10" else min_val
                     stats[col] = {
                        "type": "density",
                        "x": [float(x_val)],
                        "y": [float(count_val)],
                        "transform": transform,
                        "min": min_val,
                        "max": max_val,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                    }
                     continue

                 bins_arg = 10 
                 adaptive = True
                 if col in viz_config and viz_config[col].get('bins') is not None:
                      bins_arg = viz_config[col]['bins']
                      adaptive = False
                 
                 if transform == "log10":
                     # Log Transform
                     if isinstance(clamped_series.dtype, pd.ArrowDtype):
                         log_data = np.log10(clamped_series.to_numpy() + 1)
                     else:
                         log_data = np.log10(clamped_series + 1)
                         
                     log_min = np.log10(min_val + 1)
                     log_max = np.log10(max_val + 1)
                     
                     if adaptive and isinstance(bins_arg, int):
                          counts, bin_centers = calculate_adaptive_histogram(log_data, log_min, log_max, bins=bins_arg)
                     else:
                          chosen_bins = bins_arg
                          if isinstance(chosen_bins, (list, np_ndarray)):
                              chosen_bins = [np.log10(b + 1) for b in chosen_bins]
                          
                          counts, bin_edges = np.histogram(log_data, bins=chosen_bins, range=(log_min, log_max) if isinstance(chosen_bins, int) else None, density=True)
                          bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
                     
                     tick_vals = []
                     tick_text = []
                     if 0 >= min_val and 0 <= max_val:
                        tick_vals.append(np.log10(1))
                        tick_text.append("0")
                     p = 0
                     while True:
                        v = 10**p
                        if v > max_val: break
                        if v >= min_val:
                            tick_vals.append(np.log10(v + 1))
                            tick_text.append(f"{v:,}")
                        p += 1
                     
                     stats[col] = {
                        "type": "density",
                        "x": bin_centers.tolist(),
                        "y": counts.tolist(),
                        "transform": transform,
                        "min": min_val,
                        "max": max_val,
                        "tick_vals": tick_vals,
                        "tick_text": tick_text,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                     }
                 
                 else:
                     arr_data = clamped_series.to_numpy()
                     
                     if adaptive and isinstance(bins_arg, int):
                         counts, bin_centers = calculate_adaptive_histogram(arr_data, min_val, max_val, bins=bins_arg)
                     else:
                         counts, bin_edges = np.histogram(arr_data, bins=bins_arg, range=(min_val, max_val) if isinstance(bins_arg, int) else None, density=True)
                         bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
                         
                     stats[col] = {
                        "type": "density",
                        "x": bin_centers.tolist(),
                        "y": counts.tolist(),
                        "transform": transform,
                        "min": min_val,
                        "max": max_val,
                        "mean": mean_val,
                        "std": std_val,
                        "count": count_val
                     }
             
             except Exception as e:
                 print(f"Error stats {col}: {e}")
                 stats[col] = {}

        elif dtype == "category":
            vc = df[col].value_counts().head(20).to_dict()
            stats[col] = vc

        elif dtype == "list":
             if isinstance(df[col].dtype, pd.ArrowDtype) and 'list' in str(df[col].dtype):
                  try:
                      exploded = df[col].explode().dropna()
                      vc = exploded.value_counts().head(20).to_dict()
                      stats[col] = vc
                      continue
                  except:
                      pass
             
             all_items = []
             s = df[col].dropna()
             for row in s:
                  if isinstance(row, (list, np_ndarray)):
                      all_items.extend(row)
             from collections import Counter
             stats[col] = dict(Counter(all_items).most_common(20))

    print(f"    ...done calculating stats for viewer and explorer. Time: {datetime.now()-t1}")


    return {"count": count, "stats": stats}
