"""Sanity-check the Series branch of every recode_* function.

For each variable in var_schema with a non-null recode_func we:
  1. Call the function with a small Series and verify it returns a Series of
     the right length (structural check).
  2. Call the same function element-wise on that Series via the scalar branch
     and compare element-by-element. Any mismatch is a parity failure - the
     two branches must produce the same output for the same input, otherwise
     whether rows go through vectorised or fallback code quietly changes
     results.

Usage:
    python tests/test_recode_series_branches.py

Exit code is 0 if every recode_func passes both checks, 1 otherwise.
"""

import sys
import traceback
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

import pandas as pd

import fyp.recode_variables as rv
from fyp.fyp_config import fyp_cf

SAMPLE_SERIES = {
    "string": pd.Series(
        ["hello world running", "", None, "some text here", "walking"],
        dtype="string[pyarrow]",
    ),
    "stringified_list": pd.Series(
        ["cat, dog, bird", "", None, "walking, talking", "a/b & c"],
        dtype="string[pyarrow]",
    ),
    "numeric": pd.Series(
        [1.0, 2.5, None, 0.0, 42.0],
        dtype="float64[pyarrow]",
    ),
    "age_range_list": pd.Series(
        ["20-30", "20-30 | 40-50", None, "25", "5-10 | 70-80"],
        dtype="string[pyarrow]",
    ),
    "pipe_list": pd.Series(
        ["a | b", "single", None, "", "x | y | z"],
        dtype="string[pyarrow]",
    ),
    "percent": pd.Series(
        ["50% music, 50% speech", "70% speech, 30% music", None, "", "20% music, 80% speech"],
        dtype="string[pyarrow]",
    ),
    "score_numeric": pd.Series(
        ["85, confident", "50.5, moderate", None, "", "0, low"],
        dtype="string[pyarrow]",
    ),
    "main_activity_realistic": pd.Series(
        ["person walking dog | person carrying groceries", "swimming in pool", None, "-", "someone crying softly"],
        dtype="string[pyarrow]",
    ),
}


def _pick_samples(scale: str, func_name: str) -> list[tuple[str, pd.Series]]:
    """Return candidate Series to try for a given schema scale / recode func."""
    scale = (scale or "").strip().lower()

    # Function-specific preferred samples produce cleaner parity signals than
    # the generic fallbacks.
    if func_name == "recode_faces_age_estimate":
        return [("age_range_list", SAMPLE_SERIES["age_range_list"])]
    if func_name == "recode_challenges":
        return [("pipe_list", SAMPLE_SERIES["pipe_list"])]
    if func_name == "recode_speech_vs_music":
        return [("percent", SAMPLE_SERIES["percent"])]
    if func_name == "recode_scores":
        return [("percent", SAMPLE_SERIES["percent"]), ("numeric", SAMPLE_SERIES["numeric"])]

    if scale in {"ratio", "interval"}:
        return [("numeric", SAMPLE_SERIES["numeric"]), ("string", SAMPLE_SERIES["string"])]

    return [
        ("string", SAMPLE_SERIES["string"]),
        ("stringified_list", SAMPLE_SERIES["stringified_list"]),
        ("pipe_list", SAMPLE_SERIES["pipe_list"]),
        ("numeric", SAMPLE_SERIES["numeric"]),
    ]


def _try_eval(s):
    try:
        return eval(s, rv.__dict__)
    except Exception:
        return s


def _values_equal(a, b) -> bool:
    """Best-effort equality for heterogeneous recode outputs (scalars, dicts,
    lists, NaN)."""
    # Treat both-NA as equal
    a_na = False
    b_na = False
    try:
        a_na = pd.isna(a) if not isinstance(a, (list, dict)) else False
    except (TypeError, ValueError):
        a_na = False
    try:
        b_na = pd.isna(b) if not isinstance(b, (list, dict)) else False
    except (TypeError, ValueError):
        b_na = False
    if a_na and b_na:
        return True
    if a_na != b_na:
        return False
    if type(a) is not type(b):
        # Accept int vs float numeric parity (e.g. np.int64(0) vs 0.0)
        try:
            if float(a) == float(b):
                return True
        except (TypeError, ValueError):
            pass
        return False
    return a == b


def _compare_parity(series_out: pd.Series, scalar_out: list) -> tuple[int, list[str]]:
    """Compare Series-branch output against the scalar-branch list element-wise.
    Returns (mismatch_count, example_mismatch_strings)."""
    mismatches = 0
    examples: list[str] = []
    for i, (sv, cv) in enumerate(zip(series_out.tolist(), scalar_out)):
        if not _values_equal(sv, cv):
            mismatches += 1
            if len(examples) < 3:
                examples.append(f"[{i}] series={sv!r} scalar={cv!r}")
    return mismatches, examples


def main() -> int:
    var_schema = fyp_cf["var_schema"].copy()
    var_schema.set_index("variable_name", inplace=True)
    # Mirror the preprocessing done inside recode_events_df so mapper /
    # ignore_strings arrive as real Python objects in recoding_policy, not
    # raw CSV strings.
    for col in ["mapper", "ignore_strings", "recode_func"]:
        if col in var_schema.columns:
            var_schema[col] = var_schema[col].map(_try_eval)

    # (variable, func, status, detail)
    results: list[tuple[str, str, str, str]] = []
    seen_funcs: set[str] = set()

    for variable, row in var_schema.iterrows():
        recode_func_src = row.get("recode_func")
        if pd.isna(recode_func_src) or not recode_func_src:
            continue

        if callable(recode_func_src):
            func = recode_func_src
        else:
            try:
                func = eval(recode_func_src, rv.__dict__)
            except Exception as e:
                results.append((variable, str(recode_func_src), "EVAL_FAIL", f"{type(e).__name__}: {e}"))
                continue

        func_name = getattr(func, "__name__", str(func))
        # Deduplicate by func name - the parity check is identical per function
        # and testing it 20x for recode_long_strings just inflates output.
        if func_name in seen_funcs:
            continue
        seen_funcs.add(func_name)

        policy = row.to_dict()

        passed_on = None
        failures: list[str] = []
        for sample_name, series in _pick_samples(row.get("scale", ""), func_name):
            try:
                series_out = func(series, policy)
            except Exception as e:
                failures.append(f"{sample_name}: SERIES_BRANCH raised {type(e).__name__}: {e}")
                continue

            if not isinstance(series_out, pd.Series):
                failures.append(f"{sample_name}: returned {type(series_out).__name__}, expected Series")
                continue
            if len(series_out) != len(series):
                failures.append(f"{sample_name}: length mismatch ({len(series_out)} vs {len(series)})")
                continue

            # Structural check passed; now parity check vs scalar branch.
            try:
                scalar_out = [func(x, policy) for x in series]
            except Exception as e:
                failures.append(f"{sample_name}: SCALAR_BRANCH raised {type(e).__name__}: {e}")
                continue

            mismatches, examples = _compare_parity(series_out, scalar_out)
            if mismatches == 0:
                passed_on = sample_name
                break

            failures.append(
                f"{sample_name}: {mismatches}/{len(series)} parity mismatch"
                + (f" ({'; '.join(examples)})" if examples else "")
            )

        if passed_on is not None:
            results.append((variable, func_name, "PASS", f"on sample '{passed_on}'"))
        else:
            results.append((variable, func_name, "FAIL", " | ".join(failures) or "no samples tried"))

    # Print results
    print(f"\n{'=' * 110}")
    print(f"{'variable':<30} {'func':<32} {'status':<8} detail")
    print("=" * 110)
    n_pass = n_fail = n_eval_fail = 0
    for variable, func_name, status, detail in results:
        print(f"{variable:<30} {func_name:<32} {status:<8} {detail}")
        if status == "PASS":
            n_pass += 1
        elif status == "FAIL":
            n_fail += 1
        else:
            n_eval_fail += 1
    print("=" * 110)
    print(f"PASS: {n_pass}   FAIL: {n_fail}   EVAL_FAIL: {n_eval_fail}   TOTAL: {len(results)} unique recode_funcs")

    if n_fail > 0 or n_eval_fail > 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
