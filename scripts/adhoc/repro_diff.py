"""Diff reproduced session metrics vs the ground-truth cached metrics table."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import fyp.data_io as data_io
from fyp import niche_detection, session_profile
from fyp.fyp_config import initialize

initialize()
study = "paper_three"
df = data_io.load_parquet(storage_location="cache", filename=f"_repro_assembled_{study}.parquet")
gt = data_io.load_parquet(storage_location="cache", filename=f"{study}_session_metrics.parquet")
print(f"assembled={len(df):,} rows, gt sessions={len(gt):,}")

scope = sys.argv[1] if len(sys.argv) > 1 else "perstudy"
n_niches = int(sys.argv[2]) if len(sys.argv) > 2 else 150

if scope == "global":
    gm = data_io.load_parquet(storage_location="cache", filename=f"_repro_global_niche_K{n_niches}.parquet")
    nmap = gm.set_index("item_id")["niche"]
    df["niche"] = df["item_id"].map(nmap)
elif scope == "content_category":
    cc = df["content_category"].astype(str).str.lower()
    df["niche"] = cc.where(df["is_annotated"] & (cc != "nan") & (cc != "<na>"))
elif scope == "allrows":
    labels, _, _ = niche_detection.detect_niches(df, n_niches=n_niches)
    df["niche2"] = labels.values
    df["niche"] = df["niche2"].where(df["is_annotated"])
else:
    annot = df[df["is_annotated"]].copy()
    labels, _, _ = niche_detection.detect_niches(annot, n_niches=n_niches)
    annot["niche"] = labels.values
    nmap = annot.drop_duplicates("item_id").set_index("item_id")["niche"]
    df["niche"] = df["item_id"].map(nmap)
print(f"scope={scope} K={n_niches}: {df['niche'].nunique()} niches assigned")

m = session_profile.build_session_metrics(df)
print(f"my sessions={len(m):,}")

# session_id overlap
gt_sids = set(gt["session_id"]); my_sids = set(m["session_id"])
inter = gt_sids & my_sids
print(f"session_id overlap: {len(inter):,} / gt {len(gt_sids):,} / mine {len(my_sids):,}")

j = m.merge(gt, on="session_id", suffixes=("_mine", "_gt"))
print(f"merged on session_id: {len(j):,}")
for c in ["completion_early", "completion_late", "entropy_early", "entropy_late",
          "top_share_early", "top_share_late"]:
    a, b = j[f"{c}_mine"], j[f"{c}_gt"]
    print(f"  {c:18s} mine_mean={a.mean():.4f} gt_mean={b.mean():.4f} "
          f"corr={a.corr(b):.3f}")
print("\nmy delta entropy:", round(float(m["d_entropy"].mean()), 4),
      "gt:", round(float(gt["d_entropy"].mean()), 4))
