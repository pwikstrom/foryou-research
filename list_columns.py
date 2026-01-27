
import sys
import os
import pandas as pd
from fyp.fyp_config import fyp_cf
import fyp.data_io as data_io

def list_columns():
    filename = "ddp_metadata.parquet"
    if not data_io.exists(storage_location="ddp_main", filename=filename):
        print("File not found")
        return

    df = data_io.load_parquet(storage_location="ddp_main", filename=filename)
    if df is None:
        print("Could not load DataFrame")
        return

    print(f"Index: {df.index.name}")
    print("Columns:")
    for col in df.columns:
        print(col)

if __name__ == "__main__":
    list_columns()
