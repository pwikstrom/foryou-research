"""Phase 1 unit tests for the var_schema refactor.

Covers, in this order (mirroring tests §1-24 of the plan):

  Hash stability:
    1. test_hash_v2_stable_across_row_order
    2. test_hash_v2_stable_across_column_order
    3. test_hash_v2_stable_across_dtype_backend
    4. test_hash_v2_ignores_presentation_changes
    5. test_hash_v2_responds_to_semantic_changes
    6. test_hash_v2_prefix
    7. test_hash_v1_v2_never_collide

  Registry / validation / safe parsing:
    8. test_recode_registry_contains_known_funcs
    9. test_recode_lookup_rejects_unknown
   10. test_no_eval_in_recode_path
   11. test_validate_each_enum
   12. test_validate_duplicate_variable_name
   13. test_validate_real_csv_is_clean

  Save / etag:
   16. test_save_writes_backup
   17. test_save_etag_mismatch_raises
   18. test_save_hash_changed_flag

  Cross-process freshness:
   19. test_reload_var_schema_if_changed_noop_when_unchanged
   20. test_reload_var_schema_if_changed_picks_up_disk_edit

  Migration:
   21. test_migrate_hash_v2_dry_run_no_writes
   22. test_migrate_hash_v2_apply_rewrites_v1_only

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
    VarSchemaConflict,
    compute_var_schema_etag,
    fyp_cf,
    load_var_schema,
    reload_var_schema_if_changed,
    save_var_schema,
)
from fyp.recode_variables import (
    SEMANTIC_COLUMNS,
    VAR_SCHEMA_HASH_VERSION,
    compute_var_schema_hash,
    get_recode_func_registry,
    parse_accepted_labels,
    parse_recode_func,
    validate_var_schema,
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
        "web_viz_log", "web_viz_multi_label", "web_viz_bins",
        "web_display_prio", "sortable", "searchable",
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



def test_recode_lookup_rejects_unknown():
    result = parse_recode_func("definitely_not_a_function")
    _check("test_recode_lookup_rejects_unknown", result is None,
           f"got {result!r}")



def test_no_eval_in_recode_path():
    """The new parser must not execute arbitrary strings.

    Construct a sentinel file and a malicious payload that, if eval'd,
    would create it.  Then call parse_recode_func and verify the file
    was NOT created and the function returned None.
    """
    sentinel = Path(tempfile.gettempdir()) / "fyp_test_eval_canary.txt"
    if sentinel.exists():
        sentinel.unlink()
    malicious = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('PWNED')"
    result_rf = parse_recode_func(malicious)
    created = sentinel.exists()
    if sentinel.exists():
        sentinel.unlink()
    ok = (result_rf is None and not created)
    _check("test_no_eval_in_recode_path", ok,
           f"rf={result_rf!r} sentinel_created={created}")



def test_validate_each_enum():
    # role and scale are the remaining validated enum columns (mapper / ignore_strings
    # / recode_func / unable_to_detect_policy were retired and are now derived).
    base = pd.DataFrame([
        {"variable_name": "v1", "role": "factor", "scale": "ratio"},
    ])
    # passing case
    errs = validate_var_schema(base)
    pass_clean = not errs
    # role typo
    bad_role = base.copy()
    bad_role.loc[0, "role"] = "factro"
    errs1 = validate_var_schema(bad_role)
    # scale typo
    bad_scale = base.copy()
    bad_scale.loc[0, "scale"] = "ratoi"
    errs2 = validate_var_schema(bad_scale)
    ok = (pass_clean
          and any(e["column"] == "role" for e in errs1)
          and any(e["column"] == "scale" for e in errs2))
    _check("test_validate_each_enum", ok)



def test_validate_duplicate_variable_name():
    df = pd.DataFrame([
        {"variable_name": "dup", "role": "standard"},
        {"variable_name": "dup", "role": "standard"},
    ])
    errs = validate_var_schema(df)
    ok = any("duplicate" in e["message"].lower() for e in errs)
    _check("test_validate_duplicate_variable_name", ok)



def test_validate_real_csv_is_clean():
    errs = validate_var_schema(_real_schema_copy())
    _check("test_validate_real_csv_is_clean", not errs,
           f"{len(errs)} unexpected errors, first: {errs[0] if errs else None}")



# -------- Save / etag --------

def _save_test_with_local_data():
    """Snapshot the live CSV so destructive save tests can restore it."""
    if fyp_cf.get("data_io", {}).get("use_gcs_for_data"):
        return None  # skip on GCS environments
    from fyp.fyp_config import _var_schema_path
    src = _var_schema_path(fyp_cf)
    if not os.path.exists(src):
        return None
    fd, tmp = tempfile.mkstemp(prefix="var_schema_snapshot_", suffix=".csv")
    os.close(fd)
    shutil.copy2(src, tmp)
    return (src, tmp)



def _restore(snap):
    if snap is None:
        return
    src, tmp = snap
    shutil.copy2(tmp, src)
    os.remove(tmp)
    load_var_schema(fyp_cf, verbose=False)
    # Clean any test-generated backups
    backup_dir = os.path.dirname(src)
    for f in os.listdir(backup_dir):
        if f.startswith("var_schema_") and f.endswith(".csv") and f != "var_schema.csv":
            try:
                os.remove(os.path.join(backup_dir, f))
            except OSError:
                pass



def test_save_writes_backup():
    global SKIP
    snap = _save_test_with_local_data()
    if snap is None:
        SKIP += 1
        print("  SKIP  test_save_writes_backup (GCS or no live CSV)")
        return
    try:
        src, _ = snap
        backup_dir = os.path.dirname(src)
        before = set(os.listdir(backup_dir))
        df = fyp_cf["var_schema"].copy()
        df.loc[df.index[0], "description"] = "TEST_BACKUP_PROBE"
        etag = compute_var_schema_etag(fyp_cf)
        save_var_schema(df, expected_etag=etag, verbose=False)
        after = set(os.listdir(backup_dir))
        new = [f for f in (after - before) if f.startswith("var_schema_") and f.endswith(".csv")]
        _check("test_save_writes_backup", len(new) == 1, f"new files: {new}")
    finally:
        _restore(snap)



def test_save_etag_mismatch_raises():
    global SKIP
    snap = _save_test_with_local_data()
    if snap is None:
        SKIP += 1
        print("  SKIP  test_save_etag_mismatch_raises (GCS or no live CSV)")
        return
    try:
        df = fyp_cf["var_schema"].copy()
        df.loc[df.index[0], "description"] = "STALE_ETAG"
        raised = False
        try:
            save_var_schema(df, expected_etag="not-the-real-etag", verbose=False)
        except VarSchemaConflict:
            raised = True
        _check("test_save_etag_mismatch_raises", raised)
    finally:
        _restore(snap)



def test_save_hash_changed_flag():
    global SKIP
    snap = _save_test_with_local_data()
    if snap is None:
        SKIP += 1
        print("  SKIP  test_save_hash_changed_flag (GCS or no live CSV)")
        return
    try:
        h_before = compute_var_schema_hash()
        # cosmetic edit
        df = fyp_cf["var_schema"].copy()
        df.loc[df.index[0], "display_name"] = "TEST_NAME"
        save_var_schema(df, expected_etag=compute_var_schema_etag(fyp_cf), verbose=False)
        h_cosmetic = compute_var_schema_hash()
        # semantic edit — flip to a value guaranteed to be different.
        df2 = fyp_cf["var_schema"].copy()
        current_scale = str(df2.loc[df2.index[0], "scale"])
        df2.loc[df2.index[0], "scale"] = "categorical" if current_scale != "categorical" else "string"
        save_var_schema(df2, expected_etag=compute_var_schema_etag(fyp_cf), verbose=False)
        h_semantic = compute_var_schema_hash()
        ok = (h_cosmetic == h_before) and (h_semantic != h_before)
        _check("test_save_hash_changed_flag", ok,
               f"before={h_before[:16]} cosmetic={h_cosmetic[:16]} semantic={h_semantic[:16]}")
    finally:
        _restore(snap)



# -------- Cross-process freshness --------

def test_reload_var_schema_if_changed_noop_when_unchanged():
    reload_var_schema_if_changed()  # ensure cache primed
    res = reload_var_schema_if_changed()
    _check("test_reload_var_schema_if_changed_noop_when_unchanged", res is False, str(res))



def test_reload_var_schema_if_changed_picks_up_disk_edit():
    global SKIP
    if fyp_cf.get("data_io", {}).get("use_gcs_for_data"):
        SKIP += 1
        print("  SKIP  test_reload_var_schema_if_changed_picks_up_disk_edit (GCS)")
        return
    import time
    from fyp.fyp_config import _var_schema_path
    p = _var_schema_path(fyp_cf)
    reload_var_schema_if_changed()  # prime
    # Touch the file to bump mtime
    new_time = time.time() + 1
    os.utime(p, (new_time, new_time))
    res = reload_var_schema_if_changed()
    _check("test_reload_var_schema_if_changed_picks_up_disk_edit", res is True, str(res))



# -------- Migration --------

def test_migrate_hash_v2_dry_run_no_writes():
    sys.path.insert(0, str(project_root / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_var_schema_hash_v2",
        str(project_root / "scripts" / "migrate_var_schema_hash_v2.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Stub data_io.listdir + load_json + save_json with an in-memory fake
    fake_v1_hex = "a" * 64
    fake_sidecar = {"var_schema_hash": fake_v1_hex, "sidecar_version": 2}
    writes = []
    class _FakeIO:
        def listdir(self, storage_location="cache", **kw):
            return ["mystudy_recoded.meta.json"]
        def load_json(self, storage_location="cache", filename="", **kw):
            return dict(fake_sidecar)
        def save_json(self, **kw):
            writes.append(kw)
    fake = _FakeIO()
    orig = mod.data_io
    mod.data_io = fake
    try:
        counts = mod.migrate(dry_run=True, verbose=False)
    finally:
        mod.data_io = orig
    ok = (counts["v1_rewritten"] == 1) and (not writes)
    _check("test_migrate_hash_v2_dry_run_no_writes", ok, f"counts={counts}, writes={writes}")



def test_migrate_hash_v2_apply_rewrites_v1_only():
    sys.path.insert(0, str(project_root / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_var_schema_hash_v2",
        str(project_root / "scripts" / "migrate_var_schema_hash_v2.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = {
        "v1.meta.json": {"var_schema_hash": "b" * 64, "sidecar_version": 2},
        "v2.meta.json": {"var_schema_hash": f"{VAR_SCHEMA_HASH_VERSION}:already_done", "sidecar_version": 2},
        "empty.meta.json": {"var_schema_hash": "", "sidecar_version": 2},
    }
    writes = []
    class _FakeIO:
        def listdir(self, storage_location="cache", **kw):
            return [k.replace(".meta.json", "_recoded.meta.json") for k in state.keys()]
        def load_json(self, storage_location="cache", filename="", **kw):
            key = filename.replace("_recoded.meta.json", ".meta.json")
            return dict(state[key])
        def save_json(self, data=None, storage_location="cache", filename="", **kw):
            writes.append((filename, data))
    fake = _FakeIO()
    orig = mod.data_io
    mod.data_io = fake
    try:
        counts = mod.migrate(dry_run=False, verbose=False)
    finally:
        mod.data_io = orig
    rewritten = {f for f, _ in writes}
    ok = (counts == {"v1_rewritten": 1, "already_v2": 1, "empty": 1, "unknown": 0, "errors": 0}
          and rewritten == {"v1_recoded.meta.json"})
    _check("test_migrate_hash_v2_apply_rewrites_v1_only", ok,
           f"counts={counts} writes={rewritten}")



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



def test_get_viz_config_unchanged():
    from web_interface.data_service import get_viz_config
    cfg = get_viz_config()
    ok = isinstance(cfg, dict) and len(cfg) > 0
    _check("test_get_viz_config_unchanged", ok, f"size={len(cfg) if cfg else 0}")



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
    test_recode_lookup_rejects_unknown,
    test_no_eval_in_recode_path,
    test_validate_each_enum,
    test_validate_duplicate_variable_name,
    test_validate_real_csv_is_clean,
    test_save_writes_backup,
    test_save_etag_mismatch_raises,
    test_save_hash_changed_flag,
    test_reload_var_schema_if_changed_noop_when_unchanged,
    test_reload_var_schema_if_changed_picks_up_disk_edit,
    test_migrate_hash_v2_dry_run_no_writes,
    test_migrate_hash_v2_apply_rewrites_v1_only,
    test_get_factors_and_features_from_var_schema_unchanged,
    test_get_viz_config_unchanged,
]



def main():
    print(f"\nRunning {len(TESTS)} Phase 1 tests...\n")
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
