"""RQ1 — per-donor time-shuffle null for the episode count.

The episode segmenter is an absolute-threshold detector: over tens of thousands
of plays even a randomly-ordered diet will occasionally put four mutually
similar videos back-to-back, so "donor has >=1 episode" conflates real temporal
clustering with the luck of large numbers. This script quantifies the chance
component per donor: shuffle WHICH video occupies each play slot across the
donor's whole history (timestamps, session boundaries and session sizes all stay
fixed; the segmenter still hard-breaks at session edges), re-run the identical
segmenter, and repeat ``n_perm`` times. The donor's p-value is the share of
shuffles producing at least as many episodes as observed; BH-FDR is applied
across donors.

A donor whose diet is narrow overall produces many episodes under shuffling too
— and is correctly *not* significant: the null asks whether similar content
clusters in time beyond what the diet alone implies. (Note one deliberate
consequence: a donor who binges by devoting whole sessions to one topic is
partially absorbed into the null only if those sessions are large; because the
shuffle is across the whole history, cross-session mixing still breaks
single-topic sessions apart, so session-level binges do count as signal.)

Run after ``build_episodes.py`` (same segmentation defaults):
    python experiments/embedding_entropy/run_episode_null.py
"""

import argparse
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
from aggregate import bh_fdr
from build_episodes import (CUT, MEM, MIN_MINUTES, MIN_VIDEOS,
                            load_directional_store, select_donors)

N_PERM = 200
SEED = 11
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def count_episodes(codes: np.ndarray, ts_ns: np.ndarray, sess_offsets: np.ndarray,
                   U: np.ndarray, cut: float, mem: int, min_videos: int,
                   min_span_ns: int) -> int:
    """Count episodes the segmenter finds in one ordering of a donor's plays.

    A lean, count-only mirror of ``build_episodes.segment_session`` (same rule:
    grow while the next distinct video sits within ``cut`` of the centroid of
    the last ``mem`` members; repeats extend the span; close on threshold break
    or session edge; keep if >= ``min_videos`` distinct over >= ``min_span_ns``).

    Args:
        codes: ``(n,)`` row indices into ``U``, one per play slot, in slot order.
        ts_ns: ``(n,)`` slot timestamps (int64 ns), aligned to ``codes``.
        sess_offsets: ``(s+1,)`` slot offsets delimiting sessions.
        U: Directional vector store.
        cut: Focus threshold on cosine distance to the recent centroid.
        mem: Number of recent members in the centroid.
        min_videos: Minimum distinct videos to keep an episode.
        min_span_ns: Minimum episode span in nanoseconds.

    Returns:
        The number of qualifying episodes.
    """
    n_eps = 0
    for s in range(len(sess_offsets) - 1):
        lo, hi = sess_offsets[s], sess_offsets[s + 1]
        members: list[int] = []
        seen: set[int] = set()
        start_ts = end_ts = 0
        for i in range(lo, hi):
            r = int(codes[i])
            t = int(ts_ns[i])
            if not members:
                members = [r]
                seen = {r}
                start_ts = end_ts = t
                continue
            if r in seen:
                end_ts = t
                continue
            centroid = U[members[-mem:]].mean(axis=0)
            dist = 1.0 - float(U[r] @ centroid)
            if dist <= cut:
                members.append(r)
                seen.add(r)
                end_ts = t
            else:
                if len(members) >= min_videos and (end_ts - start_ts) >= min_span_ns:
                    n_eps += 1
                members = [r]
                seen = {r}
                start_ts = end_ts = t
        if len(members) >= min_videos and (end_ts - start_ts) >= min_span_ns:
            n_eps += 1
    return n_eps




def donor_slots(plays: pd.DataFrame, id2idx: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build the slot arrays (codes, timestamps, session offsets) for one donor.

    Mirrors ``build_episodes.run_donor``'s session handling: embedded plays
    only, time-sorted, ``session_id`` as the grouping key with rows lacking one
    isolated as singletons.

    Args:
        plays: The donor's play rows with a parsed ``_ts`` column.
        id2idx: item_id -> row index in the directional store.

    Returns:
        ``(codes, ts_ns, sess_offsets)`` ready for :func:`count_episodes`.
    """
    emb = plays[plays["item_id"].isin(id2idx)].sort_values("_ts")
    sess = emb["session_id"].astype("string")
    sess = sess.where(sess.notna(), "na_" + emb.index.astype("string"))
    # Stable sort by session, preserving time order inside each session, so
    # sessions become contiguous slot ranges.
    emb = emb.assign(_sess=sess)
    emb = emb.sort_values(["_sess", "_ts"], kind="stable")
    codes = np.fromiter((id2idx[i] for i in emb["item_id"]), dtype=np.int64, count=len(emb))
    ts_ns = emb["_ts"].astype("int64").to_numpy()
    sess_sizes = emb.groupby("_sess", sort=False).size().to_numpy()
    sess_offsets = np.concatenate([[0], np.cumsum(sess_sizes)])
    return codes, ts_ns, sess_offsets




def main() -> None:
    """Run the per-donor null over the analyzable population and apply BH-FDR."""
    parser = argparse.ArgumentParser(description="RQ1 per-donor episode-count null.")
    parser.add_argument("--collections-file", default=None)
    parser.add_argument("--cut", type=float, default=CUT)
    parser.add_argument("--mem", type=int, default=MEM)
    parser.add_argument("--min-videos", type=int, default=MIN_VIDEOS)
    parser.add_argument("--min-minutes", type=float, default=MIN_MINUTES)
    parser.add_argument("--n-perm", type=int, default=N_PERM)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()
    min_span_ns = int(args.min_minutes * 60 * 1e9)

    if args.collections_file:
        with open(args.collections_file) as fh:
            donors = fh.read().split()
    else:
        donors = select_donors()
    if args.limit:
        donors = donors[:args.limit]
    print(f"Donors: {len(donors)}  n_perm={args.n_perm}  "
          f"(cut={args.cut}, mem={args.mem}, min_videos={args.min_videos}, "
          f"min_minutes={args.min_minutes})")

    corpus_mean = data_access.corpus_mean()
    id2idx, U = load_directional_store(corpus_mean)
    print(f"Store: {len(id2idx):,} vectors")

    pl = data_access.load_plays(donors).to_pandas()
    pl["_ts"] = pd.to_datetime(pl["local_timestamp"], errors="coerce")
    pl = pl.dropna(subset=["_ts"])

    inv = pd.read_parquet(os.path.join(OUT_DIR, "collection_inventory.parquet"))
    cov = inv.set_index("collection_id")["coverage"].to_dict()

    results = []
    for di, cid in enumerate(donors):
        sub = pl[pl["collection_id"] == cid]
        if sub.empty:
            continue
        codes, ts_ns, offs = donor_slots(sub, id2idx)
        obs = count_episodes(codes, ts_ns, offs, U, args.cut, args.mem,
                             args.min_videos, min_span_ns)
        rec = {"collection_id": cid, "coverage": cov.get(cid),
               "n_emb_play": int(len(codes)), "obs_episodes": obs}
        if obs == 0:
            rec.update({"null_mean": None, "null_p95": None, "p_value": 1.0})
        else:
            rng = np.random.default_rng(SEED + di)
            null = np.empty(args.n_perm, dtype=np.int64)
            for b in range(args.n_perm):
                perm = rng.permutation(codes)
                null[b] = count_episodes(perm, ts_ns, offs, U, args.cut, args.mem,
                                         args.min_videos, min_span_ns)
            p = float((np.sum(null >= obs) + 1) / (args.n_perm + 1))
            rec.update({"null_mean": round(float(null.mean()), 2),
                        "null_p95": int(np.percentile(null, 95)),
                        "p_value": round(p, 4)})
        results.append(rec)
        print(f"  [{di + 1}/{len(donors)}] {cid[:24]:24} obs={obs:3d} "
              f"null_mean={rec['null_mean']} p={rec['p_value']}")

    pvals = [r["p_value"] for r in results]
    reject = bh_fdr(pvals, 0.05)
    for r, rej in zip(results, reject):
        r["fdr_significant"] = bool(rej)

    out = os.path.join(OUT_DIR, f"episode_null_{args.tag}.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nWrote -> {out}")

    n = len(results)
    n_obs = sum(r["obs_episodes"] > 0 for r in results)
    n_raw = sum(r["p_value"] < 0.05 for r in results)
    n_fdr = int(reject.sum())
    print("\n=== RQ1 SUMMARY ===")
    print(f"Analyzable donors: {n}")
    print(f"Detector fired (>=1 episode): {n_obs} ({n_obs/n:.0%})")
    print(f"Beyond own chance baseline, raw p<0.05: {n_raw} ({n_raw/n:.0%})")
    print(f"Beyond chance, BH-FDR q<0.05: {n_fdr} ({n_fdr/n:.0%})")




if __name__ == "__main__":
    main()
