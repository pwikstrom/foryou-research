"""Phase 0 — segment binge episodes and build the episode table.

The spine of the study. For every analyzable donor, scan each viewing session
in time order and grow **focus episodes**: maximal within-session runs whose
content stays semantically focused on the embeddings. Each episode is then
measured for its geometry (stationary binge vs directed drift) and its content
(topic, author, valence), producing one row per episode — the table every later
research question reads from.

Segmentation rule (per session, on embedded plays, distinct videos):
    grow the current episode while the next distinct video's **mean cosine
    distance to the centroid of the last `mem` members ≤ `cut`**; otherwise close
    the episode (if it has ≥ `min_videos` distinct videos over ≥ `min_minutes`)
    and start a new one. `session_id` boundaries hard-break episodes; repeated
    plays of a video already in the episode extend its span but are not new
    members. `mem` controls drift tolerance (small = follows a moving topic,
    large = strict cluster); it and `cut` are the specification-curve knobs.

Outputs (under `tmp/`, build-excluded): `episodes_<tag>.parquet` (one row per
episode) and `episode_donor_summary_<tag>.parquet` (one row per donor).
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet  # noqa: F401

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access
import entropy_metrics
import fyp.embeddings as embeddings

# Locked population floors (design note §10).
FLOOR_COVERAGE = 0.15
FLOOR_N_PLAY = 5000
FLOOR_N_DAYS = 30
FLOOR_N_EMB = 1000

CUT = 0.5
MEM = 6
MIN_VIDEOS = 4
MIN_MINUTES = 3.0
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def select_donors() -> list[str]:
    """Return the analyzable donor ids from the cached inventory."""
    inv = pd.read_parquet(os.path.join(OUT_DIR, "collection_inventory.parquet"))
    m = (
        (inv["coverage"] >= FLOOR_COVERAGE) & (inv["n_play"] >= FLOOR_N_PLAY)
        & (inv["n_days"] >= FLOOR_N_DAYS) & (inv["n_emb_play"] >= FLOOR_N_EMB)
    )
    return inv[m]["collection_id"].astype(str).tolist()




def load_directional_store(corpus_mean: np.ndarray) -> tuple[dict, np.ndarray]:
    """Load the whole embedding store as in-place directional float32 vectors.

    Args:
        corpus_mean: The global mean for anisotropy removal.

    Returns:
        ``(id_to_idx, U)`` where ``U`` is an ``(n, d)`` float32 array of
        corpus-mean-centred, L2-normalised vectors and ``id_to_idx`` maps
        item_id to its row.
    """
    ids, mat = embeddings.load_embeddings()
    mat = mat.astype(np.float32, copy=False)
    mat -= corpus_mean.astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    np.divide(mat, np.where(norms < 1e-8, 1e-8, norms), out=mat)
    return {iid: i for i, iid in enumerate(ids)}, mat




def segment_session(seq: list[tuple], U: np.ndarray, cut: float, mem: int,
                    min_videos: int, min_minutes: float) -> list[dict]:
    """Grow focus episodes within one session's embedded plays.

    Args:
        seq: Time-ordered ``(item_id, row_idx, ts, dur)`` tuples for the
            session's embedded plays.
        U: Directional vector store.
        cut: Focus threshold on mean cosine distance to the recent centroid.
        mem: Number of recent members the centroid is taken over.
        min_videos: Minimum distinct videos to keep an episode.
        min_minutes: Minimum span (minutes) to keep an episode.

    Returns:
        A list of episode dicts (raw members + span; geometry/content added later).
    """
    episodes: list[dict] = []
    cur: dict | None = None

    def close(c: dict | None) -> None:
        if c is None or len(c["idx"]) < min_videos:
            return
        if (c["end_ts"] - c["start_ts"]).total_seconds() / 60.0 >= min_minutes:
            episodes.append(c)

    for iid, ridx, ts, dur in seq:
        if cur is None:
            cur = {"ids": [iid], "idx": [ridx], "seen": {iid},
                   "start_ts": ts, "end_ts": ts, "n_plays": 1}
            continue
        if iid in cur["seen"]:
            cur["n_plays"] += 1
            cur["end_ts"] = ts
            continue
        centroid = U[cur["idx"][-mem:]].mean(axis=0)
        dist = 1.0 - float(U[ridx] @ centroid)
        if dist <= cut:
            cur["ids"].append(iid)
            cur["idx"].append(ridx)
            cur["seen"].add(iid)
            cur["n_plays"] += 1
            cur["end_ts"] = ts
        else:
            close(cur)
            cur = {"ids": [iid], "idx": [ridx], "seen": {iid},
                   "start_ts": ts, "end_ts": ts, "n_plays": 1}
    close(cur)
    return episodes




def _dominant(series: pd.Series) -> tuple[object, float]:
    """Return the modal value of a series and its share."""
    s = series.dropna()
    if s.empty:
        return None, 0.0
    vc = s.value_counts()
    return vc.index[0], round(float(vc.iloc[0]) / float(len(s)), 3)




def episode_record(ep: dict, cid: str, sess: object, U: np.ndarray,
                   feat: pd.DataFrame, play_ts: np.ndarray) -> dict:
    """Reduce one raw episode to a fully-attributed table row."""
    idx = np.asarray(ep["idx"])
    Uep = U[idx]
    k = len(idx)
    span_min = round((ep["end_ts"] - ep["start_ts"]).total_seconds() / 60.0, 2)

    geo = entropy_metrics.trajectory_geometry(Uep)
    ent_bits, eff_rank = entropy_metrics.spectral_entropy(Uep)
    focus = entropy_metrics.mean_pairwise_cosine_distance(Uep)

    # Plays of any kind (incl. unembedded) inside the episode's span — measures
    # how much off-corpus content interleaved the focused run.
    lo, hi = np.searchsorted(play_ts, [ep["start_ts"].value, ep["end_ts"].value + 1])
    n_in_span = int(hi - lo)

    f = feat.reindex(ep["ids"])
    niche, niche_share = _dominant(f["niche_name"])
    author, author_share = _dominant(f["author"])
    adv, adv_share = _dominant(f["advertising"])

    return {
        "collection_id": cid,
        "session_id": str(sess),
        "start_ts": ep["start_ts"],
        "end_ts": ep["end_ts"],
        "duration_min": span_min,
        "n_plays": int(ep["n_plays"]),
        "n_distinct": k,
        "repeat_rate": round(ep["n_plays"] / k, 2),
        "n_interleaved": max(n_in_span - int(ep["n_plays"]), 0),
        "focus": round(float(focus), 4),
        "diameter": round(geo["diameter"], 4),
        "step_mean": round(geo["step_mean"], 4),
        "straightness": round(geo["straightness"], 4) if np.isfinite(geo["straightness"]) else None,
        "spectral_entropy_bits": round(float(ent_bits), 4),
        "effective_rank": round(float(eff_rank), 3),
        "dominant_niche": niche,
        "dominant_niche_share": niche_share,
        "n_niches": int(f["niche_name"].nunique()),
        "n_authors": int(f["author"].nunique()),
        "dominant_author_share": author_share,
        "advertising": adv,
        "advertising_share": adv_share,
        "mean_political": round(float(pd.to_numeric(f["political_score"], errors="coerce").mean()), 4),
        "mean_sensitivity": round(float(pd.to_numeric(f["sensitivity_score"], errors="coerce").mean()), 4),
    }




def run_donor(cid: str, plays: pd.DataFrame, id2idx: dict, U: np.ndarray,
              feat: pd.DataFrame, args: argparse.Namespace) -> tuple[list[dict], dict]:
    """Segment one donor's sessions and attribute every episode."""
    plays = plays.sort_values("_ts")
    play_ts = plays["_ts"].astype("int64").to_numpy()
    emb = plays[plays["item_id"].isin(id2idx)].copy()

    # Stable session key (isolate rows with no session_id rather than merging them).
    sess = emb["session_id"].astype("string")
    sess = sess.where(sess.notna(), "na_" + emb.index.astype("string"))
    emb = emb.assign(_sess=sess)

    rows: list[dict] = []
    for s, g in emb.groupby("_sess", sort=False):
        seq = [(iid, id2idx[iid], ts, dur) for iid, ts, dur in
               zip(g["item_id"], g["_ts"], g["play_duration"])]
        for ep in segment_session(seq, U, args.cut, args.mem, args.min_videos, args.min_minutes):
            rows.append(episode_record(ep, cid, s, U, feat, play_ts))

    ep_plays = int(sum(r["n_plays"] for r in rows))
    total_watch = float(pd.to_numeric(plays["play_duration"], errors="coerce").fillna(0).sum())
    ep_watch = 0.0
    if rows:
        edf = pd.DataFrame(rows)
        for _, r in edf.iterrows():
            lo, hi = np.searchsorted(play_ts, [pd.Timestamp(r["start_ts"]).value,
                                               pd.Timestamp(r["end_ts"]).value + 1])
            ep_watch += float(pd.to_numeric(plays.iloc[lo:hi]["play_duration"], errors="coerce").fillna(0).sum())

    summary = {
        "collection_id": cid,
        "n_play": int(len(plays)),
        "n_emb_play": int(len(emb)),
        "coverage": round(len(emb) / max(len(plays), 1), 3),
        "n_sessions": int(emb["_sess"].nunique()),
        "n_episodes": len(rows),
        "n_episode_plays": ep_plays,
        "frac_plays_in_episodes": round(ep_plays / max(len(plays), 1), 4),
        "frac_watchtime_in_episodes": round(ep_watch / total_watch, 4) if total_watch > 0 else None,
        "median_episode_distinct": float(np.median([r["n_distinct"] for r in rows])) if rows else 0.0,
        "median_episode_minutes": float(np.median([r["duration_min"] for r in rows])) if rows else 0.0,
        "median_episode_focus": round(float(np.median([r["focus"] for r in rows])), 4) if rows else None,
    }
    return rows, summary




def main() -> None:
    """Build the episode table over the analyzable donor population."""
    parser = argparse.ArgumentParser(description="Phase 0 — binge-episode segmenter.")
    parser.add_argument("--collections-file", default=None)
    parser.add_argument("--cut", type=float, default=CUT)
    parser.add_argument("--mem", type=int, default=MEM)
    parser.add_argument("--min-videos", type=int, default=MIN_VIDEOS)
    parser.add_argument("--min-minutes", type=float, default=MIN_MINUTES)
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N donors.")
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    if args.collections_file:
        with open(args.collections_file) as fh:
            donors = fh.read().split()
    else:
        donors = select_donors()
    if args.limit:
        donors = donors[:args.limit]
    print(f"Donors: {len(donors)}  (cut={args.cut}, mem={args.mem}, "
          f"min_videos={args.min_videos}, min_minutes={args.min_minutes})")

    corpus_mean = data_access.corpus_mean()
    print("Loading directional embedding store...")
    id2idx, U = load_directional_store(corpus_mean)
    print(f"  {len(id2idx):,} vectors")
    print("Loading video features...")
    feat = data_access.load_video_features()

    print("Loading plays...")
    pl = data_access.load_plays(donors).to_pandas()
    pl["_ts"] = pd.to_datetime(pl["local_timestamp"], errors="coerce")
    pl = pl.dropna(subset=["_ts"])

    all_eps: list[dict] = []
    summaries: list[dict] = []
    for i, cid in enumerate(donors):
        sub = pl[pl["collection_id"] == cid]
        if sub.empty:
            continue
        rows, summary = run_donor(cid, sub, id2idx, U, feat, args)
        all_eps.extend(rows)
        summaries.append(summary)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(donors)} donors, {len(all_eps):,} episodes so far")

    ep_df = pd.DataFrame(all_eps)
    sm_df = pd.DataFrame(summaries)
    for df in (ep_df, sm_df):
        for col in ("start_ts", "end_ts"):
            if col in df.columns:
                df[col] = df[col].astype("string")
    ep_path = os.path.join(OUT_DIR, f"episodes_{args.tag}.parquet")
    sm_path = os.path.join(OUT_DIR, f"episode_donor_summary_{args.tag}.parquet")
    pa.parquet.write_table(pa.Table.from_pandas(ep_df, preserve_index=False), ep_path)
    pa.parquet.write_table(pa.Table.from_pandas(sm_df, preserve_index=False), sm_path)
    print(f"\nWrote {len(ep_df):,} episodes -> {ep_path}")
    print(f"Wrote {len(sm_df)} donor summaries -> {sm_path}")




if __name__ == "__main__":
    main()
