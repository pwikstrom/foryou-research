"""Per-collection semantic centre-of-gravity, entropy, and trajectory.

Projects one collection's play activity onto the global Semantic Space map so
the dashboard can overlay where that collection sits in embedding space, how
diverse its diet is, and how it drifts over time.

Because the 2D map is a t-SNE embedding (non-parametric, no out-of-sample
transform), a collection cannot be projected by its mean embedding. Instead the
embedding-derived ``niche`` labels are used as the bridge: every played video
carries a niche, each niche already has a stable 2D centroid, and a collection's
centre of gravity is the play-weighted mean of those niche centroids. Entropy is
the Shannon entropy of the play-weighted niche distribution (niches are
PCA/KMeans clusters of the embeddings, so this is embeddings-grounded), and the
spatial spread is a covariance ellipse of the played videos' own 2D positions.

The map geometry is read from ``recoded/video_map.parquet`` (cached in-process);
the collection's plays are read straight from the consolidated activity file
``recoded/collections_recoded.parquet`` with a column projection and a
``collection_id`` filter. That file is clustered by ``collection_id``, so the
filter prunes row groups and one collection loads in ~100 ms / <1 MB — light
enough for a live request (unlike the full enrichment merge the timeline worker
runs). The trade-off: ``video_duration`` lives only in the scrape enrichment, so
the watch-time weight is ``play_duration`` (already capped at 600 s in ingest);
collections with no recorded watch time (e.g. ``observe``-only baselines) fall
back to a unit count, surfaced as ``weight_mode``.
"""

import math

import numpy as np
import pandas as pd

import fyp.data_io as data_io
import fyp.embeddings as embeddings
import fyp.video_map as video_map
from fyp.organize_datasets import COLLECTIONS_LABEL
from fyp.timeline_analysis import compute_linreg

# Annotation scalars (denormalised into the map file) whose per-period
# watch-time-weighted mean is tracked over time — e.g. is the donor drifting
# toward more political / more sensitive content. Only those present in the map
# are used, so older maps degrade gracefully.
_OVERLAY_SCALARS = ["political_score", "sensitivity_score"]

# Cached map geometry, rebuilt only when the map file's fingerprint changes.
_GEO_CACHE: dict = {
    "fingerprint": None, "item_geo": None,
    "niche_centroids": None, "niche_names": None,
}

# Bounded in-process cache of built trajectories, keyed on request params plus
# the map fingerprint (so a map rebuild invalidates every entry).
_TRAJ_CACHE: dict = {}
_TRAJ_CACHE_MAX = 16

# Ellipse semi-axis = _ELLIPSE_K * sqrt(eigenvalue); ~1.5 sigma reads as a halo
# without swallowing the whole map. Fewer than _ELLIPSE_MIN_POINTS mapped plays
# cannot define a covariance, so no ellipse is drawn.
_ELLIPSE_K = 1.5
_ELLIPSE_MIN_POINTS = 3

# Buckets below this play count are still returned but flagged so the UI can
# de-emphasise their (noisy) centroid.
_MIN_PLAYS_FLAG = 3






def _map_fingerprint() -> str | None:
    """Return a size:mtime fingerprint of the map file, or None if absent."""
    fp = data_io.stat(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE,
    )
    return None if fp is None else f"{fp.get('size')}:{fp.get('mtime')}"






def _map_built_at() -> float | None:
    """Return the map file's mtime (used by the frontend freshness check)."""
    fp = data_io.stat(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.MAP_FILE,
    )
    return fp.get("mtime") if fp else None






def _load_niche_geometry() -> tuple[pd.DataFrame, dict, dict]:
    """Load and cache the map geometry needed to place a collection.

    Returns:
        A tuple ``(item_geo, niche_centroids, niche_names)`` where ``item_geo``
        is a DataFrame indexed by ``item_id`` with ``niche``/``x``/``y`` columns
        (one row per embedded video; ``x``/``y`` are NaN for unsampled videos),
        ``niche_centroids`` maps ``niche_id -> (cx, cy)`` (median 2D position of
        that niche's mapped members), and ``niche_names`` maps
        ``niche_id -> display name``.
    """
    fingerprint = _map_fingerprint()
    if _GEO_CACHE["item_geo"] is not None and _GEO_CACHE["fingerprint"] == fingerprint:
        return _GEO_CACHE["item_geo"], _GEO_CACHE["niche_centroids"], _GEO_CACHE["niche_names"]

    cols = ["item_id", "niche", "x", "y"]
    try:
        map_df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=video_map.MAP_FILE, columns=cols + _OVERLAY_SCALARS,
        )
    except Exception:
        map_df = data_io.load_parquet_selective(
            storage_location=embeddings.STORE_LOCATION,
            filename=video_map.MAP_FILE, columns=cols,
        )

    item_geo = map_df.copy()
    item_geo["item_id"] = item_geo["item_id"].astype("string")
    item_geo["niche"] = pd.to_numeric(item_geo["niche"], errors="coerce")
    item_geo["x"] = pd.to_numeric(item_geo["x"], errors="coerce")
    item_geo["y"] = pd.to_numeric(item_geo["y"], errors="coerce")
    for sc in _OVERLAY_SCALARS:
        if sc in item_geo.columns:
            item_geo[sc] = pd.to_numeric(item_geo[sc], errors="coerce")
    item_geo = item_geo.dropna(subset=["niche"])
    item_geo["niche"] = item_geo["niche"].astype("int64")
    item_geo = item_geo.set_index("item_id")

    # Niche centroid = median (x, y) over that niche's mapped members, matching
    # the frontend's _ssComputeCentroids so the marker agrees with the labels.
    mapped = item_geo.dropna(subset=["x", "y"])
    centroids_df = mapped.groupby("niche")[["x", "y"]].median()
    niche_centroids = {
        int(n): (float(row["x"]), float(row["y"]))
        for n, row in centroids_df.iterrows()
    }

    niches_meta = data_io.load_json(
        storage_location=embeddings.STORE_LOCATION, filename=video_map.NICHES_FILE,
    ) or {}
    niche_names = {int(k): v.get("name", f"Niche {k}") for k, v in niches_meta.items()}

    _GEO_CACHE.update({
        "fingerprint": fingerprint, "item_geo": item_geo,
        "niche_centroids": niche_centroids, "niche_names": niche_names,
    })
    return item_geo, niche_centroids, niche_names






def _load_collection_plays(
        collection_id: str,
        start: str | None,
        end: str | None,
    ) -> tuple[pd.DataFrame | None, str]:
    """Load one collection's play/observe rows with a watch-time weight.

    Reads only the activity columns for ``collection_id`` from the consolidated
    activity file (column projection + filter pushdown), so the load is light
    enough for a live request. Per-row weight is ``play_duration`` (capped at
    600 s upstream); when no watch time is recorded at all (observe-only
    baselines, Zeeschuimer donations) the weight falls back to a unit count.

    Args:
        collection_id: The collection to load.
        start: Optional inclusive ``YYYY-MM-DD`` lower bound on local date.
        end: Optional inclusive ``YYYY-MM-DD`` upper bound on local date.

    Returns:
        A tuple ``(plays, weight_mode)`` where ``plays`` has ``item_id``/
        ``_date``/``_w`` columns (or None if empty) and ``weight_mode`` is
        ``"watch_time"`` or ``"count"``.
    """
    df = data_io.load_parquet_selective(
        storage_location=embeddings.STORE_LOCATION,
        filename=f"{COLLECTIONS_LABEL}_recoded.parquet",
        columns=["item_id", "local_date", "activity_type", "play_duration"],
        filters=[("collection_id", "==", collection_id)],
    )
    if df is None or df.empty or "item_id" not in df.columns:
        return None, "watch_time"

    if "activity_type" in df.columns:
        df = df[df["activity_type"].isin(["play", "observe"])].copy()
    if df.empty:
        return None, "watch_time"

    dates = pd.to_datetime(df["local_date"], errors="coerce")
    df["_dt"] = dates
    df["_date"] = dates.dt.date.astype("string")
    df = df.dropna(subset=["_date"])

    if start:
        df = df[df["_date"] >= start]
    if end:
        df = df[df["_date"] <= end]
    if df.empty:
        return None, "watch_time"

    weight_mode = "watch_time"
    if "play_duration" in df.columns:
        play_dur = pd.to_numeric(df["play_duration"], errors="coerce").astype("float64")
        df["_w"] = play_dur.fillna(0.0)
    else:
        df["_w"] = 0.0
    if float(df["_w"].sum()) <= 0:
        weight_mode = "count"
        df["_w"] = 1.0

    df["item_id"] = df["item_id"].astype("string")
    return df[["item_id", "_date", "_dt", "_w"]].copy(), weight_mode






def _ellipse(mapped: pd.DataFrame, center: tuple | None = None) -> dict | None:
    """Weighted dispersion ellipse of a bucket's mapped plays.

    Args:
        mapped: Rows with ``x``/``y``/``_w`` for plays that carry 2D coords.
        center: Optional ``(x, y)`` to centre the ellipse on (the period's
            centre of gravity). When given, the spread is measured *about that
            point* so the COG dot is always the ellipse centre — otherwise the
            two diverge (the COG is a niche-centroid weighting over all plays,
            while the spread is fit to only the t-SNE-sampled subset, so for
            small/daily buckets the sampled mean drifts off the COG). Defaults
            to the weighted mean of the sampled points.

    Returns:
        ``{cx, cy, rx, ry, theta}`` (theta in degrees) or None when too few
        points or a degenerate covariance.
    """
    pts = mapped.dropna(subset=["x", "y"])
    if len(pts) < _ELLIPSE_MIN_POINTS:
        return None

    xy = pts[["x", "y"]].to_numpy(dtype="float64")
    w = pts["_w"].to_numpy(dtype="float64")
    if w.sum() <= 0:
        w = np.ones(len(pts), dtype="float64")
    wsum = float(w.sum())

    if center is not None and center[0] is not None and center[1] is not None:
        mean = np.array([float(center[0]), float(center[1])], dtype="float64")
    else:
        mean = (xy * w[:, None]).sum(axis=0) / wsum
    d = xy - mean
    cov = (d.T * w) @ d / wsum
    if not np.all(np.isfinite(cov)):
        return None

    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(vals)[::-1]
    vals = np.clip(vals[order], 0.0, None)
    vecs = vecs[:, order]
    rx = _ELLIPSE_K * math.sqrt(vals[0])
    ry = _ELLIPSE_K * math.sqrt(vals[1])
    theta = math.degrees(math.atan2(vecs[1, 0], vecs[0, 0]))
    return {
        "cx": round(float(mean[0]), 3), "cy": round(float(mean[1]), 3),
        "rx": round(float(rx), 3), "ry": round(float(ry), 3),
        "theta": round(float(theta), 2),
    }






def _bucket_metrics(sub: pd.DataFrame, niche_centroids: dict, niche_names: dict) -> dict:
    """Reduce one bucket of joined plays to centre-of-gravity + entropy metrics.

    Args:
        sub: Plays for this bucket joined to geometry (``_w``/``niche``/``x``/
            ``y``; ``niche``/``x``/``y`` may be NaN for unmapped videos).
        niche_centroids: ``niche_id -> (cx, cy)`` lookup.
        niche_names: ``niche_id -> name`` lookup.

    Returns:
        A metrics dict (see module docstring / API payload shape).
    """
    n_plays = int(len(sub))            # all plays in the bucket (incl. uncorpus'd)
    watch_time = round(float(sub["_w"].sum()), 1)
    has_niche = sub.dropna(subset=["niche"])
    n_mapped = int(len(has_niche))      # plays WITH a niche — the set every metric uses

    base = {
        "x": None, "y": None, "niche_entropy": None, "niche_entropy_norm": None,
        "n_plays": n_plays, "n_mapped": n_mapped, "watch_time": watch_time,
        "top_niches": [], "ellipse": None, "low_volume": n_plays < _MIN_PLAYS_FLAG,
        "_probs": {},
    }

    # Watch-time-weighted mean of each available annotation scalar (drift in
    # content character over time — e.g. political/sensitive).
    for sc in _OVERLAY_SCALARS:
        if sc not in sub.columns:
            continue
        vals = pd.to_numeric(sub[sc], errors="coerce")
        m = vals.notna()
        wsc = float(sub["_w"][m].sum())
        base[f"mean_{sc}"] = round(float((vals[m] * sub["_w"][m]).sum() / wsc), 4) if wsc > 0 else None

    if has_niche.empty:
        return base

    # Play-weighted niche distribution; fall back to counts if every weight is 0.
    w = has_niche.groupby("niche")["_w"].sum()
    if float(w.sum()) <= 0:
        w = has_niche.groupby("niche").size().astype("float64")
    p = w / w.sum()
    base["_probs"] = {int(k): float(v) for k, v in p.items()}
    pv = p.to_numpy(dtype="float64")

    entropy = float(-(pv * np.log2(np.clip(pv, 1e-12, 1.0))).sum())
    k = int((pv > 0).sum())
    entropy_norm = float(entropy / math.log2(k)) if k > 1 else 0.0

    # Centre of gravity = play-weighted mean of niche centroids. Niches whose
    # members are all unsampled have no centroid; they still count for entropy
    # but are skipped (and their weight removed) from the position average.
    cx = cy = wsum = 0.0
    for niche_id, prob in p.items():
        centroid = niche_centroids.get(int(niche_id))
        if centroid is None:
            continue
        cx += prob * centroid[0]
        cy += prob * centroid[1]
        wsum += prob
    if wsum > 0:
        base["x"] = round(cx / wsum, 3)
        base["y"] = round(cy / wsum, 3)

    top = p.sort_values(ascending=False).head(3)
    base["top_niches"] = [
        {"niche": int(n), "name": niche_names.get(int(n), f"Niche {n}"),
         "share": round(float(s), 4)}
        for n, s in top.items()
    ]
    base["niche_entropy"] = round(entropy, 4)
    base["niche_entropy_norm"] = round(entropy_norm, 4)
    # Centre the ellipse on the COG so the dot is always its centre (see _ellipse).
    base["ellipse"] = _ellipse(has_niche, center=(base["x"], base["y"]))
    return base






def _js_divergence(p: np.ndarray, q: np.ndarray) -> float:
    """Jensen-Shannon divergence (base 2, range [0, 1]) of two aligned pmfs."""
    m = 0.5 * (p + q)

    def _kl(a: np.ndarray, b: np.ndarray) -> float:
        mask = a > 0
        return float(np.sum(a[mask] * np.log2(a[mask] / b[mask])))

    return 0.5 * _kl(p, m) + 0.5 * _kl(q, m)






def _js_divergence_dicts(a: dict, b: dict) -> float:
    """JS divergence between two ``niche -> probability`` distributions."""
    keys = set(a) | set(b)
    p = np.array([a.get(k, 0.0) for k in keys], dtype="float64")
    q = np.array([b.get(k, 0.0) for k in keys], dtype="float64")
    s, t = p.sum(), q.sum()
    if s <= 0 or t <= 0:
        return 0.0
    return _js_divergence(p / s, q / t)






def _enrich_change_metrics(payload: dict) -> None:
    """Add per-period change metrics + trajectory-level summaries in place.

    Per period: ``js_from_prev`` (distributional velocity — JS divergence from
    the previous period's niche mix), ``novelty`` (share of the period's
    attention on niches never watched before), ``cum_niches`` (cumulative
    distinct niches — the discovery curve). Trajectory-level: ``tortuosity``
    (net distributional displacement ÷ total path = directional vs churny),
    ``total_js_path``, ``velocity_mean``, and ``trends`` (slope + total change
    of entropy/novelty/velocity/scalars via the timeline linreg). Strips the
    internal ``_probs`` from every bucket.

    Args:
        payload: The trajectory payload to mutate.
    """
    points = payload.get("points") or []
    seen: set = set()
    prev = None
    prob_seq = []
    for p in points:
        probs = p.pop("_probs", {}) or {}
        if probs:
            p["novelty"] = round(float(sum(v for k, v in probs.items() if k not in seen)), 4)
            seen.update(probs.keys())
            prob_seq.append(probs)
        else:
            p["novelty"] = None
        p["cum_niches"] = len(seen)
        p["js_from_prev"] = (round(_js_divergence_dicts(prev, probs), 4)
                             if (prev and probs) else None)
        if probs:
            prev = probs

    if payload.get("all_time"):
        payload["all_time"].pop("_probs", None)

    js_steps = [p["js_from_prev"] for p in points if p.get("js_from_prev") is not None]
    total_js = float(sum(js_steps))
    payload["total_js_path"] = round(total_js, 4)
    payload["velocity_mean"] = round(float(np.mean(js_steps)), 4) if js_steps else None
    payload["tortuosity"] = (
        round(_js_divergence_dicts(prob_seq[0], prob_seq[-1]) / total_js, 4)
        if len(prob_seq) >= 2 and total_js > 0 else None
    )

    trends = {}
    for key in ["niche_entropy", "novelty", "js_from_prev"] + [f"mean_{s}" for s in _OVERLAY_SCALARS]:
        vals = [p[key] for p in points if p.get(key) is not None]
        if len(vals) >= 2:
            lr = compute_linreg(vals)
            trends[key] = {"slope": lr["slope"], "total_change": lr["total_change"]}
    payload["trends"] = trends






def build_trajectory(
        collection_id: str,
        interval: str = "day",
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
    """Build the centre-of-gravity / entropy / trajectory payload for a collection.

    Args:
        collection_id: The collection to project onto the map.
        interval: ``"day"``/``"week"``/``"month"`` for a per-period trajectory
            (each period gets a centroid + dispersion ellipse) plus an all-time
            aggregate, or ``"all"`` for the all-time aggregate only.
        start: Optional inclusive ``YYYY-MM-DD`` lower date bound.
        end: Optional inclusive ``YYYY-MM-DD`` upper date bound.

    Returns:
        A JSON-serialisable dict with ``points`` (per-day metrics), ``all_time``
        (aggregate metrics), ``weight_mode``, coverage counts, and the map's
        build time.
    """
    cache_key = (collection_id, interval, start, end, _map_fingerprint())
    cached = _TRAJ_CACHE.get(cache_key)
    if cached is not None:
        return cached

    item_geo, niche_centroids, niche_names = _load_niche_geometry()
    plays, weight_mode = _load_collection_plays(collection_id, start, end)

    payload = {
        "collection_id": collection_id, "interval": interval,
        "weight_mode": weight_mode, "start": start, "end": end,
        "map_built_at": _map_built_at(),
        "n_plays_total": 0, "n_unmapped": 0,
        "points": [], "all_time": None,
    }

    if plays is None or plays.empty:
        _TRAJ_CACHE[cache_key] = payload
        return payload

    joined = plays.join(item_geo, on="item_id")
    payload["n_plays_total"] = int(len(joined))
    payload["n_unmapped"] = int(joined["niche"].isna().sum())

    if interval != "all":
        if interval == "month":
            joined["_period"] = joined["_dt"].dt.strftime("%Y-%m")
        elif interval == "week":
            joined["_period"] = joined["_dt"].dt.strftime("%G-W%V")
        else:  # day
            joined["_period"] = joined["_date"].astype("string")
        points = []
        for period, sub in joined.groupby("_period", sort=True):
            metrics = _bucket_metrics(sub, niche_centroids, niche_names)
            metrics["date"] = str(period)
            points.append(metrics)
        payload["points"] = points

    payload["all_time"] = _bucket_metrics(joined, niche_centroids, niche_names)
    _enrich_change_metrics(payload)

    _TRAJ_CACHE[cache_key] = payload
    if len(_TRAJ_CACHE) > _TRAJ_CACHE_MAX:
        _TRAJ_CACHE.pop(next(iter(_TRAJ_CACHE)))
    return payload
