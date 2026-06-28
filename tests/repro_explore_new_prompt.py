import traceback

from fyp import fyp_config

fyp_config.initialize()

from web_interface import data_service
from web_interface import explorer_backend as explorer

STUDY = "new_prompt_test"

print("=== get_explorer_data ===")
try:
    df, col_types = data_service.get_explorer_data(STUDY, context="explorer", verbose=True)
    print("rows:", None if df is None else len(df))
    print("ncols:", None if df is None else len(df.columns))
except Exception:
    traceback.print_exc()
    raise

print("\n=== get_metadata ===")
try:
    metadata = explorer.get_metadata(df, col_types)
    print("metadata keys:", list(metadata.keys())[:10], "...")
except Exception:
    traceback.print_exc()
    raise

print("\n=== get_current_stats ===")
try:
    res = explorer.get_current_stats(df, col_types, number_meta=metadata)
    print("stats ok; keys:", list(res.keys())[:10] if isinstance(res, dict) else type(res))
except Exception:
    traceback.print_exc()
    raise

print("\nDONE OK")
