import datetime as _dt
from collections import Counter

import numpy as np
import pandas as pd
import pyarrow as pa

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import create_study_recoded_dataset


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





def get_metadata(df, column_types, verbose=False):
    """
    Returns metadata for frontend:
    - columns: { name: type }
    - stats: min/max for numbers, unique values for categories, null_counts
    """
    t1 = _dt.datetime.now()
    if verbose:
        print("    Calculating things for viewer and explorer...")
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
            # Limit for UI filters — show top 200 most frequent values
            is_dt = pd.api.types.is_datetime64_any_dtype(df[col])
            col_data = df[col]
            if is_dt or "date" in col.lower():
                col_data = df[col].astype(str).str[:10]

            vc_full = col_data.value_counts()
            total_unique = len(vc_full)
            vc = vc_full.head(200)

            # Sort by frequency (default from value_counts)
            top_keys = vc.index.tolist()

            unique_vals = [{"value": str(x), "count": int(vc[x])} for x in top_keys]

            base_meta.update({
                "type": "category",
                "values": unique_vals,
                "total_unique": total_unique
            })
            metadata[col] = base_meta

        elif dtype == "list":
            # Extract all unique items from lists
            # Flatten
            all_items = []
            for row in df[col].dropna():
                if isinstance(row, (list, np.ndarray)):
                    # Deduplicate within row to count Document Frequency (rows with tag) instead of Term Frequency
                    try:
                        all_items.extend(list(set(str(x) for x in row)))
                    except:
                        pass

            # Use Counter to find top 200 tags
            c = Counter(all_items)
            total_unique = len(c)
            top_items = c.most_common(200)

            items_list = [{"value": str(k), "count": v} for k, v in top_items]

            base_meta.update({
                "type": "list",
                "values": items_list,
                "total_unique": total_unique
            })
            metadata[col] = base_meta
        
        # Explicitly ignore long_text and identifier
        elif dtype in ["long_text", "identifier"]:
            continue
    
    if verbose:
        print(f"    ...done calculating things for viewer and explorer. Time: {_dt.datetime.now()-t1}")
    return metadata





def filter_dataframe(df, column_types, filters, search_query=None):

    filtered_df = df.copy()

    for col, criteria in filters.items():
        # Handle virtual Collection Tags filter
        if col == 'Collection Tags':
            val = criteria.get("value")
            if isinstance(val, (list, np.ndarray)) and len(val) > 0 and 'collection_id' in filtered_df.columns:
                try:
                    # Lazy import to avoid circular dependency with data_service
                    from .data_service import get_collection_tags
                    annotations = get_collection_tags()
                except Exception:
                    annotations = {}
                selected_tags = set(str(v) for v in val)
                matching_cids = set()
                for cid, anno in annotations.items():
                    anno_tags = set(str(t).strip() for t in anno.get('annotation_tags', []))
                    if anno_tags & selected_tags:
                        matching_cids.add(str(cid))
                filtered_df = filtered_df[filtered_df['collection_id'].astype(str).isin(matching_cids)]
            continue

        if col not in df.columns:
            continue
        
        val = criteria.get("value")
        # If no value criteria, skip
        if val is None or val == "":
            continue

        dtype = column_types.get(col)
        
        value_mask = pd.Series(False, index=filtered_df.index)
        has_value_criteria = False

        if dtype == "number":
            # Robustness: If frontend sends a list (checkboxes) for a numeric column (e.g. bools, discrete ints),
            # treat it as a categorical "isin" filter.
            if isinstance(val, (list, np.ndarray)):
                value_mask = filtered_df[col].astype(str).isin([str(v) for v in val])
                has_value_criteria = True
            else:
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
            if isinstance(val, (list, np.ndarray)) and len(val) > 0:
                is_dt = pd.api.types.is_datetime64_any_dtype(filtered_df[col])
                if is_dt or "date" in col.lower():
                    value_mask = filtered_df[col].astype(str).str[:10].isin([str(v)[:10] for v in val])
                else:
                    value_mask = filtered_df[col].astype(str).isin(val)
                has_value_criteria = True
        
        elif dtype == "list":
            if isinstance(val, (list, np.ndarray)) and len(val) > 0:
                search_set = set(str(v) for v in val) # Ensure strings
                
                def robust_check(x):
                    if not isinstance(x, (list, np.ndarray)): return False
                    try:
                        # Ensure x items are also hashable/strings
                        check_set = set(str(item) for item in x)
                        return bool(check_set & search_set)
                    except:
                        return False
                        
                value_mask = filtered_df[col].apply(robust_check)
                has_value_criteria = True

        if has_value_criteria:
             filtered_df = filtered_df[value_mask]

    # Global Search Logic
    if search_query and isinstance(search_query, str):
        terms = [t.strip().lower() for t in search_query.split(",") if t.strip()]
        if terms:
            # We want rows where ALL terms appear ANYWHERE in the row
            original_indices = filtered_df.index
            final_mask = pd.Series(True, index=original_indices)
            
            # Extract explicitly searchable columns from schema
            explicit_searchable_vars = set()
            if 'var_schema' in fyp_cf and isinstance(fyp_cf['var_schema'], pd.DataFrame):
                vs = fyp_cf['var_schema']
                if 'searchable' in vs.columns and 'variable_name' in vs.columns:
                    # Look for '1' or 1.0
                    is_searchable = vs['searchable'].astype(str).str.strip().str.startswith('1')
                    explicit_searchable_vars = set(vs.loc[is_searchable, 'variable_name'].astype(str).tolist())
            
            # Pre-filter columns to search
            # Avoid casting huge numeric arrays to string if the search term isn't a number
            searchable_cols = []
            for col in filtered_df.columns:
                # If the column is in the schema, it MUST have searchable == 1
                if explicit_searchable_vars and 'var_schema' in fyp_cf:
                    # To handle dynamic columns not in schema (like User Tags), we still check schema presence
                    if col in vs['variable_name'].values and col not in explicit_searchable_vars:
                         continue # Skip this column if it's in schema but not searchable

                dtype = column_types.get(col)
                if dtype in ["category", "long_text", "identifier", "list"]:
                    searchable_cols.append(col)
            
            for term in terms:
                term_mask = pd.Series(False, index=original_indices)
                term_is_numeric = term.replace('.', '', 1).isdigit()
                
                cols_to_search = searchable_cols.copy()
                if term_is_numeric:
                    # Add numeric columns if the term looks like a number
                    for col in filtered_df.columns:
                        if column_types.get(col) == "number":
                            cols_to_search.append(col)
                
                for col in cols_to_search:
                    try:
                        dtype = column_types.get(col)
                        if dtype == "list":
                            # Lists are complex, fallback to standard object string casting
                            mask = filtered_df[col].astype(str).str.lower().str.contains(term, regex=False, na=False)
                        else:
                            try:
                                # Attempt fast PyArrow string engine
                                col_str = filtered_df[col].astype("string[pyarrow]").str.lower()
                                mask = col_str.str.contains(term, regex=False, na=False)
                            except:
                                # Fallback if PyArrow string conversion fails
                                mask = filtered_df[col].astype(str).str.lower().str.contains(term, regex=False, na=False)
                                
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

    if isinstance(obj, (pd.Timestamp, _dt.datetime)):
        return obj.isoformat()
        
    if hasattr(obj, 'tolist'):  # generic numpy scalar fallback
        return obj.tolist()
        
    return obj


def classify_columns(df: pd.DataFrame) -> dict:
    """Classify DataFrame columns into type categories based on Arrow dtypes and heuristics.

    Returns:
        Dict mapping column name to type string: 'list', 'number', 'identifier', 'long_text', or 'category'.
    """
    column_types = {}

    for col in df.columns:
        dtype = df[col].dtype

        # 1. Check for Lists (List, LargeList, FixedSizeList)
        is_list = False
        if isinstance(dtype, pd.ArrowDtype):
            pa_type = dtype.pyarrow_dtype
            if (pa.types.is_list(pa_type) or
                pa.types.is_large_list(pa_type) or
                pa.types.is_fixed_size_list(pa_type)):
                is_list = True

        if is_list:
            column_types[col] = "list"
            continue

        # 2. Check for Numbers (Integers, Floats)
        # We explicitly exclude booleans from "number" to treat them as categorical/other
        is_numeric = False
        is_bool = False

        if isinstance(dtype, pd.ArrowDtype):
            pa_type = dtype.pyarrow_dtype
            if pa.types.is_boolean(pa_type):
                is_bool = True
            elif pa.types.is_integer(pa_type) or pa.types.is_floating(pa_type):
                is_numeric = True
        else:
            # Fallback for numpy dtypes (though we expect Arrow)
            if pd.api.types.is_bool_dtype(dtype):
                is_bool = True
            elif pd.api.types.is_numeric_dtype(dtype):
                is_numeric = True

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

        # 3. Strings / Categories
        # Boolean also falls through here to be treated as category (heuristic)

        # Check for Long Text / Category / Identifier
        # We still use data-based heuristics for this distinction as Arrow string type is generic
        series_sample = df[col].dropna()
        if len(series_sample) > 1000:
            series_sample = series_sample.head(1000)

        series_sample = series_sample[series_sample != fyp_cf['labels']['OTHER_THINGS']]

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

    return column_types


def load_data(study: str, verbose: bool = False):

    if verbose:
        print("    Loading study data for viewer/explorer...")
    df = None

    if not data_io.exists(
        storage_location = "cache",
        filename = f"{study}_recoded.parquet",
        verbose=verbose
        ):
        # Cold-start path: only attempt to build the recoded dataset for a
        # study that actually exists in the config. An unknown name would
        # otherwise crash deep inside create_study_recoded_dataset and
        # surface as a 500 in every route that calls get_explorer_data.
        study_defs = fyp_cf.get("study_defs", {}) or {}
        if study not in study_defs:
            if verbose:
                print(f"    Study '{study}' is not defined in config; nothing to load.")
            return None, {}

        print("@@ No cached recoded study dataset found. I must run the recoding process to create it. Please wait a moment...")
        df = create_study_recoded_dataset(
            study_name = study,
            save_to_cache=True,
            verbose = verbose
        )
        print("@@ Back after finalising the recoding process.")
    else:
        df = data_io.load_parquet(
            storage_location="cache",
            filename=f"{study}_recoded.parquet",
            verbose=verbose,
            )

    if df is None:
        print("ERROR: This process cannot run without a study dataset. Process failed.")
        return None, {}

    column_types = classify_columns(df)

    return df, column_types





def get_current_stats(df, column_types, viz_config=None, verbose=False):
    
    pd.set_option('future.no_silent_downcasting', True)
    
    t1 = _dt.datetime.now()
    
    if verbose:
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
                          if isinstance(chosen_bins, (list, np.ndarray)):
                              chosen_bins = [np.log10(b + 1) for b in chosen_bins]
                          
                          counts, bin_edges = np.histogram(log_data, bins=chosen_bins, range=(log_min, log_max) if isinstance(chosen_bins, int) else None, density=True)
                          bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
                     
                     tick_vals = []
                     tick_text = []
                     if min_val <= 0 and max_val >= 0:
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
                  if isinstance(row, (list, np.ndarray)):
                      # Deduplicate within row to count Document Frequency
                      try:
                          all_items.extend(list(set(str(x) for x in row)))
                      except:
                          pass
             stats[col] = dict(Counter(all_items).most_common(20))

    if verbose:
        print(f"    ...done calculating stats for viewer and explorer. Time: {_dt.datetime.now()-t1}")


    return {"count": count, "stats": stats}
