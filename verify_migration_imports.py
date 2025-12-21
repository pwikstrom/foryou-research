
import sys
import os
from pathlib import Path

# Add current directory to sys.path to simulate project root execution
PROJECT_ROOT = Path(os.getcwd()).resolve()
sys.path.append(str(PROJECT_ROOT))

print(f"Project Root: {PROJECT_ROOT}")

try:
    print("Attempting to import fyp...")
    import fyp
    print("✅ fyp imported")

    print("Attempting to import fyp.data_io...")
    import fyp.data_io
    print("✅ fyp.data_io imported")

    print("Checking data_io attributes...")
    if hasattr(fyp.data_io, 'load_dataset') and hasattr(fyp.data_io, 'save_dataset'):
        print("✅ load_dataset and save_dataset found")
    else:
        print("❌ Missing load_dataset or save_dataset")
        sys.exit(1)

    print("Checking if utilities were moved...")
    if hasattr(fyp.data_io, 'get_study_export_files') and hasattr(fyp.data_io, 'get_dataset_details'):
        print("✅ Utils (get_study_export_files, get_dataset_details) found in data_io")
    else:
         print("❌ Missing utils in data_io")
         sys.exit(1)

    print("\nVERIFICATION SUCCESS: All modules imported and structure looks correct.")

except ImportError as e:
    print(f"\n❌ ImportError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"\n❌ Unexpected Error: {e}")
    sys.exit(1)
