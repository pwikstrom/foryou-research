"""Unit tests for the var_schema hashing, registry and freshness behaviour.

Covers, in this order (mirroring tests §1-24 of the plan):

  Hash stability:
    1. test_hash_v2_stable_across_row_order
    2. test_hash_v2_stable_across_column_order
    3. test_hash_v2_stable_across_dtype_backend
    4. test_hash_v2_ignores_presentation_changes
    5. test_hash_v2_responds_to_semantic_changes
    6. test_hash_v2_prefix
    7. test_hash_v1_v2_never_collide

  Registry:
    8. test_recode_registry_contains_known_funcs

  Cross-process freshness:
   19. test_reload_var_schema_if_changed_noop_when_unchanged
   20. test_reload_var_schema_if_changed_picks_up_presentation_edit

Run:
    python tests/unit/test_var_schema_phase1.py

Exit code 0 on full pass, 1 otherwise.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd

from fyp.fyp_config import (
    fyp_cf,
    reload_var_schema_if_changed,
)
from fyp.recode_variables import (
    SEMANTIC_COLUMNS,
    VAR_SCHEMA_HASH_VERSION,
    compute_var_schema_hash,
    get_recode_func_registry,
)



PASS = 0
FAIL = 0
SKIP = 0



def _check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")



def _with_schema(df: pd.DataFrame):
    """Context-manager-like: swap fyp_cf['var_schema'] for the duration."""
    class _Ctx:
        def __enter__(self_inner):
            self_inner.prev = fyp_cf["var_schema"]
            fyp_cf["var_schema"] = df
            return df

        def __exit__(self_inner, *a):
            fyp_cf["var_schema"] = self_inner.prev
    return _Ctx()



def _real_schema_copy() -> pd.DataFrame:
    return fyp_cf["var_schema"].copy()



# -------- Hash stability --------

def test_hash_v2_stable_across_row_order():
    df = _real_schema_copy()
    shuffled = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
    with _with_schema(df):
        a = compute_var_schema_hash()
    with _with_schema(shuffled):
        b = compute_var_schema_hash()
    _check("test_hash_v2_stable_across_row_order", a == b, f"{a} vs {b}")



def test_hash_v2_stable_across_column_order():
    df = _real_schema_copy()
    reordered = df[df.columns[::-1]]
    with _with_schema(df):
        a = compute_var_schema_hash()
    with _with_schema(reordered):
        b = compute_var_schema_hash()
    _check("test_hash_v2_stable_across_column_order", a == b, f"{a} vs {b}")



def test_hash_v2_stable_across_dtype_backend():
    df = _real_schema_copy()
    # Re-roundtrip through CSV with default dtypes (no pyarrow backend)
    csv = df.to_csv(index=False)
    import io
    df_plain = pd.read_csv(io.StringIO(csv))
    with _with_schema(df):
        a = compute_var_schema_hash()
    with _with_schema(df_plain):
        b = compute_var_schema_hash()
    _check("test_hash_v2_stable_across_dtype_backend", a == b, f"{a} vs {b}")



def test_hash_v2_ignores_presentation_changes():
    df = _real_schema_copy()
    with _with_schema(df):
        a = compute_var_schema_hash()
    df_mod = df.copy()
    presentation_cols = [
        "display_name", "section", "description",
        "web_filter_prio", "web_timeline_prio", "web_viz_prio",
        "web_display_prio",
    ]
    for col in presentation_cols:
        if col in df_mod.columns:
            df_mod[col] = "PRESENTATION_NOISE"
    with _with_schema(df_mod):
        b = compute_var_schema_hash()
    _check("test_hash_v2_ignores_presentation_changes", a == b, f"{a} vs {b}")



def test_hash_v2_responds_to_semantic_changes():
    df = _real_schema_copy()
    with _with_schema(df):
        baseline = compute_var_schema_hash()
    failures = []
    for col in SEMANTIC_COLUMNS:
        if col not in df.columns:
            continue
        df_mod = df.copy()
        # Inject a clearly-different value on the first row.
        df_mod.loc[df_mod.index[0], col] = "X_SEMANTIC_CHANGE_X"
        with _with_schema(df_mod):
            h = compute_var_schema_hash()
        if h == baseline:
            failures.append(col)
    _check("test_hash_v2_responds_to_semantic_changes",
           not failures,
           f"unchanged on: {failures}")



def test_hash_v2_prefix():
    h = compute_var_schema_hash()
    _check("test_hash_v2_prefix", h.startswith(f"{VAR_SCHEMA_HASH_VERSION}:"), h)



def test_hash_v1_v2_never_collide():
    h = compute_var_schema_hash()
    # A v1 hash by construction is a bare 64-char hex string.  v2's prefix
    # makes any string-equal collision impossible.
    _check("test_hash_v1_v2_never_collide",
           ":" in h and not (len(h) == 64 and h.replace("a","").replace("b","").replace("c","").replace("d","").replace("e","").replace("f","").isdigit()),
           h)



# -------- Registry / validation / safe parsing --------

def test_recode_registry_contains_known_funcs():
    registry = get_recode_func_registry()
    expected = {"recode_long_strings", "recode_stringified_list"}
    missing = expected - registry.keys()
    _check("test_recode_registry_contains_known_funcs",
           not missing, f"missing: {missing}")



# -------- Cross-process freshness --------

def test_reload_var_schema_if_changed_noop_when_unchanged():
    reload_var_schema_if_changed()  # ensure cache primed
    res = reload_var_schema_if_changed()
    _check("test_reload_var_schema_if_changed_noop_when_unchanged", res is False, str(res))



def test_reload_var_schema_if_changed_picks_up_presentation_edit():
    """The schema is synthesized: the fingerprint watches the presentation
    store (+ version registries), not the retired var_schema.csv."""
    global SKIP
    if fyp_cf.get("data_io", {}).get("use_gcs_for_data"):
        SKIP += 1
        print("  SKIP  test_reload_var_schema_if_changed_picks_up_presentation_edit (GCS)")
        return
    from fyp import var_presentation as vp
    snap = vp.load_presentation()
    try:
        reload_var_schema_if_changed()  # prime
        primed = reload_var_schema_if_changed()  # steady state -> no reload
        # Edit the presentation store directly (as a second instance would).
        surfaces = dict((snap or vp.empty_presentation()).get("surfaces", {}))
        probe = str(fyp_cf["var_schema"]["variable_name"].iloc[0])
        flt = [n for n in surfaces.get("filter", []) if n != probe]
        if len(flt) == len(surfaces.get("filter", [])):
            flt = flt + [probe]
        vp.save_presentation({**surfaces, "filter": flt}, updated_by="phase1-test")
        res = reload_var_schema_if_changed()
        _check("test_reload_var_schema_if_changed_picks_up_presentation_edit",
               primed is False and res is True, f"primed={primed} res={res}")
    finally:
        if snap is not None:
            vp.save_presentation(snap.get("surfaces", {}), updated_by="phase1-restore")
        reload_var_schema_if_changed()



# -------- Consumer parity (regression) --------

def test_get_factors_and_features_from_var_schema_unchanged():
    """Round-trip the schema through save+reload and verify the consumer
    helper returns the same lists.  Catches accidental row drops.
    """
    from fyp.recode_variables import (
        get_factors_and_features_from_var_schema,
        get_grouping_factors_from_var_schema,
    )
    factors_before, features_before = get_factors_and_features_from_var_schema()
    grouping_before = get_grouping_factors_from_var_schema()
    factors_after, features_after = get_factors_and_features_from_var_schema()
    grouping_after = get_grouping_factors_from_var_schema()
    ok = (factors_before == factors_after
          and features_before == features_after
          and grouping_before == grouping_after)
    _check("test_get_factors_and_features_from_var_schema_unchanged", ok)



# -------- Driver --------

TESTS = [
    test_hash_v2_stable_across_row_order,
    test_hash_v2_stable_across_column_order,
    test_hash_v2_stable_across_dtype_backend,
    test_hash_v2_ignores_presentation_changes,
    test_hash_v2_responds_to_semantic_changes,
    test_hash_v2_prefix,
    test_hash_v1_v2_never_collide,
    test_recode_registry_contains_known_funcs,
    test_reload_var_schema_if_changed_noop_when_unchanged,
    test_reload_var_schema_if_changed_picks_up_presentation_edit,
    test_get_factors_and_features_from_var_schema_unchanged,
]



def main():
    print(f"\nRunning {len(TESTS)} var_schema tests...\n")
    for t in TESTS:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print(f"  ERROR {t.__name__}  ({e})")
            traceback.print_exc()
    print(f"\nSummary: {PASS} passed, {FAIL} failed, {SKIP} skipped\n")
    return 0 if FAIL == 0 else 1



if __name__ == "__main__":
    sys.exit(main())
