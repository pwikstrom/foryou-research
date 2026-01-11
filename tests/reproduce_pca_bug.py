
import pandas as pd
import numpy as np

def reproduce_bug():
    # 1. Create a DataFrame with 10,000 rows
    N = 10000
    df = pd.DataFrame({
        'all_data': np.random.rand(N),
        'category': ['A'] * N
    })
    
    # 2. Add x_col and y_col valid only for 10 rows (0.1% of data)
    df['x_col'] = np.nan
    df['y_col'] = np.nan
    
    valid_indices = np.random.choice(N, 10, replace=False)
    df.loc[valid_indices, 'x_col'] = np.random.rand(10)
    df.loc[valid_indices, 'y_col'] = np.random.rand(10)
    
    print(f"Total rows: {len(df)}")
    print(f"Valid rows for X/Y: {len(df.dropna(subset=['x_col', 'y_col']))}")
    
    # --- Simulate Buggy Logic ---
    print("\n--- Simulating Logic BEFORE Fix ---")
    MAX_POINTS = 5000
    
    # Current Bug: Sample FIRST
    buggy_df = df.copy()
    if len(buggy_df) > MAX_POINTS:
        buggy_df = buggy_df.sample(MAX_POINTS, random_state=42) # Fixed seed to ensure reproducibility of bad luck
    
    # Then Dropna
    buggy_df = buggy_df.dropna(subset=['x_col', 'y_col'])
    print(f"Resulting rows (Buggy): {len(buggy_df)}")
    
    if len(buggy_df) == 0:
        print(">> FAILURE REPRODUCED: Result is empty despite having valid data.")
    else:
        print(f">> NO FAILURE (Lucky sample): Got {len(buggy_df)} rows.")

    # --- Simulate Fixed Logic ---
    print("\n--- Simulating Logic AFTER Fix ---")
    
    # Fix: Dropna FIRST
    fixed_df = df.copy()
    fixed_df = fixed_df.dropna(subset=['x_col', 'y_col'])
    
    # Then Sample
    if len(fixed_df) > MAX_POINTS:
        fixed_df = fixed_df.sample(MAX_POINTS, random_state=42)
        
    print(f"Resulting rows (Fixed): {len(fixed_df)}")
    
    if len(fixed_df) > 0:
        print(">> VERIFIED: Result contains data.")
    else:
        print(">> FAILURE: Fixed logic still returned empty (should not happen).")

if __name__ == "__main__":
    reproduce_bug()
