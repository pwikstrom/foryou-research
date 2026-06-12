"""Phase 3 — RQ11: can an episode's onset be predicted from what precedes it?

Unit: every embedded play with at least ``LOOKBACK`` embedded predecessors in
its session. Label: this play is the FIRST member of an episode (the segmenter's
onset). Features come only from the preceding plays / events — never from the
play itself or anything after it.

Two feature families, separated by design so the ablation can distinguish
"the feed announces itself by narrowing" from "a distinct precursor signature":

* **Momentum** — semantic state of the last ``LOOKBACK`` plays: their mean
  pairwise cosine distance (is the stream already narrowing?), the last step
  distance, and the trend of step distances. Note the segmenter opens an
  episode only at 4 consecutive similar videos, so pre-onset plays are by
  construction not yet a run — momentum is a fair, not circular, predictor.
* **Precursor** — behavioural/contextual: recent dwell level and trend, time
  in session, position in session, hour of day, same-author streak, and
  whether the donor faved / followed / searched in the last 10 minutes
  (seed actions).

Model: logistic regression (class-weighted, scaled), per-donor time-ordered
split (first 70% of each donor's candidates train, last 30% test — no
leakage). Onsets are rare (~0.1% of candidates), so headline metrics are
PR-AUC against the base rate and lift in the top 1% of scores; AUROC is
reported for completeness. Prior work in this programme (linger -> feed null)
predicts weak results; the point is to measure, not to hope.
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
from build_episodes import load_directional_store, select_donors

LOOKBACK = 5
SEED_WINDOW_MIN = 10.0
TRAIN_FRAC = 0.7
SEED = 47
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))

MOMENTUM_COLS = ["mom_mean_pairwise", "mom_last_step", "mom_step_trend", "mom_dist_to_centroid"]
PRECURSOR_COLS = ["dwell_mean", "dwell_trend", "session_elapsed_min", "session_position",
                  "hour_sin", "hour_cos", "same_author_streak", "seed_action_10m"]




def build_candidates(donors: list[str], id2idx: dict, U: np.ndarray,
                     ep: pd.DataFrame, feat_author: pd.Series) -> pd.DataFrame:
    """Build the candidate table: one row per eligible embedded play.

    Args:
        donors: Analyzable donor ids.
        id2idx: item_id -> row in the directional store.
        U: Directional vector store.
        ep: The v1 episode table (for onset labels).
        feat_author: item_id -> author lookup.

    Returns:
        A DataFrame with label ``y``, the momentum/precursor features, and
        bookkeeping columns (donor, timestamp).
    """
    onsets = set(zip(ep["collection_id"], ep["start_ts"].astype(str)))

    acts = pq.read_table(
        os.path.join(data_access.RECODED_DIR, data_access.COLLECTIONS_FILE),
        columns=["collection_id", "item_id", "local_timestamp", "activity_type",
                 "play_duration", "session_id"],
        filters=[("collection_id", "in", donors)],
    ).to_pandas()
    acts["_ts"] = pd.to_datetime(acts["local_timestamp"], errors="coerce")
    acts = acts.dropna(subset=["_ts"])
    plays = acts[acts["activity_type"] == "play"]
    seeds = acts[acts["activity_type"].isin(["fave", "following", "search"])]

    rows = []
    for cid, g in plays.groupby("collection_id"):
        g = g.sort_values("_ts")
        emb = g[g["item_id"].isin(id2idx)].copy()
        if emb.empty:
            continue
        sess = emb["session_id"].astype("string")
        sess = sess.where(sess.notna(), "na_" + emb.index.astype("string"))
        emb = emb.assign(_sess=sess)
        seed_ts = np.sort(seeds[seeds["collection_id"] == cid]["_ts"].astype("int64").to_numpy())

        for _, s in emb.groupby("_sess", sort=False):
            n = len(s)
            if n <= LOOKBACK:
                continue
            items = s["item_id"].to_numpy()
            ridx = np.fromiter((id2idx[i] for i in items), dtype=np.int64, count=n)
            ts = s["_ts"].to_numpy()
            ts_ns = s["_ts"].astype("int64").to_numpy()
            dur = pd.to_numeric(s["play_duration"], errors="coerce").fillna(0.0).to_numpy()
            authors = feat_author.reindex(items).to_numpy()
            sess_start_ns = ts_ns[0]

            for t in range(LOOKBACK, n):
                win = ridx[t - LOOKBACK:t]
                V = U[win]
                sims = V @ V.T
                iu = np.triu_indices(LOOKBACK, k=1)
                steps = 1.0 - np.einsum("ij,ij->i", V[1:], V[:-1])
                centroid = V.mean(axis=0)

                hour = pd.Timestamp(ts[t]).hour + pd.Timestamp(ts[t]).minute / 60
                k_seed = np.searchsorted(seed_ts, [ts_ns[t] - int(SEED_WINDOW_MIN * 60 * 1e9), ts_ns[t]])
                streak = 0
                for back in range(t - 1, max(t - 1 - LOOKBACK, -1), -1):
                    if authors[back] is not None and authors[back] == authors[t - 1]:
                        streak += 1
                    else:
                        break
                d_win = dur[t - LOOKBACK:t]
                rows.append({
                    "collection_id": cid,
                    "ts_ns": int(ts_ns[t]),
                    "y": int((cid, str(pd.Timestamp(ts[t]))) in onsets),
                    "mom_mean_pairwise": float(1.0 - sims[iu].mean()),
                    "mom_last_step": float(steps[-1]),
                    "mom_step_trend": float(steps[-1] - steps[0]),
                    "mom_dist_to_centroid": float(1.0 - V[-1] @ (centroid / max(np.linalg.norm(centroid), 1e-8))),
                    "dwell_mean": float(d_win.mean()),
                    "dwell_trend": float(d_win[-2:].mean() - d_win[:2].mean()),
                    "session_elapsed_min": float((ts_ns[t] - sess_start_ns) / 6e10),
                    "session_position": float(t / n),
                    "hour_sin": float(np.sin(2 * np.pi * hour / 24)),
                    "hour_cos": float(np.cos(2 * np.pi * hour / 24)),
                    "same_author_streak": float(streak),
                    "seed_action_10m": float(k_seed[1] > k_seed[0]),
                })
    return pd.DataFrame(rows)




def evaluate(cand: pd.DataFrame, cols: list[str], label: str) -> dict:
    """Train/evaluate one feature set with a per-donor time-ordered split."""
    cand = cand.sort_values(["collection_id", "ts_ns"])
    tr_idx, te_idx = [], []
    for _, g in cand.groupby("collection_id"):
        k = int(len(g) * TRAIN_FRAC)
        tr_idx.extend(g.index[:k])
        te_idx.extend(g.index[k:])
    tr, te = cand.loc[tr_idx], cand.loc[te_idx]
    if tr["y"].sum() < 10 or te["y"].sum() < 5:
        return {"feature_set": label, "note": "too few onsets for a split"}

    sc = StandardScaler()
    Xtr = sc.fit_transform(tr[cols])
    Xte = sc.transform(te[cols])
    model = LogisticRegression(class_weight="balanced", max_iter=2000)
    model.fit(Xtr, tr["y"])
    score = model.predict_proba(Xte)[:, 1]

    base = float(te["y"].mean())
    pr = float(average_precision_score(te["y"], score))
    roc = float(roc_auc_score(te["y"], score))
    k = max(int(len(te) * 0.01), 1)
    top = te.iloc[np.argsort(-score)[:k]]
    lift = float(top["y"].mean() / base) if base > 0 else None
    coefs = {c: round(float(w), 3) for c, w in zip(cols, model.coef_[0])}
    return {
        "feature_set": label, "n_train": len(tr), "n_test": len(te),
        "onsets_train": int(tr["y"].sum()), "onsets_test": int(te["y"].sum()),
        "base_rate": round(base, 5), "pr_auc": round(pr, 5),
        "pr_auc_over_base": round(pr / base, 2) if base > 0 else None,
        "roc_auc": round(roc, 4), "lift_at_top1pct": round(lift, 2) if lift else None,
        "coefficients": coefs,
    }




def main() -> None:
    """Build candidates, run the three ablations, write the results JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    ep = pd.read_parquet(os.path.join(OUT_DIR, f"episodes_{args.tag}.parquet"))
    donors = select_donors()
    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    feat = data_access.load_video_features()

    print("Building candidate table...")
    cand = build_candidates(donors, id2idx, U, ep, feat["author"])
    print(f"  {len(cand):,} candidates, {int(cand['y'].sum())} onsets "
          f"(base rate {cand['y'].mean():.4%})")
    cand.to_parquet(os.path.join(OUT_DIR, f"phase3_candidates_{args.tag}.parquet"))

    results = [
        evaluate(cand, MOMENTUM_COLS, "momentum_only"),
        evaluate(cand, PRECURSOR_COLS, "precursor_only"),
        evaluate(cand, MOMENTUM_COLS + PRECURSOR_COLS, "full"),
    ]
    for r in results:
        print(json.dumps(r, indent=2))

    out = os.path.join(OUT_DIR, f"phase3_{args.tag}.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"Wrote -> {out}")




if __name__ == "__main__":
    main()
