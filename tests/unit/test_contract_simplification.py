"""Guards for the 2026-07 annotation-contract simplification.

The contract went sectionless (flat prompt bullet list), ``scale`` became
inferred-with-override, and the per-field web-UI ``description`` key was
retired (tooltips fall back to ``desc``). These tests pin the three safety
properties of that migration:

  * legacy sectioned contracts (stored ab_eval candidates, registered
    versions) keep rendering byte-identical prompts — their av_ hashes must
    never drift;
  * the migrated baked contract carries identical role/scale/display_name
    metadata (no var_schema-hash churn, no study-cache invalidation);
  * validation makes the one non-inferable choice (free-text categorical vs
    text) explicit and rejects stale section keys.
"""

import json
import tomllib
from pathlib import Path

import pytest

from fyp.annotation import annotation_contract as ac
from fyp.annotation import annotation_schema as sch

FIXTURES = Path(__file__).parent / "fixtures"
SECTIONED_TOML = FIXTURES / "annotation_contract_sectioned_v1.toml"
SECTIONED_PROMPT = FIXTURES / "annotation_prompt_sectioned_v1.txt"
METADATA_V1 = FIXTURES / "annotation_contract_column_metadata_v1.json"




def _sectioned_contract() -> dict:
    return ac.load_contract(SECTIONED_TOML)




def _baked_contract() -> dict:
    return ac.load_contract(ac.default_contract_path())




def test_sectioned_legacy_prompt_byte_identical():
    """A [[section]] contract renders exactly the pre-migration prompt text."""
    prompt = sch.build_prompt(_sectioned_contract())
    assert prompt == SECTIONED_PROMPT.read_text(encoding="utf-8")




def test_sectionless_prompt_is_flat():
    contract = _baked_contract()
    prompt = sch.build_prompt(contract)
    lines = prompt.split("\n")
    assert lines[0] == contract["prompt"]["header"]
    assert lines[-1] == contract["prompt"]["footer"]
    # No numbered section headings; one unindented bullet per field.
    assert "**" not in "\n".join(lines[1:-1])
    bullets = [ln for ln in lines if ln.startswith("• '")]
    assert len(bullets) == len(contract["fields"])




def test_migration_only_changed_the_prompt():
    """The section removal must not have touched the response schema."""
    assert (
        sch.get_annotation_json_schema(_sectioned_contract())
        == sch.get_annotation_json_schema(_baked_contract())
    )
    assert sch.build_prompt(_sectioned_contract()) != sch.build_prompt(_baked_contract())




@pytest.mark.parametrize(
    "field, expected",
    [
        ({"type": "int", "min": 0, "max": 100}, "numeric"),
        ({"array": True}, "list"),
        ({"array": 2, "enum": "content_category"}, "list"),
        ({"enum": "yes_no"}, "categorical"),
        ({}, None),                       # free-text: ambiguous
        ({"type": "object"}, None),       # objects: per-sub-key
    ],
)
def test_infer_scale_matrix(field, expected):
    assert ac.infer_scale(field) == expected




@pytest.mark.parametrize(
    "spec, parent_array, expected",
    [
        ("list: notable sounds", False, "list"),
        ("int(0,100): pct", False, "numeric"),
        ("int: age", True, "numeric"),            # numeric-mean aggregation
        ("enum:gender", True, "list"),            # pipe-joined across elements
        ("enum:gender", False, "categorical"),
        ("the apparent ethnicity", True, "list"),
        ("free text", False, None),               # ambiguous
        ({"spec": "enum:gender", "scale": "list"}, False, "categorical"),  # infer ignores meta
    ],
)
def test_infer_subkey_scale_matrix(spec, parent_array, expected):
    assert ac.infer_subkey_scale(spec, parent_array=parent_array) == expected




def test_effective_scale_prefers_explicit():
    assert ac.effective_scale({"scale": "text"}) == "text"
    assert ac.effective_scale({"enum": "yes_no"}) == "categorical"
    assert ac.effective_subkey_scale({"spec": "enum:gender", "scale": "raw"}) == "raw"




def test_metadata_identical_to_pre_migration():
    """role/scale/display_name must match the pre-migration snapshot exactly.

    role and scale feed the var_schema semantic hash — any drift here would
    invalidate every cached study parquet. description is expected to differ:
    the migration retired the separate web-UI description, falling back to the
    prompt desc. Roles compare modulo LEGACY_ROLE_ALIASES: the fixture is a
    frozen pre-rename snapshot ("feature"), and the 2026-08 vocabulary rename
    ("measure") was a deliberate, hash-versioned change (v3).
    """
    from fyp.recode_variables import normalize_role

    old = json.loads(METADATA_V1.read_text(encoding="utf-8"))
    new = ac.contract_column_metadata(_baked_contract())
    assert sorted(old) == sorted(new)
    for col, meta in old.items():
        for key in ("scale", "display_name"):
            assert new[col].get(key) == meta.get(key), f"{col}.{key} drifted"
        assert normalize_role(new[col].get("role")) == normalize_role(meta.get("role")), \
            f"{col}.role drifted"
        assert new[col].get("description")  # falls back to desc, never empty




def test_validation_free_text_needs_scale():
    contract = _baked_contract()
    contract["fields"].append({"name": "new_free_text", "desc": "test"})
    errors = ac.validate_contract(contract)
    assert any("free-text field needs an explicit scale" in e for e in errors)

    contract["fields"][-1]["scale"] = "categorical"
    assert not ac.validate_contract(contract)




def test_validation_rejects_stale_section_key():
    contract = _baked_contract()
    contract["fields"][0]["section"] = "profile"
    errors = ac.validate_contract(contract)
    assert any("no [[section]] entries" in e for e in errors)




def test_validation_accepts_legacy_sectioned_contract():
    assert not ac.validate_contract(_sectioned_contract())




def test_serialize_drops_section_aot():
    """Flattening a legacy contract removes [[section]] and per-field keys."""
    base_text = SECTIONED_TOML.read_text(encoding="utf-8")
    flattened = tomllib.loads(base_text)
    flattened.pop("section", None)
    for field in flattened["fields"]:
        field.pop("section", None)
    out = ac.serialize_contract(flattened, base_text=base_text)
    # No [[section]] tables survive (the literal may linger in comments).
    assert not any(line.strip() == "[[section]]" for line in out.splitlines())
    assert tomllib.loads(out) == flattened
    assert not ac.validate_contract(tomllib.loads(out))
