import sys
import os

# Add the current directory to sys.path so we can import fyp
sys.path.append(os.getcwd())

try:
    import fyp.fyp_main as fyp
    cf = fyp.init_config()
    print(f"MAIN_DATA_PATH: {cf['paths']['main']}")
except Exception as e:
    print(f"Error loading config: {e}")
