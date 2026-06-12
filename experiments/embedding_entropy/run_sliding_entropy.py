"""Event-anchored sliding-window entropy on embeddings.

Where ``run_window_entropy.py`` uses fixed clock-aligned tumbling bins (a binge
straddling ``:00`` is split across two windows and diluted), this scans an
``[t, t+W)`` window anchored at *every* play, so the single tightest W-minute
stretch is found regardless of clock alignment — the most sensitive form for
"is there *any* low-entropy hour".

Speed comes from two identities:
    * Mean pairwise cosine distance over ``k`` unit vectors needs only their sum:
      ``mean_pairwise_cos = (||Σu||² − k) / (k(k−1))`` — O(k·d), not O(k²).
    * As the window slides, the distinct-video set changes by one play at a
      time, so ``Σu`` and ``k`` are maintained incrementally (add on the right,
      drop on the left) — the whole scan is O(n·d), and so is each shuffle of
      the permutation null.

Repeated plays of the same video within the window are collapsed to one (a
running per-item count gates the add/drop), so a rewatch loop cannot fake a
binge — the same guardrail as the tumbling-window script.
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
import entropy_metrics
from run_window_entropy import densest_window

WINDOW_MINUTES = 60
N_DAYS = 21
MIN_EMB = 5
N_PERM = 200
SEED = 7
OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def slide_min_cos_dist(
        order_items: np.ndarray,
        unit_by_item: np.ndarray,
        ts_ns: np.ndarray,
        width_ns: int,
        min_emb: int,
    ) -> tuple[float, int, int]:
    """Scan all ``[t, t+W)`` windows; return the minimum cosine distance.

    Args:
        order_items: ``(n,)`` int codes mapping each time-sorted play to a row
            in ``unit_by_item``. Repeated codes are the same video.
        unit_by_item: ``(m, d)`` directional vectors, one per distinct video.
        ts_ns: ``(n,)`` sorted play timestamps in nanoseconds.
        width_ns: Window width ``W`` in nanoseconds.
        min_emb: Minimum distinct videos for a window to count.

    Returns:
        A tuple ``(min_cos_dist, anchor_index, k_at_min)``. ``min_cos_dist`` is
        ``inf`` when no window ever holds ``min_emb`` distinct videos.
    """
    n = order_items.shape[0]
    m = unit_by_item.shape[0]
    counts = np.zeros(m, dtype=np.int64)
    sumvec = np.zeros(unit_by_item.shape[1], dtype=np.float64)
    k = 0
    r = 0
    best = np.inf
    best_anchor = -1
    best_k = 0
    for l in range(n):
        limit = ts_ns[l] + width_ns
        while r < n and ts_ns[r] < limit:
            it = order_items[r]
            if counts[it] == 0:
                sumvec += unit_by_item[it]
                k += 1
            counts[it] += 1
            r += 1
        if k >= min_emb and k >= 2:
            mean_cos = (float(sumvec @ sumvec) - k) / (k * (k - 1))
            cd = 1.0 - mean_cos
            if cd < best:
                best, best_anchor, best_k = cd, l, k
        it = order_items[l]
        counts[it] -= 1
        if counts[it] == 0:
            sumvec -= unit_by_item[it]
            k -= 1
    return best, best_anchor, best_k




def run_collection(cid: str, plays: pd.DataFrame, emb_ids: set, corpus_mean, args, rng):
    """Sliding-window scan + timestamp-shuffle null for one collection."""
    plays = plays.sort_values("_ts").copy()
    plays["_emb"] = plays["item_id"].isin(emb_ids)
    emb_plays = plays[plays["_emb"]]
    if emb_plays.empty:
        return {"collection_id": cid, "note": "no embedded plays"}

    start, end = densest_window(emb_plays["_ts"].dt.normalize(), args.n_days)
    mask = (emb_plays["_ts"] >= start) & (emb_plays["_ts"] < end + pd.Timedelta(days=1))
    span = emb_plays[mask].copy()
    vec_lookup = data_access.load_embeddings_for(set(span["item_id"]))
    span = span[span["item_id"].isin(vec_lookup)].sort_values("_ts")
    if len(span) < args.min_emb:
        return {"collection_id": cid, "note": "too few embedded plays in span"}

    # Map each distinct video to a row index and a directional vector once.
    uniq_items = list(dict.fromkeys(span["item_id"].tolist()))
    item_to_row = {it: i for i, it in enumerate(uniq_items)}
    raw = np.vstack([vec_lookup[it] for it in uniq_items])
    unit_by_item = entropy_metrics.to_directional(raw, corpus_mean)
    order_items = np.fromiter((item_to_row[it] for it in span["item_id"]), dtype=np.int64, count=len(span))
    ts_ns = span["_ts"].astype("int64").to_numpy()
    width_ns = args.window_minutes * 60 * 1_000_000_000

    obs_cd, anchor, k_at = slide_min_cos_dist(order_items, unit_by_item, ts_ns, width_ns, args.min_emb)
    if not np.isfinite(obs_cd):
        return {"collection_id": cid, "note": "no window reached min_emb"}

    # Null: keep the sorted timestamp sequence, shuffle which video sits at each
    # slot, recompute the tightest window. Tests temporal clustering of
    # similar content vs. the same diet in random order.
    null_min = np.empty(args.n_perm)
    for b in range(args.n_perm):
        shuffled = order_items.copy()
        rng.shuffle(shuffled)
        null_min[b], _, _ = slide_min_cos_dist(shuffled, unit_by_item, ts_ns, width_ns, args.min_emb)
    p = float((np.sum(null_min <= obs_cd) + 1) / (args.n_perm + 1))

    anchor_ts = pd.Timestamp(ts_ns[anchor])
    return {
        "collection_id": cid,
        "window_start_date": str(start.date()),
        "window_end_date": str(end.date()),
        "n_emb_plays_in_span": int(len(span)),
        "n_distinct_in_span": int(len(uniq_items)),
        "obs_min_cos_dist": round(obs_cd, 4),
        "k_at_min": int(k_at),
        "anchor_ts": str(anchor_ts),
        "null_min_cos_dist_mean": round(float(np.mean(null_min)), 4),
        "null_min_cos_dist_p05": round(float(np.percentile(null_min, 5)), 4),
        "p_value_min_cos_dist": round(p, 4),
    }




def main() -> None:
    """Run the sliding-window scan over the chosen collections."""
    parser = argparse.ArgumentParser(description="Event-anchored sliding-window entropy.")
    parser.add_argument("--collections", nargs="+", default=None)
    parser.add_argument("--collections-file", default=None)
    parser.add_argument("--window-minutes", type=int, default=WINDOW_MINUTES)
    parser.add_argument("--n-days", type=int, default=N_DAYS)
    parser.add_argument("--min-emb", type=int, default=MIN_EMB)
    parser.add_argument("--n-perm", type=int, default=N_PERM)
    parser.add_argument("--tag", default="sliding")
    args = parser.parse_args()

    if args.collections_file:
        with open(args.collections_file) as fh:
            args.collections = fh.read().split()
    if not args.collections:
        raise SystemExit("Provide --collections or --collections-file")
    print(f"Collections: {len(args.collections)}")

    rng = np.random.default_rng(SEED)
    corpus_mean = data_access.corpus_mean()
    emb_ids = data_access.embedded_id_set()

    table = data_access.load_plays(args.collections)
    plays_all = table.to_pandas()
    plays_all["_ts"] = pd.to_datetime(plays_all["local_timestamp"], errors="coerce")
    plays_all = plays_all.dropna(subset=["_ts"])

    summaries = []
    for cid in args.collections:
        sub = plays_all[plays_all["collection_id"] == cid]
        if sub.empty:
            continue
        s = run_collection(cid, sub, emb_ids, corpus_mean, args, rng)
        summaries.append(s)
        print("  " + json.dumps(s, default=str))

    out = os.path.join(OUT_DIR, f"embedding_window_entropy_{args.tag}_summary.json")
    with open(out, "w") as fh:
        json.dump(summaries, fh, indent=2, default=str)
    print(f"Wrote summary -> {out}")




if __name__ == "__main__":
    main()
