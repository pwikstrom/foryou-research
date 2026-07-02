"""Unit tests for per-user variable preferences (Stage 2).

Pins:

  * ``compose_effective_variables``: (global ∪ include) − exclude in canonical
    order; unknown names ignored; non-schema extras (dynamic prepends,
    machine_state) preserved and not excludable; ``available`` clips user
    includes but never global members.
  * ``_validate_variable_prefs``: accepts the documented shape, rejects
    unknown surfaces/keys, non-list values and oversized lists.

No network, no Gemini, no data files.

Usage:
    python tests/unit/test_variable_prefs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web_interface.data_service import compose_effective_variables
from web_interface.routes.auth_routes import _validate_variable_prefs

ALL_ORDER = ["a", "b", "c", "d", "e"]
GLOBAL = ["b", "d"]


def test_defaults_without_prefs() -> None:
    for prefs in (None, {}, {"include": [], "exclude": []}):
        got = compose_effective_variables(GLOBAL, prefs, ALL_ORDER)
        assert got == ["b", "d"], got


def test_include_and_exclude_compose_in_canonical_order() -> None:
    got = compose_effective_variables(
        GLOBAL, {"include": ["e", "a"], "exclude": ["d"]}, ALL_ORDER)
    assert got == ["a", "b", "e"], got


def test_unknown_names_ignored() -> None:
    got = compose_effective_variables(
        GLOBAL, {"include": ["nope"], "exclude": ["ghost"]}, ALL_ORDER)
    assert got == ["b", "d"], got


def test_non_schema_extras_preserved_first() -> None:
    # machine_state / dynamic user-tag columns live in the global list but not
    # in all_variables_order; they survive composition ahead of the ordering.
    got = compose_effective_variables(
        ["machine_state"] + GLOBAL, {"exclude": ["b"]}, ALL_ORDER)
    assert got == ["machine_state", "d"], got


def test_available_clips_includes_but_not_globals() -> None:
    got = compose_effective_variables(
        GLOBAL, {"include": ["a", "c"]}, ALL_ORDER, available={"a", "b"})
    # 'c' has no data -> clipped; 'd' is global -> kept even without data.
    assert got == ["a", "b", "d"], got


def test_validation_accepts_documented_shape() -> None:
    assert _validate_variable_prefs({}) is None
    assert _validate_variable_prefs(
        {"filter": {"include": ["x"], "exclude": []},
         "display": {}, "timeline": {"exclude": ["y"]}, "viz": {"include": []}}) is None


def test_validation_rejects_bad_shapes() -> None:
    assert _validate_variable_prefs([]) is not None
    assert _validate_variable_prefs({"nope": {}}) is not None
    assert _validate_variable_prefs({"filter": []}) is not None
    assert _validate_variable_prefs({"filter": {"add": []}}) is not None
    assert _validate_variable_prefs({"filter": {"include": "x"}}) is not None
    assert _validate_variable_prefs({"filter": {"include": [1]}}) is not None
    assert _validate_variable_prefs(
        {"filter": {"include": ["v"] * 501}}) is not None


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
