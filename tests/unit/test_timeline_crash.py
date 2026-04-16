import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(dirname(__file__)))

from web_interface.data_service import get_timeline_data

print("Testing get_timeline_data for BASELINE...")
try:
    res = get_timeline_data("BASELINE 2024", interval="day")
    print("Success")
except Exception as e:
    print(f"Failed: {e}")
