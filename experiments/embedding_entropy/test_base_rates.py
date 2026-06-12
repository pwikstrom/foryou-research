"""Formal base-rate tests for the episode-character claims (RQ5 / RQ6 / RQ7).

The descriptive readout said episodes are (5) concentrated in lifestyle niches,
(6) heavily single-author, and (7) non-political / low-sensitivity. Each claim
needs its base rate: a niche can dominate episodes simply because donors watch
a lot of it; author concentration is partially mechanical (same-author videos
are similar, and the segmenter selects on similarity); valence must be compared
with the same donors' overall diets, not zero.

Design — matched random draws from each donor's own diet:
    For every episode (a donor, k distinct member videos) draw k distinct
    videos uniformly from that donor's distinct embedded played videos, B
    times. This holds the donor mix and episode sizes fixed and asks "what
    would episodes look like if they were random samples of the same people's
    diets?" — the right null for *enrichment*. (Uniform over distinct videos,
    not play-weighted: a deliberate simplification, noted in the output.)

    * RQ5: pooled member-niche counts vs the draw distribution -> obs/exp ratio
      and permutation p per niche (BH-FDR across the niches tested).
    * RQ6: per-episode dominant-author share, observed vs draws.
    * RQ7: per-donor paired contrast of episode-member means vs diet means
      (political, sensitivity), Wilcoxon signed-rank across donors.
"""

import argparse
import json
import os
import sys
from argparse import Namespace
from collections import Counter

import numpy as np
import pandas as pd
from scipy import stats

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
from aggregate import bh_fdr
from build_episodes import (CUT, MEM, MIN_MINUTES, MIN_VIDEOS,
                            load_directional_store, segment_session, select_donors)

B_DRAWS = 500
SEED = 31
TOP_NICHES = 15
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def collect_episodes(donors: list[str], id2idx: dict, U) -> tuple[list[dict], dict[str, list[str]]]:
    """Re-segment all donors, returning episode member lists and donor diets.

    Args:
        donors: Collection ids to process.
        id2idx: item_id -> row in the directional store.
        U: Directional vector store.

    Returns:
        ``(episodes, diets)`` where each episode dict has ``collection_id`` and
        ``ids`` (member item_ids) and ``diets`` maps donor -> distinct embedded
        item_ids played (the draw urn).
    """
    sargs = Namespace(cut=CUT, mem=MEM, min_videos=MIN_VIDEOS, min_minutes=MIN_MINUTES)
    pl = data_access.load_plays(donors).to_pandas()
    pl["_ts"] = pd.to_datetime(pl["local_timestamp"], errors="coerce")
    pl = pl.dropna(subset=["_ts"])

    episodes: list[dict] = []
    diets: dict[str, list[str]] = {}
    for cid, sub in pl.groupby("collection_id"):
        sub = sub.sort_values("_ts")
        emb = sub[sub["item_id"].isin(id2idx)].copy()
        if emb.empty:
            continue
        diets[str(cid)] = emb["item_id"].astype(str).unique().tolist()
        sess = emb["session_id"].astype("string")
        sess = sess.where(sess.notna(), "na_" + emb.index.astype("string"))
        emb = emb.assign(_sess=sess)
        for _, g in emb.groupby("_sess", sort=False):
            seq = [(iid, id2idx[iid], ts, dur) for iid, ts, dur in
                   zip(g["item_id"], g["_ts"], g["play_duration"])]
            for e in segment_session(seq, U, sargs.cut, sargs.mem,
                                     sargs.min_videos, sargs.min_minutes):
                episodes.append({"collection_id": str(cid), "ids": [str(i) for i in e["ids"]]})
    return episodes, diets




def draw_matched(episodes: list[dict], diets: dict, rng: np.random.Generator) -> list[list[str]]:
    """One full matched draw: per episode, k distinct videos from its donor's diet."""
    out = []
    for e in episodes:
        urn = diets[e["collection_id"]]
        k = min(len(e["ids"]), len(urn))
        idx = rng.choice(len(urn), size=k, replace=False)
        out.append([urn[i] for i in idx])
    return out




def test_niche_enrichment(episodes: list[dict], diets: dict, feat: pd.DataFrame,
                          rng: np.random.Generator, b_draws: int) -> list[dict]:
    """RQ5 — which niches are over-represented in episodes vs matched draws."""
    niche_of = feat["niche_name"]
    obs = Counter()
    for e in episodes:
        obs.update(niche_of.reindex(e["ids"]).dropna())
    top = [n for n, _ in obs.most_common(TOP_NICHES)]

    null_counts = {n: np.zeros(b_draws) for n in top}
    for b in range(b_draws):
        c = Counter()
        for ids in draw_matched(episodes, diets, rng):
            c.update(niche_of.reindex(ids).dropna())
        for n in top:
            null_counts[n][b] = c.get(n, 0)

    rows = []
    for n in top:
        exp = float(null_counts[n].mean())
        p = float((np.sum(null_counts[n] >= obs[n]) + 1) / (b_draws + 1))
        rows.append({"niche": n, "obs": int(obs[n]), "exp": round(exp, 1),
                     "ratio": round(obs[n] / exp, 2) if exp > 0 else None,
                     "p_value": round(p, 4)})
    reject = bh_fdr([r["p_value"] for r in rows], 0.05)
    for r, rej in zip(rows, reject):
        r["fdr_significant"] = bool(rej)
    return rows




def test_author_concentration(episodes: list[dict], diets: dict, feat: pd.DataFrame,
                              rng: np.random.Generator, b_draws: int) -> dict:
    """RQ6 — dominant-author share in episodes vs matched draws."""
    author_of = feat["author"]

    def dom_share(ids: list[str]) -> float:
        a = author_of.reindex(ids).dropna()
        if a.empty:
            return np.nan
        return float(a.value_counts().iloc[0] / len(a))

    obs = np.array([dom_share(e["ids"]) for e in episodes])
    null_med = np.empty(b_draws)
    null_dom = np.empty(b_draws)
    for b in range(b_draws):
        shares = np.array([dom_share(ids) for ids in draw_matched(episodes, diets, rng)])
        null_med[b] = np.nanmedian(shares)
        null_dom[b] = np.nanmean(shares >= 0.5)
    obs_med = float(np.nanmedian(obs))
    obs_dom = float(np.nanmean(obs >= 0.5))
    return {
        "obs_median_dom_share": round(obs_med, 3),
        "null_median_dom_share": round(float(null_med.mean()), 3),
        "p_median": round(float((np.sum(null_med >= obs_med) + 1) / (b_draws + 1)), 4),
        "obs_pct_author_dominated": round(obs_dom, 3),
        "null_pct_author_dominated": round(float(null_dom.mean()), 3),
        "p_dominated": round(float((np.sum(null_dom >= obs_dom) + 1) / (b_draws + 1)), 4),
    }




def test_valence(episodes: list[dict], diets: dict, feat: pd.DataFrame) -> dict:
    """RQ7 — per-donor paired contrast of episode vs diet valence."""
    out = {}
    for col in ("political_score", "sensitivity_score"):
        vals = feat[col]
        per_donor: dict[str, list[float]] = {}
        for e in episodes:
            v = pd.to_numeric(vals.reindex(e["ids"]), errors="coerce").dropna()
            if len(v):
                per_donor.setdefault(e["collection_id"], []).append(float(v.mean()))
        ep_mean, diet_mean = [], []
        for cid, eps in per_donor.items():
            d = pd.to_numeric(vals.reindex(diets[cid]), errors="coerce").dropna()
            if len(d):
                ep_mean.append(float(np.mean(eps)))
                diet_mean.append(float(d.mean()))
        ep_a, di_a = np.array(ep_mean), np.array(diet_mean)
        try:
            w = stats.wilcoxon(ep_a, di_a)
            pw = float(w.pvalue)
        except ValueError:
            pw = None
        out[col] = {
            "n_donors": len(ep_a),
            "median_episode": round(float(np.median(ep_a)), 4),
            "median_diet": round(float(np.median(di_a)), 4),
            "median_paired_diff": round(float(np.median(ep_a - di_a)), 4),
            "wilcoxon_p": round(pw, 5) if pw is not None else None,
        }
    return out




def main() -> None:
    """Run all three base-rate tests and write the results JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--b-draws", type=int, default=B_DRAWS)
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    donors = select_donors()
    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    feat = data_access.load_video_features()
    rng = np.random.default_rng(SEED)

    print("Segmenting + collecting members...")
    episodes, diets = collect_episodes(donors, id2idx, U)
    print(f"  {len(episodes)} episodes across {len({e['collection_id'] for e in episodes})} donors")

    print(f"\n=== RQ5: niche enrichment (B={args.b_draws} matched draws) ===")
    niches = test_niche_enrichment(episodes, diets, feat, rng, args.b_draws)
    for r in niches:
        sig = " *" if r["fdr_significant"] else ""
        print(f"  {r['niche']:32} obs={r['obs']:4d}  exp={r['exp']:7.1f}  "
              f"ratio={r['ratio']}  p={r['p_value']}{sig}")

    print("\n=== RQ6: author concentration ===")
    authors = test_author_concentration(episodes, diets, feat, rng, args.b_draws)
    for k, v in authors.items():
        print(f"  {k}: {v}")

    print("\n=== RQ7: valence vs own diet (paired per donor) ===")
    valence = test_valence(episodes, diets, feat)
    for k, v in valence.items():
        print(f"  {k}: {v}")

    out = os.path.join(OUT_DIR, f"base_rates_{args.tag}.json")
    with open(out, "w") as fh:
        json.dump({"niche_enrichment": niches, "author_concentration": authors,
                   "valence": valence}, fh, indent=2)
    print(f"\nWrote -> {out}")




if __name__ == "__main__":
    main()
