import os
import sys
import argparse

# Ensure fyp can be imported
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from fyp.data_io import load_dataset, save_dataset
    from fyp.fyp_main import convert_dtypes_to_pyarrow
except ImportError as e:
    # If running from root, simple import might work if package structure is right,
    # but the sys.path append above handles the subfolder case.
    print(f"Could not import fyp.data_io. Error: {e}")
    sys.exit(1)

def scan_and_save_w_pyarrow_dtypes(start_path):
    print(f"Scanning for parquet files in: {os.path.abspath(start_path)}")
    count = 0
    success = 0
    failures = 0
    changes = 0
    
    for root, dirs, files in os.walk(start_path):
        for file in files:
            if file.endswith(".parquet"):
                count += 1
                full_path = os.path.join(root, file)
                print(f"[{count}] Loading: {full_path}")
                try:
                    #df = load_dataset(cf, "exports", full_path, verbose=True)
                    row_count = len(df)
                    col_count = len(df.columns)
                    print(f"  ✅ Success! Shape: ({row_count}, {col_count})")
                    hej = convert_dtypes_to_pyarrow(df, verbose=True)
                    made_changes = sum((hej.dtypes != df.dtypes)*1)
                    if made_changes > 0:
                        print(f"  ⚠️ Made changes to {made_changes} columns")
                        save_dataset(df, full_path, verbose=True)
                        changes += 1
                    success += 1
                except Exception as e:
                    print(f"  ❌ FAILED: {e}")
                    failures += 1
                print()
    
    print("\n" + "=" * 30)
    print(f"Summary:")
    print(f"Total Parquet Files Found: {count}")
    print(f"Successfully Loaded:     {success}")
    print(f"Failed to Load:          {failures}")
    print(f"Made changes to:         {changes}")
    print("=" * 30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan and load all parquet files in a directory tree using fyp.data_io.load_parquet.")
    parser.add_argument("path", nargs="?", default=".", help="Root path to scan (defaults to current directory)")
    args = parser.parse_args()
    
    scan_and_load(args.path)
