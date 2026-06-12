"""Render a stratified sample of episodes for human calibration of the cut.

The 0.5 focus cut is a judgment call; this script makes it inspectable. It
samples ~20 episodes stratified by focus — the tightest, the mid-range, and the
ones sitting just under the cut (where over-segmentation would show first) —
re-derives each episode's member videos, and writes a markdown file listing
every member's niche, author and story, with a blank verdict line per episode.
Judge each episode "coherent binge? yes / borderline / no"; if the near-cut
stratum reads as noise, the cut should tighten (the spec curve quantifies what
that does to the headlines).
"""

import argparse
import os
import sys
from argparse import Namespace

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
from build_episodes import (CUT, MEM, MIN_MINUTES, MIN_VIDEOS,
                            load_directional_store, segment_session)

SEED = 23
N_TIGHT = 6
N_MID = 8
N_NEAR_CUT = 6
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def pick_sample(ep: pd.DataFrame) -> pd.DataFrame:
    """Stratify the episode table by focus: tightest / mid-range / near-cut."""
    ep = ep.sort_values("focus")
    tight = ep.head(N_TIGHT)
    mid = ep[(ep["focus"] >= 0.30) & (ep["focus"] < 0.42)].sample(
        min(N_MID, ((ep["focus"] >= 0.30) & (ep["focus"] < 0.42)).sum()),
        random_state=SEED)
    near = ep[ep["focus"] >= 0.42].sample(
        min(N_NEAR_CUT, (ep["focus"] >= 0.42).sum()), random_state=SEED)
    out = pd.concat([tight.assign(stratum="tightest"),
                     mid.assign(stratum="mid"),
                     near.assign(stratum="near-cut")])
    return out.drop_duplicates(subset=["collection_id", "start_ts"])




def recover_members(sample: pd.DataFrame, id2idx: dict, U) -> dict[tuple, list[str]]:
    """Re-segment the sampled donors and match episodes back by start time.

    Segmentation is deterministic, so re-running it with the table's defaults
    reproduces the same episodes; matching on ``(collection_id, start_ts)``
    recovers each sampled episode's member item_ids.

    Args:
        sample: The sampled episode rows.
        id2idx: item_id -> row in the directional store.
        U: Directional vector store.

    Returns:
        A dict ``(collection_id, start_ts_string) -> [item_id, ...]``.
    """
    sargs = Namespace(cut=CUT, mem=MEM, min_videos=MIN_VIDEOS, min_minutes=MIN_MINUTES)
    donors = sorted(sample["collection_id"].unique())
    pl = data_access.load_plays(donors).to_pandas()
    pl["_ts"] = pd.to_datetime(pl["local_timestamp"], errors="coerce")
    pl = pl.dropna(subset=["_ts"])

    members: dict[tuple, list[str]] = {}
    for cid, sub in pl.groupby("collection_id"):
        wanted = set(sample[sample["collection_id"] == cid]["start_ts"])
        sub = sub.sort_values("_ts")
        emb = sub[sub["item_id"].isin(id2idx)].copy()
        sess = emb["session_id"].astype("string")
        sess = sess.where(sess.notna(), "na_" + emb.index.astype("string"))
        emb = emb.assign(_sess=sess)
        for _, g in emb.groupby("_sess", sort=False):
            seq = [(iid, id2idx[iid], ts, dur) for iid, ts, dur in
                   zip(g["item_id"], g["_ts"], g["play_duration"])]
            for e in segment_session(seq, U, sargs.cut, sargs.mem,
                                     sargs.min_videos, sargs.min_minutes):
                key_ts = str(e["start_ts"])
                if key_ts in wanted:
                    members[(cid, key_ts)] = e["ids"]
    return members




def main() -> None:
    """Write the calibration markdown to ``tmp/episode_review_sample.md``."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    ep = pd.read_parquet(os.path.join(OUT_DIR, f"episodes_{args.tag}.parquet"))
    sample = pick_sample(ep)
    print(f"Sampled {len(sample)} episodes "
          f"({sample['stratum'].value_counts().to_dict()})")

    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    members = recover_members(sample, id2idx, U)
    feat = data_access.load_video_features()

    lines = [
        "# Episode calibration sample",
        "",
        "Judge each episode: **coherent binge? yes / borderline / no** — fill the",
        "Verdict line. Strata: *tightest* (sanity anchors), *mid* (typical),",
        "*near-cut* (focus >= 0.42 — if these read as noise, tighten the cut).",
        "",
    ]
    for i, (_, r) in enumerate(sample.sort_values(["stratum", "focus"]).iterrows(), 1):
        key = (r["collection_id"], str(r["start_ts"]))
        ids = members.get(key, [])
        lines += [
            f"## {i}. [{r['stratum']}]  focus={r['focus']:.3f}  "
            f"diameter={r['diameter']:.3f}  straightness={r['straightness']}",
            f"*{r['collection_id'][:16]}… · {r['start_ts']} · "
            f"{r['n_distinct']} videos / {r['duration_min']} min · "
            f"dominant: {r['dominant_niche']} ({r['dominant_niche_share']:.0%}) · "
            f"{r['n_authors']} authors*",
            "",
        ]
        # The map's hover story exists only for t-SNE-sampled videos, so pull
        # stories via the labels loader; niche/author come from the feature table.
        labels = data_access.load_video_labels(set(ids))
        for iid in ids:
            lab = labels.get(iid, {})
            if iid in feat.index:
                niche = feat.at[iid, "niche_name"] or "?"
                author = feat.at[iid, "author"] or "?"
            else:
                niche = author = "?"
            story = (lab.get("story") or "").replace("\n", " ")[:140]
            lines.append(f"- [{niche}] (@{author}) {story}")
        lines += ["", "**Verdict:** ______", ""]

    out = os.path.join(OUT_DIR, "episode_review_sample.md")
    with open(out, "w") as fh:
        fh.write("\n".join(lines))
    print(f"Wrote -> {out}")




if __name__ == "__main__":
    main()
