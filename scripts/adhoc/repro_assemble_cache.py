"""Assemble paper_three full annotated df once and cache to tmp for fast iteration."""
import sys, time
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf, initialize
from fyp.studies import init_study_defs

initialize(); init_study_defs()
study = sys.argv[1] if len(sys.argv) > 1 else "paper_three"
cols = {str(c).strip() for c in fyp_cf["study_defs"][study]["SELECTED_COLLECTIONS"]}

t = time.perf_counter()
plays = data_io.load_parquet_selective(
    storage_location="recoded", filename="collections_recoded.parquet",
    columns=["collection_id", "item_id", "activity_type", "play_duration",
             "utc_timestamp", "session_id"])
plays = plays[plays["collection_id"].astype(str).str.strip().isin(cols)]
plays = plays[plays["activity_type"].isin(("play", "observe"))].copy()

scr = data_io.load_parquet_selective(
    storage_location="recoded", filename="scrapes_recoded.parquet",
    columns=["item_id", "video_duration", "author_id", "stats_playCount",
             "desc_hashtags", "desc_not_hashtags", "music_title"]).drop_duplicates("item_id")
ann = data_io.load_parquet(storage_location="recoded",
                           filename="machine_annotations_recoded.parquet")
ann = ann[ann["annotated_ok"] == True].copy()  # noqa: E712
ann_text = ["video_story", "main_activity", "objects", "text_overlays",
            "symbols_and_brands", "transcript_no_repetitions"]
ann_feat = ["political_score", "sensitivity_score", "main_gender",
            "advertising", "aigc", "trend", "content_category"]
ann = ann[["item_id"] + [c for c in ann_text + ann_feat if c in ann.columns]].drop_duplicates("item_id")

df = plays.merge(scr, on="item_id", how="left").merge(ann, on="item_id", how="left")
df["video_duration"] = pd.to_numeric(df["video_duration"], errors="coerce")
df["play_duration"] = pd.to_numeric(df["play_duration"], errors="coerce")
df["dwell"] = df["play_duration"]
df["completion"] = df["play_duration"] / df["video_duration"]
df["log_playcount"] = np.log1p(pd.to_numeric(df["stats_playCount"], errors="coerce"))
df["utc_timestamp"] = pd.to_datetime(df["utc_timestamp"])
df = df.sort_values(["collection_id", "utc_timestamp"], kind="mergesort")
df["feed_position"] = df.groupby("collection_id", sort=False).cumcount()
df["is_annotated"] = df["item_id"].isin(set(ann["item_id"]))

data_io.save_parquet(df=df, storage_location="cache", filename=f"_repro_assembled_{study}.parquet")
print(f"saved {len(df):,} rows, {df['is_annotated'].sum():,} annotated, "
      f"{time.perf_counter()-t:.1f}s")
