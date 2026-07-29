"""Group-differences statistics (fyp/analysis/stats.py) + reliability harvest.

Golden-value checks against scipy for the ANOVA/Kruskal–Wallis sweep, sanity
checks for PERMANOVA, and the reliability-artifact selection logic with
fabricated ab_eval / human_eval data.
"""

import numpy as np
import pandas as pd
import pytest

from fyp.analysis import stats as fstats






def _synthetic_scores(n_per_level=20, seed=5):
    """Two factors (one strong, one null) over two component families."""
    rng = np.random.RandomState(seed)
    levels = ["red", "green", "blue"]
    rows = []
    for li, level in enumerate(levels):
        for _ in range(n_per_level):
            rows.append({
                "grp_factor": level,
                "null_factor": rng.choice(["x", "y"]),
                # famA_C0 shifts strongly with grp_factor; famA_C1 is noise
                "famA_C0": li * 2.0 + rng.normal(),
                "famA_C1": rng.normal(),
                "famB_C0": rng.normal(),
                "famB_C1": rng.normal(),
            })
    return pd.DataFrame(rows)






def test_anova_sweep_matches_scipy():
    from scipy import stats as sps

    df = _synthetic_scores()
    factors = {"grp_factor": ["red", "green", "blue"]}
    results = fstats.component_anova_sweep(df, factors, ["famA_C0", "famA_C1"])
    by_comp = {r["component"]: r for r in results}

    # F and p match scipy.f_oneway for the strong component
    groups = [g["famA_C0"].to_numpy() for _, g in df.groupby("grp_factor")]
    ref = sps.f_oneway(*groups)
    strong = by_comp["famA_C0"]
    assert strong["F"] == pytest.approx(float(ref.statistic), rel=1e-9)
    assert strong["p"] == pytest.approx(float(ref.pvalue), abs=1e-12)
    assert strong["magnitude"] == "large"
    assert strong["n"] == 60 and strong["levels"] == 3

    # Kruskal–Wallis companion matches scipy.kruskal
    kw_ref = sps.kruskal(*groups)
    assert strong["kw_H"] == pytest.approx(float(kw_ref.statistic), rel=1e-9)
    assert strong["kw_p"] == pytest.approx(float(kw_ref.pvalue), abs=1e-12)

    # Eta-squared equals SS_between / SS_total for one-way ANOVA
    y = df["famA_C0"].to_numpy()
    grand = y.mean()
    ss_total = ((y - grand) ** 2).sum()
    ss_between = sum(len(g) * (g.mean() - grand) ** 2 for g in groups)
    assert strong["eta2"] == pytest.approx(ss_between / ss_total, rel=1e-9)

    # BH q present and ordered sensibly (strong effect ranks first)
    assert strong["q"] is not None and strong["q"] <= by_comp["famA_C1"]["q"]






def test_permanova_detects_strong_family_only():
    df = _synthetic_scores()
    factors = {"grp_factor": ["red", "green", "blue"]}
    families = {"famA_C0": "famA", "famA_C1": "famA", "famB_C0": "famB", "famB_C1": "famB"}

    results = fstats.family_permanova(df, factors, families, permutations=199)
    by_family = {r["family"]: r for r in results}
    assert set(by_family) == {"famA", "famB"}
    for r in results:
        assert r["permutations"] == 199
        assert r["n"] == 60 and r["levels"] == 3 and r["n_components"] == 2
        assert 0 <= r["p"] <= 1 and r["q"] is not None
    # The family containing the shifted component separates clearly
    assert by_family["famA"]["p"] < 0.05
    assert by_family["famA"]["pseudo_F"] > by_family["famB"]["pseudo_F"]






def test_eligible_factors_guards(monkeypatch):
    df = pd.DataFrame({
        "good": ["a"] * 10 + ["b"] * 10,
        "too_small_levels": ["a"] * 18 + ["b", "b"],   # level b has n < 3
        "near_unique": [str(i) for i in range(20)],
        "session_id": ["s"] * 10 + ["t"] * 10,
        "comp": np.arange(20.0),
    })
    monkeypatch.setattr(fstats, "get_factors_and_features_from_var_schema",
                        lambda **kw: (["good", "too_small_levels", "near_unique", "session_id"], []))
    out = fstats.eligible_factors(df)
    assert "good" in out and out["good"] == ["a", "b"]
    assert "too_small_levels" not in out
    assert "near_unique" not in out
    assert "session_id" not in out






def test_compute_group_stats_artifact_shape(monkeypatch):
    df = _synthetic_scores(n_per_level=10)
    monkeypatch.setattr(fstats, "get_factors_and_features_from_var_schema",
                        lambda **kw: (["grp_factor", "null_factor"], []))
    monkeypatch.setattr(fstats, "variable_families",
                        lambda cols: {c: c.split("_")[0] for c in cols})
    monkeypatch.setattr(fstats, "_cf", lambda: {"correlations": {"permanova_permutations": 99}})

    payload = fstats.compute_group_stats_artifact(df, "mystudy")
    assert payload["version"] == fstats.ARTIFACT_VERSION
    assert payload["study"] == "mystudy"
    assert payload["n_groups"] == 30
    assert payload["config"]["permanova_permutations"] == 99
    assert payload["anova"] and payload["permanova"]
    # JSON-safe: no NaN anywhere
    import json
    json.dumps(payload, allow_nan=False)






def test_eta2_magnitude_labels():
    assert fstats.eta2_magnitude(0.005) == "negligible"
    assert fstats.eta2_magnitude(0.03) == "small"
    assert fstats.eta2_magnitude(0.1) == "medium"
    assert fstats.eta2_magnitude(0.3) == "large"
    assert fstats.eta2_magnitude(None) is None






# --- Reliability harvesting -------------------------------------------------


def _fake_ab_runs(monkeypatch, arms, comparisons):
    import fyp.annotation.ab_eval as ab

    monkeypatch.setattr(ab, "load_runs_index",
                        lambda: [{"run_id": "r1", "status": "complete"}])
    monkeypatch.setattr(ab, "load_run", lambda run_id: {
        "manifest": {"arms": arms},
        "report": {"comparisons": comparisons},
    })






def test_reliability_test_retest_selection(monkeypatch):
    from fyp.analysis import reliability as rel

    # Two arms, same contract + backend + overrides -> pure repeat pair
    arms = [
        {"name": "live", "etag": "live:abc", "backend": "gemini", "gen_overrides": {}},
        {"name": "live~2", "etag": "live:abc", "backend": "gemini", "gen_overrides": {}},
    ]
    comparisons = {"live|live~2": {"columns": {
        "sensitivity_score": {"kind": "numeric", "correlation": 0.82,
                              "n_compared": 40, "caveat": None},
        "advertising": {"kind": "enum", "agreement_filled": 0.9,
                        "n_filled_both": 40, "caveat": None},
        "too_few": {"kind": "numeric", "correlation": 0.9,
                    "n_compared": 4, "caveat": None},
        "flagged": {"kind": "numeric", "correlation": 0.9,
                    "n_compared": 40, "caveat": "low_variance"},
    }}}
    _fake_ab_runs(monkeypatch, arms, comparisons)
    monkeypatch.setattr(rel, "_harvest_human_eval_icr", lambda: {})

    out = rel.estimate_annotation_reliability()
    vars_ = out["variables"]
    assert vars_["sensitivity_score"]["reliability"] == pytest.approx(0.82)
    assert vars_["sensitivity_score"]["source"] == "machine test-retest"
    assert vars_["advertising"]["reliability"] == pytest.approx(0.9)
    assert "too_few" not in vars_ and "flagged" not in vars_






def test_reliability_ignores_cross_backend_pairs(monkeypatch):
    from fyp.analysis import reliability as rel

    arms = [
        {"name": "live", "etag": "live:abc", "backend": "gemini", "gen_overrides": {}},
        {"name": "qwen", "etag": "live:abc", "backend": "qwen_api", "gen_overrides": {}},
    ]
    comparisons = {"live|qwen": {"columns": {
        "sensitivity_score": {"kind": "numeric", "correlation": 0.7,
                              "n_compared": 40, "caveat": None},
    }}}
    _fake_ab_runs(monkeypatch, arms, comparisons)
    monkeypatch.setattr(rel, "_harvest_human_eval_icr", lambda: {})

    out = rel.estimate_annotation_reliability()
    assert out["variables"] == {}






def test_reliability_retest_wins_over_human(monkeypatch):
    from fyp.analysis import reliability as rel

    monkeypatch.setattr(rel, "_harvest_ab_eval_test_retest", lambda: {
        "advertising": {"reliability": 0.9, "n": 40, "kind": "enum",
                        "source": "machine test-retest", "detail": "d1"},
    })
    monkeypatch.setattr(rel, "_harvest_human_eval_icr", lambda: {
        "advertising": {"reliability": 0.6, "n": 30, "kind": "enum",
                        "source": "human–machine agreement", "detail": "d2"},
        "aigc": {"reliability": 0.7, "n": 30, "kind": "enum",
                 "source": "human–machine agreement", "detail": "d2"},
    })

    vars_ = rel.estimate_annotation_reliability()["variables"]
    assert vars_["advertising"]["source"] == "machine test-retest"
    assert vars_["aigc"]["source"] == "human–machine agreement"






def test_spearman_brown_and_column_reliability(monkeypatch):
    from web_interface.services import correlations_service as cs

    assert cs._spearman_brown(0.5, 10) == pytest.approx(10 * 0.5 / (1 + 9 * 0.5))
    assert cs._spearman_brown(0.0, 10) == 0.0

    monkeypatch.setattr(cs, "load_reliability_map", lambda: {
        "advertising": {"reliability": 0.5, "n": 40, "source": "machine test-retest"},
    })
    monkeypatch.setattr(cs, "fyp_cf", {"correlations": {"minimum_group_size": 10}})
    out = cs.column_reliability(
        ["advertising_C0", "unmapped_col"],
        {"advertising_C0": "advertising"})
    assert set(out) == {"advertising_C0"}
    assert out["advertising_C0"]["group_r"] == pytest.approx(
        round(10 * 0.5 / (1 + 9 * 0.5), 4))
    assert out["advertising_C0"]["item_r"] == 0.5
    assert out["advertising_C0"]["source"] == "machine test-retest"
