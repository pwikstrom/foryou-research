"""Specification curve over the episode-segmentation parameters.

Every headline number in the study is downstream of the segmenter's knobs
(focus cut, centroid memory, minimum length). This script re-segments the full
analyzable population under a grid of specifications and reports how each
headline claim moves: prevalence (% donors with >=1 episode), within-donor
exposure, episode size/duration, the creator-loyalty share, valence, and the
binge-vs-drift split. Claims that hold across the grid are robust; claims that
exist only at one knob setting are artefacts of that knob.

Data (directional store, plays, features) is loaded once; each spec is a pure
re-segmentation pass (~seconds per spec), so the whole grid runs in one job.
"""

import argparse
import os
import sys
from argparse import Namespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet  # noqa: F401

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
from build_episodes import load_directional_store, run_donor, select_donors
from describe_episodes import classify

# The grid: focus cut x centroid memory (min_videos=4, min_minutes=3), plus one
# stricter-length variant at the central spec.
SPECS = [
    {"cut": c, "mem": m, "min_videos": 4, "min_minutes": 3.0}
    for c in (0.4, 0.5, 0.6) for m in (3, 6, 12)
] + [{"cut": 0.5, "mem": 6, "min_videos": 6, "min_minutes": 3.0}]

OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def spec_headlines(rows: list[dict], summaries: list[dict], n_donors: int) -> dict:
    """Reduce one spec's episodes + donor summaries to the headline claims.

    Args:
        rows: Episode records across all donors for this spec.
        summaries: Per-donor summaries for this spec.
        n_donors: Size of the analyzable population (the denominator).

    Returns:
        A flat dict of headline metrics.
    """
    sm = pd.DataFrame(summaries)
    fired = sm[sm["n_episodes"] > 0]
    out = {
        "n_episodes": len(rows),
        "pct_donors_ge1": round(len(fired) / n_donors, 3),
        "median_exposure_plays": None, "median_n_distinct": None,
        "median_duration_min": None, "pct_author_dominated": None,
        "median_political": None, "median_sensitivity": None,
        "pct_stationary": None, "pct_directed_drift": None,
        "median_straightness": None,
    }
    if fired.empty or not rows:
        return out
    ep = pd.DataFrame(rows)
    kind = classify(ep)
    out.update({
        "median_exposure_plays": round(float(pd.to_numeric(
            fired["frac_plays_in_episodes"], errors="coerce").median()), 5),
        "median_n_distinct": float(ep["n_distinct"].median()),
        "median_duration_min": round(float(ep["duration_min"].median()), 2),
        "pct_author_dominated": round(float((ep["dominant_author_share"] >= 0.5).mean()), 3),
        "median_political": round(float(pd.to_numeric(ep["mean_political"], errors="coerce").median()), 4),
        "median_sensitivity": round(float(pd.to_numeric(ep["mean_sensitivity"], errors="coerce").median()), 4),
        "pct_stationary": round(float((kind == "stationary_binge").mean()), 3),
        "pct_directed_drift": round(float((kind == "directed_drift").mean()), 3),
        "median_straightness": round(float(pd.to_numeric(ep["straightness"], errors="coerce").median()), 3),
    })
    return out




def main() -> None:
    """Run the segmentation grid and write the specification-curve table."""
    parser = argparse.ArgumentParser(description="Episode segmentation spec curve.")
    parser.add_argument("--limit", type=int, default=None, help="First N donors only.")
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    donors = select_donors()
    if args.limit:
        donors = donors[:args.limit]
    print(f"Donors: {len(donors)}  specs: {len(SPECS)}")

    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    feat = data_access.load_video_features()
    pl = data_access.load_plays(donors).to_pandas()
    pl["_ts"] = pd.to_datetime(pl["local_timestamp"], errors="coerce")
    pl = pl.dropna(subset=["_ts"])
    by_donor = {cid: g for cid, g in pl.groupby("collection_id")}

    table = []
    for si, spec in enumerate(SPECS):
        sargs = Namespace(**spec)
        rows: list[dict] = []
        summaries: list[dict] = []
        for cid in donors:
            sub = by_donor.get(cid)
            if sub is None or sub.empty:
                continue
            r, s = run_donor(cid, sub, id2idx, U, feat, sargs)
            rows.extend(r)
            summaries.append(s)
        head = {**spec, **spec_headlines(rows, summaries, len(donors))}
        table.append(head)
        print(f"  [{si + 1}/{len(SPECS)}] cut={spec['cut']} mem={spec['mem']} "
              f"minv={spec['min_videos']}: n_ep={head['n_episodes']} "
              f"donors>=1={head['pct_donors_ge1']:.0%}")

    df = pd.DataFrame(table)
    out = os.path.join(OUT_DIR, f"spec_curve_{args.tag}.parquet")
    pa.parquet.write_table(pa.Table.from_pandas(df, preserve_index=False), out)
    print(f"\nWrote -> {out}")
    print(df.to_string(index=False))




if __name__ == "__main__":
    main()
