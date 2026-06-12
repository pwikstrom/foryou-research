"""Phase 2 — donor correlates (RQ8), recurrence (RQ9), retention (RQ10).

Three analyses over the locked v1 episode table:

* **RQ8 — who binges more?** Donor-level correlates of binge intensity
  (episodes per 1,000 embedded plays). Predictors: embedding coverage
  (nuisance, always controlled), diet semantic diversity (mean pairwise cosine
  distance over a sample of the donor's distinct embedded videos), play volume,
  session size, baseline dwell, and active-engagement propensity
  (faves/follows/comments/searches per play). Exploratory: Spearman
  correlations plus an OLS of log1p(rate) with coverage partialled.

* **RQ9 — habit or one-off?** Within donors with >=2 episodes, the share of
  episode pairs that share a dominant niche, against a permutation null that
  shuffles episode niches across donors (each donor keeps their episode count;
  the global niche multiset is preserved). High observed share = donors return
  to *their* niche (habit), not just to popular niches.

* **RQ10 — does a binge keep you watching?** Paired within donor: for each
  episode, the time remaining in its session after the episode ends, versus
  the median remaining time at the same elapsed point in the donor's
  non-episode sessions (only sessions that last at least that long are
  eligible, removing the "longer sessions host more episodes" mechanical
  confound). Also dwell during the episode vs the same session outside it.
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
from build_episodes import load_directional_store, select_donors

DIET_SAMPLE = 1500
N_PERM = 1000
SEED = 43
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def load_all_activity(donors: list[str]) -> pd.DataFrame:
    """Load every activity row (all types) for the donors, with parsed time."""
    t = pq.read_table(
        os.path.join(data_access.RECODED_DIR, data_access.COLLECTIONS_FILE),
        columns=["collection_id", "item_id", "local_timestamp", "activity_type",
                 "play_duration", "session_id"],
        filters=[("collection_id", "in", donors)],
    ).to_pandas()
    t["_ts"] = pd.to_datetime(t["local_timestamp"], errors="coerce")
    return t.dropna(subset=["_ts"])




def rq8_correlates(donors: list[str], acts: pd.DataFrame, sm: pd.DataFrame,
                   id2idx: dict, U: np.ndarray, rng: np.random.Generator) -> dict:
    """RQ8 — donor-level correlates of binge intensity."""
    plays = acts[acts["activity_type"] == "play"]
    rows = []
    for cid in donors:
        g = plays[plays["collection_id"] == cid]
        if g.empty:
            continue
        emb_items = g[g["item_id"].isin(id2idx)]["item_id"].unique()
        if len(emb_items) < 50:
            continue
        sample = rng.choice(emb_items, size=min(DIET_SAMPLE, len(emb_items)), replace=False)
        V = U[[id2idx[i] for i in sample]]
        sims = V @ V.T
        iu = np.triu_indices(len(sample), k=1)
        diet_div = 1.0 - float(sims[iu].mean())

        all_g = acts[acts["collection_id"] == cid]
        n_play = len(g)
        n_engage = int(all_g["activity_type"].isin(
            ["fave", "following", "comment", "search"]).sum())
        sess_sizes = g.groupby("session_id").size()
        rows.append({
            "collection_id": cid,
            "diet_diversity": round(diet_div, 4),
            "log_plays": round(float(np.log10(n_play)), 3),
            "median_session_plays": float(sess_sizes.median()),
            "mean_dwell": round(float(pd.to_numeric(g["play_duration"], errors="coerce").mean()), 2),
            "engage_per_100_plays": round(100 * n_engage / n_play, 3),
        })
    feats = pd.DataFrame(rows)

    d = sm.merge(feats, on="collection_id")
    d["rate"] = 1000 * d["n_episodes"] / d["n_emb_play"]
    d["log_rate"] = np.log1p(d["rate"])

    predictors = ["diet_diversity", "log_plays", "median_session_plays",
                  "mean_dwell", "engage_per_100_plays", "coverage"]
    cors = {}
    for p_ in predictors:
        r, pv = stats.spearmanr(d[p_], d["rate"])
        cors[p_] = {"spearman_r": round(float(r), 3), "p": round(float(pv), 4)}

    # Coverage-partialled check for the substantive predictors: residualise both
    # the outcome and the predictor on coverage, then correlate the residuals.
    partial = {}
    cov = d["coverage"].to_numpy(dtype=float)
    y_res = d["log_rate"] - np.poly1d(np.polyfit(cov, d["log_rate"], 1))(cov)
    for p_ in predictors[:-1]:
        x = d[p_].to_numpy(dtype=float)
        x_res = x - np.poly1d(np.polyfit(cov, x, 1))(cov)
        r, pv = stats.spearmanr(x_res, y_res)
        partial[p_] = {"partial_spearman_r": round(float(r), 3), "p": round(float(pv), 4)}

    d.to_parquet(os.path.join(OUT_DIR, "phase2_donor_features.parquet"))
    return {"n_donors": len(d), "raw": cors, "coverage_partialled": partial,
            "rate_median": round(float(d["rate"].median()), 3),
            "rate_p90": round(float(d["rate"].quantile(.9)), 3)}




def rq9_recurrence(ep: pd.DataFrame, rng: np.random.Generator) -> dict:
    """RQ9 — do donors return to the same niche across episodes?"""
    ok = ep.dropna(subset=["dominant_niche"])
    counts = ok.groupby("collection_id").size()
    multi = counts[counts >= 2].index
    sub = ok[ok["collection_id"].isin(multi)]

    def same_pair_share(frame: pd.DataFrame) -> float:
        tot = same = 0
        for _, g in frame.groupby("collection_id"):
            n = len(g)
            tot += n * (n - 1) / 2
            vc = g["dominant_niche"].value_counts()
            same += float((vc * (vc - 1) / 2).sum())
        return same / tot if tot else np.nan

    obs = same_pair_share(sub)
    niches = sub["dominant_niche"].to_numpy().copy()
    null = np.empty(N_PERM)
    shuffled = sub.copy()
    for b in range(N_PERM):
        rng.shuffle(niches)
        shuffled["dominant_niche"] = niches
        null[b] = same_pair_share(shuffled)
    p = float((np.sum(null >= obs) + 1) / (N_PERM + 1))

    # Temporal spacing of same-niche returns: gaps between consecutive episodes
    # in the same (donor, niche), in days.
    sub2 = sub.copy()
    sub2["_t"] = pd.to_datetime(sub2["start_ts"])
    gaps = []
    for _, g in sub2.sort_values("_t").groupby(["collection_id", "dominant_niche"]):
        if len(g) >= 2:
            gaps.extend(g["_t"].diff().dropna().dt.total_seconds() / 86400)
    modal_majority = float(np.mean([
        g["dominant_niche"].value_counts(normalize=True).iloc[0] >= 0.5
        for _, g in sub.groupby("collection_id")
    ]))
    return {
        "n_donors_ge2_episodes": int(len(multi)),
        "obs_same_niche_pair_share": round(float(obs), 4),
        "null_same_niche_pair_share": round(float(null.mean()), 4),
        "p_value": round(p, 4),
        "n_same_niche_return_gaps": len(gaps),
        "median_return_gap_days": round(float(np.median(gaps)), 2) if gaps else None,
        "pct_donors_modal_niche_majority": round(modal_majority, 3),
    }




def rq10_retention(ep: pd.DataFrame, acts: pd.DataFrame) -> dict:
    """RQ10 — session continuation after an episode, and dwell during it."""
    plays = acts[acts["activity_type"] == "play"].copy()
    sess = plays.groupby(["collection_id", "session_id"])["_ts"].agg(["min", "max", "size"])
    sess["dur_s"] = (sess["max"] - sess["min"]).dt.total_seconds()

    ep = ep.copy()
    ep["_start"] = pd.to_datetime(ep["start_ts"])
    ep["_end"] = pd.to_datetime(ep["end_ts"])
    ep_sessions = set(zip(ep["collection_id"], ep["session_id"]))

    remaining_obs, remaining_ctl, dwell_in, dwell_out = [], [], [], []
    for cid, g_ep in ep.groupby("collection_id"):
        try:
            donor_sess = sess.loc[cid]
        except KeyError:
            continue
        ctl_sess = donor_sess[~donor_sess.index.isin(
            [s for c, s in ep_sessions if c == cid])]
        donor_plays = plays[plays["collection_id"] == cid]
        for _, e in g_ep.iterrows():
            try:
                srow = donor_sess.loc[e["session_id"]]
            except KeyError:
                continue
            elapsed = (e["_end"] - srow["min"]).total_seconds()
            rem = (srow["max"] - e["_end"]).total_seconds()
            eligible = ctl_sess[ctl_sess["dur_s"] >= elapsed]
            if len(eligible) < 3:
                continue
            remaining_obs.append(rem)
            remaining_ctl.append(float((eligible["dur_s"] - elapsed).median()))

            sp = donor_plays[donor_plays["session_id"] == e["session_id"]]
            in_ep = sp[(sp["_ts"] >= e["_start"]) & (sp["_ts"] <= e["_end"])]
            out_ep = sp[(sp["_ts"] < e["_start"]) | (sp["_ts"] > e["_end"])]
            di = pd.to_numeric(in_ep["play_duration"], errors="coerce").dropna()
            do = pd.to_numeric(out_ep["play_duration"], errors="coerce").dropna()
            if len(di) >= 3 and len(do) >= 3:
                dwell_in.append(float(di.mean()))
                dwell_out.append(float(do.mean()))

    ro, rc = np.array(remaining_obs), np.array(remaining_ctl)
    di_a, do_a = np.array(dwell_in), np.array(dwell_out)
    w1 = stats.wilcoxon(ro, rc) if len(ro) > 10 else None
    w2 = stats.wilcoxon(di_a, do_a) if len(di_a) > 10 else None
    return {
        "n_episodes_matched": int(len(ro)),
        "median_remaining_after_episode_min": round(float(np.median(ro)) / 60, 2),
        "median_remaining_control_min": round(float(np.median(rc)) / 60, 2),
        "median_paired_diff_min": round(float(np.median(ro - rc)) / 60, 2),
        "wilcoxon_p_remaining": round(float(w1.pvalue), 5) if w1 else None,
        "n_episodes_dwell": int(len(di_a)),
        "median_dwell_in_episode_s": round(float(np.median(di_a)), 2),
        "median_dwell_outside_s": round(float(np.median(do_a)), 2),
        "wilcoxon_p_dwell": round(float(w2.pvalue), 5) if w2 else None,
    }




def main() -> None:
    """Run all Phase-2 analyses and write the results JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()
    rng = np.random.default_rng(SEED)

    ep = pd.read_parquet(os.path.join(OUT_DIR, f"episodes_{args.tag}.parquet"))
    sm = pd.read_parquet(os.path.join(OUT_DIR, f"episode_donor_summary_{args.tag}.parquet"))
    donors = select_donors()

    print("Loading store + activity...")
    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    acts = load_all_activity(donors)

    print("\n=== RQ8: correlates of binge intensity ===")
    rq8 = rq8_correlates(donors, acts, sm, id2idx, U, rng)
    print(json.dumps(rq8, indent=2))

    print("\n=== RQ9: niche recurrence ===")
    rq9 = rq9_recurrence(ep, rng)
    print(json.dumps(rq9, indent=2))

    print("\n=== RQ10: retention ===")
    rq10 = rq10_retention(ep, acts)
    print(json.dumps(rq10, indent=2))

    out = os.path.join(OUT_DIR, f"phase2_{args.tag}.json")
    with open(out, "w") as fh:
        json.dump({"rq8": rq8, "rq9": rq9, "rq10": rq10}, fh, indent=2)
    print(f"\nWrote -> {out}")




if __name__ == "__main__":
    main()
