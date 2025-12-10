import pandas as pd
import ast
import numpy as np

def load_data(csv_path):
    """
    Loads the CSV and detects column types.
    Parses stringified lists.
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"Error loading CSV {csv_path}: {e}")
        return None, {}

    column_types = {}

    for col in df.columns:
        # Check first non-null value to guess type if pandas is ambiguous
        first_valid_index = df[col].first_valid_index()
        if first_valid_index is None:
            # All null, treat as category (text)
            column_types[col] = "category"
            continue
        
        val = df[col].loc[first_valid_index]

        # 1. Check for List (string starting with '[')
        if isinstance(val, str) and val.strip().startswith('[') and val.strip().endswith(']'):
            try:
                # Attempt to parse entire column as list
                # We use a safe converter
                df[col] = df[col].apply(lambda x: ast.literal_eval(x) if isinstance(x, str) and x.strip().startswith('[') else (x if isinstance(x, list) else []))
                column_types[col] = "list"
            except (ValueError, SyntaxError):
                # Fallback to category if parsing fails
                column_types[col] = "category"
        
        # 2. Check for Number
        elif pd.api.types.is_numeric_dtype(df[col]):
            column_types[col] = "number"
        
        # 3. Default to Category
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
            # Limit unique values sent to frontend to avoid massive payloads
            # Let's say top 100 or just all if < 500
            unique_vals = df[col].dropna().unique().tolist()
            if len(unique_vals) > 500:
                 # If too many, maybe don't send them all? 
                 # Or send top 500? For now, let's just sort and send.
                 unique_vals = sorted([str(x) for x in unique_vals])[:500] 
            else:
                 unique_vals = sorted([str(x) for x in unique_vals])
            
            metadata[col] = {
                "type": "category",
                "values": unique_vals
            }
        elif dtype == "list":
            # Extract all unique items from lists
            # Flatten
            all_items = set()
            for row in df[col].dropna():
                if isinstance(row, list):
                    all_items.update(row)
            
            items_list = sorted([str(x) for x in list(all_items)])
            metadata[col] = {
                "type": "list",
                "values": items_list
            }
            
    return metadata


def filter_dataframe(df, column_types, filters):
    """
    Filters dataframe based on criteria.
    filters: { col_name: { type: '...', value: ... } }
    """
    filtered_df = df.copy()

    for col, criteria in filters.items():
        if col not in df.columns:
            continue
        
        val = criteria.get("value")
        if val is None or val == "":
            continue

        dtype = column_types.get(col)

        if dtype == "number":
            # Expecting value to be { min: ..., max: ... }
            min_val = val.get("min")
            max_val = val.get("max")
            if min_val is not None:
                filtered_df = filtered_df[filtered_df[col] >= float(min_val)]
            if max_val is not None:
                filtered_df = filtered_df[filtered_df[col] <= float(max_val)]

        elif dtype == "category":
            # Expecting value to be list of selected categories [CHECKED]
            # or single value? Let's assume list for 'One of'
            if isinstance(val, list) and len(val) > 0:
                # Filter rows where col is in val
                # Convert to string for safety if category is mixed
                filtered_df = filtered_df[filtered_df[col].astype(str).isin(val)]
        
        elif dtype == "list":
            # Expecting value to be list of selected tags
            # Logic: Row must contain ANY of the selected tags? Or ALL?
            # Let's go with ANY for now (standard 'filter by tag' behavior)
            if isinstance(val, list) and len(val) > 0:
                # Make set for O(1)
                search_set = set(val)
                # Apply is slow but convenient for lists
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
             # Box plot metrics
             desc = df[col].describe(percentiles=[.25, .5, .75])
             stats[col] = {
                 "min": float(desc['min']),
                 "q1": float(desc['25%']),
                 "median": float(desc['50%']),
                 "mean": float(desc['mean']), # Keep mean just in case
                 "q3": float(desc['75%']),
                 "max": float(desc['max'])
             }
        
        elif dtype == "category":
            # Full value counts for stacked bar
            # Normalized? Let's send raw counts, frontend can normalize
            stats[col] = df[col].value_counts().to_dict()

        elif dtype == "list":
            # Flatten and count
            all_items = []
            for row in df[col].dropna():
                    if isinstance(row, list):
                        all_items.extend(row)
            
            from collections import Counter
            stats[col] = dict(Counter(all_items))

    return {"count": count, "stats": stats}
