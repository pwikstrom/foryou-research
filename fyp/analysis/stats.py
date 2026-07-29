"""Group-comparison statistics for the Correlations tab.

Computes, per study, the "Group differences" artifact served by
``/api/correlations/group_stats``:

- a one-way **ANOVA sweep** over every eligible (factor × component) pair on
  the group-level PCA score table, with eta²/omega² effect sizes and a
  **Kruskal–Wallis** companion (rank-based robustness check), Benjamini–
  Hochberg-adjusted across the whole sweep;
- **PERMANOVA** per variable family (the components that share one
  per-variable PCA basis), so the multivariate test never mixes bases.

Everything is reported honestly and completely — no significance filtering
happens here; the UI ranks by effect size and shows q-values. The heavy
work runs in the ``pca_refresh`` worker (``run_pca_refresh.py``) which saves
``{study}_corr_stats.json`` to the ``cache`` location.
"""

import time

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from statsmodels.stats.multitest import multipletests

from fyp.logging_setup import get_logger
from fyp.recode_variables import get_factors_and_features_from_var_schema

logger = get_logger(__name__)


ARTIFACT_VERSION = 1

# Factor eligibility guards for the sweep: a level needs at least this many
# groups, and a factor at most this many levels (drops near-unique factors
# like activity_date, where every group is its own level).
MIN_LEVEL_N = 3
MAX_FACTOR_LEVELS = 50

# Eta-squared magnitude conventions (Cohen): small/medium/large.
ETA2_THRESHOLDS = (0.01, 0.06, 0.14)






def _cf():
    """Lazy fyp_config accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf






def compute_effect_sizes(anova_table, factor):
    """
    Compute eta-squared (η²) and omega-squared (ω²) for a one-way ANOVA.

    Parameters
    ----------
    anova_table : pandas.DataFrame
        ANOVA table from statsmodels, containing:
        - sum_sq
        - df
        - PR(>F)
    factor : str
        The row label (index) corresponding to your factor,
        e.g. 'C(local_week)'.

    Returns
    -------
    dict
        {'eta2': value, 'omega2': value}
    """

    ss_effect = anova_table.loc[factor, "sum_sq"]
    df_effect = anova_table.loc[factor, "df"]

    ss_resid = anova_table.loc["Residual", "sum_sq"]
    df_resid = anova_table.loc["Residual", "df"]

    # Eta squared
    eta2 = ss_effect / (ss_effect + ss_resid)

    # Mean squared residual
    ms_resid = ss_resid / df_resid

    # Omega squared
    omega2 = (ss_effect - df_effect * ms_resid) / (ss_effect + ss_resid + ms_resid)

    return {"eta2": eta2, "omega2": omega2}






def eta2_magnitude(eta2) -> str | None:
    """Cohen-convention label for an eta-squared value."""
    if eta2 is None or not np.isfinite(eta2):
        return None
    small, medium, large = ETA2_THRESHOLDS
    if eta2 < small:
        return "negligible"
    if eta2 < medium:
        return "small"
    if eta2 < large:
        return "medium"
    return "large"






def variable_families(columns) -> dict:
    """Map each derived column to its base schema variable (longest-prefix match).

    ``advertising_C0`` / ``advertising_entropy`` -> ``advertising``; columns
    that match no schema variable map to themselves.
    """
    cf = _cf()
    names = []
    if 'var_schema' in cf and isinstance(cf['var_schema'], pd.DataFrame):
        names = [str(v) for v in cf['var_schema']['variable_name'].tolist()]
    sorted_names = sorted(names, key=len, reverse=True)

    out = {}
    for col in columns:
        col_s = str(col)
        if col_s in names:
            out[col] = col_s
            continue
        base = col_s
        for name in sorted_names:
            if col_s.startswith(name + '_'):
                base = name
                break
        out[col] = base
    return out






def eligible_factors(scores_df: pd.DataFrame) -> dict:
    """Return {factor: usable level values} for factors worth testing.

    A factor qualifies when, after dropping levels with fewer than
    ``MIN_LEVEL_N`` groups, it has 2..``MAX_FACTOR_LEVELS`` levels and at
    least 2 residual degrees of freedom.
    """
    factors, _ = get_factors_and_features_from_var_schema(
        some_events_df=scores_df, verbose=False)
    factors = [f for f in factors if f.lower() != 'session_id']

    out = {}
    for f in factors:
        counts = scores_df[f].astype(str).value_counts(dropna=True)
        levels = counts[counts >= MIN_LEVEL_N].index.tolist()
        n_rows = int(counts[levels].sum()) if levels else 0
        if len(levels) < 2 or len(levels) > MAX_FACTOR_LEVELS:
            continue
        if n_rows - len(levels) < 2:
            continue
        out[f] = levels
    return out






def component_columns(scores_df: pd.DataFrame) -> list:
    """Numeric, non-``_raw`` columns with more than one distinct value."""
    numeric = scores_df.select_dtypes(include=['number'])
    return [c for c in numeric.columns
            if not str(c).endswith('_raw') and numeric[c].nunique(dropna=True) > 1]






def component_anova_sweep(scores_df: pd.DataFrame, factors: dict, components: list) -> list:
    """One-way ANOVA + Kruskal–Wallis for every (factor × component) pair.

    Returns a list of result dicts (one per testable pair) with F/p, eta²/ω²
    and the Kruskal–Wallis H/p. Benjamini–Hochberg q-values are added across
    the whole sweep (separately for the ANOVA and KW p-value families).
    """
    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    results = []
    for factor, levels in factors.items():
        level_set = set(levels)
        fvals = scores_df[factor].astype(str)
        for comp in components:
            sub = pd.DataFrame({
                "y": pd.to_numeric(scores_df[comp], errors='coerce').astype('float64'),
                "g": fvals,
            })
            sub = sub[sub["g"].isin(level_set)].dropna()
            counts = sub["g"].value_counts()
            used_levels = counts[counts >= MIN_LEVEL_N].index.tolist()
            sub = sub[sub["g"].isin(used_levels)]
            if len(used_levels) < 2 or len(sub) - len(used_levels) < 2:
                continue
            if float(sub["y"].std()) == 0.0:
                continue

            try:
                model = smf.ols('y ~ C(g)', data=sub).fit()
                anova_table = sm.stats.anova_lm(model, typ=2)
                effect = compute_effect_sizes(anova_table, 'C(g)')
                f_stat = float(anova_table.loc['C(g)', 'F'])
                p_val = float(anova_table.loc['C(g)', 'PR(>F)'])
            except Exception as e:
                logger.warning(f"ANOVA failed for {comp} ~ {factor}: {e}")
                continue

            groups = [g["y"].to_numpy() for _, g in sub.groupby("g")]
            kw_h = kw_p = None
            try:
                kw = scipy_stats.kruskal(*groups)
                if np.isfinite(kw.statistic):
                    kw_h, kw_p = float(kw.statistic), float(kw.pvalue)
            except Exception:
                pass

            results.append({
                "factor": factor,
                "component": comp,
                "n": int(len(sub)),
                "levels": int(len(used_levels)),
                "F": f_stat if np.isfinite(f_stat) else None,
                "p": p_val if np.isfinite(p_val) else None,
                "eta2": float(effect["eta2"]) if np.isfinite(effect["eta2"]) else None,
                "omega2": float(effect["omega2"]) if np.isfinite(effect["omega2"]) else None,
                "magnitude": eta2_magnitude(effect["eta2"]),
                "kw_H": kw_h,
                "kw_p": kw_p,
            })

    _add_bh_q(results, "p", "q")
    _add_bh_q(results, "kw_p", "kw_q")
    return results






def family_permanova(scores_df: pd.DataFrame, factors: dict, families: dict,
                     permutations: int = 999) -> list:
    """PERMANOVA of each variable family's component block against each factor.

    A family is the set of components sharing one per-variable PCA basis, so
    the multivariate distance never mixes bases. Families with fewer than 2
    columns are skipped (a univariate family is covered by the ANOVA sweep).
    Reports every result with its pseudo-F and permutation p; Benjamini–
    Hochberg q across all reported tests.
    """
    from skbio.stats.distance import DistanceMatrix
    from skbio.stats.distance import permanova as skbio_permanova
    from sklearn.metrics import pairwise_distances

    # family -> its component columns (>= 2 to be meaningfully multivariate)
    fam_cols: dict = {}
    for col, fam in families.items():
        fam_cols.setdefault(fam, []).append(col)
    fam_cols = {f: cols for f, cols in fam_cols.items() if len(cols) >= 2}

    results = []
    for family, cols in sorted(fam_cols.items()):
        block = scores_df[cols].apply(pd.to_numeric, errors='coerce').astype('float64')
        for factor, levels in factors.items():
            fvals = scores_df[factor].astype(str)
            mask = block.notna().all(axis=1) & fvals.isin(set(levels))
            sub = block[mask]
            grouping = fvals[mask]
            counts = grouping.value_counts()
            used_levels = counts[counts >= MIN_LEVEL_N].index
            keep = grouping.isin(set(used_levels))
            sub, grouping = sub[keep], grouping[keep]
            if len(used_levels) < 2 or len(sub) < 6:
                continue

            try:
                d = pairwise_distances(sub.to_numpy(), metric='euclidean')
                # Repair tiny floating-point asymmetry / diagonal noise
                d = (d + d.T) / 2
                np.fill_diagonal(d, 0.0)
                dm = DistanceMatrix(d)
                res = skbio_permanova(dm, grouping.tolist(), permutations=permutations)
                pseudo_f = float(res["test statistic"])
                p_val = float(res["p-value"])
            except Exception as e:
                logger.warning(f"PERMANOVA failed for {family} ~ {factor}: {e}")
                continue

            results.append({
                "family": family,
                "factor": factor,
                "n": int(len(sub)),
                "levels": int(len(used_levels)),
                "n_components": len(cols),
                "pseudo_F": pseudo_f if np.isfinite(pseudo_f) else None,
                "p": p_val if np.isfinite(p_val) else None,
                "permutations": int(permutations),
            })

    _add_bh_q(results, "p", "q")
    return results






def _add_bh_q(results: list, p_key: str, q_key: str) -> None:
    """Attach Benjamini–Hochberg q-values across ``results`` in place."""
    idx = [i for i, r in enumerate(results) if r.get(p_key) is not None]
    for r in results:
        r[q_key] = None
    if not idx:
        return
    pvals = [results[i][p_key] for i in idx]
    _, qvals, _, _ = multipletests(pvals, method='fdr_bh')
    for i, qv in zip(idx, qvals):
        results[i][q_key] = float(qv)






def compute_group_stats_artifact(scores_df: pd.DataFrame, study_name: str) -> dict:
    """Build the full ``{study}_corr_stats.json`` payload for one study."""
    permutations = int(_cf().get("correlations", {}).get("permanova_permutations", 999))

    components = component_columns(scores_df)
    factors = eligible_factors(scores_df)
    families = variable_families(components)

    t0 = time.perf_counter()
    anova = component_anova_sweep(scores_df, factors, components)
    t1 = time.perf_counter()
    perma = family_permanova(scores_df, factors, families, permutations=permutations)
    t2 = time.perf_counter()
    logger.info(
        f"[group-stats] {study_name}: {len(anova)} ANOVA + {len(perma)} PERMANOVA tests "
        f"({len(factors)} factors, {len(components)} components; "
        f"anova={t1 - t0:.1f}s permanova={t2 - t1:.1f}s)")

    return {
        "version": ARTIFACT_VERSION,
        "study": study_name,
        "generated_at": time.time(),
        "n_groups": int(len(scores_df)),
        "factors": sorted(factors.keys()),
        "anova": anova,
        "permanova": perma,
        "config": {
            "permanova_permutations": permutations,
            "min_level_n": MIN_LEVEL_N,
            "max_factor_levels": MAX_FACTOR_LEVELS,
        },
    }
