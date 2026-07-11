import traceback

from fyp import fyp_config

fyp_config.initialize()

from fyp import data_io
from web_interface import data_service
from web_interface.routes import api_explorer_routes as R

STUDY = "new_prompt_test"

print("=== base: _finalize_base_metadata(cached json) ===")
try:
    metadata = data_io.load_json(storage_location="cache", filename=f"{STUDY}_explorer_metadata.json")
    fin = R._finalize_base_metadata(metadata, STUDY)
    print("finalize ok; type:", type(fin), "None?" , fin is None)
except Exception:
    traceback.print_exc()

print("\n=== overlay: get_explorer_data + enrich + _compute_dynamic_overlay ===")
try:
    df, col_types = data_service.get_explorer_data(STUDY, context="explorer")
    df2, ct2 = R.enrich_with_user_tags(df, col_types, "info@foryouresearch.net", shared_users_tags=None)
    overlay = R._compute_dynamic_overlay(df2, ct2)
    print("overlay ok; keys:", list(overlay.keys()))
except Exception:
    traceback.print_exc()

print("\n=== cold path: _build_full_metadata (regeneration) ===")
try:
    meta2 = R._build_full_metadata(df, col_types, STUDY)
    print("build_full ok; keys:", len(meta2))
    fin2 = R._finalize_base_metadata(meta2, STUDY)
    print("finalize(cold) ok; None?", fin2 is None)
except Exception:
    traceback.print_exc()

print("\nDONE")
