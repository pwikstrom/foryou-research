import os
import shutil
import sys
import importlib.util
from unittest.mock import MagicMock, ModuleType

# 1. Mock 'fyp' package to prevent __init__.py from running
if "fyp" in sys.modules:
    del sys.modules["fyp"]
fyp_pkg = ModuleType("fyp")
fyp_pkg.__path__ = []
sys.modules["fyp"] = fyp_pkg

# 2. Mock 'fyp.fyp_main' which data_io imports
mock_fyp_main = MagicMock()
sys.modules["fyp.fyp_main"] = mock_fyp_main

# 3. Manually load 'fyp.data_io' from file
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
data_io_path = os.path.join(project_root, "fyp", "data_io.py")

spec = importlib.util.spec_from_file_location("fyp.data_io", data_io_path)
data_io = importlib.util.module_from_spec(spec)
sys.modules["fyp.data_io"] = data_io
spec.loader.exec_module(data_io)

def test_move_temp_to_local():
    # Setup manual config
    cf = {
        "paths": {
            "temp": "/tmp/fyp_test_temp",
            "archive": "/tmp/fyp_test_archive"
        },
        "data_io": {
            "use_gcs_for_data": False,
            "bucket": None,
            "GCS_bucket_name": "test-bucket"
        },
        "misc": {
            "local_mode": True
        }
    }

    # Define paths
    temp_dir = cf['paths']['temp']
    archive_dir = cf['paths']['archive']
    
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    filename = "test_move_file.txt"
    src_path = os.path.join(temp_dir, filename)
    dst_path = os.path.join(archive_dir, filename)

    # Clean up any existing test files
    if os.path.exists(src_path):
        os.remove(src_path)
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # Create dummy file in temp
    with open(src_path, "w") as f:
        f.write("This is a test file.")

    # Execute move
    print(f"Moving {filename} from temp to archive...")
    data_io.move(cf, "temp", "archive", filename, verbose=True)

    # Verify
    if not os.path.exists(dst_path):
        print("FAILED: Destination file does not exist.")
        exit(1)
    
    if os.path.exists(src_path):
        print("FAILED: Source file still exists.")
        exit(1)
        
    print("SUCCESS: File moved correctly.")
    
    # Cleanup
    shutil.rmtree(temp_dir)
    shutil.rmtree(archive_dir)

if __name__ == "__main__":
    try:
        test_move_temp_to_local()
    except Exception as e:
        print(f"Test Exception: {e}")
        exit(1)
