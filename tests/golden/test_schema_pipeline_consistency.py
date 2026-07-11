"""Consistency test for the hand-synced sources of annotation truth.

Today a variable's definition is duplicated across places that a human must
keep in sync by hand:

  1. the Gemini prompt (generated from the annotation contract) — field names +
     allowed values
  2. flatten_one_machine_response()  (fyp/machine_annotation.py) — hardcoded keys
  3. the synthesized var_schema — variable_name / recode plan / accepted_labels

This test pins that coupling so it cannot drift silently:

  * HARD invariants (always fail if violated, because they break the pipeline):
      - every ``recode_func`` named in var_schema exists in the allow-listed
        registry (otherwise it silently no-ops at runtime)
      - the live var_schema passes ``validate_var_schema`` (no malformed rows)
      - ``variable_name`` values are unique

  * DRIFT baseline (fails when the cross-source relationship *changes*):
      - the set differences between prompt fields, flatten keys and schema
        variables are snapshotted to ``fixtures/coupling_baseline.json``.
        If they move — e.g. a prompt field is added without a schema row — the
        test fails and shows what changed.  Re-bless intentionally with
        ``python tests/golden/test_schema_pipeline_consistency.py --bless``.

When the pipeline is eventually generated *from* the schema (the inversion
goal), this test becomes the proof that the generators keep the four artifacts
in lockstep.

Usage:
    python tests/golden/test_schema_pipeline_consistency.py [--bless]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import FIXTURE_DIR, fyp_cf

import fyp.annotation_versioning as annotation_versioning
import fyp.recode_variables as rv

COUPLING_BASELINE = FIXTURE_DIR / "coupling_baseline.json"

# Keys that flatten_one_machine_response() special-cases.  Sourced by hand from
# fyp/machine_annotation.py (the scenes / transcript / objects / audio_summary /
# faces handlers).  Kept here so the test alerts when the prompt or schema drift
# relative to the flattener; update deliberately alongside that function.
FLATTEN_HANDLED_KEYS = {
    "scenes",
    "transcript",
    "objects",
    "symbols_and_brands",
    "text_overlays",
    "content_category",
    "audio_summary",
    "faces",
}


def extract_prompt_fields() -> set[str]:
    """Parse quoted field names from the *active* Gemini prompt.

    Routes through ``active_prompt_text()`` so it reflects the contract-generated
    prompt the model is actually sent. (Reading a static file directly would pin
    a vestigial prompt that no longer matches the contract.)
    """
    text = annotation_versioning.active_prompt_text()
    # Field lines look like:  • 'transcript': A verbatim transcript ...
    fields = set(re.findall(r"[•·]\s*['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]", text))
    return fields


def schema_variable_names() -> set[str]:
    vs = fyp_cf["var_schema"]
    return {str(v) for v in vs["variable_name"].dropna().tolist()}


def schema_gemini_sourced() -> set[str]:
    """Schema variables whose ``source`` mentions Gemini (the model-facing rows).

    Restricted to columns the *current* contract owns
    (``contract_column_metadata``): the var_schema also carries legacy rows
    injected from the on-disk annotation version registry, which is live data
    state — absent on a fresh checkout / CI — not prompt↔schema coupling.
    """
    import fyp.annotation_contract as ac

    vs = fyp_cf["var_schema"]
    if "source" not in vs.columns:
        return set()
    mask = vs["source"].astype(str).str.contains("Gemini", case=False, na=False)
    gemini_vars = {str(v) for v in vs.loc[mask, "variable_name"].dropna().tolist()}
    current_columns = set(ac.contract_column_metadata(ac.load_contract()).keys())
    return gemini_vars & current_columns


def build_coupling_report() -> dict:
    prompt_fields = extract_prompt_fields()
    schema_vars = schema_variable_names()
    gemini_vars = schema_gemini_sourced()

    return {
        "prompt_fields_not_in_schema": sorted(prompt_fields - schema_vars),
        "flatten_keys_not_in_prompt": sorted(FLATTEN_HANDLED_KEYS - prompt_fields),
        "flatten_keys_not_in_schema": sorted(FLATTEN_HANDLED_KEYS - schema_vars),
        "gemini_schema_vars_not_in_prompt": sorted(gemini_vars - prompt_fields),
    }


# ----------------------------------------------------------------------------
# Hard invariants
# ----------------------------------------------------------------------------

def test_recode_funcs_are_registered() -> None:
    # recode_func is no longer a column — the op is derived per field by
    # build_recode_plan (scale + source). Assert every live variable resolves to
    # a registered callable or None (no dangling/unknown function).
    vs = fyp_cf["var_schema"]
    registry = set(rv.get_recode_func_registry().values())
    plan = rv.build_recode_plan(vs.set_index("variable_name"))
    unknown = sorted(
        name for name, func in plan.items()
        if func is not None and func not in registry
    )
    assert not unknown, (
        f"build_recode_plan resolved variables to non-registry callables: {unknown}."
    )


def test_live_schema_validates() -> None:
    errors = rv.validate_var_schema(fyp_cf["var_schema"])
    assert not errors, (
        f"Live var_schema has {len(errors)} validation error(s); first few: "
        f"{[dict(e) for e in errors[:5]]}"
    )


def test_variable_names_unique() -> None:
    names = [str(v) for v in fyp_cf["var_schema"]["variable_name"].dropna().tolist()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"Duplicate variable_name(s) in var_schema: {dupes}"


# ----------------------------------------------------------------------------
# Drift baseline
# ----------------------------------------------------------------------------

def test_coupling_matches_baseline() -> None:
    report = build_coupling_report()
    if not COUPLING_BASELINE.exists():
        raise AssertionError(
            f"No coupling baseline at {COUPLING_BASELINE}. "
            "Create it with: python tests/golden/test_schema_pipeline_consistency.py --bless"
        )
    baseline = json.loads(COUPLING_BASELINE.read_text(encoding="utf-8"))
    changed = {
        k: {"baseline": baseline.get(k), "now": report.get(k)}
        for k in set(baseline) | set(report)
        if baseline.get(k) != report.get(k)
    }
    assert not changed, (
        "Prompt ↔ flatten ↔ var_schema coupling changed:\n"
        + json.dumps(changed, indent=2, ensure_ascii=False)
        + "\n\nIf intentional, re-bless: "
        "python tests/golden/test_schema_pipeline_consistency.py --bless"
    )


def bless() -> None:
    report = build_coupling_report()
    COUPLING_BASELINE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote coupling baseline to {COUPLING_BASELINE}:")
    print(json.dumps(report, indent=2, ensure_ascii=False))


def _main(argv: list[str]) -> int:
    if "--bless" in argv:
        bless()
        return 0
    # Always print the current coupling report for visibility.
    print("Current coupling report:")
    print(json.dumps(build_coupling_report(), indent=2, ensure_ascii=False))
    print()
    tests = [
        test_recode_funcs_are_registered,
        test_live_schema_validates,
        test_variable_names_unique,
        test_coupling_matches_baseline,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}\n      {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
