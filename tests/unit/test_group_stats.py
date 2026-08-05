"""Unit tests for fyp/analysis/stats.py (Group-differences artifact v2).

Covers the personalization / within-feed split:

  * personalization rows = one-way variance decomposition on collection_id
    (eta² = ICC), carrying NO p/q;
  * comparison rows = ANOVA blocked on collection with PARTIAL effect sizes,
    verified against a direct statsmodels fit and the committed partial-ω²
    formula;
  * blocking recovers a within-collection effect that collection offsets
    would otherwise drown;
  * nested factors (constant per collection) are detected, run one-way on raw
    values, flagged, and excluded from the BH families;
  * single-collection studies fall back to one-way with ``blocked: false``;
  * the KW companion runs on centered values for non-nested factors and raw
    values for nested ones (verified against scipy on manually prepared y);
  * PERMANOVA runs uncentered for personalization and centered for
    comparisons, with nested rows raw + q-less;
  * the artifact has the version-2 shape and is JSON-safe (no NaN);
  * ``center_within_collection`` zeroes every collection's mean.

Synthetic data only — no config, no files, no network.

Usage:
    pytest tests/unit/test_group_stats.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
import pytest
from scipy import stats as scipy_stats

from fyp.analysis import stats as fstats


N_COLLECTIONS = 6
N_PER_COLLECTION = 30






def _synthetic_scores(seed: int = 5) -> pd.DataFrame:
    """Six collections × 30 days with known structure.

    * ``famA_C0``: big per-collection offsets (personalization) + a genuine
      within-collection weekend effect (+1.0 on level "wknd").
    * ``famA_C1`` / ``famB_C1``: pure noise.
    * ``famB_C0``: differs only between platform groups (nested factor).
    * ``wkend_factor``: varies within every collection (blockable).
    * ``platform_factor``: constant per collection (nested).
    * ``null_factor``: 3 random levels, no effect anywhere.
    """
    rng = np.random.RandomState(seed)
    rows = []
    for ci in range(N_COLLECTIONS):
        coll = f"coll_{ci}"
        platform = "tiktok" if ci < 3 else "insta"
        coll_offset = ci * 3.0
        platform_offset = 0.0 if ci < 3 else 2.0
        for di in range(N_PER_COLLECTION):
            wkend = "wknd" if di % 3 == 0 else "wkday"
            rows.append({
                "collection_id": coll,
                "wkend_factor": wkend,
                "platform_factor": platform,
                "null_factor": ["red", "green", "blue"][rng.randint(3)],
                "famA_C0": coll_offset + (1.0 if wkend == "wknd" else 0.0)
                           + rng.normal(0, 0.5),
                "famA_C1": rng.normal(0, 1.0),
                "famB_C0": platform_offset + rng.normal(0, 0.5),
                "famB_C1": rng.normal(0, 1.0),
            })
    return pd.DataFrame(rows)






def _patch_roles(monkeypatch, comparisons=("wkend_factor", "platform_factor", "null_factor")):
    """Route the role getter to the synthetic frame's comparison factors."""
    def fake_get_vars_by_role(roles, some_events_df=None, verbose=False):
        cols = list(comparisons) if "comparison" in roles else []
        if some_events_df is not None:
            cols = [c for c in cols if c in some_events_df.columns]
        return sorted(cols)
    monkeypatch.setattr(fstats, "get_vars_by_role", fake_get_vars_by_role)






def test_personalization_rows_are_icc_without_p():
    df = _synthetic_scores()
    components = fstats.component_columns(df)
    rows = fstats.personalization_anova_sweep(df, components)
    by_comp = {r["component"]: r for r in rows}

    assert set(by_comp) == set(components)
    for r in rows:
        assert "p" not in r and "q" not in r, "personalization rows must carry no p/q"
        assert r["levels"] == N_COLLECTIONS
        assert r["n"] == len(df)

    # eta² == between-collection SS / total SS, hand-computed.
    y = df["famA_C0"].to_numpy()
    grand = y.mean()
    ss_total = ((y - grand) ** 2).sum()
    ss_between = sum(
        len(g) * (g["famA_C0"].mean() - grand) ** 2
        for _, g in df.groupby("collection_id")
    )
    assert by_comp["famA_C0"]["eta2"] == pytest.approx(ss_between / ss_total, rel=1e-9)
    # Collection offsets dominate famA_C0 → large ICC; noise column → tiny.
    assert by_comp["famA_C0"]["eta2"] > 0.9
    assert by_comp["famA_C1"]["eta2"] < 0.1
    assert by_comp["famA_C0"]["magnitude"] == "large"






def test_blocked_partial_effect_sizes_match_statsmodels(monkeypatch):
    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    _patch_roles(monkeypatch)
    df = _synthetic_scores()
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)
    rows = fstats.comparison_anova_sweep(df, factors, ["famA_C0"], nested)
    row = next(r for r in rows
               if r["factor"] == "wkend_factor" and r["component"] == "famA_C0")
    assert row["blocked"] is True and row["nested_in_collection"] is False

    # Direct fit of the same model + the committed partial-ω² formula.
    sub = pd.DataFrame({
        "y": df["famA_C0"].astype("float64"),
        "g": df["wkend_factor"].astype(str),
        "c": df["collection_id"].astype(str),
    })
    table = sm.stats.anova_lm(smf.ols("y ~ C(g) + C(c)", data=sub).fit(), typ=2)
    ss_g = table.loc["C(g)", "sum_sq"]
    df_g = table.loc["C(g)", "df"]
    ss_res = table.loc["Residual", "sum_sq"]
    ms_err = ss_res / table.loc["Residual", "df"]
    n_obs = len(sub)

    assert row["F"] == pytest.approx(float(table.loc["C(g)", "F"]), rel=1e-9)
    assert row["p"] == pytest.approx(float(table.loc["C(g)", "PR(>F)"]), abs=1e-12)
    assert row["eta2"] == pytest.approx(float(ss_g / (ss_g + ss_res)), rel=1e-9)
    expected_omega2 = float((ss_g - df_g * ms_err) / (ss_g + (n_obs - df_g) * ms_err))
    assert row["omega2"] == pytest.approx(expected_omega2, rel=1e-9)






def test_blocking_recovers_within_collection_effect(monkeypatch):
    """Collection offsets drown the weekend effect one-way; blocking finds it."""
    import statsmodels.api as sm
    import statsmodels.formula.api as smf

    _patch_roles(monkeypatch)
    df = _synthetic_scores()
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)
    rows = fstats.comparison_anova_sweep(df, factors, ["famA_C0"], nested)
    blocked_row = next(r for r in rows if r["factor"] == "wkend_factor")

    # The naive one-way on the same pair (what artifact v1 computed).
    sub = pd.DataFrame({
        "y": df["famA_C0"].astype("float64"),
        "g": df["wkend_factor"].astype(str),
    })
    naive = sm.stats.anova_lm(smf.ols("y ~ C(g)", data=sub).fit(), typ=2)
    naive_eta2 = float(naive.loc["C(g)", "sum_sq"]
                       / (naive.loc["C(g)", "sum_sq"] + naive.loc["Residual", "sum_sq"]))

    # Partial eta² (collection variance out of the denominator) must be much
    # larger than the naive one-way eta², and clearly detect the +1.0 effect.
    assert blocked_row["eta2"] > 5 * naive_eta2
    assert blocked_row["eta2"] > 0.3
    assert blocked_row["p"] < 1e-6






def test_nested_factor_flagged_and_out_of_bh_family(monkeypatch):
    _patch_roles(monkeypatch)
    df = _synthetic_scores()
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)
    assert nested == {"platform_factor"}

    components = ["famA_C0", "famB_C0"]
    rows = fstats.comparison_anova_sweep(df, factors, components, nested)
    nested_rows = [r for r in rows if r["factor"] == "platform_factor"]
    other_rows = [r for r in rows if r["factor"] != "platform_factor"]
    assert nested_rows and other_rows

    for r in nested_rows:
        assert r["nested_in_collection"] is True
        assert r["blocked"] is False
        assert r["q"] is None and r["kw_q"] is None
        assert r["kw_centered"] is False
        assert r["p"] is not None  # descriptive p is still reported

    # BH family = the non-nested rows only.
    from statsmodels.stats.multitest import multipletests
    pvals = [r["p"] for r in other_rows if r["p"] is not None]
    _, expected_q, _, _ = multipletests(pvals, method="fdr_bh")
    got_q = [r["q"] for r in other_rows if r["p"] is not None]
    assert got_q == pytest.approx(list(expected_q), rel=1e-9)






def test_single_collection_falls_back_to_oneway(monkeypatch):
    _patch_roles(monkeypatch, comparisons=("wkend_factor", "null_factor"))
    df = _synthetic_scores()
    df = df[df["collection_id"] == "coll_0"].reset_index(drop=True)
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)
    assert nested == set()

    rows = fstats.comparison_anova_sweep(df, factors, ["famA_C0"], nested)
    row = next(r for r in rows if r["factor"] == "wkend_factor")
    assert row["blocked"] is False
    assert row["nested_in_collection"] is False
    assert row["p"] is not None and row["q"] is not None






def test_kw_centered_for_blocked_raw_for_nested(monkeypatch):
    _patch_roles(monkeypatch)
    df = _synthetic_scores()
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)
    rows = fstats.comparison_anova_sweep(df, factors, ["famA_C0"], nested)

    # Non-nested: KW on within-collection-centered y.
    row = next(r for r in rows if r["factor"] == "wkend_factor")
    assert row["kw_centered"] is True
    y = df["famA_C0"].astype("float64")
    centered = y - y.groupby(df["collection_id"].astype(str).values).transform("mean")
    groups = [centered[df["wkend_factor"] == lv].to_numpy()
              for lv in ("wknd", "wkday")]
    kw = scipy_stats.kruskal(*groups)
    assert row["kw_H"] == pytest.approx(float(kw.statistic), rel=1e-9)
    assert row["kw_p"] == pytest.approx(float(kw.pvalue), abs=1e-12)

    # Nested: KW on raw y.
    nrow = next(r for r in rows if r["factor"] == "platform_factor")
    groups_raw = [y[df["platform_factor"] == lv].to_numpy()
                  for lv in ("tiktok", "insta")]
    kw_raw = scipy_stats.kruskal(*groups_raw)
    assert nrow["kw_H"] == pytest.approx(float(kw_raw.statistic), rel=1e-9)






def test_permanova_personalization_and_centered_comparison(monkeypatch):
    _patch_roles(monkeypatch)
    df = _synthetic_scores()
    families = {"famA_C0": "famA", "famA_C1": "famA",
                "famB_C0": "famB", "famB_C1": "famB"}
    factors = fstats.eligible_comparison_factors(df)
    nested = fstats.detect_nested_factors(df, factors)

    pers = fstats.family_permanova(
        df, {"collection_id": fstats.collection_levels(df)}, families,
        permutations=199, center=False)
    assert pers, "personalization PERMANOVA produced no rows"
    for r in pers:
        assert r["centered"] is False and r["nested_in_collection"] is False
        assert r["permutations"] == 199
    fam_a = next(r for r in pers if r["family"] == "famA")
    assert fam_a["p"] is not None and fam_a["p"] < 0.05  # huge offsets

    comp = fstats.family_permanova(
        df, factors, families, permutations=199, center=True, nested=nested)
    by_key = {(r["family"], r["factor"]): r for r in comp}
    # Non-nested rows are centered and carry q; nested rows raw and q-less.
    wk = by_key[("famA", "wkend_factor")]
    assert wk["centered"] is True and wk["q"] is not None
    pf = by_key[("famB", "platform_factor")]
    assert pf["centered"] is False and pf["nested_in_collection"] is True
    assert pf["q"] is None
    # famB's platform signal is between-collection: the raw nested test sees
    # a strong separation.
    assert pf["p"] is not None and pf["p"] < 0.05






def test_artifact_v2_shape_and_json_safety(monkeypatch):
    _patch_roles(monkeypatch)
    monkeypatch.setattr(fstats, "_cf",
                        lambda: {"correlations": {"permanova_permutations": 99}})
    # The stubbed _cf has no var_schema, so longest-prefix family mapping
    # degenerates — supply the families directly.
    monkeypatch.setattr(
        fstats, "variable_families",
        lambda cols: {c: str(c).rsplit("_", 1)[0] for c in cols})
    df = _synthetic_scores()
    payload = fstats.compute_group_stats_artifact(df, "unit_test_study")

    assert payload["version"] == 2
    assert payload["study"] == "unit_test_study"
    assert payload["n_groups"] == len(df)
    assert payload["n_collections"] == N_COLLECTIONS
    assert payload["comparison_factors"] == ["null_factor", "platform_factor",
                                             "wkend_factor"]
    for key in ("personalization", "anova", "permanova_personalization",
                "permanova"):
        assert isinstance(payload[key], list) and payload[key], f"{key} empty"
    assert payload["config"]["permanova_permutations"] == 99
    # NaN-safety contract: the artifact must serialize under allow_nan=False.
    json.dumps(payload, allow_nan=False)






def test_center_within_collection_zeroes_group_means():
    df = _synthetic_scores()
    out, applied = fstats.center_within_collection(df, ["famA_C0", "famB_C0"])
    assert applied is True
    means = out.groupby("collection_id")[["famA_C0", "famB_C0"]].mean()
    assert np.allclose(means.to_numpy(), 0.0, atol=1e-12)
    # No collection column → honest no-op.
    plain = df.drop(columns=["collection_id"])
    same, applied2 = fstats.center_within_collection(plain, ["famA_C0"])
    assert applied2 is False and same is plain






def test_eligible_comparison_factors_guards(monkeypatch):
    _patch_roles(monkeypatch,
                 comparisons=("wkend_factor", "tiny_factor", "unique_factor"))
    df = _synthetic_scores()
    df["tiny_factor"] = "a"
    df.loc[df.index[:2], "tiny_factor"] = "b"      # level under MIN_LEVEL_N
    df["unique_factor"] = [f"u{i}" for i in range(len(df))]  # near-unique

    out = fstats.eligible_comparison_factors(df)
    assert "wkend_factor" in out
    assert "tiny_factor" not in out
    assert "unique_factor" not in out
