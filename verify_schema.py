import sys
from pathlib import Path
import pandas as pd
import os

# Add project root to sys.path
file_path = Path(__file__).resolve()
project_root = file_path.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from web_interface.hub_config import fyp_cf, PROJECT_ROOT

print("Checking fyp_cf keys...")
if 'var_schema' in fyp_cf:
    print("Found 'var_schema' in fyp_cf")
    df = fyp_cf['var_schema']
    print("\n--- Rows with accepted_labels ---")
    print(df[['variable_name', 'accepted_labels']].dropna().head())
    
    # Check type of first non-na
    val = df['accepted_labels'].dropna().iloc[0]
    print(f"\nType of value: {type(val)}")
    print(f"Value: {val}")

else:
    print("'var_schema' NOT found in fyp_cf. Loading from CSV...")
    var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
    if var_schema_path.exists():
        df = pd.read_csv(var_schema_path)
        print("\n--- Rows with accepted_labels ---")
        print(df[['variable_name', 'accepted_labels']].dropna().head())
        
        if not df['accepted_labels'].dropna().empty:
             val = df['accepted_labels'].dropna().iloc[0]
             print(f"\nType of value: {type(val)}")
             print(f"Value: {val}")
