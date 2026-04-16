import os
import sys
import warnings

# Add the project root to sys.path to ensure we can import fyp
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fyp.data_io as data_io
from fyp.fyp_main import initialize
from fyp.pca import calculate_scaled_pca_scores


def reproduce():
    print("Initializing configuration...")
    # Initialize with verbose=False to keep output clean, unless debugging init fails
    try:
        cf = initialize()
    except Exception as e:
        print(f"Failed to initialize: {e}")
        return
    
    cache_path = cf['paths']['cache']
    print(f"Cache path: {cache_path}")
    
    study_name = "small study 1"
    filename = f"{study_name}_recoded.parquet"
    full_path = os.path.join(cache_path, filename)
    
    if os.path.exists(full_path):
        print(f"Found file at: {full_path}")
    else:
        print(f"WARNING: File not found at {full_path}")
        # List files in cache to help debug if it's not found
        if os.path.exists(cache_path):
            print(f"Files in cache: {os.listdir(cache_path)}")
        else:
            print(f"Cache directory does not exist: {cache_path}")
            
    print("\nRunning calculate_scaled_pca_scores...")
    # Capture warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            calculate_scaled_pca_scores(
                cf=cf,
                study_name=study_name,
                verbose=True
            )
        except Exception as e:
            print(f"Caught exception: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nCaught {len(w)} warnings:")
    categories = set()
    for warning in w:
        cat_name = warning.category.__name__
        if cat_name not in categories: # print first example of each category to avoid spam
             print(f"{cat_name}: {warning.message}")
             print(f"  File: {warning.filename}, Line: {warning.lineno}")
             categories.add(cat_name)
    
    if len(categories) < len(w):
         print(f"... and {len(w) - len(categories)} more warnings.")

if __name__ == "__main__":
    reproduce()
