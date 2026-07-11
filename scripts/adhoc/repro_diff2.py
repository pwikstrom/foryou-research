"""Test impression-level niche fit + dedup-order variants vs ground truth."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import fyp.data_io as data_io
from fyp import niche_detection, session_profile
from fyp.fyp_config import initialize

initialize()
mode = sys.argv[1] if len(sys.argv) > 1 else "impressions"
df = data_io.load_parquet(storage_location="cache", filename="_repro_assembled_paper_three.parquet")
gt = data_io.load_parquet(storage_location="cache", filename="paper_three_session_metrics.parquet")
annot = df[df["is_annotated"]].copy()

if mode == "impressions":
    # Fit on every annotated impression (no dedup), assign per impression.
    docs = niche_detection.assemble_documents(annot)
    model = niche_detection.fit_niche_model(docs, n_niches=150)
    df.loc[annot.index, "niche"] = model["labels"]
elif mode == "unsorted":
    # Dedup keeping arbitrary (storage) order, not chronological.
    a2 = annot.sample(frac=1.0, random_state=1)  # shuffle
    labels, _, _ = niche_detection.detect_niches(a2, n_niches=150)
    a2["niche"] = labels.values
    nmap = a2.drop_duplicates("item_id").set_index("item_id")["niche"]
    df["niche"] = df["item_id"].map(nmap)

m = session_profile.build_session_metrics(df)
prof = session_profile.compute_profile(m)
agg = {a["feature"]: a for a in prof["aggregate"]}
print(f"\nmode={mode}")
for f in ["completion", "dwell", "entropy", "top_share"]:
    if f in agg:
        a = agg[f]
        print(f"  {f:11s} delta={a['delta']:+.4f} pct_up={a['pct_up']:.1%} "
              f"(gt entropy +0.044@97%)")
print(f"  pct_narrowing={prof['session_distributions']['pct_narrowing']:.1%}")
