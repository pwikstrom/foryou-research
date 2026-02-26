import sys
from os.path import abspath, dirname, join
sys.path.insert(0, abspath(dirname(__file__)))

import pandas as pd
from fyp.data_io import load_parquet

try:
    print("Testing read_parquet with empty list filter...")
    sel = [("item_id", "in", [])]
    df = load_parquet(storage_location="recoded", filename="scrape_recoded.parquet", filters=sel)
    print("Shape:", df.shape)
except Exception as e:
    print(f"Failed: {e}")
