"""Unit tests for deriving australian_relevance from primary_country.

The generalized contract replaced the australian_relevance yes/no field with
primary_country (any country). ``recode_variables.derive_australian_relevance``
keeps the existing dichotomous australian_relevance feature populated by
coalescing: old-version rows keep their model value; new rows are filled from
``primary_country == "Australia"``. No API calls, no storage.

Usage:
    python tests/unit/test_australian_relevance_derivation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import fyp.recode_variables as rv


def _amap(df: pd.DataFrame) -> dict:
    return dict(zip(df["item_id"].astype(str),
                    df["australian_relevance"].astype("string")))


def test_coalesces_old_and_new_rows() -> None:
    # o* = old rows (have australian_relevance, no primary_country);
    # n* = new rows (have primary_country, no australian_relevance).
    df = pd.DataFrame({
        "item_id": ["o1", "o2", "n1", "n2", "n3", "n4"],
        "australian_relevance": ["yes", "no", pd.NA, pd.NA, pd.NA, pd.NA],
        "primary_country": [pd.NA, pd.NA, "australia", "united states", "-", "unable to detect"],
    })
    out = rv.derive_australian_relevance(df.copy())
    m = _amap(out)
    assert m["o1"] == "yes" and m["o2"] == "no"      # old values untouched
    assert m["n1"] == "yes"                           # Australia -> yes
    assert m["n2"] == "no"                            # other country -> no
    assert m["n3"] == "no" and m["n4"] == "no"        # no clear country -> no


def test_case_insensitive_and_whitespace() -> None:
    df = pd.DataFrame({
        "item_id": ["a", "b"],
        "australian_relevance": [pd.NA, pd.NA],
        "primary_country": ["  Australia ", "AUSTRALIA"],
    })
    out = rv.derive_australian_relevance(df.copy())
    assert all(v == "yes" for v in out["australian_relevance"].astype("string"))


def test_creates_column_when_absent() -> None:
    df = pd.DataFrame({"item_id": ["a", "b"], "primary_country": ["australia", "india"]})
    out = rv.derive_australian_relevance(df.copy())
    assert list(out["australian_relevance"].astype("string")) == ["yes", "no"]


def test_noop_without_primary_country() -> None:
    df = pd.DataFrame({"item_id": ["x"], "australian_relevance": ["yes"]})
    out = rv.derive_australian_relevance(df.copy())
    assert out["australian_relevance"].iloc[0] == "yes"
    assert "primary_country" not in out.columns


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
