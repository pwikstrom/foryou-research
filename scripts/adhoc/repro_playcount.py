import pandas as pd

from fyp import fyp_config

fyp_config.initialize()

from fyp import data_io

STUDY = "new_prompt_test"
df = data_io.load_parquet(storage_location="cache", filename=f"{STUDY}_recoded.parquet")

for col in ["stats_playCount", "plays_per_day", "stats_diggCount", "stats_shareCount",
            "stats_commentCount", "stats_collectCount"]:
    if col in df.columns:
        s = df[col]
        print(f"\n{col}: dtype={s.dtype}")
        print(f"  min={s.min()} max={s.max()} n_negative={(s < 0).sum()}")
        neg = df.loc[s < 0, [c for c in ['item_id','stats_playCount','plays_per_day'] if c in df.columns]]
        if len(neg):
            print("  negative rows (head):")
            print(neg.head(5).to_string())
    else:
        print(f"\n{col}: NOT PRESENT")
