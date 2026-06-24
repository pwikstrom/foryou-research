import json

from fyp import fyp_config

fyp_config.initialize()

from web_interface import data_service
from web_interface import explorer_backend as explorer

STUDY = "new_prompt_test"

NEW_FIELDS = [
    "spoken_language", "primary_country", "multilingual",
    "trend_technical", "trend_cultural", "trend",
    "australian_relevance",
]

df, col_types = data_service.get_explorer_data(STUDY, context="explorer", verbose=False)
print("rows:", len(df), "cols:", len(df.columns))

print("\n=== new-field presence / col_type / value sample ===")
for f in NEW_FIELDS:
    in_df = f in df.columns
    ct = col_types.get(f)
    if in_df:
        nn = df[f].notna().sum()
        vc = df[f].dropna().astype(str).value_counts().head(6).to_dict()
        print(f"{f:24s} in_df={in_df} col_type={ct!r} non_null={nn} sample={vc}")
    else:
        print(f"{f:24s} in_df={in_df} col_type={ct!r}  (NOT IN DF)")

print("\n=== columns in col_types but NOT in df (typed-but-missing) ===")
missing = [c for c in col_types if c not in df.columns]
print(missing)

print("\n=== columns in df but NOT in col_types (untyped) ===")
untyped = [c for c in df.columns if c not in col_types]
print(untyped)

# Cached explorer metadata: does it reference columns that no longer exist?
import os
from fyp import data_io
print("\n=== cached explorer metadata column set vs current ===")
try:
    meta = data_io.load_json(storage_location="cache", filename=f"{STUDY}_explorer_metadata.json")
    if isinstance(meta, dict):
        cols_in_meta = meta.get("columns") or list(meta.keys())
        print("meta top-level keys:", list(meta.keys())[:15])
except Exception as e:
    print("could not load meta:", e)
