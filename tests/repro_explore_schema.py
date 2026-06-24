import json

from fyp import fyp_config

fyp_config.initialize()

from fyp import data_io
from web_interface.routes import api_explorer_routes as R

STUDY = "new_prompt_test"

metadata = data_io.load_json(storage_location="cache", filename=f"{STUDY}_explorer_metadata.json")
fin = R._finalize_base_metadata(metadata, STUDY)

NEW = ["spoken_language", "primary_country", "multilingual",
       "trend_technical", "trend_cultural", "trend", "australian_relevance",
       "content_category", "type_of_story"]

print("=== schema_map entries for new/changed fields ===")
sm = fin.get("schema_map", {})
for f in NEW:
    print(f"\n--- {f} ---")
    print("  in schema_map:", f in sm, " entry:", json.dumps(sm.get(f), ensure_ascii=False))
    print("  in metadata  :", f in fin, " type:", (fin.get(f) or {}).get("type") if isinstance(fin.get(f), dict) else fin.get(f))
    ent = fin.get(f)
    if isinstance(ent, dict):
        keys = list(ent.keys())
        print("  meta keys:", keys)
        if "values" in ent:
            print("  values:", json.dumps(ent["values"][:8], ensure_ascii=False))
        if "accepted_labels" in ent:
            print("  accepted_labels:", ent["accepted_labels"])

print("\n\n=== sections present in schema_map ===")
sections = {}
for k, v in sm.items():
    if isinstance(v, dict):
        sec = v.get("section")
        sections.setdefault(sec, []).append(k)
for sec, fields in sections.items():
    print(f"  [{sec}]: {fields}")

print("\n=== filter_priority (first 40) ===")
print(fin.get("filter_priority", [])[:40])

# Look for fields in filter_priority/display_priority that are NOT in metadata (could break JS)
fp = fin.get("filter_priority", [])
dp = fin.get("display_priority", [])
print("\n=== priority entries missing from metadata ===")
print("filter_priority missing:", [c for c in fp if c not in fin])
print("display_priority missing:", [c for c in dp if c not in fin])

print("\n=== schema_map fields missing from metadata ===")
print([c for c in sm if c not in fin and c not in ('User Tags','Has Annotation','Machine Annotations')])
