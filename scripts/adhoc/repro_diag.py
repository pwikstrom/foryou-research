"""Diagnose where my entropy delta vanishes vs ground truth."""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import fyp.data_io as data_io
from fyp import niche_detection, session_profile
from fyp.fyp_config import initialize

initialize()
df = data_io.load_parquet(storage_location="cache", filename="_repro_assembled_paper_three.parquet")
annot = df[df["is_annotated"]].copy()
labels, _, _ = niche_detection.detect_niches(annot, n_niches=150)
annot["niche"] = labels.values
nmap = annot.drop_duplicates("item_id").set_index("item_id")["niche"]
df["niche"] = df["item_id"].map(nmap)

# niche distribution sanity
vc = df.loc[df["niche"].notna(), "niche"].value_counts(normalize=True)
print(f"niches: {len(vc)}, top-1 share={vc.iloc[0]:.3f}, top-5 share={vc.head(5).sum():.3f}")

# replicate band+segments
work = df[df["niche"].notna()].copy()
cnt = work.groupby("session_id")["niche"].transform("size")
work = work[(cnt >= 12) & (cnt <= 80)].copy()
work = work.sort_values(["session_id", "feed_position"], kind="mergesort")
rank = work.groupby("session_id").cumcount()
n = work.groupby("session_id")["feed_position"].transform("size")
frac = rank / (n - 1).clip(lower=1)
work["_seg"] = np.where(frac < 1/3, "early", np.where(frac >= 2/3, "late", "mid"))

seg = work[work["_seg"].isin(["early", "late"])]
g = seg.groupby(["session_id", "_seg"])
agg = g.agg(n_imp=("niche", "size"),
            n_uniq_item=("item_id", "nunique"),
            n_uniq_niche=("niche", "nunique")).reset_index()
piv = agg.pivot_table(index="session_id", columns="_seg",
                      values=["n_imp", "n_uniq_item", "n_uniq_niche"])
piv.columns = [f"{a}_{b}" for a, b in piv.columns]
piv = piv.dropna()
print("\nMean per-session segment structure (early vs late):")
for base in ["n_imp", "n_uniq_item", "n_uniq_niche"]:
    e, l = piv[f"{base}_early"].mean(), piv[f"{base}_late"].mean()
    print(f"  {base:14s} early={e:.3f} late={l:.3f} delta={l-e:+.3f}")
print(f"\n  repeat ratio (imp/uniq_item) early="
      f"{(piv['n_imp_early']/piv['n_uniq_item_early']).mean():.4f} "
      f"late={(piv['n_imp_late']/piv['n_uniq_item_late']).mean():.4f}")
