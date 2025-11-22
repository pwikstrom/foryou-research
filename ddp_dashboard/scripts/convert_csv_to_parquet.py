# convert_csv_to_parquet.py
#import polars as pl
import pathlib
import pandas as pd
#from tabulate import tabulate

csv_path = pathlib.Path("../backend/data/views.csv")
parquet_path = csv_path.with_suffix(".parquet")


df = pd.read_csv(csv_path)

df["viewed_at"] = pd.to_datetime(df.viewed_at)

#print(tabulate(df, headers='keys', tablefmt='pipe'))

df.to_parquet(parquet_path, compression="zstd")

print(f"Wrote {len(df):,} rows to {parquet_path} ({parquet_path.stat().st_size/1e3:.1f} KB)")
