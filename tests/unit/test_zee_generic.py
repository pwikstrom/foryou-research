import sys
from os.path import abspath, dirname, join

sys.path.insert(0, abspath(dirname(__file__)))

from web_interface.data_service import check_and_update_timeline_cache, load_schema_metadata

print("Testing check_and_update_timeline_cache for Zee_generic...")
try:
    meta = {}
    load_schema_metadata(meta)
    viz_vars = meta.get('timeline_priority', [])
    res = check_and_update_timeline_cache("Zee_generic", viz_vars=viz_vars, verbose=True)
    print("Success")
except Exception as e:
    print(f"Failed: {e}")
