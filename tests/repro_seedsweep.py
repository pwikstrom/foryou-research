"""Is the entropy diversification sensitive to the KMeans partition (seed/order)?"""
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
annot_full = df[df["is_annotated"]].copy()

for seed in range(5):
    annot = annot_full
    labels, _, _ = niche_detection.detect_niches(annot, n_niches=150, random_state=seed)
    a = annot.copy()
    a["niche"] = labels.values
    nmap = a.drop_duplicates("item_id").set_index("item_id")["niche"]
    df["niche"] = df["item_id"].map(nmap)
    m = session_profile.build_session_metrics(df)
    agg = {x["feature"]: x for x in session_profile.aggregate_contrast(m)}
    e = agg["entropy"]
    print(f"seed={seed}: entropy delta={e['delta']:+.4f} pct_up={e['pct_up']:.1%} "
          f"early={e['early']:.3f} late={e['late']:.3f}")
