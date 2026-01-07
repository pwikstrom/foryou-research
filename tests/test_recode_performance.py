
import sys
import os
import time
import pandas as pd
import numpy as np
import pyarrow as pa
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import fyp.recode_variables as recode_variables

def create_synthetic_data(n_rows=1000):
    print(f"Generating {n_rows} rows of synthetic data...")
    
    # Create synthetic columns similar to real data
    
    # 1. Machine annotations (lists of strings) - emulating "G_faces_age_estimate"
    # Some rows have valid lists, some empty lists, some None/NA
    age_ranges = ["18-24", "25-34", "35-44", "45-54", "55-64"]
    g_faces = []
    for _ in range(n_rows):
        r = np.random.random()
        if r < 0.1:
            g_faces.append(None) # Missing
        elif r < 0.2:
            g_faces.append("unable to detect") # Explicit missing
        elif r < 0.3:
            g_faces.append([]) # Empty list
        elif r < 0.4:
            g_faces.append(["not coded"]) # Not coded
        else:
            # Random valid ages
            k = np.random.randint(1, 4)
            g_faces.append(list(np.random.choice(age_ranges, k)))
            
    # 2. Scene sentiments - string with keywords
    sentiments = []
    words = ["positive", "negative", "highenergy", "lowenergy", "neutral", "calm", "exciting"]
    for _ in range(n_rows):
        r = np.random.random()
        if r < 0.1:
            sentiments.append(None)
        else:
            k = np.random.randint(1, 4)
            sentiments.append(", ".join(np.random.choice(words, k)))

    # 3. Numeric column with missing values
    play_counts = np.random.randint(0, 100000, n_rows).astype(float)
    play_counts[np.random.random(n_rows) < 0.1] = np.nan
    
    # 4. A categorical column for validation
    categories = ["cat_a", "cat_b", "cat_c", "cat_d"]
    cats = np.random.choice(categories, n_rows)
    
    df = pd.DataFrame({
        "G_faces_age_estimate": g_faces,
        "G_scene_sentiments": sentiments,
        "S_stats_playCount": play_counts,
        "some_category": cats,
        "T_days_since_created": np.random.randint(1, 1000, n_rows),
        "session_id": np.arange(n_rows)
    })
    
    # Convert to PyArrow types where applicable to match user environment
    df["G_scene_sentiments"] = df["G_scene_sentiments"].astype("string[pyarrow]")
    df["some_category"] = df["some_category"].astype("string[pyarrow]")
    
    return df

def mock_config():
    # Create a mock var_schema DataFrame
    var_schema_data = {
        "variable_name": ["G_faces_age_estimate", "G_scene_sentiments", "S_stats_playCount", "some_category", "session_id", "T_days_since_created"],
        "role": ["feature", "feature", "feature", "factor", "id", "feature"],
        "scale": ["others", "others", "ratio", "categorical", "nominal", "ratio"],
        "recode_func": [
            "recode_faces_age_estimate", 
            "recode_scene_sentiments", 
            None, 
            None, 
            None, 
            None
        ],
        "unable_to_detect_policy": ["keep", "keep", "median", "drop", "keep", "keep"],
        "missing_data_policy": ["keep", "keep", "median", "drop", "keep", "keep"],
        "mapper": [None, None, None, None, None, None],
        "ignore_strings": [None, None, None, None, None, None]
    }
    var_schema = pd.DataFrame(var_schema_data)
    
    return {
        "var_schema": var_schema,
        "misc": {
            "file_format": ".parquet",
            "use_gcs_for_data": False
        },
        "exports_path": "/tmp"
    }

def run_benchmark():
    n_rows = 20000
    df = create_synthetic_data(n_rows)
    cf = mock_config()
    
    print(f"Starting benchmark with {n_rows} rows...")
    
    start_time = time.time()
    
    # Mocking data_io.save_parquet to avoid writing to disk during test
    recode_variables.data_io = MagicMock()
    
    # Run clean_up_machine_annotations
    print("Running clean_up_machine_annotations...")
    t0 = time.time()
    df_cleaned = recode_variables.clean_up_machine_annotations(df, verbose=False)
    t1 = time.time()
    print(f"clean_up_machine_annotations took: {t1 - t0:.4f} seconds")
    
    # Run recode_events_df
    # We need to simulate the environment where recode_events_df normally runs
    # It copies the input df, and 'cool_events_in' argument is available
    print("Running recode_events_df...")
    t2 = time.time()
    
    # Note: recode_variables.recode_events_df modifies logic heavily internally e.g. eval() on schema
    try:
        recode_variables.recode_events_df(
            cf=cf, 
            cool_events_in=df_cleaned, 
            verbose=False, 
            save_it=False
        )
    except Exception as e:
        print(f"Caught exception during execution (might be expected due to environment): {e}")
        # Depending on how far it got, that's fine for now, we want to see where time goes
        import traceback
        traceback.print_exc()

    t3 = time.time()
    print(f"recode_events_df took: {t3 - t2:.4f} seconds")
    print(f"Total time: {t3 - start_time:.4f} seconds")

if __name__ == "__main__":
    run_benchmark()
