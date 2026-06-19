"""Run the annotation-pipeline safety net in one shot.

Executes the cost-free regression + consistency suite that guards any refactor
of the machine-annotation refinement / recode code:

  * test_refinement_golden          — refined output matches the frozen golden
  * test_schema_pipeline_consistency — the four sources of truth stay in lockstep
  * test_recode_series_branches      — Series/scalar parity of every recode_func
    (existing test under tests/unit, included here because it covers the same
    refinement surface)

No Gemini API calls; runs entirely on saved fixtures.

Usage:
    python tests/golden/run_safety_net.py
Exit code 0 iff every module passes.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = GOLDEN_DIR.parents[1]

MODULES = [
    GOLDEN_DIR / "test_refinement_golden.py",
    GOLDEN_DIR / "test_schema_pipeline_consistency.py",
    GOLDEN_DIR / "test_generated_contract_equivalence.py",
    GOLDEN_DIR / "test_structured_flatten_equivalence.py",
    GOLDEN_DIR / "test_structured_refinement_path.py",
    GOLDEN_DIR / "test_versioning_consolidation.py",
    GOLDEN_DIR / "test_contract_cutover.py",
    GOLDEN_DIR / "test_batch_annotation.py",
    GOLDEN_DIR / "test_batch_worker.py",
    PROJECT_ROOT / "tests" / "unit" / "test_recode_series_branches.py",
    PROJECT_ROOT / "tests" / "unit" / "test_annotation_repair.py",
    PROJECT_ROOT / "tests" / "unit" / "test_schema_cell_parsers.py",
    PROJECT_ROOT / "tests" / "unit" / "test_call_machine_retry.py",
    PROJECT_ROOT / "tests" / "unit" / "test_annotation_versioning.py",
    PROJECT_ROOT / "tests" / "unit" / "test_media_resolution.py",
    PROJECT_ROOT / "tests" / "unit" / "test_generated_prompt_flag.py",
    PROJECT_ROOT / "tests" / "unit" / "test_contract_accepted_labels.py",
    PROJECT_ROOT / "tests" / "unit" / "test_australian_relevance_derivation.py",
]


def _run(path: Path) -> int:
    print("=" * 78)
    print(f"RUN  {path.relative_to(PROJECT_ROOT)}")
    print("=" * 78)
    try:
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


def main() -> int:
    results = {}
    for mod in MODULES:
        if not mod.exists():
            print(f"SKIP (missing): {mod}")
            results[mod.name] = "skip"
            continue
        results[mod.name] = "pass" if _run(mod) == 0 else "fail"
        print()

    print("=" * 78)
    print("SAFETY NET SUMMARY")
    for name, status in results.items():
        print(f"  {status.upper():5} {name}")
    failed = [n for n, s in results.items() if s == "fail"]
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
