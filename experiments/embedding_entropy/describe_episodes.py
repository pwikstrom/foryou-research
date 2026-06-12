"""Descriptive readout of the episode table (early answers to RQ1–RQ7).

Reads the Phase-0 outputs and prints: prevalence across donors (conditional on
observability), within-donor exposure, episode character, the binge-vs-drift
split (RQ2), the same- vs cross-author split (RQ6), the most "bingeable" niches
(RQ5), and valence vs the corpus baseline (RQ7). Purely descriptive — the
inferential per-donor null (RQ1) and base-rate tests come in Phase 1.
"""

import argparse
import os

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))

# Geometry cuts for the binge-vs-drift taxonomy (descriptive, tunable).
TIGHT_DIAMETER = 0.5     # below = a compact cluster
STRAIGHT_HI = 0.5        # above = a directed path rather than a wander




def classify(ep: pd.DataFrame) -> pd.Series:
    """Label each episode stationary-binge / directed-drift / meander."""
    diam = ep["diameter"]
    straight = ep["straightness"].fillna(0.0)
    out = pd.Series("meander", index=ep.index)
    out[diam < TIGHT_DIAMETER] = "stationary_binge"
    out[(diam >= TIGHT_DIAMETER) & (straight >= STRAIGHT_HI)] = "directed_drift"
    return out




def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    ep = pd.read_parquet(os.path.join(OUT_DIR, f"episodes_{args.tag}.parquet"))
    sm = pd.read_parquet(os.path.join(OUT_DIR, f"episode_donor_summary_{args.tag}.parquet"))

    n_donors = len(sm)
    with_ep = (sm["n_episodes"] > 0).sum()
    print("=== PREVALENCE (conditional on observability) ===")
    print(f"Analyzable donors: {n_donors}")
    print(f"Donors with >=1 episode: {with_ep} ({with_ep/n_donors:.0%})")
    for thr in (1, 3, 10):
        n = (sm["n_episodes"] >= thr).sum()
        print(f"  >= {thr} episodes: {n} ({n/n_donors:.0%})")
    print(f"Episodes total: {len(ep):,}")
    print("Within-donor exposure (donors with >=1 episode):")
    act = sm[sm["n_episodes"] > 0]
    for col in ("frac_plays_in_episodes", "frac_watchtime_in_episodes"):
        v = pd.to_numeric(act[col], errors="coerce").dropna()
        print(f"  {col}: median={v.median():.3%}  p90={v.quantile(.9):.3%}")
    print(f"  n_episodes per donor: median={act['n_episodes'].median():.0f}  max={act['n_episodes'].max():.0f}")

    print("\n=== EPISODE CHARACTER ===")
    for col in ("n_distinct", "duration_min", "focus", "diameter", "step_mean", "straightness", "repeat_rate"):
        v = pd.to_numeric(ep[col], errors="coerce").dropna()
        print(f"  {col:14} median={v.median():.3f}  p10={v.quantile(.1):.3f}  p90={v.quantile(.9):.3f}")
    diluted = (ep["n_interleaved"] > ep["n_plays"]).mean()
    print(f"  episodes with more interleaved-unembedded plays than members: {diluted:.0%}")

    print("\n=== BINGE vs DRIFT (RQ2) ===")
    ep = ep.assign(kind=classify(ep))
    print(ep["kind"].value_counts(normalize=True).round(3).to_string())

    print("\n=== SAME- vs CROSS-AUTHOR (RQ6) ===")
    single = (ep["dominant_author_share"] >= 0.999)
    mostly = (ep["dominant_author_share"] >= 0.5)
    print(f"  single-author episodes (share=1.0):  {single.mean():.0%}")
    print(f"  author-dominated (share>=0.5):       {mostly.mean():.0%}")
    print(f"  median distinct authors per episode: {ep['n_authors'].median():.0f}")
    print("  same-author share by kind:")
    print(ep.groupby("kind")["dominant_author_share"].median().round(3).to_string())

    print("\n=== MOST 'BINGEABLE' NICHES (RQ5) ===")
    print(ep["dominant_niche"].value_counts().head(12).to_string())

    print("\n=== VALENCE vs baseline (RQ7) ===")
    for col in ("mean_political", "mean_sensitivity"):
        v = pd.to_numeric(ep[col], errors="coerce").dropna()
        print(f"  episode {col}: median={v.median():.3f}  p90={v.quantile(.9):.3f}")




if __name__ == "__main__":
    main()
