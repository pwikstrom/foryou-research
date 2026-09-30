"""Unit tests for per-user variable preferences (Stage 2).

Pins:

  * ``compose_effective_variables``: (global ∪ include) − exclude in canonical
    order; unknown names ignored; non-schema extras (dynamic prepends,
    machine_state) preserved and not excludable; ``available`` clips user
    includes but never global members.
  * ``_validate_variable_prefs``: accepts the documented shape, rejects
    unknown surfaces/keys, non-list values and oversized lists.
  * ``apply_section_order``: per-section slot-fill — listed variables re-sort
    within their own section, unlisted ones keep their slot.
  * ``compose_effective_variables`` layers the user's ``order`` on top of the
    surface's default order.
  * ``load_schema_metadata``: backstage and ``role=skip`` variables never
    reach a user-facing list (but stay in ``schema_map``); ``default_order``
    carries the admins' arrangement.

No network, no Gemini, no data files.

Usage:
    python tests/unit/test_variable_prefs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from web_interface.routes.auth_routes.user import _validate_variable_prefs
from web_interface.services.user_variables import (
    apply_section_order,
    compose_effective_variables,
    fyp_cf,
    load_schema_metadata,
)

ALL_ORDER = ["a", "b", "c", "d", "e"]
GLOBAL = ["b", "d"]


def test_defaults_without_prefs() -> None:
    for prefs in (None, {}, {"include": [], "exclude": []}):
        got = compose_effective_variables(GLOBAL, prefs, ALL_ORDER)
        assert got == ["b", "d"], got


def test_include_and_exclude_compose_in_canonical_order() -> None:
    got = compose_effective_variables(GLOBAL, {"include": ["e", "a"], "exclude": ["d"]}, ALL_ORDER)
    assert got == ["a", "b", "e"], got


def test_unknown_names_ignored() -> None:
    got = compose_effective_variables(
        GLOBAL, {"include": ["nope"], "exclude": ["ghost"]}, ALL_ORDER
    )
    assert got == ["b", "d"], got


def test_non_schema_extras_preserved_first() -> None:
    # machine_state / dynamic user-tag columns live in the global list but not
    # in all_variables_order; they survive composition ahead of the ordering.
    got = compose_effective_variables(["machine_state"] + GLOBAL, {"exclude": ["b"]}, ALL_ORDER)
    assert got == ["machine_state", "d"], got


def test_available_clips_includes_but_not_globals() -> None:
    got = compose_effective_variables(
        GLOBAL, {"include": ["a", "c"]}, ALL_ORDER, available={"a", "b"}
    )
    # 'c' has no data -> clipped; 'd' is global -> kept even without data.
    assert got == ["a", "b", "d"], got


# --- ordering ---------------------------------------------------------------

SECTIONS = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "b2": "B"}
NAMES = ["a1", "a2", "a3", "b1", "b2"]


def test_section_order_slot_fill() -> None:
    got = apply_section_order(NAMES, ["a3", "a1", "b2", "b1"], SECTIONS)
    assert got == ["a3", "a2", "a1", "b2", "b1"], got


def test_section_order_keeps_unlisted_slots_and_ignores_unknown() -> None:
    # a2 is unlisted (added after the order was saved): it keeps its slot.
    got = apply_section_order(NAMES, ["ghost", "a3", "a1"], SECTIONS)
    assert got == ["a3", "a2", "a1", "b1", "b2"], got
    assert apply_section_order(NAMES, None, SECTIONS) == NAMES
    assert apply_section_order(NAMES, [], SECTIONS) == NAMES


def test_section_order_never_moves_across_sections() -> None:
    # An order saved when b1 sat in section A (it has since moved to B) must
    # not drag b1 into A's slots.
    got = apply_section_order(NAMES, ["b1", "a2", "a1"], SECTIONS)
    assert got == ["a2", "a1", "a3", "b1", "b2"], got


def test_section_order_callable_and_whole_list() -> None:
    got = apply_section_order(NAMES, ["b2", "a1"], SECTIONS.get)
    assert got == NAMES, got  # different sections: nothing to swap
    got = apply_section_order(NAMES, ["b2", "a1"])
    assert got == ["b2", "a2", "a3", "b1", "a1"], got  # one section


def test_compose_applies_user_order_within_sections() -> None:
    got = compose_effective_variables(
        ["a1", "a2", "b1"],
        {"include": ["b2"], "order": ["a2", "a1", "b2", "b1"]},
        NAMES,
        section_of=SECTIONS,
    )
    assert got == ["a2", "a1", "b2", "b1"], got


def test_compose_layers_user_order_on_default_order() -> None:
    # The admins' default order puts a3 before a1; the user only rearranged B.
    default_order = apply_section_order(NAMES, ["a3", "a1"], SECTIONS)
    got = compose_effective_variables(
        ["a1", "a3", "b1", "b2"], {"order": ["b2", "b1"]}, default_order, section_of=SECTIONS
    )
    assert got == ["a3", "a1", "b2", "b1"], got


# --- load_schema_metadata -----------------------------------------------------


def _fake_schema() -> pd.DataFrame:
    rows = [
        # variable_name, section, role, scale, filter, viz
        ("act_cat", "Activity", "", "categorical", "1", "1"),
        ("act_num", "Activity", "", "numeric", "1", pd.NA),
        ("act_z", "Activity", "", "categorical", "1", pd.NA),
        ("pop_num", "Popularity", "", "numeric", pd.NA, "1"),
        ("status", "backstage", "", "categorical", "1", "1"),
        ("gone", "Item metadata", "skip", "categorical", "1", pd.NA),
    ]
    return pd.DataFrame(
        {
            "variable_name": [r[0] for r in rows],
            "section": [r[1] for r in rows],
            "role": [r[2] for r in rows],
            "scale": [r[3] for r in rows],
            "display_name": [r[0] for r in rows],
            "description": ["" for _ in rows],
            "web_filter_prio": [r[4] for r in rows],
            "web_viz_prio": [r[5] for r in rows],
            "web_timeline_prio": [pd.NA for _ in rows],
            "web_display_prio": [pd.NA for _ in rows],
        }
    )


def _with_schema(order):
    saved = {k: fyp_cf.get(k) for k in ("var_schema", "var_presentation_order")}
    fyp_cf["var_schema"] = _fake_schema()
    fyp_cf["var_presentation_order"] = order
    try:
        return load_schema_metadata({})
    finally:
        for k, v in saved.items():
            if v is None:
                fyp_cf.pop(k, None)
            else:
                fyp_cf[k] = v


def test_metadata_hides_backstage_and_skip() -> None:
    meta = _with_schema({})
    assert meta["all_variables_order"] == ["act_cat", "act_z", "act_num", "pop_num"], meta[
        "all_variables_order"
    ]
    assert meta["filter_priority"] == ["act_cat", "act_z", "act_num"], meta["filter_priority"]
    assert meta["viz_priority"] == ["act_cat", "pop_num"], meta["viz_priority"]
    # Still resolvable for display names elsewhere.
    assert "status" in meta["schema_map"] and "gone" in meta["schema_map"]
    assert meta["default_order"]["filter"] == meta["all_variables_order"]


def test_metadata_applies_admin_default_order() -> None:
    meta = _with_schema({"filter": ["act_num", "act_cat", "status"]})
    assert meta["default_order"]["filter"] == ["act_num", "act_z", "act_cat", "pop_num"], meta[
        "default_order"
    ]["filter"]
    assert meta["filter_priority"] == ["act_num", "act_z", "act_cat"], meta["filter_priority"]
    # Other surfaces keep the computed order.
    assert meta["default_order"]["viz"] == meta["all_variables_order"]


def test_validation_accepts_documented_shape() -> None:
    assert _validate_variable_prefs({}) is None
    assert (
        _validate_variable_prefs(
            {
                "filter": {"include": ["x"], "exclude": [], "order": ["x", "y"]},
                "display": {},
                "timeline": {"exclude": ["y"]},
                "viz": {"include": []},
            }
        )
        is None
    )


def test_validation_rejects_bad_shapes() -> None:
    assert _validate_variable_prefs([]) is not None
    assert _validate_variable_prefs({"nope": {}}) is not None
    assert _validate_variable_prefs({"filter": []}) is not None
    assert _validate_variable_prefs({"filter": {"add": []}}) is not None
    assert _validate_variable_prefs({"filter": {"include": "x"}}) is not None
    assert _validate_variable_prefs({"filter": {"include": [1]}}) is not None
    assert _validate_variable_prefs({"filter": {"include": ["v"] * 501}}) is not None
    assert _validate_variable_prefs({"filter": {"order": "x"}}) is not None
    assert _validate_variable_prefs({"filter": {"order": [None]}}) is not None
    assert _validate_variable_prefs({"filter": {"order": ["v"] * 501}}) is not None


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
