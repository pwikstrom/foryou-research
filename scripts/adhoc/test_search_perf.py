import sys
import os
import time
import pandas as pd
import numpy as np
import pyarrow as pa

# Ensure fyp module can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from web_interface.explorer_backend import filter_dataframe

def build_dummy_dataframe(n_rows=100000) -> tuple[pd.DataFrame, dict]:
    print("Building large dummy dataframe (PyArrow backend)...")
    
    # Create pyarrow arrays directly or via pandas with arrow dtype
    data = {
        'id_col': pd.Series(np.arange(n_rows), dtype="int64[pyarrow]"),
        'numeric_col': pd.Series(np.random.rand(n_rows), dtype="float64[pyarrow]"),
        'category_col': pd.Series(np.random.choice(["cat", "dog", "mouse", "elephant", None], n_rows), dtype="string[pyarrow]"),
        'long_text_col': pd.Series(["This is some long text about a random subject and also has the word specific_search_term in it sometimes " + str(i) for i in range(n_rows)], dtype="string[pyarrow]"),
        'list_col': pd.Series([["tag1", "tag2"] if i % 2 == 0 else ["tag3"] for i in range(n_rows)]) # lists are objects
    }
    
    # Add many numeric columns to simulate the real dataset
    for i in range(20):
        data[f'feat_{i}'] = pd.Series(np.random.rand(n_rows), dtype="float64[pyarrow]")
        
    df = pd.DataFrame(data)
    
    column_types = {
        'id_col': 'identifier',
        'numeric_col': 'number',
        'category_col': 'category',
        'long_text_col': 'long_text',
        'list_col': 'list'
    }
    for i in range(20):
        column_types[f'feat_{i}'] = 'number'
        
    print(f"Built DF with {len(df)} rows and {len(df.columns)} columns.")
    return df, column_types


def test_search_perf():
    df, col_types = build_dummy_dataframe(150000)
    
    # Test 1: Search for a random string
    print("\n--- Test 1: String Search ('elephant') ---")
    start = time.time()
    res1 = filter_dataframe(df, col_types, {}, search_query="elephant")
    dur1 = time.time() - start
    print(f"Found {len(res1)} rows in {dur1:.4f} seconds")
    
    # Test 2: Search for a number
    print("\n--- Test 2: Numeric Search ('1234') ---")
    start = time.time()
    try:
        res2 = filter_dataframe(df, col_types, {}, search_query="1234")
        dur2 = time.time() - start
        print(f"Found {len(res2)} rows in {dur2:.4f} seconds")
    except Exception as e:
        print(f"Exception! {e}")
    print("Done test 2")

if __name__ == "__main__":
    test_search_perf()
