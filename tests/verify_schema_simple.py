import pandas as pd
from pathlib import Path
import ast

csv_path = Path("config/var_schema.csv")
if csv_path.exists():
    df = pd.read_csv(csv_path)
    print("Columns:", df.columns.tolist())
    
    if 'accepted_labels' in df.columns:
        print("\n--- Rows with accepted_labels ---")
        subset = df[['variable_name', 'accepted_labels']].dropna()
        print(subset.head())
        
        if not subset.empty:
            val = subset['accepted_labels'].iloc[0]
            print(f"\nType: {type(val)}")
            print(f"Value: {val}")
            
            # Try parsing
            try:
                parsed = ast.literal_eval(val)
                print(f"Parsed: {parsed}, Type: {type(parsed)}")
            except:
                print("Parse failed")
    else:
        print("'accepted_labels' column not found")
else:
    print("CSV not found")
