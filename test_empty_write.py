import sys
from os.path import abspath, dirname, join
sys.path.insert(0, abspath(dirname(__file__)))

import pandas as pd
from fyp.data_io import save_parquet

try:
    print("Testing save_parquet with completely empty dataframe...")
    df = pd.DataFrame([])
    save_parquet(df=df, storage_location="cache", filename="empty_test.parquet")
    print("Done")
except Exception as e:
    print(f"Failed: {e}")
