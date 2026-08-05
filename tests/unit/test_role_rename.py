"""Guards for the 2026-08 role-vocabulary rename.

The contract role values were renamed (group_factor→grouping, factor→comparison,
feature→measure; new: descriptor) with legacy strings normalized at var_schema
load. These tests pin:

  * the alias map is total over the old vocabulary and lands inside the new one;
  * ``load_var_schema`` yields only new role values even when a legacy registry
    snapshot injects an old string;
  * the contract validators keep accepting legacy role strings (older uploaded
    runtime annotation contracts must not start failing validation);
  * ``get_vars_by_role`` filters by normalized role;
  * the two legacy getters reproduce their pre-rename selections on a fixture
    schema written in the OLD vocabulary (alias path) and the new one alike.

Usage:
    pytest tests/unit/test_role_rename.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.recode_variables as rv






def test_alias_map_total_and_disjoint() -> None:
    """Every old value maps to a current one; no alias collides with a role."""
    assert set(rv.LEGACY_ROLE_ALIASES) == {"group_factor", "factor", "feature"}
    assert set(rv.LEGACY_ROLE_ALIASES.values()) <= set(rv.VAR_SCHEMA_ROLES)
    assert not set(rv.LEGACY_ROLE_ALIASES) & set(rv.VAR_SCHEMA_ROLES)
    assert "skip" in rv.VAR_SCHEMA_ROLES  # unchanged value
    assert rv.normalize_role("factor") == "comparison"
    assert rv.normalize_role("group_factor") == "grouping"
    assert rv.normalize_role("feature") == "measure"
    assert rv.normalize_role("measure") == "measure"  # identity on new values
    assert rv.normalize_role("skip") == "skip"






def test_live_var_schema_has_only_new_roles() -> None:
    """Post-load normalization leaves no legacy role string in the live schema."""
    from fyp.fyp_config import fyp_cf

    roles = set(str(r) for r in fyp_cf["var_schema"]["role"].dropna().unique())
    legacy_seen = roles & set(rv.LEGACY_ROLE_ALIASES)
    assert not legacy_seen, (
        f"load_var_schema left legacy role value(s) {legacy_seen} un-normalized"
    )
    assert roles <= set(rv.VAR_SCHEMA_ROLES) | {""}






def test_validators_accept_legacy_roles() -> None:
    """A contract carrying a pre-rename role string must still validate."""
    import fyp.annotation_contract as ac

    contract = ac.load_contract()
    # Force one field to the OLD vocabulary, as an old uploaded contract would.
    for field in contract["fields"]:
        if field.get("role") == "measure":
            field["role"] = "feature"
            break
    else:
        raise AssertionError("no role=measure field found to legacy-mutate")
    errors = ac.validate_contract(contract)
    role_errors = [e for e in errors if "invalid role" in e]
    assert not role_errors, role_errors






def test_get_vars_by_role_filters_and_normalizes(monkeypatch) -> None:
    schema = pd.DataFrame(
        {
            "variable_name": ["cid", "date", "week", "wkd", "score", "junk"],
            # Mixed old/new vocabulary: the getter must normalize both.
            "role": ["group_factor", "grouping", "descriptor", "factor", "feature", "skip"],
        }
    )
    monkeypatch.setattr(rv, "_cf", lambda: {"var_schema": schema})

    assert rv.get_vars_by_role(("grouping",)) == ["cid", "date"]
    assert rv.get_vars_by_role(("comparison",)) == ["wkd"]
    assert rv.get_vars_by_role(("descriptor",)) == ["week"]
    assert rv.get_vars_by_role(("measure",)) == ["score"]
    df = pd.DataFrame(columns=["cid", "score"])
    assert rv.get_vars_by_role(("grouping", "measure"), some_events_df=df) == ["cid", "score"]






def test_legacy_getters_reproduce_pre_rename_selections(monkeypatch) -> None:
    """The wrappers select exactly what the pre-rename implementations did."""
    schema = pd.DataFrame(
        {
            "variable_name": ["cid", "date", "platform", "week", "score", "raws"],
            "role": ["group_factor", "group_factor", "factor", "factor", "feature", "skip"],
        }
    )
    monkeypatch.setattr(rv, "_cf", lambda: {"var_schema": schema})

    factors, features = rv.get_factors_and_features_from_var_schema()
    # Pre-rename: factors = role in {factor, group_factor}; features = feature.
    assert factors == ["cid", "date", "platform", "week"]
    assert features == ["score"]
    assert rv.get_grouping_factors_from_var_schema() == ["cid", "date"]

    # Empty-schema guard keeps its historical return shapes.
    monkeypatch.setattr(rv, "_cf", lambda: {})
    assert rv.get_factors_and_features_from_var_schema() == ([], [])
    assert rv.get_grouping_factors_from_var_schema() == []
