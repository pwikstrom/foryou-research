"""Trend statistics for the Sessions tab's episode detail.

A monotone-trend scan over the variables of a run's members (exact Spearman
rank correlation against a sampled null, Benjamini–Hochberg over the scanned
variables), min/max ranges, creator counts, and variable labels. Pure
computation: no caches or I/O beyond the variable-schema label lookup.
"""

import itertools
from functools import lru_cache

import numpy as np
import pandas as pd

from fyp.core.fyp_config import fyp_cf

# Enumerate every ordering up to this length; sample above it. The Spearman
# null depends only on n, so each length's null is built once per process.
_TREND_MAX_EXACT = 8
_TREND_SAMPLES = 20_000


@lru_cache(maxsize=32)
def _spearman_null(n: int) -> np.ndarray:
    """Sorted ``|rho|`` under a random ordering of ``n`` items.

    Distribution-free in the ranks, so it depends only on ``n`` — building it
    once per length is what makes an exact test affordable per request.
    Deliberately NOT scipy's default p-value: that is a t-approximation which
    returns p ~ 0 for a perfect ordering of 4 items, where the exact answer is
    0.083. On this corpus the approximation turned a 3.4% hit rate into 21.6%.
    """
    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    if n <= _TREND_MAX_EXACT:
        orders = np.array(list(itertools.permutations(range(n))), dtype=float)
    else:
        rng = np.random.default_rng(0)
        orders = np.array([rng.permutation(n) for _ in range(_TREND_SAMPLES)], dtype=float)
    oc = orders - orders.mean(axis=1, keepdims=True)
    return np.sort(np.abs((oc @ xc) / (xc**2).sum()))


def spearman_exact(y: np.ndarray) -> tuple[float, float]:
    """Spearman rho of ``y`` against position, with an exact permutation p."""
    n = len(y)
    ranks = pd.Series(y).rank().to_numpy()
    x = np.arange(n, dtype=float)
    xc, rc = x - x.mean(), ranks - ranks.mean()
    denom = np.sqrt((rc**2).sum() * (xc**2).sum())
    if denom <= 0:
        return float("nan"), 1.0
    rho = float((rc @ xc) / denom)
    null = _spearman_null(n)
    hits = int((null >= abs(rho) - 1e-12).sum())
    if n <= _TREND_MAX_EXACT:
        # Enumerated null: the observed ordering is one of them, so the count
        # already carries its own floor (2/n!, since reversal ties it).
        return rho, hits / len(null)
    # Sampled null: (1 + hits) / (1 + m), the standard permutation-test
    # estimator. A plain mean can return exactly 0, which claims a certainty
    # the sample cannot support.
    return rho, (1 + hits) / (1 + len(null))


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """BH-adjusted q-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [1.0] * m
    running = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        running = min(running, pvalues[i] * m / (m - rank + 1))
        q[i] = running
    return q


def min_max_ranges(series: dict[str, np.ndarray]) -> list[dict]:
    """Observed min/max per numeric variable, for the "(more info)" panels.

    Purely descriptive — no test, no threshold — so it is computed for every
    binge/session, including ones too short for the trend scan.

    Args:
        series: variable name → aligned value array (NaN for missing).

    Returns:
        ``[{"variable", "label", "min", "max", "n"}, ...]`` sorted by label;
        variables with no finite value are omitted.
    """
    out = []
    for name, values in series.items():
        ok = np.isfinite(values)
        if not ok.any():
            continue
        out.append(
            {
                "variable": name,
                # dwell_s is per-play, not a var_schema variable, so it has no
                # display name to look up.
                "label": "Dwell (s)" if name == "dwell_s" else variable_label(name),
                "min": round(float(values[ok].min()), 3),
                "max": round(float(values[ok].max()), 3),
                "n": int(ok.sum()),
            }
        )
    return sorted(out, key=lambda r: r["label"].lower())


def scan_trend(members: list[dict], feat: pd.DataFrame, min_n: int) -> dict:
    """Find the strongest monotone trend across one binge's ordered members.

    Every numeric variable is tested with an exact permutation Spearman against
    member position, and the resulting p-values are Benjamini-Hochberg adjusted
    ACROSS the variables scanned — without that correction, scanning ~9
    variables on a short run manufactures a "finding" for most binges.

    Args:
        members: The binge's members in time order (each with ``item_id`` and
            ``dwell_s``).
        feat: Numeric per-video variables, item_id-indexed.
        min_n: Fewest non-null points a variable needs to be tested.

    Returns:
        A dict the card renders verbatim: ``scanned`` (how many variables had
        enough data), ``n_members``, ``min_n``, and either ``trend`` (the
        single strongest surviving result) or ``trend: None``. A null trend
        with ``scanned: 0`` means "not testable", which the UI must not present
        as "no trend exists". ``ranges`` (per-variable observed min/max, see
        :func:`min_max_ranges`) is descriptive and present regardless of the
        ``min_n`` gate — a binge too short to test still has extremes.
    """
    ids = [str(m.get("item_id")) for m in members]
    series: dict[str, np.ndarray] = {}
    if not feat.empty:
        sub = feat.reindex(ids)
        for col in feat.columns:
            series[col] = sub[col].to_numpy(dtype=float)
    # Dwell rides along from the member list — it is per-PLAY, so it never
    # appears in the per-video map, yet it is the variable most likely to
    # trend within a binge (the satiation effect).
    series["dwell_s"] = np.array(
        [np.nan if m.get("dwell_s") is None else float(m["dwell_s"]) for m in members]
    )

    tested = []
    for name, values in series.items():
        ok = np.isfinite(values)
        # A variable that barely varies has no monotone trend to find, and its
        # tie-heavy ranks make the permutation null a poor approximation.
        if int(ok.sum()) < min_n or len(np.unique(values[ok])) < 3:
            continue
        rho, p = spearman_exact(values[ok])
        if np.isfinite(rho):
            tested.append(
                {"variable": name, "rho": round(rho, 3), "p": round(p, 5), "n": int(ok.sum())}
            )

    out = {
        "scanned": len(tested),
        "n_members": len(members),
        "min_n": min_n,
        "trend": None,
        "ranges": min_max_ranges(series),
    }
    if not tested:
        return out
    for entry, q in zip(tested, benjamini_hochberg([t["p"] for t in tested])):
        entry["q"] = round(q, 5)
    best = min(tested, key=lambda t: (t["q"], -abs(t["rho"])))
    if best["q"] < 0.05:
        best["direction"] = "rising" if best["rho"] > 0 else "falling"
        best["label"] = variable_label(best["variable"])
        out["trend"] = best
    return out


@lru_cache(maxsize=1024)
def variable_label(name: str) -> str:
    """Human-readable name for a scanned variable, from var_schema if present."""
    try:
        schema = fyp_cf.get("var_schema")
        if schema is not None and name in schema.index:
            display = schema.loc[name].get("display_name")
            if isinstance(display, str) and display.strip():
                return display.strip()
    except Exception:
        pass
    return name.replace("_", " ")


def creator_count(item_ids: list[str], feat: pd.DataFrame) -> dict:
    """Distinct known creators across a run, with how many items are attributed.

    A bare count would silently under-report a run whose videos were never
    scraped: 3 creators across 4 known authors is a different observation from
    3 across 12, so both numbers travel together.
    """
    known = 0
    authors: set[str] = set()
    if not feat.empty and "author" in feat.columns:
        for value in feat.reindex([str(i) for i in item_ids])["author"]:
            if value is None:
                continue
            try:
                if pd.isna(value):
                    continue
            except (TypeError, ValueError):
                pass
            known += 1
            authors.add(str(value))
    return {"n_creators": len(authors), "n_attributed": known, "n_items": len(item_ids)}
