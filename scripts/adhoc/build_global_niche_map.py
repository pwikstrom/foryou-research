"""Fit niches once on the FULL annotated corpus (+ scrape text), save item->niche."""
import sys, time
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import pandas as pd
import fyp.data_io as data_io
from fyp import niche_detection
from fyp.fyp_config import initialize

initialize()
n_niches = int(sys.argv[1]) if len(sys.argv) > 1 else 150
t = time.perf_counter()
ann = data_io.load_parquet(storage_location="recoded",
                           filename="machine_annotations_recoded.parquet")
ann = ann[ann["annotated_ok"] == True].copy()  # noqa: E712
ann_text = ["video_story", "main_activity", "objects", "text_overlays",
            "symbols_and_brands", "transcript_no_repetitions"]
ann = ann[["item_id"] + [c for c in ann_text if c in ann.columns]].drop_duplicates("item_id")
scr = data_io.load_parquet_selective(
    storage_location="recoded", filename="scrapes_recoded.parquet",
    columns=["item_id", "desc_hashtags", "desc_not_hashtags", "music_title"]).drop_duplicates("item_id")
corpus = ann.merge(scr, on="item_id", how="left")
print(f"corpus: {len(corpus):,} unique annotated videos")

labels, _, _ = niche_detection.detect_niches(corpus, n_niches=n_niches)
out = corpus[["item_id"]].copy()
out["niche"] = labels.values
data_io.save_parquet(df=out, storage_location="cache", filename=f"_repro_global_niche_K{n_niches}.parquet")
print(f"saved global niche map K={n_niches}, {out['niche'].nunique()} niches, "
      f"{time.perf_counter()-t:.1f}s")
