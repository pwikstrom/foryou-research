import polars as pl, pathlib, time

PARQUET = pathlib.Path("data/views.parquet")

t0 = time.perf_counter()
DF = pl.read_parquet(PARQUET)                      # eager load into RAM
print(f"Loaded {len(DF):,} rows in {time.perf_counter()-t0:.2f}s")
