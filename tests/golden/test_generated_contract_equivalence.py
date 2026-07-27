"""Equivalence + completeness gate for the declarative annotation contract.

Workstream E moved the Gemini contract (prompt + response_schema + flattener
field specs) into one declarative source, ``config/annotation_contract.toml``.
This module is the safety gate that proves the generated artifacts are correct:

  * BYTE-IDENTICAL (the cutover proof): the contract-driven ``FIELD_SPECS``, the
    OpenAPI JSON schema (sync interactive endpoint), and the genai-proto schema
    dump (the form the BATCH path ships) must equal frozen oracles captured from
    the pre-refactor hand-written code. Freezing the proto dump too is required
    because of commit 217f40c: the batch endpoint needs the proto form, not the
    OpenAPI dict, and a contract rebuild could match one but perturb the other.
  * FUNCTIONAL (the prompt can't be byte-identical — its prose isn't in the
    structured data): the generated prompt must mention all fields, every enum
    value, and all section titles; be deterministic; and match a committed
    review snapshot.

After an INTENTIONAL contract edit (experimentation), the byte-identical checks
fail by design until re-blessed:
    python tests/golden/test_generated_contract_equivalence.py --bless

Usage:
    python tests/golden/test_generated_contract_equivalence.py [--bless]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import FIXTURE_DIR, fyp_cf

import fyp.annotation_schema as schema

FROZEN_FIELD_SPECS = FIXTURE_DIR / "field_specs.frozen.json"
FROZEN_OPENAPI = FIXTURE_DIR / "annotation_json_schema.frozen.json"
FROZEN_PROTO = FIXTURE_DIR / "response_schema_proto.frozen.json"
PROMPT_SNAPSHOT = FIXTURE_DIR / "prompt.generated.snapshot.txt"

_FIELD_RE = re.compile(r"[•·]\s*['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]")


def _current_field_specs() -> list:
    return [
        {"gemini_field": n, "node": node, "flatten_rule": r}
        for (n, node, r) in schema.FIELD_SPECS
    ]


def _current_proto() -> dict:
    # Exactly the serialization machine_annotation_batch.py ships to Vertex batch.
    return schema.build_response_schema().model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_validates() -> None:
    from fyp import annotation_contract as ac

    errors = ac.validate_contract(schema._CONTRACT)
    assert not errors, f"annotation_contract.toml has validation errors: {errors}"


def test_field_specs_match_frozen() -> None:
    assert FROZEN_FIELD_SPECS.exists(), (
        f"No frozen FIELD_SPECS at {FROZEN_FIELD_SPECS}. Re-bless if intentional."
    )
    assert _current_field_specs() == _load(FROZEN_FIELD_SPECS), (
        "Contract-driven FIELD_SPECS diverged from the frozen oracle. "
        "If this is an intentional contract change, re-bless."
    )


def test_openapi_schema_matches_frozen() -> None:
    assert schema.get_annotation_json_schema() == _load(FROZEN_OPENAPI), (
        "get_annotation_json_schema() diverged from the frozen oracle (sync endpoint)."
    )


def test_proto_schema_matches_frozen() -> None:
    # The batch-path-critical check (commit 217f40c): the genai-proto dump must
    # not drift, or the Vertex batch endpoint silently breaks.
    assert _current_proto() == _load(FROZEN_PROTO), (
        "genai-proto schema dump diverged from the frozen oracle (BATCH endpoint)."
    )


def test_prompt_functionally_complete() -> None:
    prompt = schema.build_prompt()
    found = set(_FIELD_RE.findall(prompt))
    spec_names = {n for n, _node, _rule in schema.FIELD_SPECS}
    assert found == spec_names, (
        f"prompt fields != schema fields; missing {sorted(spec_names - found)}, "
        f"extra {sorted(found - spec_names)}"
    )

    from fyp import annotation_contract as ac

    enum_values = set()
    for name in schema._CONTRACT["enums"]:
        enum_values |= set(ac.enum_values(schema._CONTRACT, name))
    missing = sorted(v for v in enum_values if v not in prompt)
    assert not missing, f"enum values missing from prompt: {missing}"

    # Legacy sectioned contracts must render their section titles; a
    # sectionless contract (the 2026-07 shape) has none to check.
    section_titles = [s.get("title", "") for s in schema._CONTRACT.get("section", [])]
    missing_titles = [t for t in section_titles if t and t not in prompt]
    assert not missing_titles, f"section titles missing from prompt: {missing_titles}"


def test_prompt_deterministic() -> None:
    assert schema.build_prompt() == schema.build_prompt(), "build_prompt() is non-deterministic"


def test_prompt_matches_snapshot() -> None:
    assert PROMPT_SNAPSHOT.exists(), (
        f"No prompt snapshot at {PROMPT_SNAPSHOT}. Re-bless if intentional."
    )
    assert schema.build_prompt() == PROMPT_SNAPSHOT.read_text(encoding="utf-8"), (
        "Generated prompt diverged from the committed snapshot. "
        "Review the diff; re-bless if intentional."
    )


def bless() -> None:
    FROZEN_FIELD_SPECS.write_text(
        json.dumps(_current_field_specs(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    FROZEN_OPENAPI.write_text(
        json.dumps(schema.get_annotation_json_schema(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    FROZEN_PROTO.write_text(
        json.dumps(_current_proto(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PROMPT_SNAPSHOT.write_text(schema.build_prompt(), encoding="utf-8")
    print("Re-blessed frozen schema fixtures + prompt snapshot.")


def _main(argv: list[str]) -> int:
    if "--bless" in argv:
        bless()
        return 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}\n      {exc}")
        except Exception:
            import traceback

            failures += 1
            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
