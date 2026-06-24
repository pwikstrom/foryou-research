"""Export the numbers behind each presentation figure to one compact JSON.

``build_deck_native.js`` rebuilds the talk deck as *native* PowerPoint
elements (editable text, shapes, charts) instead of baked PNG images; this
script gathers everything it needs — per-donor exposure bars, the anatomy
episode's dots, niche enrichment ratios, the null scatter, the episode
geometry cloud, and the Phase 2/3 headline numbers — into
``tmp/figs_presentation/deck_data.json``.
"""

import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access

OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))
FIG_DIR = os.path.join(OUT_DIR, "figs_presentation")




def anatomy_dots() -> dict:
    """Recompute the fig2 example episode's session dots (mirrors make_figures)."""
    ep = pd.read_parquet(os.path.join(OUT_DIR, "episodes_v1.parquet"))
    cand = ep[(ep["collection_id"].str.startswith("edcfa1f1"))
              & (ep["dominant_niche"] == "Beauty Product Reviews")
              & (ep["n_distinct"].between(8, 12)) & (ep["duration_min"] < 16)
              & (ep["dominant_niche_share"] >= 0.8)]
    e = cand.sort_values("focus").iloc[0]
    plays = data_access.load_plays([e["collection_id"]]).to_pandas()
    plays["_ts"] = pd.to_datetime(plays["local_timestamp"], errors="coerce")
    sp = plays[plays["session_id"].astype(str) == str(e["session_id"])].sort_values("_ts")
    t0, t1 = pd.Timestamp(e["start_ts"]), pd.Timestamp(e["end_ts"])
    lo = max(sp["_ts"].min(), t0 - pd.Timedelta(minutes=22))
    hi = min(sp["_ts"].max(), t1 + pd.Timedelta(minutes=22))
    win = sp[(sp["_ts"] >= lo) & (sp["_ts"] <= hi)]
    x = ((win["_ts"] - t0).dt.total_seconds() / 60).to_numpy()
    in_ep = ((win["_ts"] >= t0) & (win["_ts"] <= t1)).to_numpy()
    rng = np.random.default_rng(5)
    y = rng.uniform(0.25, 0.75, len(win))
    labels = data_access.load_video_labels(set(win["item_id"]))
    niche = [(labels.get(i) or {}).get("niche_name") for i in win["item_id"]]
    # Stable colour index per niche, in order of first appearance.
    seen: dict = {}
    groups = []
    for n, ie in zip(niche, in_ep):
        if ie:
            groups.append("binge")
        elif n is None:
            groups.append("unmapped")
        else:
            seen.setdefault(n, len(seen))
            groups.append(f"niche{seen[n] % 12}")
    return {
        "x": [round(float(v), 2) for v in x],
        "y": [round(float(v), 3) for v in y],
        "group": groups,
        "ep_len_min": round((t1 - t0).total_seconds() / 60, 2),
        "n_distinct": int(e["n_distinct"]),
        "duration_min": round(float(e["duration_min"]), 1),
        "niche": str(e["dominant_niche"]),
        "x_min": round(float(x.min()), 1), "x_max": round(float(x.max()), 1),
    }




def main() -> None:
    """Assemble and write deck_data.json."""
    sm = pd.read_parquet(os.path.join(OUT_DIR, "episode_donor_summary_v1.parquet"))
    ep = pd.read_parquet(os.path.join(OUT_DIR, "episodes_v1.parquet"))
    base = json.load(open(os.path.join(OUT_DIR, "base_rates_v1.json")))
    null_rows = json.load(open(os.path.join(OUT_DIR, "episode_null_v1.json")))
    p2 = json.load(open(os.path.join(OUT_DIR, "phase2_v1.json")))
    p3 = json.load(open(os.path.join(OUT_DIR, "phase3_v1.json")))

    exposure = (pd.to_numeric(sm["frac_watchtime_in_episodes"], errors="coerce")
                .fillna(0).mul(100).sort_values().round(3).tolist())

    fired = [r for r in null_rows if r["obs_episodes"] > 0]
    null_scatter = {
        "obs": [int(r["obs_episodes"]) for r in fired],
        "null_mean": [round(float(r["null_mean"] or 0), 2) for r in fired],
        "n_donors": len(null_rows),
        "n_fdr": sum(1 for r in null_rows if r.get("fdr_significant")),
    }

    geometry = {
        "diameter": pd.to_numeric(ep["diameter"], errors="coerce").round(3).tolist(),
        "straightness": pd.to_numeric(ep["straightness"], errors="coerce")
                        .fillna(0).round(3).tolist(),
    }

    p3_by = {r["feature_set"]: r for r in p3 if "pr_auc" in r}
    data = {
        "exposure_pct": exposure,
        "anatomy": anatomy_dots(),
        "niches": base["niche_enrichment"],
        "authors": base["author_concentration"],
        "null_scatter": null_scatter,
        "geometry": geometry,
        "rq9": p2["rq9"],
        "rq10": p2["rq10"],
        "p3": {k: {"lift": v["lift_at_top1pct"], "auroc": v["roc_auc"],
                   "base_rate": v["base_rate"]} for k, v in p3_by.items()},
    }
    out = os.path.join(FIG_DIR, "deck_data.json")
    with open(out, "w") as fh:
        json.dump(data, fh)
    print(f"Wrote {out}")




if __name__ == "__main__":
    main()
