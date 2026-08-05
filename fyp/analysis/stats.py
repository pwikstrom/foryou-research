"""Group-comparison statistics for the Correlations tab.

Computes, per study, the "Group differences" artifact (version 2) served by
``/api/correlations/group_stats``. The artifact separates two statistically
different questions instead of pooling them into one table:

- **Personalization** (``personalization`` / ``permanova_personalization``):
  how much of each variable's day-to-day variance lies *between* collections.
  One-way variance decomposition on ``collection_id`` — the eta² reads as an
  intraclass correlation. No p/q values: with hundreds of autocorrelated days
  per collection they are always ~0 and would only invite misreading; the
  effect size is the finding.
- **Within-feed comparisons** (``anova`` / ``permanova``): does a comparison
  variable (weekend, weekday, ...) move a component *within* feeds? The ANOVA
  is **blocked on collection** (``y ~ C(g) + C(collection_id)``) so
  between-collection variance leaves the error term, and the reported effect
  sizes are **partial** eta²/omega². The Kruskal–Wallis companion runs on
  within-collection-centered values (a documented rank approximation), and
  the comparison PERMANOVA runs on within-collection-centered component
  blocks (skbio has no strata support; free permutation over centered values
  is the honest cheap substitute — its p is still anti-conservative under
  serial dependence, which the UI caveats).
- A factor that is **constant within every collection** (e.g. platform when
  each collection donates from one platform) cannot be separated from
  personalization: it is computed one-way, flagged
  ``nested_in_collection: true``, excluded from the BH families (``q: null``)
  and captioned as having only as many independent units as collections.

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
from fyp.recode_variables import get_vars_by_role

logger = get_logger(__name__)


ARTIFACT_VERSION = 2

# The grouping column that defines the personalization / blocking structure.
COLLECTION_COL = "collection_id"

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






def center_within_collection(df: pd.DataFrame, cols) -> tuple[pd.DataFrame, bool]:
    """Subtract each collection's mean from ``cols`` (a fixed-effects move).

    Removes between-collection composition, so remaining associations are
    within-collection. Returns ``(df, applied)`` — a no-op when there is no
    ``collection_id`` column or none of ``cols`` is present.
    """
    cols = [c for c in cols if c in df.columns]
    if COLLECTION_COL not in df.columns or not cols:
        return df, False
    out = df.copy()
    vals = out[cols].astype('float64')
    group_keys = out[COLLECTION_COL].astype(str).values
    means = vals.groupby(group_keys).transform('mean')
    out[cols] = vals - means
    return out, True






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






def compute_partial_effect_sizes(anova_table, factor, n_obs: int) -> dict:
    """Partial η²/ω² for one term of a multi-term (blocked) ANOVA.

    Partial sizes relate the factor's sum of squares to itself plus the
    residual only — the block's (collection's) variance is excluded from the
    denominator, which is the point of blocking.

        partial η² = SS_eff / (SS_eff + SS_res)
        partial ω² = (SS_eff − df_eff·MS_err) / (SS_eff + (N − df_eff)·MS_err)

    Args:
        anova_table: statsmodels ``anova_lm`` table (typ=2).
        factor: Row label of the term, e.g. ``'C(g)'``.
        n_obs: Number of observations the model was fit on.

    Returns:
        ``{"eta2": partial_eta2, "omega2": partial_omega2}`` (omega may be
        slightly negative for null effects; reported as computed).
    """
    ss_effect = anova_table.loc[factor, "sum_sq"]
    df_effect = anova_table.loc[factor, "df"]
    ss_resid = anova_table.loc["Residual", "sum_sq"]
    df_resid = anova_table.loc["Residual", "df"]

    ms_resid = ss_resid / df_resid
    partial_eta2 = ss_effect / (ss_effect + ss_resid)
    partial_omega2 = (ss_effect - df_effect * ms_resid) / (
        ss_effect + (n_obs - df_effect) * ms_resid)
    return {"eta2": partial_eta2, "omega2": partial_omega2}






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






def _usable_levels(values: pd.Series) -> list:
    """Level values with at least ``MIN_LEVEL_N`` groups each."""
    counts = values.astype(str).value_counts(dropna=True)
    return counts[counts >= MIN_LEVEL_N].index.tolist()






def eligible_comparison_factors(scores_df: pd.DataFrame) -> dict:
    """Return {comparison factor: usable level values} for factors worth testing.

    Selects role ``"comparison"`` variables only — grouping keys and
    descriptors (Colour-by-only context like ``local_week``) never enter the
    sweep. A factor qualifies when, after dropping levels with fewer than
    ``MIN_LEVEL_N`` groups, it has 2..``MAX_FACTOR_LEVELS`` levels and at
    least 2 residual degrees of freedom.
    """
    factors = get_vars_by_role(("comparison",), some_events_df=scores_df)
    factors = [f for f in factors
               if f.lower() != 'session_id' and f != COLLECTION_COL]

    out = {}
    for f in factors:
        levels = _usable_levels(scores_df[f])
        n_rows = int(scores_df[f].astype(str).isin(levels).sum()) if levels else 0
        if len(levels) < 2 or len(levels) > MAX_FACTOR_LEVELS:
            continue
        if n_rows - len(levels) < 2:
            continue
        out[f] = levels
    return out






def collection_levels(scores_df: pd.DataFrame) -> list:
    """Usable ``collection_id`` levels (>= ``MIN_LEVEL_N`` groups each)."""
    if COLLECTION_COL not in scores_df.columns:
        return []
    return _usable_levels(scores_df[COLLECTION_COL])






def detect_nested_factors(scores_df: pd.DataFrame, factors: dict) -> set:
    """Factors constant within every collection (nested in the block).

    Such a factor is collinear with ``C(collection_id)`` — it cannot be
    blocked, and its comparison has only as many independent units as there
    are collections. Requires >= 2 collections to be meaningful.
    """
    if COLLECTION_COL not in scores_df.columns:
        return set()
    coll = scores_df[COLLECTION_COL].astype(str)
    if coll.nunique(dropna=True) < 2:
        return set()

    nested = set()
    for f in factors:
        per_coll = scores_df.groupby(coll)[f].nunique(dropna=True)
        if len(per_coll) and int(per_coll.max()) == 1:
            nested.add(f)
    return nested






def component_columns(scores_df: pd.DataFrame) -> list:
    """Numeric, non-``_raw`` columns with more than one distinct value."""
    numeric = scores_df.select_dtypes(include=['number'])
    return [c for c in numeric.columns
            if not str(c).endswith('_raw') and numeric[c].nunique(dropna=True) > 1]






def personalization_anova_sweep(scores_df: pd.DataFrame, components: list) -> list:
    """Between-collection variance decomposition, one row per component.

    One-way ANOVA on ``collection_id``; the reported eta² is the intraclass
    correlation ("how much of this variable's day-to-day variance is between
    collections" = personalization strength). Deliberately carries **no
    p/q values**: days within a collection are numerous and autocorrelated,
    so significance is guaranteed and meaningless — the effect size is the
    finding.
    """
    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    levels = collection_levels(scores_df)
    if len(levels) < 2:
        return []
    level_set = set(levels)
    cvals = scores_df[COLLECTION_COL].astype(str)

    results = []
    for comp in components:
        sub = pd.DataFrame({
            "y": pd.to_numeric(scores_df[comp], errors='coerce').astype('float64'),
            "g": cvals,
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
        except Exception as e:
            logger.warning(f"Personalization ANOVA failed for {comp}: {e}")
            continue

        results.append({
            "component": comp,
            "n": int(len(sub)),
            "levels": int(len(used_levels)),
            "F": f_stat if np.isfinite(f_stat) else None,
            "eta2": float(effect["eta2"]) if np.isfinite(effect["eta2"]) else None,
            "omega2": float(effect["omega2"]) if np.isfinite(effect["omega2"]) else None,
            "magnitude": eta2_magnitude(effect["eta2"]),
        })
    return results






def comparison_anova_sweep(scores_df: pd.DataFrame, factors: dict, components: list,
                           nested: set) -> list:
    """Blocked ANOVA + Kruskal–Wallis for every (comparison factor × component).

    Non-nested factors are tested with collection as a blocking term
    (``y ~ C(g) + C(collection_id)``, typ=2) and report **partial** eta²/ω²
    for the factor; their KW companion runs on within-collection-centered
    values. Nested factors (constant per collection) fall back to one-way on
    raw values, are flagged, and are excluded from both BH families. A study
    with fewer than 2 usable collections runs everything one-way with
    ``blocked: false`` (days within the single collection are the units).
    """
    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    has_blocks = len(collection_levels(scores_df)) >= 2
    cvals = (scores_df[COLLECTION_COL].astype(str)
             if COLLECTION_COL in scores_df.columns else None)

    results = []
    for factor, levels in factors.items():
        level_set = set(levels)
        fvals = scores_df[factor].astype(str)
        is_nested = factor in nested
        for comp in components:
            sub = pd.DataFrame({
                "y": pd.to_numeric(scores_df[comp], errors='coerce').astype('float64'),
                "g": fvals,
            })
            if cvals is not None:
                sub["c"] = cvals
            sub = sub[sub["g"].isin(level_set)].dropna()
            counts = sub["g"].value_counts()
            used_levels = counts[counts >= MIN_LEVEL_N].index.tolist()
            sub = sub[sub["g"].isin(used_levels)]
            if len(used_levels) < 2 or len(sub) - len(used_levels) < 2:
                continue
            if float(sub["y"].std()) == 0.0:
                continue

            blocked = (has_blocks and not is_nested and "c" in sub.columns
                       and sub["c"].nunique() >= 2)
            try:
                if blocked:
                    model = smf.ols('y ~ C(g) + C(c)', data=sub).fit()
                    anova_table = sm.stats.anova_lm(model, typ=2)
                    if float(anova_table.loc['Residual', 'df']) < 1:
                        logger.warning(
                            f"Blocked ANOVA saturated for {comp} ~ {factor}; skipping")
                        continue
                    effect = compute_partial_effect_sizes(
                        anova_table, 'C(g)', n_obs=len(sub))
                else:
                    model = smf.ols('y ~ C(g)', data=sub).fit()
                    anova_table = sm.stats.anova_lm(model, typ=2)
                    effect = compute_effect_sizes(anova_table, 'C(g)')
                f_stat = float(anova_table.loc['C(g)', 'F'])
                p_val = float(anova_table.loc['C(g)', 'PR(>F)'])
            except Exception as e:
                logger.warning(f"ANOVA failed for {comp} ~ {factor}: {e}")
                continue

            # KW companion: centered within collection for non-nested factors
            # (rank approximation of the blocked test); raw for nested ones —
            # centering would annihilate a collection-constant factor's signal.
            kw_centered = bool(blocked)
            kw_sub = sub
            if kw_centered:
                kw_sub = sub.copy()
                kw_sub["y"] = kw_sub["y"] - kw_sub.groupby("c")["y"].transform("mean")
            groups = [g["y"].to_numpy() for _, g in kw_sub.groupby("g")]
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
                "blocked": bool(blocked),
                "nested_in_collection": bool(is_nested),
                "F": f_stat if np.isfinite(f_stat) else None,
                "p": p_val if np.isfinite(p_val) else None,
                "eta2": float(effect["eta2"]) if np.isfinite(effect["eta2"]) else None,
                "omega2": float(effect["omega2"]) if np.isfinite(effect["omega2"]) else None,
                "magnitude": eta2_magnitude(effect["eta2"]),
                "kw_H": kw_h,
                "kw_p": kw_p,
                "kw_centered": kw_centered,
            })

    not_nested = lambda r: not r.get("nested_in_collection")  # noqa: E731
    _add_bh_q(results, "p", "q", eligible=not_nested)
    _add_bh_q(results, "kw_p", "kw_q", eligible=not_nested)
    return results






def family_permanova(scores_df: pd.DataFrame, factors: dict, families: dict,
                     permutations: int = 999, center: bool = False,
                     nested: set = frozenset()) -> list:
    """PERMANOVA of each variable family's component block against each factor.

    A family is the set of components sharing one per-variable PCA basis, so
    the multivariate distance never mixes bases. Families with fewer than 2
    columns are skipped (a univariate family is covered by the ANOVA sweep).

    With ``center=True`` (the within-feed comparison call) each family block
    is within-collection-centered before distances, so the test asks whether
    the factor separates day-profiles *inside* feeds. skbio offers no strata
    support, so permutation stays free — the p is anti-conservative under
    collection dependence (documented; the UI caveats it). Factors in
    ``nested`` are run on the RAW block (centering would erase their signal),
    flagged ``nested_in_collection`` and excluded from the BH family.

    Reports every result with its pseudo-F and permutation p; Benjamini–
    Hochberg q across the non-nested tests of this call.
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
        raw_block = scores_df[cols].apply(pd.to_numeric, errors='coerce').astype('float64')
        centered_block = raw_block
        block_centered = False
        if center:
            frame = scores_df[[COLLECTION_COL]].join(raw_block) \
                if COLLECTION_COL in scores_df.columns else raw_block
            frame, block_centered = center_within_collection(frame, cols)
            centered_block = frame[cols]
        for factor, levels in factors.items():
            is_nested = factor in nested
            # A nested factor's signal lives between collections — test raw.
            block = raw_block if is_nested else centered_block
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
                "centered": bool(block_centered and not is_nested),
                "nested_in_collection": bool(is_nested),
            })

    _add_bh_q(results, "p", "q",
              eligible=lambda r: not r.get("nested_in_collection"))
    return results






def _add_bh_q(results: list, p_key: str, q_key: str, eligible=None) -> None:
    """Attach Benjamini–Hochberg q-values across ``results`` in place.

    Rows failing ``eligible`` (e.g. nested-in-collection tests, whose p is
    pseudoreplicated) keep ``q_key=None`` and do not enter the BH family.
    """
    if eligible is None:
        eligible = lambda r: True  # noqa: E731
    idx = [i for i, r in enumerate(results)
           if r.get(p_key) is not None and eligible(r)]
    for r in results:
        r[q_key] = None
    if not idx:
        return
    pvals = [results[i][p_key] for i in idx]
    _, qvals, _, _ = multipletests(pvals, method='fdr_bh')
    for i, qv in zip(idx, qvals):
        results[i][q_key] = float(qv)






def compute_group_stats_artifact(scores_df: pd.DataFrame, study_name: str) -> dict:
    """Build the full ``{study}_corr_stats.json`` (version 2) payload."""
    permutations = int(_cf().get("correlations", {}).get("permanova_permutations", 999))

    components = component_columns(scores_df)
    factors = eligible_comparison_factors(scores_df)
    families = variable_families(components)
    nested = detect_nested_factors(scores_df, factors)
    coll_levels = collection_levels(scores_df)
    n_collections = (int(scores_df[COLLECTION_COL].nunique(dropna=True))
                     if COLLECTION_COL in scores_df.columns else 0)

    t0 = time.perf_counter()
    personalization = personalization_anova_sweep(scores_df, components)
    anova = comparison_anova_sweep(scores_df, factors, components, nested)
    t1 = time.perf_counter()
    perma_pers = []
    if len(coll_levels) >= 2:
        perma_pers = family_permanova(
            scores_df, {COLLECTION_COL: coll_levels}, families,
            permutations=permutations, center=False)
    perma = family_permanova(
        scores_df, factors, families,
        permutations=permutations, center=True, nested=nested)
    t2 = time.perf_counter()
    logger.info(
        f"[group-stats] {study_name}: {len(personalization)} personalization + "
        f"{len(anova)} ANOVA + {len(perma_pers) + len(perma)} PERMANOVA tests "
        f"({len(factors)} comparison factors, {len(nested)} nested, "
        f"{len(components)} components; "
        f"anova={t1 - t0:.1f}s permanova={t2 - t1:.1f}s)")

    return {
        "version": ARTIFACT_VERSION,
        "study": study_name,
        "generated_at": time.time(),
        "n_groups": int(len(scores_df)),
        "n_collections": n_collections,
        "comparison_factors": sorted(factors.keys()),
        "personalization": personalization,
        "anova": anova,
        "permanova_personalization": perma_pers,
        "permanova": perma,
        "config": {
            "permanova_permutations": permutations,
            "min_level_n": MIN_LEVEL_N,
            "max_factor_levels": MAX_FACTOR_LEVELS,
        },
    }
