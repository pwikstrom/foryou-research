"""Unit tests for contract-sourced ``accepted_labels`` (column dropped from CSV).

``accepted_labels`` no longer lives in ``var_schema.csv``; the annotation contract
(``config/annotation_contract.toml``) is the single source for the enum vocabularies.
``fyp_config._apply_contract_accepted_labels`` rebuilds the column in memory at load.
Pins:

  * the overlay creates the column and fills closed-tag enum fields from the contract;
  * membership is derived from var_schema's recode config — a field is closed-tag
    only when ``recode_func == "recode_stringified_list"`` with an empty ``mapper``
    and a contract enum (so a free-text-but-enum field like aussie_political_positioning
    is excluded, and folding fields like main_ethnicity are excluded);
  * dropping the column from the CSV does NOT change the var_schema hash (no study
    invalidation) — the reconstructed column equals a column-present schema;
  * the contract is genuinely the source (an enum edit flows through).

No Gemini API calls.

Usage:
    python tests/unit/test_contract_accepted_labels.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.annotation_contract as ac
import fyp.recode_variables as rv
from fyp.fyp_config import _apply_contract_accepted_labels, _var_schema_path, fyp_cf

CLOSED_TAG_FIELDS = [
    "content_category",
    "type_of_story",
    "advertising",
    "tiktok_native",
    "aigc",
    "trend_technical",
    "trend_cultural",
    "multilingual",
]


def _schema_without_labels() -> pd.DataFrame:
    """The on-disk var_schema (no ``accepted_labels`` column anymore)."""
    df = pd.read_csv(_var_schema_path(fyp_cf), dtype_backend="pyarrow", encoding="utf-8")
    return df.drop(columns=["accepted_labels"], errors="ignore")


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


def test_membership_excludes_freetext_and_folding_fields() -> None:
    frame = _schema_without_labels()
    with _swapped(frame):
        _apply_contract_accepted_labels(fyp_cf)
    # aussie_political_positioning: enum + empty mapper but recode_long_strings (free text).
    # main_gender / main_ethnicity: recode_stringified_list but fold via GENERIC_MAPPER.
    for name in ["aussie_political_positioning", "main_gender", "main_ethnicity"]:
        cell = frame.loc[frame["variable_name"] == name, "accepted_labels"].iloc[0]
        assert pd.isna(cell) or str(cell).strip() in ("", "<NA>"), (
            f"{name} should not be a closed-tag field: {cell!r}"
        )


def test_hash_invariant_to_dropping_column() -> None:
    base = _schema_without_labels()

    # As if the column were still in the CSV with the contract values.
    with_col = base.copy()
    labels = {n: _expected_labels(n) for n in CLOSED_TAG_FIELDS}
    with_col["accepted_labels"] = with_col["variable_name"].map(labels).astype("string[pyarrow]")
    with _swapped(with_col):
        hash_with = rv.compute_var_schema_hash()

    # Column dropped from CSV, reconstructed by the overlay.
    without_col = base.copy()
    with _swapped(without_col):
        _apply_contract_accepted_labels(fyp_cf)
        hash_without = rv.compute_var_schema_hash()

    assert hash_with == hash_without, (
        "dropping accepted_labels from the CSV changed the var_schema hash -> would "
        "invalidate study caches; the reconstructed column must match a column-present schema"
    )


def test_contract_is_the_source() -> None:
    original = ac.load_contract

    def _patched(path=None):
        contract = original(path)
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
