"""Group-comparison statistics helpers.

Currently only :func:`compute_effect_sizes` lives here. The former
``run_anova`` / ``run_permanova`` / ``run_many_permanova`` prototypes were
never wired into the platform and were removed in 2026-07; the Correlations
roadmap (Phase 3) rebuilds ANOVA/Kruskal–Wallis and PERMANOVA sweeps on top
of this module with honest, complete reporting.
"""

from fyp.logging_setup import get_logger

logger = get_logger(__name__)




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
