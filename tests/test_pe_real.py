import pyarrow.parquet as pq
import ast
import pandas as pd
import json
import re

schema_path = "data/var_schema.csv"
if not __import__("os").path.exists(schema_path):
    print(f"{schema_path} missing")

storage_location = "activity_data/processed"
filename = "ddp_metadata.parquet"
if not __import__("os").path.exists(f"{storage_location}/{filename}"):
    print(f"{storage_location}/{filename} missing. Looking for it...")
    import glob
    for p in glob.glob("**/*/ddp_metadata.parquet", recursive=True):
        print(f"Found at {p}")
        storage_location = __import__("os").path.dirname(p)
        filename = __import__("os").path.basename(p)
        break

try:
    path = f"{storage_location}/{filename}"
    print(f"Loading from {path}")
    table = pq.read_table(path)
    data = {}
    for i, col_name in enumerate(table.column_names):
        data[col_name] = table.column(i).to_pandas()
    df = pd.DataFrame(data)

    print(f"Total donations in DB: {len(df)}")
    if 'collection_id' in str(df.columns):
        print("Has collection_id column")
    else:
        print("NO collection_id. Columns:", df.columns[:10])
except Exception as e:
    print("ERROR:", e)
