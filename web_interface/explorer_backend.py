import pandas as pd
import ast
import numpy as np

def load_data(file_path):
    """
    Loads the dataset (CSV or PKL) and detects column types.
    Parses stringified lists if CSV, or identifies lists if PKL.
    """
    try:
        if file_path.endswith('.pkl'):
            df = pd.read_pickle(file_path)
        else:
            df = pd.read_csv(file_path)
    except Exception as e:
        print(f"Error loading data {file_path}: {e}")
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
        if isinstance(val, list) or isinstance(val, np.ndarray):
            column_types[col] = "list"
        elif isinstance(val, str) and val.strip().startswith('[') and val.strip().endswith(']'):
            try:
                # Attempt to parse entire column as list
                df[col] = df[col].apply(lambda x: ast.literal_eval(x) if isinstance(x, str) and x.strip().startswith('[') else (x if isinstance(x, list) else []))
                column_types[col] = "list"
            except (ValueError, SyntaxError):
                column_types[col] = "category"
        
        # 2. Check for Number
        elif pd.api.types.is_numeric_dtype(df[col]):
            column_types[col] = "number"
        
        # 3. Check for Long Text (if category/string)
        else:
            # Check average length of non-null values
            sample = df[col].dropna().head(100).astype(str)
            if not sample.empty:
                mean_len = sample.str.len().mean()
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


def get_metadata(df, column_types):
    """
    Returns metadata for frontend:
    - columns: { name: type }
    - stats: min/max for numbers, unique values for categories
    """
    metadata = {}
    for col, dtype in column_types.items():
        if dtype == "number":
            metadata[col] = {
                "type": "number",
                "min": float(df[col].min()) if not df[col].empty else 0,
                "max": float(df[col].max()) if not df[col].empty else 0
            }
        elif dtype == "category":
            # Strict limit for UI filters
            # Only send top 50 most frequent values for filtering to save DOM
            unique_vals = df[col].value_counts().head(50).index.tolist()
            unique_vals = sorted([str(x) for x in unique_vals])
            
            metadata[col] = {
                "type": "category",
                "values": unique_vals
            }
        elif dtype == "list":
            # Extract all unique items from lists
            # Flatten
            all_items = []
            for row in df[col].dropna():
                if isinstance(row, list):
                    all_items.extend(row)
            
            # Use Counter to find top 50 tags
            from collections import Counter
            c = Counter(all_items)
            items_list = sorted([str(x) for x, _ in c.most_common(50)])

            metadata[col] = {
                "type": "list",
                "values": items_list
            }
        
        # Explicitly ignore long_text and identifier
        elif dtype in ["long_text", "identifier"]:
            continue
            
    return metadata


def filter_dataframe(df, column_types, filters):
    filtered_df = df.copy()

    for col, criteria in filters.items():
        if col not in df.columns:
            continue
        
        val = criteria.get("value")
        if val is None or val == "":
            continue

        dtype = column_types.get(col)

        if dtype == "number":
            min_val = val.get("min")
            max_val = val.get("max")
            if min_val is not None:
                filtered_df = filtered_df[filtered_df[col] >= float(min_val)]
            if max_val is not None:
                filtered_df = filtered_df[filtered_df[col] <= float(max_val)]

        elif dtype == "category":
            if isinstance(val, list) and len(val) > 0:
                filtered_df = filtered_df[filtered_df[col].astype(str).isin(val)]
        
        elif dtype == "list":
            if isinstance(val, list) and len(val) > 0:
                search_set = set(val)
                filtered_df = filtered_df[filtered_df[col].apply(lambda x: bool(set(x) & search_set) if isinstance(x, list) else False)]

    return filtered_df


def get_current_stats(df, column_types):
    """
    Returns robust stats for the (filtered) dataframe.
    """
    count = len(df)
    stats = {}

    if count == 0:
        return {"count": 0, "stats": {}}

    for col, dtype in column_types.items():
        if dtype == "number":
             desc = df[col].describe(percentiles=[.25, .5, .75])
             stats[col] = {
                 "min": float(desc['min']),
                 "q1": float(desc['25%']),
                 "median": float(desc['50%']),
                 "mean": float(desc['mean']), 
                 "q3": float(desc['75%']),
                 "max": float(desc['max'])
             }
        
        elif dtype == "category":
            # Cap value counts for charts to Top 20
            # Sending thousands of bars crashes frontend
            vc = df[col].value_counts().head(20).to_dict()
            stats[col] = vc

        elif dtype == "list":
            # Flatten and count
            all_items = []
            for row in df[col].dropna():
                    if isinstance(row, list):
                        all_items.extend(row)
            
            from collections import Counter
            # Cap list items to Top 20
            stats[col] = dict(Counter(all_items).most_common(20))

    return {"count": count, "stats": stats}
