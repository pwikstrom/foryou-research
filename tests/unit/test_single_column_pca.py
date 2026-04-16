import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

import numpy as np
import pandas as pd

from fyp.pca import (
    pairwise_matrix_for_categorical_groups,
    transform_categories_to_components_and_diversity,
)


def test_single_col():
    print("Testing single column dataframe...")
    # Create a dummy counts dataframe with 1 column (category) and multiple groups
    df = pd.DataFrame({
        "cat1": [10, 20, 30, 40, 50]
    }, index=["g1", "g2", "g3", "g4", "g5"])
    
    print("Dataframe:")
    print(df)
    
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            # Replicate the call from pca.py
            wer, the_pc_df, comp_interpretation = transform_categories_to_components_and_diversity(
                df,
                metric="hellinger",
                gamma=0.8,
                max_components=15,
                target_explained_variance=0.8,
                drop_rare_globally_below=0.001,
                verbose=True)
            
            print("\nResult:")
            print(wer)
            
        except Exception as e:
            print(f"\nCaught exception: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nCaught {len(w)} warnings.")
    for warning in w:
        print(f"{warning.category.__name__}: {warning.message}")
        print(f"  File: {warning.filename}, Line: {warning.lineno}")

if __name__ == "__main__":
    test_single_col()
