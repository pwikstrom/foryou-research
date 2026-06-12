"""Find low-entropy viewing windows in collections, measured on embeddings.

Experiment: does any collection contain a window of ``WINDOW_MINUTES`` (default
60) during which the videos watched are semantically *homogeneous* — a focused
binge — where homogeneity is a property of the dense 1536-d embeddings rather
than the discrete niche labels?

Pipeline per collection:
    1. Load all ``play`` rows, pick the densest contiguous ``N_DAYS`` calendar
       window (by embedded-play count) so the first pass stays fast.
    2. Tile that window into ``WINDOW_MINUTES`` tumbling clock-bins; keep bins
       holding at least ``MIN_EMB`` embedded plays.
    3. Per bin, compute the embedding-entropy measures (see
       :mod:`entropy_metrics`) over the played videos' vectors.
    4. Run a within-collection time-shuffle permutation null: hold the bin
       sizes fixed but scramble which video lands in which bin, recompute, and
       test whether the real low-entropy tail beats chance — i.e. whether
       similar content genuinely *clusters in time* (real bursts) or the donor
       is merely narrow overall.

Outputs (all under ``tmp/`` — build-excluded): a per-window parquet, a
per-collection summary JSON, and a printed report of the lowest-entropy windows
with their actual niche names and stories.

Run from the project root:
    python experiments/embedding_entropy/run_window_entropy.py
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet  # noqa: F401  (registers pa.parquet.write_table)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import data_access
import entropy_metrics

# Defaults — override on the command line. The three seed collections are the
# high-coverage donors found during feasibility probing (39% / 18% / 15%).
DEFAULT_COLLECTIONS = [
    "f10e0f10-ed15-434c-9f37-2bffcbf6ed41",
    "cb8b3260-f79c-4442-a3ca-ca401fb74606",
    "2ee17644-2d6e-437f-bd18-3d29a95fb5cc",
]
WINDOW_MINUTES = 60
N_DAYS = 21
MIN_EMB = 5
N_PERM = 200
TOP_LOWEST = 8
SEED = 7

OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))




def densest_window(days: pd.Series, n_days: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the ``n_days`` calendar span containing the most plays.

    Args:
        days: A Series of per-play calendar dates (normalised timestamps) that
            are already restricted to embedded plays.
        n_days: Window width in days.

    Returns:
        An inclusive ``(start_date, end_date)`` pair of ``Timestamp`` days.
    """
    per_day = days.value_counts().sort_index()
    full = pd.date_range(per_day.index.min(), per_day.index.max(), freq="D")
    per_day = per_day.reindex(full, fill_value=0)
    rolling = per_day.rolling(window=n_days, min_periods=1).sum()
    end = rolling.idxmax()
    start = end - pd.Timedelta(days=n_days - 1)
    return start, end




def bin_metrics_table(
        bins: np.ndarray,
        matrix: np.ndarray,
        weights: np.ndarray | None,
        corpus_mean: np.ndarray,
        min_emb: int,
    ) -> dict[object, dict]:
    """Compute embedding-entropy metrics for every qualifying bin.

    Args:
        bins: ``(n,)`` array of bin identifiers, one per embedded play.
        matrix: ``(n, d)`` raw embeddings aligned to ``bins``.
        weights: Optional ``(n,)`` per-play weights aligned to ``bins``.
        corpus_mean: Global mean vector for anisotropy removal.
        min_emb: Minimum embedded plays for a bin to be measured.

    Returns:
        A dict mapping bin id to its metrics dict (only bins meeting
        ``min_emb``).
    """
    out: dict[object, dict] = {}
    order = np.argsort(bins, kind="stable")
    b_sorted = bins[order]
    uniq, starts = np.unique(b_sorted, return_index=True)
    bounds = list(starts) + [len(b_sorted)]
    for i, b in enumerate(uniq):
        idx = order[bounds[i]:bounds[i + 1]]
        if idx.size < min_emb:
            continue
        w = None if weights is None else weights[idx]
        out[b] = entropy_metrics.window_metrics(matrix[idx], corpus_mean, w)
    return out




def permutation_null(
        bins: np.ndarray,
        matrix: np.ndarray,
        weights: np.ndarray | None,
        corpus_mean: np.ndarray,
        min_emb: int,
        n_perm: int,
        rng: np.random.Generator,
    ) -> dict:
    """Time-shuffle null for the low-entropy tail of a collection's windows.

    Bin sizes are held fixed while video-to-bin assignment is scrambled, so the
    null asks: given the *same* set of watched videos and the *same* window
    occupancy, would windows this focused arise if the videos were ordered at
    random in time? A real minimum well below the null minimum means similar
    content clusters in time (genuine bursts).

    Args:
        bins: ``(n,)`` real bin ids per embedded play.
        matrix: ``(n, d)`` embeddings aligned to ``bins``.
        weights: Optional ``(n,)`` weights aligned to ``bins``.
        corpus_mean: Global mean vector.
        min_emb: Minimum embedded plays per measured bin.
        n_perm: Number of shuffles.
        rng: Seeded random generator.

    Returns:
        A dict of observed-vs-null statistics and one-sided p-values for the
        minimum spectral entropy and minimum mean pairwise cosine distance.
    """
    real = bin_metrics_table(bins, matrix, weights, corpus_mean, min_emb)
    obs_ent = [m["spectral_entropy_bits"] for m in real.values()]
    obs_cos = [m["mean_pairwise_cosine_distance"] for m in real.values()]
    if not obs_ent:
        return {}
    obs_min_ent = float(np.nanmin(obs_ent))
    obs_min_cos = float(np.nanmin(obs_cos))

    null_min_ent = np.empty(n_perm)
    null_min_cos = np.empty(n_perm)
    for k in range(n_perm):
        perm = rng.permutation(len(bins))
        shuffled = bin_metrics_table(bins[perm], matrix, weights, corpus_mean, min_emb)
        ents = [m["spectral_entropy_bits"] for m in shuffled.values()]
        coss = [m["mean_pairwise_cosine_distance"] for m in shuffled.values()]
        null_min_ent[k] = np.nanmin(ents) if ents else np.nan
        null_min_cos[k] = np.nanmin(coss) if coss else np.nan

    p_ent = float((np.nansum(null_min_ent <= obs_min_ent) + 1) / (n_perm + 1))
    p_cos = float((np.nansum(null_min_cos <= obs_min_cos) + 1) / (n_perm + 1))
    return {
        "n_measured_bins": len(real),
        "obs_min_spectral_entropy_bits": round(obs_min_ent, 4),
        "null_min_spectral_entropy_mean": round(float(np.nanmean(null_min_ent)), 4),
        "null_min_spectral_entropy_p05": round(float(np.nanpercentile(null_min_ent, 5)), 4),
        "p_value_min_entropy": round(p_ent, 4),
        "obs_min_mean_cos_dist": round(obs_min_cos, 4),
        "null_min_mean_cos_dist_mean": round(float(np.nanmean(null_min_cos)), 4),
        "p_value_min_cos_dist": round(p_cos, 4),
    }




def run_collection(
        collection_id: str,
        plays: pd.DataFrame,
        emb_ids: set[str],
        corpus_mean: np.ndarray,
        args: argparse.Namespace,
        rng: np.random.Generator,
    ) -> tuple[pd.DataFrame, dict]:
    """Window one collection and compute per-window metrics plus the null.

    Args:
        collection_id: Collection under analysis.
        plays: That collection's ``play`` rows with parsed ``_ts``.
        emb_ids: Global set of embedded item_ids.
        corpus_mean: Global mean vector.
        args: Parsed CLI args (window/n_days/min_emb/weight/n_perm).
        rng: Seeded generator for the null.

    Returns:
        A tuple ``(windows_df, summary)``.
    """
    plays = plays.sort_values("_ts").copy()
    plays["_emb"] = plays["item_id"].isin(emb_ids)
    emb_plays = plays[plays["_emb"]]
    if emb_plays.empty:
        return pd.DataFrame(), {"collection_id": collection_id, "note": "no embedded plays"}

    start, end = densest_window(emb_plays["_ts"].dt.normalize(), args.n_days)
    mask = (plays["_ts"] >= start) & (plays["_ts"] < end + pd.Timedelta(days=1))
    win = plays[mask].copy()
    win_emb = win[win["_emb"]].copy()
    if win_emb.empty:
        return pd.DataFrame(), {"collection_id": collection_id, "note": "no embedded plays in window"}

    # Load vectors for the embedded plays in the chosen span.
    vec_lookup = data_access.load_embeddings_for(set(win_emb["item_id"]))
    win_emb = win_emb[win_emb["item_id"].isin(vec_lookup)].copy()
    if win_emb.empty:
        return pd.DataFrame(), {"collection_id": collection_id, "note": "vectors missing"}
    win_emb["_bin"] = win_emb["_ts"].dt.floor(f"{args.window_minutes}min")

    # Repeated plays of the same video within a window collapse the embedding
    # spectrum (eff_rank -> 1) and deflate cosine distance, so a rewatch loop
    # masquerades as a semantic binge. For the semantic-focus question, dedupe
    # to distinct videos per window (the default); the raw repeat behaviour is
    # still reported as repeat_rate. With --no-dedupe every play is kept and a
    # window's "focus" mixes topical concentration with rewatching.
    if args.dedupe:
        metric_src = win_emb.drop_duplicates(["_bin", "item_id"]).copy()
        if args.weight:
            dur = pd.to_numeric(win_emb["play_duration"], errors="coerce").fillna(0.0)
            tot = dur.groupby([win_emb["_bin"], win_emb["item_id"]]).sum()
            metric_src["_w"] = metric_src.set_index(["_bin", "item_id"]).index.map(tot).astype("float64")
    else:
        metric_src = win_emb.copy()
        if args.weight:
            metric_src["_w"] = pd.to_numeric(metric_src["play_duration"], errors="coerce").fillna(0.0)

    matrix = np.vstack([vec_lookup[i] for i in metric_src["item_id"]])
    weights = metric_src["_w"].to_numpy() if args.weight else None
    bin_codes = metric_src["_bin"].astype("int64").to_numpy()

    metrics_by_bin = bin_metrics_table(bin_codes, matrix, weights, corpus_mean, args.min_emb)

    # Per-bin context counts (all plays, embedded plays, distinct videos, watch time).
    all_bins = win.assign(_bin=win["_ts"].dt.floor(f"{args.window_minutes}min"))
    rows = []
    for b, m in metrics_by_bin.items():
        bin_ts = pd.Timestamp(b)
        sub_emb = win_emb[win_emb["_bin"] == bin_ts]
        sub_all = all_bins[all_bins["_bin"] == bin_ts]
        rows.append({
            "collection_id": collection_id,
            "window_start": bin_ts,
            "n_plays_total": int(len(sub_all)),
            "n_emb_plays": int(len(sub_emb)),
            "n_unique_emb": int(sub_emb["item_id"].nunique()),
            "repeat_rate": round(len(sub_emb) / max(sub_emb["item_id"].nunique(), 1), 2),
            "emb_coverage": round(len(sub_emb) / max(len(sub_all), 1), 3),
            "watch_time_s": round(float(pd.to_numeric(sub_all["play_duration"], errors="coerce").fillna(0).sum()), 1),
            **{k: (round(v, 4) if isinstance(v, float) and np.isfinite(v) else v) for k, v in m.items()},
        })
    # Rank by the size-robust focus measure (low cosine distance = homogeneous),
    # not absolute entropy bits which scale with window size.
    windows_df = pd.DataFrame(rows).sort_values("mean_pairwise_cosine_distance").reset_index(drop=True)

    null = permutation_null(bin_codes, matrix, weights, corpus_mean, args.min_emb, args.n_perm, rng)
    summary = {
        "collection_id": collection_id,
        "window_start_date": str(start.date()),
        "window_end_date": str(end.date()),
        "n_days": args.n_days,
        "window_minutes": args.window_minutes,
        "min_emb": args.min_emb,
        "deduped": bool(args.dedupe),
        "weighted": bool(args.weight),
        "n_plays_in_span": int(len(win)),
        "n_emb_plays_in_span": int(len(win_emb)),
        "span_emb_coverage": round(len(win_emb) / max(len(win), 1), 3),
        "n_measured_windows": int(len(windows_df)),
        "cos_dist_min": round(float(windows_df["mean_pairwise_cosine_distance"].min()), 4) if len(windows_df) else None,
        "cos_dist_median": round(float(windows_df["mean_pairwise_cosine_distance"].median()), 4) if len(windows_df) else None,
        "entropy_norm_min": round(float(windows_df["spectral_entropy_norm"].min()), 4) if len(windows_df) else None,
        "entropy_norm_median": round(float(windows_df["spectral_entropy_norm"].median()), 4) if len(windows_df) else None,
        "entropy_bits_min": round(float(windows_df["spectral_entropy_bits"].min()), 4) if len(windows_df) else None,
        "entropy_bits_median": round(float(windows_df["spectral_entropy_bits"].median()), 4) if len(windows_df) else None,
        **null,
    }
    return windows_df, summary




def describe_window(row: pd.Series, win_emb_lookup: dict, labels: dict) -> str:
    """Render one low-entropy window as a legible block for the report."""
    iid_counts = win_emb_lookup.get(pd.Timestamp(row["window_start"]), {})
    lines = [
        f"  {row['collection_id'][:8]}  {row['window_start']}  "
        f"cos_dist={row['mean_pairwise_cosine_distance']:.3f}  "
        f"H_norm={row['spectral_entropy_norm']:.3f}  "
        f"H={row['spectral_entropy_bits']:.2f}b  eff_rank={row['effective_rank']:.1f}  "
        f"({row['n_unique_emb']} uniq / {row['n_emb_plays']} emb plays, rpt={row['repeat_rate']}, "
        f"{row['n_plays_total']} total)"
    ]
    top = sorted(iid_counts.items(), key=lambda kv: -kv[1])[:6]
    for iid, cnt in top:
        lab = labels.get(iid, {})
        niche = lab.get("niche_name") or "?"
        story = (lab.get("story") or "")[:90].replace("\n", " ")
        tag = f" x{cnt}" if cnt > 1 else ""
        lines.append(f"      [{niche}]{tag} {story}")
    return "\n".join(lines)




def main() -> None:
    """Parse args, run the experiment over the chosen collections, write outputs."""
    parser = argparse.ArgumentParser(description="Embedding-space windowed entropy experiment.")
    parser.add_argument("--collections", nargs="+", default=DEFAULT_COLLECTIONS)
    parser.add_argument("--collections-file", default=None,
                        help="Path to a whitespace-separated id file (overrides --collections).")
    parser.add_argument("--window-minutes", type=int, default=WINDOW_MINUTES)
    parser.add_argument("--n-days", type=int, default=N_DAYS)
    parser.add_argument("--min-emb", type=int, default=MIN_EMB,
                        help="Min distinct embedded videos per window (plays if --no-dedupe).")
    parser.add_argument("--n-perm", type=int, default=N_PERM)
    parser.add_argument("--weight", action="store_true", help="Weight by play_duration.")
    parser.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                        help="Keep repeated plays (focus then mixes topic + rewatching).")
    parser.set_defaults(dedupe=True)
    parser.add_argument("--quiet", action="store_true", help="Skip the per-window content dump.")
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()

    if args.collections_file:
        with open(args.collections_file) as fh:
            args.collections = fh.read().split()
    print(f"Collections: {len(args.collections)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print(f"Loading corpus mean (cached at {data_access.CORPUS_MEAN_CACHE})...")
    corpus_mean = data_access.corpus_mean()
    print("Loading embedded id set...")
    emb_ids = data_access.embedded_id_set()
    print(f"  {len(emb_ids):,} embedded videos")

    print("Loading plays...")
    table = data_access.load_plays(args.collections)
    plays_all = table.to_pandas()
    plays_all["_ts"] = pd.to_datetime(plays_all["local_timestamp"], errors="coerce")
    plays_all = plays_all.dropna(subset=["_ts"])

    all_windows = []
    summaries = []
    for cid in args.collections:
        print(f"\n=== {cid} ===")
        sub = plays_all[plays_all["collection_id"] == cid]
        if sub.empty:
            print("  (no plays)")
            continue
        windows_df, summary = run_collection(cid, sub, emb_ids, corpus_mean, args, rng)
        summaries.append(summary)
        print("  " + json.dumps(summary, default=str))
        if windows_df.empty:
            continue
        all_windows.append(windows_df)
        if args.quiet:
            continue

        # Build the per-bin item->count lookup and labels for the lowest windows.
        win_emb = sub.copy()
        win_emb["_ts"] = pd.to_datetime(win_emb["local_timestamp"], errors="coerce")
        win_emb["_bin"] = win_emb["_ts"].dt.floor(f"{args.window_minutes}min")
        lowest = windows_df.head(TOP_LOWEST)
        bin_lookup = {}
        need_ids: set[str] = set()
        for _, r in lowest.iterrows():
            b = pd.Timestamp(r["window_start"])
            ids = win_emb[(win_emb["_bin"] == b) & (win_emb["item_id"].isin(emb_ids))]["item_id"]
            counts = ids.value_counts().to_dict()
            bin_lookup[b] = counts
            need_ids.update(counts)
        labels = data_access.load_video_labels(need_ids)
        print(f"  --- {len(lowest)} lowest-entropy windows ---")
        for _, r in lowest.iterrows():
            print(describe_window(r, bin_lookup, labels))

    if all_windows:
        combined = pd.concat(all_windows, ignore_index=True)
        out_parquet = os.path.join(OUT_DIR, f"embedding_window_entropy_{args.tag}.parquet")
        arrow_df = combined.copy()
        arrow_df["window_start"] = arrow_df["window_start"].astype("string")
        pa_table = pa.Table.from_pandas(arrow_df, preserve_index=False)
        pa.parquet.write_table(pa_table, out_parquet)
        print(f"\nWrote {len(combined)} windows -> {out_parquet}")

    out_json = os.path.join(OUT_DIR, f"embedding_window_entropy_{args.tag}_summary.json")
    with open(out_json, "w") as fh:
        json.dump(summaries, fh, indent=2, default=str)
    print(f"Wrote summary -> {out_json}")




if __name__ == "__main__":
    main()
