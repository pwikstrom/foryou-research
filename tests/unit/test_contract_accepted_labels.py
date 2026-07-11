"""Unit tests for contract-sourced ``accepted_labels`` (synthesized schema).

``accepted_labels`` is never persisted anywhere — ``var_schema.csv`` is retired and
the schema is synthesized from the contract TOMLs; the annotation contract
(``config/annotation_contract.toml``) is the single source for the enum vocabularies.
``fyp_config._apply_contract_accepted_labels`` rebuilds the column in memory at the
end of every ``load_var_schema`` synthesis. Pins:

  * on a synthesized frame missing the column, the overlay creates it and fills
    closed-tag enum fields from the contract;
  * membership is derived from the contract alone — a field is closed-tag when the
    contract defines an enum for it and declares a closed scale (categorical/list);
    a field with no contract enum, like the free-text video_story, is excluded;
  * reconstructing the column does NOT change the var_schema hash (no study
    invalidation) — it equals a schema whose column was pre-populated verbatim;
  * the contract is genuinely the source (an enum edit flows through).

No Gemini API calls.

Usage:
    python tests/unit/test_contract_accepted_labels.py
"""

from __future__ import annotations

import contextlib
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.annotation_contract as ac
import fyp.recode_variables as rv
from fyp.fyp_config import _apply_contract_accepted_labels, fyp_cf

CLOSED_TAG_FIELDS = [
    "content_category",
    "type_of_story",
    "advertising",
    "tiktok_native",
    "aigc",
    "trend_technical",
    "trend_cultural",
    "multilingual",
    "main_gender",
    "main_ethnicity",
]


def _schema_without_labels() -> pd.DataFrame:
    """A copy of the live synthesized var_schema with ``accepted_labels`` dropped."""
    return fyp_cf["var_schema"].copy().drop(columns=["accepted_labels"], errors="ignore")


@contextlib.contextmanager
def _swapped(frame: pd.DataFrame):
    saved = fyp_cf.get("var_schema")
    fyp_cf["var_schema"] = frame
    try:
        yield
    finally:
        fyp_cf["var_schema"] = saved


def _expected_labels(field_name: str) -> str:
    contract = ac.load_contract()
    field = next(f for f in contract["fields"] if f["name"] == field_name)
    values = ac.enum_values(contract, field["enum"])
    return "[" + ", ".join(str(v).lower() for v in values) + "]"


def test_overlay_creates_column_and_fills_closed_tags() -> None:
    frame = _schema_without_labels()
    assert "accepted_labels" not in frame.columns
    with _swapped(frame):
        _apply_contract_accepted_labels(fyp_cf)
    assert "accepted_labels" in frame.columns, "overlay must create the column"
    for name in CLOSED_TAG_FIELDS:
        got = str(frame.loc[frame["variable_name"] == name, "accepted_labels"].iloc[0])
        assert got == _expected_labels(name), f"{name}: {got!r} != contract-derived"


def test_membership_excludes_freetext_fields() -> None:
    frame = _schema_without_labels()
    with _swapped(frame):
        _apply_contract_accepted_labels(fyp_cf)
    # video_story: no contract enum and a free-text (string) recode, so it is not a
    # closed-tag field even though the column exists.
    for name in ["video_story"]:
        cell = frame.loc[frame["variable_name"] == name, "accepted_labels"].iloc[0]
        assert pd.isna(cell) or str(cell).strip() in ("", "<NA>"), (
            f"{name} should not be a closed-tag field: {cell!r}"
        )


def test_hash_invariant_to_reconstruction() -> None:
    base = _schema_without_labels()

    # As if the column were pre-populated verbatim with the contract values.
    with_col = base.copy()
    labels = {n: _expected_labels(n) for n in CLOSED_TAG_FIELDS}
    with_col["accepted_labels"] = with_col["variable_name"].map(labels).astype("string[pyarrow]")
    with _swapped(with_col):
        hash_with = rv.compute_var_schema_hash()

    # Column absent, reconstructed by the overlay (the load_var_schema path).
    without_col = base.copy()
    with _swapped(without_col):
        _apply_contract_accepted_labels(fyp_cf)
        hash_without = rv.compute_var_schema_hash()

    assert hash_with == hash_without, (
        "reconstructing accepted_labels via the overlay changed the var_schema hash -> "
        "would invalidate study caches; it must match a column-present schema"
    )


def test_contract_is_the_source() -> None:
    original = ac.load_contract

    def _patched(path=None):
        # Deep-copy before mutating: load_contract() returns the shared
        # process-local snapshot dict; an in-place append would leak the fake
        # enum value into every later test in the same process.
        contract = copy.deepcopy(original(path))
        contract["enums"]["type_of_story"].append("Satire-Based")
        return contract

    frame = _schema_without_labels()
    ac.load_contract = _patched
    try:
        with _swapped(frame):
            _apply_contract_accepted_labels(fyp_cf)
    finally:
        ac.load_contract = original
    got = str(frame.loc[frame["variable_name"] == "type_of_story", "accepted_labels"].iloc[0])
    assert "satire-based" in got, f"contract enum edit did not flow into accepted_labels: {got!r}"


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
