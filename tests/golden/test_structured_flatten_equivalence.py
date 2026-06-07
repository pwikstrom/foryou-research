"""Offline proof that the structured flattener reproduces the legacy flat shape.

The Phase 2 design keeps the structured-output column shape IDENTICAL to the
free-text pipeline so that the existing recode layer (and the golden corpus) are
reused unchanged, and an A/B test isolates "free-text vs structured generation"
as the only variable.

This test validates that claim for FREE, with no API calls: it parses the real
saved raw responses (which are already nested JSON of the same shape structured
output produces), runs each through both ``flatten_one_machine_response``
(legacy) and ``flatten_structured`` (new), and asserts they agree on every
shared column except the two score fields — whose representation differs by
design (legacy free string vs structured ``{score, rationale}`` object) and are
covered by a dedicated unit test below.

Usage:
    python tests/golden/test_structured_flatten_equivalence.py
"""

from __future__ import annotations

import contextlib
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import _normalize_cell, load_fixture

import fyp.machine_annotation as ma
from fyp.annotation_schema import (
    AUSSIE_CONDITIONAL_FIELDS,
    CONDITIONAL_FIELDS,
    FIELD_SPECS,
    FRAMING_FIELDS,
    apply_conditional_rules,
    build_response_schema,
    flatten_structured,
    get_annotation_json_schema,
)

# Representation differs by design (string vs object); covered by a unit test.
SCORE_FIELDS = {"political_score", "sensitivity_score"}


def _to_structured_shape(nested: dict) -> dict:
    """Convert a legacy-parsed response into the structured score shape.

    Only the score fields differ in representation; everything else (scenes,
    faces, audio_summary, lists, scalars) already matches the structured shape.
    """
    out = dict(nested)
    for key in SCORE_FIELDS:
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            parts = val.split(", ", 1)
            num = parts[0].strip()
            rationale = parts[1] if len(parts) > 1 else ""
            with contextlib.suppress(ValueError):
                out[key] = {"score": int(float(num)), "rationale": rationale}
    return out


# ---------------------------------------------------------------------------
# Schema builder structure
# ---------------------------------------------------------------------------

def test_field_spec_and_schema_structure() -> None:
    assert len(FIELD_SPECS) == 35
    js = get_annotation_json_schema()
    assert js["type"] == "object"
    assert len(js["properties"]) == 35
    assert js["propertyOrdering"][0] == "transcript"
    schema = build_response_schema()
    assert str(schema.type).endswith("OBJECT")
    assert len(schema.properties) == 35
    # Enum constraints survive the conversion.
    assert schema.properties["type_of_story"].enum
    assert schema.properties["content_category"].items.enum


def test_score_join_rule() -> None:
    flat = flatten_structured({"political_score": {"score": 85, "rationale": "high"}})
    assert flat["political_score"] == "85, high"
    flat2 = flatten_structured({"sensitivity_score": {"score": 0, "rationale": ""}})
    assert flat2["sensitivity_score"] == "0"


def test_conditional_fields_nullable_in_schema() -> None:
    js = get_annotation_json_schema()
    for field in CONDITIONAL_FIELDS:
        assert field not in js["required"], f"{field} should not be required"
        assert js["properties"][field].get("nullable") is True
    # Non-conditional fields stay required.
    assert "type_of_story" in js["required"]
    schema = build_response_schema()
    assert schema.properties["framing_analysis_moral_evaluation"].nullable is True


def test_apply_conditional_rules_framing() -> None:
    # Non issue/event story -> framing blanked to "-".
    flat = dict.fromkeys(FRAMING_FIELDS, "some framing text")
    flat["type_of_story"] = "Human-Interest"
    out = apply_conditional_rules(dict(flat), {})
    assert all(out[f] == "-" for f in FRAMING_FIELDS)
    # Issue-based story -> framing preserved.
    flat["type_of_story"] = "Issue-Based"
    out2 = apply_conditional_rules(dict(flat), {})
    assert all(out2[f] == "some framing text" for f in FRAMING_FIELDS)


def test_apply_conditional_rules_political() -> None:
    flat = dict.fromkeys(AUSSIE_CONDITIONAL_FIELDS, "some political text")
    # score <= threshold -> blanked.
    out = apply_conditional_rules(dict(flat), {"political_score": {"score": 10}})
    assert all(out[f] == "-" for f in AUSSIE_CONDITIONAL_FIELDS)
    # score > threshold -> preserved.
    out2 = apply_conditional_rules(dict(flat), {"political_score": {"score": 75}})
    assert all(out2[f] == "some political text" for f in AUSSIE_CONDITIONAL_FIELDS)


# ---------------------------------------------------------------------------
# Equivalence on real saved responses
# ---------------------------------------------------------------------------

def test_structured_flatten_matches_legacy_on_real_data() -> None:
    raw = load_fixture()
    compared = 0
    column_mismatches: dict[str, int] = {}
    examples: dict[str, tuple] = {}

    for entry in raw.values():
        resp_text = entry.get("response")
        if not resp_text:
            continue
        nested = ma.fuzzy_load_of_json_from_string(resp_text)
        if not isinstance(nested, dict):
            continue
        legacy = ma.flatten_one_machine_response(deepcopy(nested))
        if not isinstance(legacy, dict):
            continue  # legacy rejects this response (missing required keys)
        structured = flatten_structured(_to_structured_shape(nested))

        compared += 1
        shared = (set(legacy) & set(structured)) - SCORE_FIELDS
        for col in shared:
            if _normalize_cell(legacy[col]) != _normalize_cell(structured[col]):
                column_mismatches[col] = column_mismatches.get(col, 0) + 1
                examples.setdefault(col, (legacy[col], structured[col]))

    assert compared >= 50, f"Too few comparable responses ({compared}); fixture problem?"
    if column_mismatches:
        detail = "\n".join(
            f"  - {col}: {n}/{compared} differ; e.g. legacy={examples[col][0]!r} "
            f"structured={examples[col][1]!r}"
            for col, n in sorted(column_mismatches.items())
        )
        raise AssertionError(
            f"structured flattener diverges from legacy on {len(column_mismatches)} "
            f"column(s) over {compared} responses:\n{detail}"
        )
    print(f"  equivalence verified on {compared} real responses (score fields excluded)")


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
