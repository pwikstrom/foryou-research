"""Stage-B predictive modelling for sequence analysis.

Answers the honest question the descriptive lift cannot: does dwell behaviour
add *incremental* predictive skill over a feed-stickiness baseline? The baseline
already knows the current window's value of the target (autocorrelation — "the
feed keeps serving the same thing"), the time of day, day of week, and how deep
into the session the window sits. The augmented model adds the dwell features.

The claim "dwell predicts the next window's content" is supported only if the
augmented model beats the baseline under **per-participant** cross-validation
(GroupKFold on collection_id) — row-level CV would leak autocorrelated windows
across train/test and massively inflate the apparent skill.

Pure functions operating on the per-window frame produced by
``fyp.sequence_analysis``; no I/O.
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

# Dwell predictor features (the "treatment" whose incremental value we test).
DWELL_FEATURES = ("dwell_mean", "dwell_median", "dwell_p90", "completion_mean")

# Feed-stickiness + temporal + position baseline (the null to beat).
BASELINE_FEATURES = ("stickiness", "hour_sin", "hour_cos", "dow", "window_idx_f")





def build_model_table(
    windows: pd.DataFrame, target_mean_col: str, horizon: int = 1
) -> pd.DataFrame:
    """Assemble a per-window modelling table for one scalar target.

    Joins window k to window k+horizon within the same session and builds the
    predictor columns. ``stickiness`` is the current window's value of the same
    target (the autocorrelation control); the response ``y`` is the next
    window's value.

    Args:
        windows: Per-window frame from :func:`fyp.sequence_analysis.build_windows`.
        target_mean_col: The ``mn::<target>`` column to model.
        horizon: Windows ahead for the response.

    Returns:
        DataFrame with ``y``, the baseline features, the dwell features, and
        ``collection_id`` (the CV grouping key), one row per valid window pair.
    """
    keys = ["collection_id", "session_id", "window_idx"]
    nxt = windows[keys + [target_mean_col]].copy()
    nxt["window_idx"] = nxt["window_idx"] - horizon
    nxt = nxt.rename(columns={target_mean_col: "y"})
    merged = windows.merge(nxt, on=keys, how="inner")
    merged = merged[merged["y"].notna() & merged[target_mean_col].notna()].copy()

    ts = pd.to_datetime(merged["ts_start"])
    hour = ts.dt.hour
    merged["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    merged["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    merged["dow"] = ts.dt.dayofweek.astype("float64")
    merged["stickiness"] = merged[target_mean_col].astype("float64")
    merged["window_idx_f"] = merged["window_idx"].astype("float64")
    for feat in DWELL_FEATURES:
        merged[feat] = merged[feat].astype("float64")
    return merged





def evaluate_incremental_skill(
    table: pd.DataFrame,
    target_label: str,
    log_target: bool = True,
    n_splits: int = 5,
) -> dict:
    """Compare baseline vs baseline+dwell skill via per-participant GroupKFold.

    Args:
        table: Output of :func:`build_model_table`.
        target_label: Human-readable target name (for the result dict).
        log_target: ``log1p`` the response (stabilises skewed counts).
        n_splits: GroupKFold splits (capped at the number of participants).

    Returns:
        Dict with baseline / augmented mean±std R², ``delta_r2`` (the headline:
        incremental skill of dwell), and the standardised dwell coefficients
        from a full-data fit (sign/magnitude of each dwell feature's effect).
    """
    base = list(BASELINE_FEATURES)
    aug = base + list(DWELL_FEATURES)
    df = table.dropna(subset=aug + ["y"]).copy()

    groups = df["collection_id"]
    n_groups = int(groups.nunique())
    splits = max(2, min(n_splits, n_groups))

    y = df["y"].astype("float64").to_numpy()
    if log_target:
        y = np.log1p(np.clip(y, a_min=0, a_max=None))

    gkf = GroupKFold(n_splits=splits)

    def cv_r2(features: list[str]) -> np.ndarray:
        X = df[features].to_numpy()
        pipe = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
        return cross_val_score(pipe, X, y, cv=gkf, groups=groups, scoring="r2")

    sb = cv_r2(base)
    sa = cv_r2(aug)

    # Standardised dwell coefficients from a full-data fit (interpretation aid).
    pipe = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    pipe.fit(df[aug].to_numpy(), y)
    coefs = pipe.named_steps["ridge"].coef_
    dwell_coefs = {f: round(float(c), 4) for f, c in zip(aug, coefs) if f in DWELL_FEATURES}

    return {
        "target": target_label,
        "n_rows": int(len(df)),
        "n_participants": n_groups,
        "splits": splits,
        "log_target": log_target,
        "baseline_r2": round(float(sb.mean()), 4),
        "baseline_r2_std": round(float(sb.std()), 4),
        "augmented_r2": round(float(sa.mean()), 4),
        "augmented_r2_std": round(float(sa.std()), 4),
        "delta_r2": round(float(sa.mean() - sb.mean()), 4),
        "dwell_coefs": dwell_coefs,
    }
