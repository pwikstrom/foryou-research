"""Golden regression test for the machine-annotation refinement pipeline.

Re-runs ``refine_one_raw_annotation_batch`` (JSON repair → flatten → rare-column
consolidation → transcript de-dup → schema recode → label clean-up) over the
frozen fixture and asserts the output matches the committed golden snapshot.

This is the safety net for any refactor of ``fyp/machine_annotation.py`` or
``fyp/recode_variables.py``: if the refined output changes, this test fails and
prints exactly which columns/cells moved.  If the change is intended, re-bless
the snapshot with ``python tests/golden/build_golden.py`` and review the diff.

Runs entirely on saved raw responses — no Gemini API calls, no cost.

Usage (standalone, matches repo convention):
    python tests/golden/test_refinement_golden.py
Also discoverable by pytest if installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
from _harness import (
    GOLDEN_PARQUET,
    SCHEMA_SNAPSHOT,
    compare_refined,
    load_fixture,
    pinned_var_schema,
    run_current_refinement,
)


def _require_artifacts() -> None:
    missing = [p for p in (GOLDEN_PARQUET, SCHEMA_SNAPSHOT) if not Path(p).exists()]
    if missing:
        raise AssertionError(
            "Golden artifacts missing: "
            + ", ".join(str(m) for m in missing)
            + "\nBuild them with: python tests/golden/build_fixture.py && "
            "python tests/golden/build_golden.py"
        )


def test_golden_artifacts_exist() -> None:
    _require_artifacts()


def test_refinement_matches_golden() -> None:
    """The live pipeline must reproduce the committed golden output exactly."""
    _require_artifacts()
    golden = pd.read_parquet(GOLDEN_PARQUET)
    raw = load_fixture()
    with pinned_var_schema():
        got = run_current_refinement(raw, quiet=True)

    diffs = compare_refined(got, golden)
    assert not diffs, (
        f"Refinement output diverged from golden snapshot ({len(diffs)} diff group(s)):\n  - "
        + "\n  - ".join(diffs)
        + "\n\nIf this change is intentional, re-bless with: "
        "python tests/golden/build_golden.py"
    )


def test_refinement_is_deterministic() -> None:
    """Two refinement runs on the same input must agree.

    Guards the seeded-RNG workaround for ``clean_up_machine_annotations``'s
    unseeded ``Series.sample`` (machine_annotation.py).  If this ever fails,
    a new source of non-determinism has entered the pipeline.
    """
    _require_artifacts()
    raw = load_fixture()
    with pinned_var_schema():
        a = run_current_refinement(raw, quiet=True)
        b = run_current_refinement(raw, quiet=True)
    diffs = compare_refined(a, b)
    assert not diffs, (
        "Refinement is non-deterministic across runs:\n  - " + "\n  - ".join(diffs)
    )


def _main() -> int:
    tests = [
        test_golden_artifacts_exist,
        test_refinement_matches_golden,
        test_refinement_is_deterministic,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}\n      {exc}")
        except Exception as exc:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
