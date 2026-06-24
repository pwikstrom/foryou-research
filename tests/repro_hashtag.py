"""Test hashtag-based and finer niche definitions vs ground truth entropy."""
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
mode = sys.argv[1] if len(sys.argv) > 1 else "first_hashtag"


def first_hashtag(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return str(v[0]).lower() if len(v) else None
    if isinstance(v, str) and v.strip():
        return v.split()[0].lower().lstrip("#") if v.strip() else None
    return None


annot_mask = df["is_annotated"]
if mode == "first_hashtag":
    niche = df["desc_hashtags"].map(first_hashtag)
    df["niche"] = niche.where(annot_mask)
elif mode == "hashtag_set":
    df["niche"] = df["desc_hashtags"].map(
        lambda v: " ".join(sorted(str(x).lower() for x in v)) if isinstance(v, (list, tuple, np.ndarray)) and len(v) else None
    ).where(annot_mask)

n = df["niche"].notna().sum()
print(f"mode={mode}: {n:,} rows with niche, {df['niche'].nunique()} distinct niches")
m = session_profile.build_session_metrics(df)
agg = {x["feature"]: x for x in session_profile.aggregate_contrast(m)}
e = agg.get("entropy", {})
print(f"  sessions={len(m):,} entropy delta={e.get('delta'):+.4f} "
      f"pct_up={e.get('pct_up'):.1%} early={e.get('early'):.3f} late={e.get('late'):.3f}")
print("  (gt: entropy +0.044 @ 97%, early 2.003 late 2.048)")
