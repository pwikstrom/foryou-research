"""Within-session begin→end profiling.

Characterises how user behaviour and feed content differ between the start and
the end of a viewing *session* (a "phone sitting", delimited by the persistent
900s `session_id` from `fyp.ingest.assign_session_ids`). Computed on the FULL
intact sequence — NOT the per-study `cache` datasets, which are sampled and shred
sequences. Segment characteristics use ANNOTATED videos only.

Established findings (paper_three, replicated on dmrc_summer_mini): over a
session, engagement satiates (completion / dwell fall) and the feed
de-concentrates (niche entropy rises, top-niche share falls). Direction is
near-universal across participants; the interesting variation is at the SESSION
level — ~1 in 4 sessions actually narrows (a rabbit-hole shape).

The core artifact is a per-session metrics table (one row per qualifying
session). The aggregate / between-participant / temporal views are all
aggregations of that table, so the heavy work (full-data load + niche detection,
done by the worker) runs once and the views are cheap.
"""

import math
from typing import Any

import numpy as np
import pandas as pd

# --- Tuning constants -----------------------------------------------------

# Session-length band, measured in ANNOTATED videos per session. Excludes quick
# checks (too short to split) and atypical marathons.
SESSION_BAND = (12, 80)

# Fraction of the session's annotated videos taken as the early / late segment.
SEGMENT_FRACTION = 1 / 3

# Scalar per-video features averaged within each segment. Keys are the source
# column; the value marks how to reduce it ("mean" numeric, "share:<value>" =
# fraction equal to that value, lowercased).
SCALAR_FEATURES: dict[str, str] = {
    "completion": "mean",
    "dwell": "mean",
    "political_score": "mean",
    "sensitivity_score": "mean",
    "log_playcount": "mean",
    "video_duration": "mean",
    "main_gender": "share:female",
    "advertising": "share:yes",
    "aigc": "share:yes",
    "trend": "share:yes",
}

# Minimum sessions in a calendar-month bin for it to count in temporal trends.
MIN_SESSIONS_PER_MONTH = 4

# Minimum participants for an aggregate cell to be reported.
MIN_PARTICIPANTS = 5





def _segment_label(frac: pd.Series) -> pd.Series:
    """Map within-session position fraction (0=start..1=end) to early/late/mid."""
    return np.where(frac < SEGMENT_FRACTION, "early",
                    np.where(frac >= 1 - SEGMENT_FRACTION, "late", "mid"))





def build_session_metrics(
    plays: pd.DataFrame,
    band: tuple[int, int] = SESSION_BAND,
    niche_col: str = "niche",
    scalar_features: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Build the per-session begin→end metrics table.

    Args:
        plays: Annotated viewing events with ``collection_id``, ``session_id``,
            ``feed_position``, ``utc_timestamp``, ``niche``, and the feature
            columns referenced by ``scalar_features``.
        band: ``(min, max)`` annotated videos per session to keep.
        niche_col: Column holding the micro-genre label.
        scalar_features: Feature reduction spec; defaults to :data:`SCALAR_FEATURES`
            (columns absent from ``plays`` are skipped).

    Returns:
        One row per qualifying session with ``<feature>_early`` / ``_late`` and
        ``d_<feature>`` (late − early) columns, plus niche ``entropy`` /
        ``top_share`` early/late and their deltas, ``start`` (session start),
        ``n_annot``, ``collection_id``.
    """
    spec = scalar_features or SCALAR_FEATURES
    spec = {c: how for c, how in spec.items() if c in plays.columns}

    work = plays[plays[niche_col].notna()].copy()
    counts = work.groupby("session_id")[niche_col].transform("size")
    work = work[(counts >= band[0]) & (counts <= band[1])].copy()
    if work.empty:
        return work.iloc[0:0]

    work = work.sort_values(["session_id", "feed_position"], kind="mergesort")
    rank = work.groupby("session_id").cumcount()
    n = work.groupby("session_id")["feed_position"].transform("size")
    work["_seg"] = _segment_label((rank) / (n - 1).clip(lower=1))

    def _seg_metrics(g: pd.DataFrame) -> pd.Series:
        out: dict[str, float] = {}
        for col, how in spec.items():
            if how == "mean":
                out[col] = pd.to_numeric(g[col], errors="coerce").mean()
            elif how.startswith("share:"):
                target = how.split(":", 1)[1]
                out[col] = (g[col].astype(str).str.lower() == target).mean()
        vc = g[niche_col].value_counts(normalize=True)
        out["entropy"] = float(-(vc * np.log(vc)).sum()) if len(vc) else np.nan
        out["top_share"] = float(vc.iloc[0]) if len(vc) else np.nan
        return pd.Series(out)

    seg = (
        work[work["_seg"].isin(["early", "late"])]
        .groupby(["collection_id", "session_id", "_seg"])
        .apply(_seg_metrics, include_groups=False)
        .reset_index()
    )
    feat_cols = list(spec.keys()) + ["entropy", "top_share"]
    piv = seg.pivot_table(index=["collection_id", "session_id"], columns="_seg", values=feat_cols)
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    piv = piv.reset_index().dropna()

    meta = work.groupby("session_id").agg(start=("utc_timestamp", "min"),
                                          n_annot=("feed_position", "size")).reset_index()
    table = piv.merge(meta, on="session_id")
    for f in feat_cols:
        if f"{f}_late" in table.columns and f"{f}_early" in table.columns:
            table[f"d_{f}"] = table[f"{f}_late"] - table[f"{f}_early"]
    return table





def _bootstrap_ci(values: np.ndarray, n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap 95% CI for the mean of ``values``."""
    if len(values) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = [rng.choice(values, len(values), replace=True).mean() for _ in range(n_boot)]
    return tuple(np.percentile(means, [2.5, 97.5]))





def aggregate_contrast(metrics: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-participant begin→end contrast for every feature, with bootstrap CI.

    Aggregates session metrics to the participant (mean early / late), then
    summarises the participant-level deltas (mean, 95% CI, fraction of
    participants moving up). Participant is the unit of analysis.
    """
    from scipy.stats import wilcoxon

    feats = [c[2:] for c in metrics.columns if c.startswith("d_")]
    early = metrics.groupby("collection_id")[[f"{f}_early" for f in feats]].mean()
    late = metrics.groupby("collection_id")[[f"{f}_late" for f in feats]].mean()
    out: list[dict[str, Any]] = []
    raw_p: list[float] = []
    for f in feats:
        d = (late[f"{f}_late"] - early[f"{f}_early"]).dropna().to_numpy()
        if len(d) < MIN_PARTICIPANTS:
            continue
        lo, hi = _bootstrap_ci(d)
        try:
            p = float(wilcoxon(d).pvalue)
        except ValueError:
            p = float("nan")
        raw_p.append(p)
        out.append({
            "feature": f,
            "early": round(float(early[f"{f}_early"].mean()), 4),
            "late": round(float(late[f"{f}_late"].mean()), 4),
            "delta": round(float(d.mean()), 4),
            "pct_up": round(float((d > 0).mean()), 3),
            "ci_lo": round(float(lo), 4),
            "ci_hi": round(float(hi), 4),
            "p": p,
        })
    # Benjamini-Hochberg FDR across the feature grid.
    order = np.argsort([o["p"] if not math.isnan(o["p"]) else 1.0 for o in out])
    m = len(out)
    for rank, idx in enumerate(order, start=1):
        p = out[idx]["p"]
        out[idx]["fdr"] = round(min(1.0, p * m / rank), 4) if not math.isnan(p) else None
    return out





def participant_variation(metrics: pd.DataFrame) -> dict[str, Any]:
    """Distribution of per-participant begin→end deltas + against-direction counts."""
    feats = [c[2:] for c in metrics.columns if c.startswith("d_")]
    pp = metrics.groupby("collection_id")[[f"d_{f}" for f in feats]].mean()
    dist: dict[str, Any] = {}
    for f in feats:
        col = pp[f"d_{f}"].dropna()
        q = np.percentile(col, [10, 25, 50, 75, 90]) if len(col) else [np.nan] * 5
        dist[f] = {"p10": round(float(q[0]), 4), "p25": round(float(q[1]), 4),
                   "median": round(float(q[2]), 4), "p75": round(float(q[3]), 4),
                   "p90": round(float(q[4]), 4)}
    return {
        "n_participants": int(len(pp)),
        "distributions": dist,
        "n_narrowing": int((pp["d_entropy"] < 0).sum()) if "d_entropy" in pp else None,
        "n_engagement_rising": int((pp["d_completion"] > 0).sum()) if "d_completion" in pp else None,
    }





def session_distributions(metrics: pd.DataFrame) -> dict[str, Any]:
    """Session-level variation — where rabbit-hole sessions live."""
    return {
        "n_sessions": int(len(metrics)),
        "pct_narrowing": round(float((metrics["d_entropy"] < 0).mean()), 3) if "d_entropy" in metrics else None,
        "pct_engagement_rising": round(float((metrics["d_completion"] > 0).mean()), 3) if "d_completion" in metrics else None,
    }





def temporal_trends(metrics: pd.DataFrame, min_sessions: int = MIN_SESSIONS_PER_MONTH) -> dict[str, Any]:
    """Per-calendar-month means of the headline session metrics (density-gated).

    Only months with at least ``min_sessions`` qualifying sessions are reported,
    to respect the DDP recency bias (older data is sparser).
    """
    if metrics.empty or "start" not in metrics.columns:
        return {"months": [], "note": "no data"}
    m = metrics.copy()
    m["month"] = pd.to_datetime(m["start"]).dt.strftime("%Y-%m")
    grp = m.groupby("month")
    sizes = grp.size()
    keep = sizes[sizes >= min_sessions].index
    headline = [c for c in ["d_completion", "d_entropy", "completion_early", "n_annot"] if c in m.columns]
    rows = []
    for month in sorted(keep):
        g = m[m["month"] == month]
        rows.append({"month": month, "n_sessions": int(len(g)),
                     **{c: round(float(g[c].mean()), 4) for c in headline}})
    return {"months": rows, "min_sessions_per_month": min_sessions}





def compute_profile(metrics: pd.DataFrame) -> dict[str, Any]:
    """Bundle all views into one JSON-serialisable profile for caching."""
    return {
        "n_sessions": int(len(metrics)),
        "n_participants": int(metrics["collection_id"].nunique()) if not metrics.empty else 0,
        "band": list(SESSION_BAND),
        "aggregate": aggregate_contrast(metrics),
        "participant_variation": participant_variation(metrics),
        "session_distributions": session_distributions(metrics),
        "temporal": temporal_trends(metrics),
    }
