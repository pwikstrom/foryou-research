"""Characterization tests for the var_schema cell parsers.

The ``parse_*`` helpers in ``fyp/recode_variables.py`` turn raw CSV cell text
(authored by hand or via the admin Variable Schema tab) into the dicts / lists /
callables that drive recoding.  They are deliberately strict — never ``eval`` —
and fall back to safe defaults on bad input.  These tests pin that grammar so a
refactor (or the schema-driven pipeline work) can't silently broaden or break
what a schema cell is allowed to contain.

Covers: parse_accepted_labels, parse_recode_func.

(``mapper`` / ``ignore_strings`` no longer exist as schema cells — their recode
normalization is derived from ``annotation_contract.toml``; see
``test_recode_series_branches`` and ``test_contract_accepted_labels``.)

Usage:
    python tests/unit/test_schema_cell_parsers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.recode_variables as rv

# ---------------------------------------------------------------------------
# parse_accepted_labels
# ---------------------------------------------------------------------------

def test_accepted_labels_blank_to_empty() -> None:
    for blank in (None, pd.NA, ""):
        assert rv.parse_accepted_labels(blank) == []


def test_accepted_labels_json_array() -> None:
    assert rv.parse_accepted_labels('["a", "b"]') == ["a", "b"]


def test_accepted_labels_legacy_bareword_list() -> None:
    assert rv.parse_accepted_labels("[performance, comedy, animals]") == [
        "performance",
        "comedy",
        "animals",
    ]


# ---------------------------------------------------------------------------
# parse_recode_func
# ---------------------------------------------------------------------------

def test_recode_func_blank_to_none() -> None:
    assert rv.parse_recode_func(None) is None
    assert rv.parse_recode_func("") is None
    assert rv.parse_recode_func(pd.NA) is None


def test_recode_func_registered_resolves_to_callable() -> None:
    func = rv.parse_recode_func("recode_scores")
    assert callable(func)
    assert func is rv.recode_scores


def test_recode_func_unknown_is_none_not_error() -> None:
    # Unknown names never raise and never eval — they no-op (with a warning).
    assert rv.parse_recode_func("nonexistent_func") is None


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
