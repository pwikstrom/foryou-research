"""Aggregate the per-donor entropy summaries into a population view.

Reads a ``*_summary.json`` written by ``run_window_entropy.py`` and answers the
population question: across the donor panel, how many collections show a
genuinely low-entropy window (beyond their own time-shuffled null), after
Benjamini-Hochberg control of the false-discovery rate over the panel?
"""

import argparse
import json
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def bh_fdr(pvals: list[float], alpha: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg: return a boolean reject mask at level ``alpha``."""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    thresh = (np.arange(1, n + 1) / n) * alpha
    passed = p[order] <= thresh
    reject = np.zeros(n, dtype=bool)
    if passed.any():
        kmax = np.max(np.where(passed)[0])
        reject[order[: kmax + 1]] = True
    return reject




def main() -> None:
    """Print the panel-level table and BH-FDR significance counts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="donors")
    parser.add_argument("--alpha", type=float, default=0.05)
    args = parser.parse_args()

    path = os.path.join(OUT_DIR, f"embedding_window_entropy_{args.tag}_summary.json")
    with open(path) as fh:
        rows = [r for r in json.load(fh) if r.get("n_measured_windows")]

    rows.sort(key=lambda r: r.get("p_value_min_cos_dist", 1.0))
    p_cos = [r["p_value_min_cos_dist"] for r in rows]
    p_ent = [r["p_value_min_entropy"] for r in rows]
    rej_cos = bh_fdr(p_cos, args.alpha)
    rej_ent = bh_fdr(p_ent, args.alpha)

    print(f"{'collection':28} {'cov':>5} {'wins':>5} {'cosMin':>7} {'cosMed':>7} "
          f"{'p_cos':>6} {'p_ent':>6} {'sig':>4}")
    for i, r in enumerate(rows):
        sig = ("C" if rej_cos[i] else "") + ("E" if rej_ent[i] else "")
        cid = str(r["collection_id"])[:28]
        print(f"{cid:28} {r['span_emb_coverage']:5.2f} {r['n_measured_windows']:5d} "
              f"{r['cos_dist_min']:7.3f} {r['cos_dist_median']:7.3f} "
              f"{r['p_value_min_cos_dist']:6.3f} {r['p_value_min_entropy']:6.3f} {sig:>4}")

    n = len(rows)
    print(f"\nPanel n={n} donors")
    print(f"  raw p<0.05:  cos_dist {sum(p < 0.05 for p in p_cos)}/{n}   "
          f"entropy {sum(p < 0.05 for p in p_ent)}/{n}")
    print(f"  BH-FDR q<{args.alpha}:  cos_dist {int(rej_cos.sum())}/{n}   "
          f"entropy {int(rej_ent.sum())}/{n}")
    print(f"  median cos_dist_min across donors: {np.median([r['cos_dist_min'] for r in rows]):.3f}")
    print(f"  median cos_dist_median across donors: {np.median([r['cos_dist_median'] for r in rows]):.3f}")




if __name__ == "__main__":
    main()
