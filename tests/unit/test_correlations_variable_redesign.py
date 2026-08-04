"""Correlations variable-set redesign: derived behavioral columns + transforms.

The merge derives ``engaged`` (own-engagement rate input), ``rewatched`` (the
loop signal completion_rate's clip discards) and ``is_weekend``; heavy-tailed
numeric features declare ``transform = "log1p"`` in their contracts, applied
before the PCA group mean; and the per-group video count is the declared
``videos_watched`` variable rather than the quarantined ``group_size``.
"""

import pandas as pd
import pytest

import fyp.analysis.organize_datasets as od
from fyp.analysis.pca import VIDEOS_WATCHED_COL, contract_numeric_transforms
from web_interface.services import correlations_service as svc


@pytest.fixture
def no_failed_scrapes(monkeypatch):
    """Keep _add_merge_calculated_columns storage-free on a fresh checkout."""
    monkeypatch.setattr(od, "load_failed_scrapes", lambda verbose=False: [])






def _frame(**overrides):
    base = {
        "item_id": ["a", "b", "c", "d"],
        "extra_data": ["fave", None, "comment:hi,share", ""],
        "play_duration": pd.array([30, 90, None, 10], dtype="int64[pyarrow]"),
        "duration": pd.array([60, 60, 60, None], dtype="int64[pyarrow]"),
        "local_weekday": pd.array(["saturday", "monday", None, "sunday"],
                                  dtype="string[pyarrow]"),
    }
    base.update(overrides)
    return pd.DataFrame(base)






def test_engaged_flags_rows_with_engagement_tokens(no_failed_scrapes):
    out = od._add_merge_calculated_columns(_frame())
    assert out["engaged"].tolist() == [1.0, 0.0, 1.0, 0.0]






def test_rewatched_true_only_when_play_exceeds_duration(no_failed_scrapes):
    out = od._add_merge_calculated_columns(_frame())
    # 30<60 → 0; 90>60 → 1; NA play or NA duration → NA
    assert out["rewatched"].tolist()[:2] == [0.0, 1.0]
    assert pd.isna(out["rewatched"].iloc[2])
    assert pd.isna(out["rewatched"].iloc[3])






def test_is_weekend_two_level_factor_with_na_passthrough(no_failed_scrapes):
    out = od._add_merge_calculated_columns(_frame())
    assert out["is_weekend"].tolist()[:2] == ["weekend", "weekday"]
    assert pd.isna(out["is_weekend"].iloc[2])
    assert out["is_weekend"].iloc[3] == "weekend"






def test_missing_inputs_yield_na_defaults_not_errors(no_failed_scrapes):
    bare = pd.DataFrame({"item_id": ["a", "b"]})
    out = od._add_merge_calculated_columns(bare)
    for col in ("engaged", "rewatched", "is_weekend", "completion_rate"):
        assert col in out.columns
        assert out[col].isna().all()






def test_new_columns_registered_for_enrichment_patch():
    assert {"engaged", "rewatched", "is_weekend"} <= od._CALCULATED_ENRICHMENT_COLUMNS






def test_contract_numeric_transforms_covers_heavy_tailed_features():
    transforms = contract_numeric_transforms()
    assert transforms["play_count"] == "log1p"
    assert transforms["plays_per_day"] == "log1p"
    assert transforms["days_since_created"] == "log1p"






def test_contract_validators_reject_unknown_transform():
    from fyp import derived_contract as dc

    contract = dc.load_contract()
    contract["fields"][0]["transform"] = "sqrt"
    errors = dc.validate_contract(contract)
    assert any("invalid transform" in e for e in errors)






def test_promoted_roles_reach_var_schema():
    from fyp.fyp_config import fyp_cf

    vs = fyp_cf["var_schema"].set_index("variable_name")
    assert vs.at["political_score", "role"] == "feature"
    assert vs.at["sensitivity_score", "role"] == "feature"
    assert vs.at["duration", "role"] == "feature"
    assert vs.at["plays_per_day", "role"] == "feature"
    assert vs.at["days_since_created", "role"] == "feature"
    assert vs.at["engaged", "role"] == "feature"
    assert vs.at["rewatched", "role"] == "feature"
    assert vs.at["is_weekend", "role"] == "factor"
    # local_hour is circular and local_day_segment varies within a
    # collection-day group — both deliberately role-free; videos_watched is
    # group-level (offered by the tab from the PCA frame, no row role).
    assert pd.isna(vs.at["local_hour", "role"])
    assert pd.isna(vs.at["local_day_segment", "role"])
    assert pd.isna(vs.at[VIDEOS_WATCHED_COL, "role"])






def test_sample_summary_prefers_videos_watched_over_legacy_group_size():
    df = pd.DataFrame({
        "collection_id": ["c1", "c1", "c2"],
        svc.VIDEOS_WATCHED_COL: [10, 20, 30],
        svc.GROUP_SIZE_COL: [1, 1, 1],
    })
    summary = svc.build_sample_summary(df, df.iloc[:2])
    assert summary["videos_total"] == 60
    assert summary["videos_selected"] == 30

    legacy = df.drop(columns=[svc.VIDEOS_WATCHED_COL])
    summary = svc.build_sample_summary(legacy, legacy)
    assert summary["videos_total"] == 3
