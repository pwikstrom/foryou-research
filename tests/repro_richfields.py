"""Test richer niche-document field sets vs ground truth entropy."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import fyp.data_io as data_io
from fyp import niche_detection, session_profile
from fyp.fyp_config import initialize

initialize()
# Reload annotation FRESH (to get all semantic fields), re-merge to assembled plays.
df = data_io.load_parquet(storage_location="cache", filename="_repro_assembled_paper_three.parquet")
ann = data_io.load_parquet(storage_location="recoded", filename="machine_annotations_recoded.parquet")
ann = ann[ann["annotated_ok"] == True].copy()  # noqa: E712
rich = ["video_story", "main_activity", "objects", "text_overlays", "symbols_and_brands",
        "transcript_no_repetitions", "content_category", "type_of_story",
        "scene_sentiments", "notable_sounds", "call_to_action_words", "background_music"]
rich = [c for c in rich if c in ann.columns]
ann_small = ann[["item_id"] + rich].drop_duplicates("item_id")
# Merge rich fields onto unique annotated videos present in df.
annot = df[df["is_annotated"]].drop_duplicates("item_id")[["item_id", "desc_hashtags",
        "desc_not_hashtags", "music_title"]].merge(ann_small, on="item_id", how="left")

fields = {"video_story": 3, "main_activity": 2, "objects": 1, "text_overlays": 1,
          "symbols_and_brands": 1, "desc_not_hashtags": 1, "transcript_no_repetitions": 1,
          "desc_hashtags": 1, "music_title": 1,
          "content_category": 2, "type_of_story": 1, "scene_sentiments": 1,
          "notable_sounds": 1}
fields = {k: v for k, v in fields.items() if k in annot.columns}
docs = niche_detection.assemble_documents(annot, fields=fields)
model = niche_detection.fit_niche_model(docs, n_niches=150)
annot["niche"] = model["labels"]
nmap = annot.set_index("item_id")["niche"]
df["niche"] = df["item_id"].map(nmap)

m = session_profile.build_session_metrics(df)
agg = {x["feature"]: x for x in session_profile.aggregate_contrast(m)}
e = agg["entropy"]
print(f"\nrich fields ({len(fields)}): sessions={len(m):,} "
      f"entropy delta={e['delta']:+.4f} pct_up={e['pct_up']:.1%} "
      f"early={e['early']:.3f} late={e['late']:.3f}")
print("  (gt: +0.044 @ 97%, early 2.003 late 2.048)")
